"""The session store: the in-memory sessions and their lifecycle.

One process-wide map from session id to :class:`Session`, backed by one file
per session under the data home. Everything that touches the map on behalf of a
request goes through here, so the two locks that matter (the config lock and a
session's own chat lock) are taken in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from easycode.agent.turns import now_iso
from easycode.models.base import DeferredProvider
from easycode.paths import data_home
from easycode.permissions.boundary import normalise_secondary, root_error
from easycode.web.locks import idle_sessions, project_key, run_mutation
from easycode.web.persistence import restore, tmp_path, to_payload
from easycode.web.session import AgentFactory, Session

log = logging.getLogger("easycode.web.store")


class SessionStore:
    """In-memory sessions backed by JSON files under ``~/.easycode/sessions``."""

    def __init__(
        self,
        cfg,
        root: Path,
        agent_factory: AgentFactory,
        restore_factory: AgentFactory | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.agent_factory = agent_factory
        # Restoring a persisted session may defer credential resolution so a
        # missing/removed key cannot hide the session from the list.
        self.restore_factory = restore_factory or agent_factory
        self.dir = data_home() / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        # Serializes config writes with session creation/turn preparation so no
        # session is assembled from a half-updated config. Never held across a
        # running turn (that is ``idle_sessions``) or any model/approval/shell
        # wait; session locks are always taken after this one.
        self._config_lock = asyncio.Lock()

    @asynccontextmanager
    async def config_change(self) -> AsyncIterator[None]:
        """Short-lived guard for config mutations and session assembly."""
        async with self._config_lock:
            yield

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def create(
        self,
        model_alias: str | None = None,
        root: str | None = None,
        secondary_roots: list[str] | None = None,
        permission_mode: str | None = None,
        **agent_kwargs: Any,
    ) -> Session:
        sid = uuid.uuid4().hex[:12]
        alias = model_alias or self.cfg.default_model
        # The primary root must satisfy the same rules as any other root:
        # never a sensitive dir (.git/.easycode), the config file, or arbitrary
        # data-home paths (permanent worktrees stay allowed).
        primary_err = root_error(Path(root) if root else self.cfg.root)
        if primary_err is not None:
            raise ValueError(primary_err)
        secondary = self.resolve_secondary(root, secondary_roots)
        if root is not None:
            agent_kwargs["root"] = root
        if secondary:
            agent_kwargs["secondary_roots"] = [str(p) for p in secondary]
        agent = self.agent_factory(alias, **agent_kwargs)
        # The agent owns the alias; the store stamps the one it asked for so a
        # replaced agent factory cannot leave the session without one.
        agent.model_alias = alias
        sess = Session(
            id=sid,
            title="新会话",
            created_at=now_iso(),
            agent=agent,
            root=root,
            secondary_roots=[str(p) for p in secondary],
        )
        # ``permission_mode`` reaching the store has already passed the
        # confirmation gate at the route/CLI that supplied it; the inherited
        # config default is the user's own standing setting.
        sess.set_permission_mode(permission_mode or self.cfg.permission_mode)
        self._sessions[sid] = sess
        self._flush(sess)
        return sess

    def _projects_secondary(self, root: str | None) -> list[str]:
        """cfg.workspace_projects secondary bindings for ``root``.

        When a session is created without explicit secondary roots, the
        config's persisted project→secondary binding for the matching primary
        is used, so secondary and extra_safe directories require no approval.
        """
        key = project_key(root)
        for proj in self.cfg.workspace_projects:
            if project_key(proj.get("root")) == key:
                return list(proj.get("secondary") or [])
        return []

    def resolve_secondary(self, root: str | None, secondary_roots: list[str] | None) -> list[Path]:
        """Resolve + validate secondary roots; fall back to project binding.

        Every entry must be an existing, canonical, non-sensitive directory; an invalid entry raises ValueError so the caller can reject the
        registration with a 4xx instead of silently accepting a bad root.
        """
        raw = [
            str(p)
            for p in (
                secondary_roots if secondary_roots is not None else self._projects_secondary(root)
            )
        ]
        paths, err = normalise_secondary(raw, self.cfg.base_dir())
        if err is not None:
            raise ValueError(err)
        return paths

    def ensure_provider(self, session: Session) -> None:
        """Resolve a lazily restored session's provider.

        Raises ``ValueError`` when the model still has no usable credential;
        the caller surfaces that as a 422 before any stream starts.
        """
        provider = session.agent.provider
        if isinstance(provider, DeferredProvider):
            provider.resolve()

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)

    def list_by_archived(self, archived: bool) -> list[Session]:
        return [s for s in self.list() if s.archived == archived]

    def set_archived(self, session_id: str, archived: bool) -> Session | None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        sess.archived = archived
        self._flush(sess)
        return sess

    def set_pinned(self, session_id: str, pinned: bool) -> Session | None:
        """Pin or unpin a session; pinning records when, for stable ordering."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        sess.pinned = pinned
        sess.pinned_at = now_iso() if pinned else None
        self._flush(sess)
        return sess

    async def archive_root(self, root: str | None) -> int:
        sessions = [s for s in self.list() if s.root == root]
        async with idle_sessions(sessions):
            for sess in sessions:
                await run_mutation(self.set_archived, sess.id, True)
        return len(sessions)

    async def delete_root(self, root: str | None) -> int:
        """Delete every session under ``root`` through the single-delete path."""
        ids = [s.id for s in self._sessions.values() if s.root == root]
        for sid in ids:
            await self.delete(sid)
        return len(ids)

    async def delete(self, session_id: str, blank_only: bool = False) -> bool:
        """Delete a session: stop its turn, wait it out, then release resources.

        Cancellation is signalled first, then the session lock is held while the
        file is removed and the session detached: no turn can be running (it
        would hold the lock) and no flush can write the file back (membership is
        gone), so a successful delete leaves neither store entry nor file. A
        file-removal failure propagates and the session stays tracked and
        retryable; the session-owned MCP process is released afterwards.

        With ``blank_only`` the session is removed only while no turn has begun:
        a session that started one (or is running one right now) is left exactly
        as it is, and the caller closes only the view. Checking the lock first
        matters — cancelling a live turn to discover it had started would kill
        work the caller only meant to inspect.
        """
        sess = self.get(session_id)
        if sess is None:
            return False
        if blank_only and sess._lock.locked():
            return False
        sess.cancel_stream()
        async with sess._lock:
            if blank_only and not sess.is_blank:
                return False
            self._path(session_id).unlink(missing_ok=True)
            self._sessions.pop(session_id, None)
        try:
            await sess.agent.close_mcp()
        except Exception:
            log.warning("failed to close MCP for session %s", session_id, exc_info=True)
        return True

    def load_all(self) -> None:
        """Restore sessions persisted on disk (survives browser refresh / restart)."""
        for p in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                sess, downgraded = restore(data, cfg=self.cfg, agent_factory=self.restore_factory)
                self._sessions[sess.id] = sess
                if downgraded:
                    # The downgrade changes what the session is allowed to do, so
                    # it is written out now: the user is asked exactly once.
                    self._flush(sess)
            except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                # A file that cannot be restored is reported, never silently
                # dropped from the list; the identifier is enough to find it and
                # the message carries no conversation text.
                log.warning("无法恢复会话文件 %s: %s: %s", p.name, type(exc).__name__, exc)
                continue

    def record_exchange(self, session: Session) -> None:
        """Persist the model context and the turn records together.

        The context cache is refreshed from the live history; the conversation
        itself is already recorded on the turns, so compaction dropping messages
        from the context no longer shortens what the transcript can show.
        """
        if session.id not in self._sessions:
            return
        session.messages = list(session.agent.history.messages)
        session.history_base = session.baseline()
        self._flush(session)

    def _flush(self, session: Session) -> None:
        # never write a session that is no longer tracked (deleted).
        if session.id not in self._sessions:
            return
        path = self._path(session.id)
        tmp = tmp_path(path, session.id)
        tmp.write_text(json.dumps(to_payload(session), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
