"""Web session management: multi-session isolation + disk persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easycode.agent.loop import Agent
from easycode.credentials import data_home
from easycode.models.base import DeferredProvider
from easycode.workspace import normalise_secondary, root_error

log = logging.getLogger("easycode.web.session")

#: Creates an agent for an alias; implementations may accept extra kwargs
#: (root/secondary_roots) from ``SessionStore.create``.
AgentFactory = Callable[..., Agent]


class SessionBusyError(RuntimeError):
    """A session-scoped operation targeted a session with an active turn."""


@asynccontextmanager
async def idle_sessions(sessions: Iterable[Session]) -> AsyncIterator[None]:
    """Serialize a session-scoped mutation against running chat turns.

    Refuses (``SessionBusyError``) when any target session holds its chat lock;
    otherwise holds every target lock for the duration so no turn can start
    mid-mutation. Must run on the event loop; targets are locked in a stable
    order so two bulk operations cannot deadlock.
    """
    ordered = sorted(sessions, key=lambda s: s.id)
    for sess in ordered:
        if sess._lock.locked():
            raise SessionBusyError(f"session busy: {sess.id}")
    async with AsyncExitStack() as stack:
        for sess in ordered:
            await stack.enter_async_context(sess._lock)
        yield


async def run_mutation(func: Callable, *args, **kwargs):
    """Finish a state-writing worker before cancellation can release its locks."""
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    result = worker.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


def project_key(root: str | None) -> str:
    """Canonical map key for a project root (default project → "")."""
    return root or ""


@dataclass
class Session:
    id: str
    title: str
    created_at: str
    model_alias: str
    agent: Agent
    messages: list[dict] = field(default_factory=list)
    root: str | None = None  # project (primary workspace root); None = default project
    secondary_roots: list[str] = field(default_factory=list)
    cancel_event: Any | None = None  # asyncio.Event; set by the cancel endpoint
    always_allow: list[str] = field(default_factory=list)  # approval_key() scopes, persists
    approval_log: list[dict] = field(default_factory=list)  # resolved approval records
    user_times: list[str] = field(default_factory=list)  # ISO timestamps per user message
    archived: bool = False  # hidden from the sidebar main list (对齐 codex 归档)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    @property
    def permission_mode(self) -> str:
        """The live agent owns the mode; the session only reads it for output."""
        return self.agent.permission_mode

    def cancel_stream(self) -> bool:
        """Request cancellation of the in-flight chat; True if one is running."""
        evt = self.cancel_event
        if evt is None or evt.is_set():
            return False
        evt.set()
        return True

    @property
    def summary(self) -> dict:
        policy = self.agent.execution_policy
        out = {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "model_alias": self.model_alias,
            "permission_mode": self.permission_mode,
            "sandbox_mode": policy.sandbox_mode,
            "approval_policy": policy.approval_policy,
            "approvals_reviewer": policy.approvals_reviewer,
        }
        if self.root:
            out["root"] = self.root
        if self.secondary_roots:
            out["secondary_roots"] = list(self.secondary_roots)
        if self.archived:
            out["archived"] = True
        return out


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
        secondary = self._resolve_secondary(root, secondary_roots)
        if root is not None:
            agent_kwargs["root"] = root
        if secondary:
            agent_kwargs["secondary_roots"] = [str(p) for p in secondary]
        agent = self.agent_factory(alias, **agent_kwargs)
        agent.permission_mode = permission_mode or self.cfg.permission_mode
        sess = Session(
            id=sid,
            title="新会话",
            created_at=_now(),
            model_alias=alias,
            agent=agent,
            root=root,
            secondary_roots=[str(p) for p in secondary],
        )
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

    def _resolve_secondary(self, root: str | None, secondary_roots: list[str] | None) -> list[Path]:
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

    async def delete(self, session_id: str) -> bool:
        """Delete a session: stop its turn, wait it out, then release resources.

        Cancellation is signalled first, then the session lock is held while the
        file is removed and the session detached: no turn can be running (it
        would hold the lock) and no flush can write the file back (membership is
        gone), so a successful delete leaves neither store entry nor file. A
        file-removal failure propagates and the session stays tracked and
        retryable; the session-owned MCP process is released afterwards.
        """
        sess = self.get(session_id)
        if sess is None:
            return False
        sess.cancel_stream()
        async with sess._lock:
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
                root = data.get("root")
                secondary = list(data.get("secondary_roots") or [])
                agent_kwargs: dict = {}
                if root:
                    agent_kwargs["root"] = root
                if secondary:
                    agent_kwargs["secondary_roots"] = secondary
                agent = self.restore_factory(
                    data.get("model_alias") or self.cfg.default_model, **agent_kwargs
                )
                agent.permission_mode = data.get("permission_mode") or self.cfg.permission_mode
                # The snapshot and the live history must not share one list:
                # record_exchange replaces the snapshot each turn while the
                # agent keeps mutating its own history.
                messages = list(data.get("messages") or [])
                agent.history.messages = list(messages)
                sess = Session(
                    id=data["id"],
                    title=data.get("title", "新会话"),
                    created_at=data.get("created_at", _now()),
                    model_alias=data.get("model_alias", self.cfg.default_model),
                    agent=agent,
                    messages=messages,
                    root=root,
                    secondary_roots=secondary,
                    always_allow=list(data.get("always_allow") or []),
                    approval_log=list(data.get("approval_log") or []),
                    user_times=list(data.get("user_times") or []),
                    archived=bool(data.get("archived")),
                )
                self._sessions[sess.id] = sess
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue

    def record_exchange(self, session: Session) -> None:
        """Persist history and align timestamps after context compaction."""
        if session.id not in self._sessions:
            return
        session.messages = list(session.agent.history.messages)
        n_user = sum(1 for m in session.messages if m.get("role") == "user")
        if len(session.user_times) > n_user:
            session.user_times = list(session.user_times[-n_user:]) if n_user else []
        self._flush(session)

    def _flush(self, session: Session) -> None:
        # never write a session that is no longer tracked (deleted).
        if session.id not in self._sessions:
            return
        payload = {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "model_alias": session.model_alias,
            "permission_mode": session.permission_mode,
            "messages": session.messages,
            "always_allow": list(session.always_allow),
            "approval_log": list(session.approval_log),
            "user_times": list(session.user_times),
            "archived": session.archived,
        }
        if session.root:
            payload["root"] = session.root
        if session.secondary_roots:
            payload["secondary_roots"] = list(session.secondary_roots)
        tmp = self._tmp_path(session.id)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(session.id))

    def _tmp_path(self, session_id: str) -> Path:
        """A per-flush temp path unique to this process/call.

        Concurrent ``_flush`` calls for the same session must never write to a
        shared ``.tmp`` file (two ``write_text`` to one path can interleave and
        move a corrupt/partial file into place). A unique suffix — pid + fresh
        uuid — makes every flush write to its own temp file; the atomic
        ``replace`` then leaves a complete, valid session file.
        """
        return self._path(session_id).with_name(
            f"{session_id}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
