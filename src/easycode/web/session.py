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
from easycode.agent.turns import (
    RUNNING,
    TurnRecord,
    full_messages,
    project_detail,
    rebuild_history,
    repair_interrupted,
    split_legacy,
    turn_statuses,
)
from easycode.models.base import DeferredProvider
from easycode.paths import data_home
from easycode.permissions.boundary import normalise_secondary, root_error
from easycode.permissions.policy import PERM_ALLOW_ALL, PERM_ASK

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
    agent: Agent
    messages: list[dict] = field(default_factory=list)
    root: str | None = None  # project (primary workspace root); None = default project
    secondary_roots: list[str] = field(default_factory=list)
    cancel_event: Any | None = None  # asyncio.Event; set by the cancel endpoint
    always_allow: list[str] = field(default_factory=list)  # approval_key() scopes, persists
    approval_log: list[dict] = field(default_factory=list)  # resolved approval records
    #: The whole conversation, one record per user turn: what the transcript
    #: renders, what an edit replays, and the only place the turns the model
    #: context has since dropped still exist. ``messages`` is just the most
    #: recently saved model context; see ``agent/turns``.
    turns: list[dict] = field(default_factory=list)
    #: Incremented when a turn is accepted. An edit states the revision it read,
    #: so a stale page cannot rewrite a conversation that has moved on.
    revision: int = 0
    #: Messages that sat in front of the first recoverable user turn of a session
    #: written before turns existed — a compaction summary, and nothing else that
    #: can be trusted as conversation. Always empty for a session that started
    #: after turns were recorded.
    history_base: list[dict] = field(default_factory=list)
    #: Bounded records of the file tools this conversation ran, in call order.
    #: They outlive the message history they came from (see ``web/artifacts``),
    #: so the pane can still show the context and diffs of earlier turns after a
    #: refresh or a context compaction.
    artifacts: list[dict] = field(default_factory=list)
    archived: bool = False  # hidden from the sidebar main list (对齐 codex 归档)
    pinned: bool = False  # kept at the top of the sidebar
    pinned_at: str | None = None  # when it was pinned, for ordering
    #: True when this session's ``allow-all`` mode is a confirmed choice. A
    #: session file that records full access without it was written before the
    #: preset was gated, so restoring it comes back in the asking mode instead.
    full_access_confirmed: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def set_permission_mode(self, mode: str) -> None:
        """Apply a permission mode the caller has already authorized.

        The confirmation flag says this session's full access was an explicit
        choice, which is what makes it restorable; leaving the mode clears it
        again, so re-entering asks for consent a second time.
        """
        self.agent.permission_mode = mode
        self.full_access_confirmed = mode == PERM_ALLOW_ALL

    @property
    def permission_mode(self) -> str:
        """The live agent owns the mode; the session only reads it for output."""
        return self.agent.permission_mode

    @property
    def model_alias(self) -> str | None:
        """The live agent owns the alias; the session only reads it for output.

        The store stamps the requested alias when it builds the agent and
        rebinding goes through ``bind_agent``/``defer_binding``, so a session's
        displayed and persisted alias cannot drift from the model it runs on.
        """
        return self.agent.model_alias

    @property
    def todos(self) -> list[dict]:
        """The live agent owns the task list; the session only reads it."""
        return self.agent.todos

    @property
    def is_blank(self) -> bool:
        """True while no turn has begun in this session.

        The turn record is created when the input is accepted, before the model
        answers, so a stopped or failed turn still counts as started: the
        definition is "nothing has run here", which is the blank-delete relies on.
        """
        return not self.messages and not self.turns

    @property
    def running(self) -> bool:
        """True while a turn holds this session's lock."""
        return self._lock.locked()

    def cancel_stream(self) -> bool:
        """Request cancellation of the in-flight chat; True if one is running."""
        evt = self.cancel_event
        if evt is None or evt.is_set():
            return False
        evt.set()
        return True

    def turn_index(self, turn_id: str) -> int:
        """Position of a recorded turn, or -1 when this session has no such turn."""
        for index, turn in enumerate(self.turns):
            if turn.get("id") == turn_id:
                return index
        return -1

    def begin_turn(
        self,
        raw_input: str,
        model_input: str,
        command_id: str | None = None,
    ) -> dict:
        """Open a new running turn at the end of the conversation.

        The task list is snapshotted here rather than read back later: an edit of
        this turn has to restore the list as it stood *before* the turn ran, and
        the live list is the model's to change as it works.
        """

        record = TurnRecord.start(
            raw_input=raw_input,
            model_input=model_input,
            todos=list(self.agent.todos),
            command_id=command_id,
        )
        turn = record.to_dict()
        self.turns.append(turn)
        self.revision += 1
        if self.title == "新会话" and raw_input.strip():
            self.title = raw_input.strip()[:30]
        return turn

    def replace_turns_from(
        self,
        index: int,
        raw_input: str,
        model_input: str,
        command_id: str | None = None,
    ) -> dict:
        """Drop everything from ``index`` on and open a new turn in its place.

        The replaced branch leaves the conversation entirely — its messages stop
        reaching the model, its records stop being shown — while the session's own
        settings (project, model, permission, authorizations, pin) are untouched,
        because an edit rewrites what was said, not where or how it runs.

        A later turn is never restored afterwards: once the new branch fails, the
        old one is already gone.
        """

        if index < 0 or index >= len(self.turns):
            raise IndexError(f"turn index out of range: {index}")
        # Replay the turns ahead of the edited one — not the edited turn itself,
        # which is the branch that is being taken out of the conversation. The
        # task list comes back with them, to the state it had before that turn ran.
        rebuild_history(self.agent.history, self.turns, self.agent.todos, before=index)
        record = TurnRecord.start(
            raw_input=raw_input,
            model_input=model_input,
            todos=self.agent.todos,
            command_id=command_id,
        )
        turn = record.to_dict()
        dropped = {str(t.get("id") or "") for t in self.turns[index:]}
        self.turns = self.turns[:index]
        # Records belong to the branch they were made on; the ones the edit
        # replaced must not reappear in the pane. Records written before turns
        # had ids cannot be attributed, so they stay.
        self.artifacts = [
            a for a in self.artifacts if a.get("turn_id") not in dropped
        ]
        self.approval_log = [
            a for a in self.approval_log if a.get("turn_id") not in dropped
        ]
        self.turns.append(turn)
        self.revision += 1
        return turn

    def record_artifact(self, tool_call, result: str | None, turn_id: str | None = None) -> None:
        """Keep one file-tool result for the pane (trimmed, never the whole output)."""
        from easycode.web.artifacts import build_record

        if not result:
            return
        record = build_record(
            tool_call.id, tool_call.name, tool_call.arguments, result, self._resolve_target
        )
        if record is not None:
            if turn_id:
                record["turn_id"] = turn_id
            self.artifacts.append(record)

    def _resolve_target(self, display: str) -> Path:
        """The canonical file a display path names, as of this session's roots."""
        return self.agent.path_context().resolve(display)

    def record_turn_failure(self, message: str, code: str | None, turn_id: str) -> None:
        """Attach a server-produced terminal error to the turn that raised it.

        The record is what a reload reads to put the error back where it
        happened, so it is stored on the turn rather than derived from a
        timestamp the renderer would have to match up.
        """
        index = self.turn_index(turn_id)
        if index < 0:
            return
        entry: dict = {"message": message}
        if code:
            entry["code"] = code
        failures = self.turns[index].setdefault("failures", [])
        if entry not in failures:
            failures.append(entry)

    def finish_turn(self, turn_id: str, status: str, error: str | None = None) -> None:
        """Close out a turn that will not report a status of its own."""
        index = self.turn_index(turn_id)
        if index < 0:
            return
        turn = self.turns[index]

        if turn.get("status") != RUNNING:
            return
        turn["status"] = status
        if error:
            self.record_turn_failure(error, None, turn_id)

    def _baseline(self) -> list[dict]:
        """The model context's own head, which is not part of the conversation.

        That is a compaction summary and nothing else. Everything the turns
        recorded is rebuilt from them, so counting any of it as baseline would
        duplicate it; a message the context dropped is still in its turn, which
        is the whole point of keeping the two apart. Deriving it here is what
        carries a summary written during a live turn through the next flush —
        without it a restart would quietly undo the last compaction.
        """
        from easycode.agent.context import History

        head = self.agent.history.messages
        if head and History.is_summary(head[0]):
            return [dict(head[0])]
        return []

    def projection(self) -> dict:
        """The conversation as the transcript reads it, plus the current revision."""

        out = project_detail(self.turns, self.history_base)
        out["revision"] = self.revision
        out["turn_status"] = turn_statuses(self.turns)
        return out

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
            "started": not self.is_blank,
        }
        if self.root:
            out["root"] = self.root
        if self.secondary_roots:
            out["secondary_roots"] = list(self.secondary_roots)
        if self.archived:
            out["archived"] = True
        if self.pinned:
            out["pinned"] = True
            out["pinned_at"] = self.pinned_at
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
        # The agent owns the alias; the store stamps the one it asked for so a
        # replaced agent factory cannot leave the session without one.
        agent.model_alias = alias
        sess = Session(
            id=sid,
            title="新会话",
            created_at=_now(),
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

    def set_pinned(self, session_id: str, pinned: bool) -> Session | None:
        """Pin or unpin a session; pinning records when, for stable ordering."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        sess.pinned = pinned
        sess.pinned_at = _now() if pinned else None
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
                root = data.get("root")
                secondary = list(data.get("secondary_roots") or [])
                agent_kwargs: dict = {}
                if root:
                    agent_kwargs["root"] = root
                if secondary:
                    agent_kwargs["secondary_roots"] = secondary
                alias = data.get("model_alias") or self.cfg.default_model
                agent = self.restore_factory(alias, **agent_kwargs)
                agent.model_alias = alias
                mode = data.get("permission_mode") or self.cfg.permission_mode
                confirmed = bool(data.get("full_access_confirmed"))
                # Full access saved without a recorded consent predates the
                # confirmation gate: it comes back asking, and the downgrade is
                # written out below so the user is asked exactly once.
                downgraded = mode == PERM_ALLOW_ALL and not confirmed
                agent.permission_mode = PERM_ASK if downgraded else mode
                # the task list lives on the agent, so restore it there
                agent.todos = list(data.get("todos") or [])
                stored = list(data.get("messages") or [])
                turn_data = list(data.get("turns") or [])
                if turn_data:
                    turns = [dict(t) for t in turn_data]
                    base = list(data.get("history_base") or [])
                else:
                    # A conversation written before turns existed is re-read from
                    # its surviving history: what compaction already dropped is
                    # gone, and the summary it left behind becomes the baseline.
                    # Its ids are derived from the file, so they hold still
                    # without a write — the migrated shape is saved when the
                    # session next changes.
                    base, turns = split_legacy(
                        stored,
                        list(data.get("user_times") or []),
                        list(data.get("turn_failures") or []),
                        str(data["id"]),
                    )
                for turn in turns:
                    repair_interrupted(turn)
                # The agent keeps the whole conversation; compaction reshapes it
                # on the next call rather than having already lost the messages
                # the transcript still shows.
                agent.history.messages = full_messages(base, turns)
                artifacts = list(data.get("artifacts") or [])
                if not artifacts:
                    from easycode.web.artifacts import records_from_messages

                    artifacts = records_from_messages(
                        agent.history.messages, agent.path_context().resolve
                    )
                sess = Session(
                    id=data["id"],
                    title=data.get("title", "新会话"),
                    created_at=data.get("created_at", _now()),
                    agent=agent,
                    messages=stored,
                    root=root,
                    secondary_roots=secondary,
                    always_allow=list(data.get("always_allow") or []),
                    approval_log=list(data.get("approval_log") or []),
                    turns=turns,
                    revision=int(data.get("revision") or 0),
                    history_base=base,
                    artifacts=artifacts,
                    archived=bool(data.get("archived")),
                    pinned=bool(data.get("pinned")),
                    pinned_at=data.get("pinned_at"),
                    full_access_confirmed=mode == PERM_ALLOW_ALL and confirmed,
                )
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
        session.history_base = session._baseline()
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
            "turns": list(session.turns),
            "revision": session.revision,
            "always_allow": list(session.always_allow),
            "approval_log": list(session.approval_log),
            "archived": session.archived,
            "todos": list(session.agent.todos),
        }
        if session.pinned:
            payload["pinned"] = True
            payload["pinned_at"] = session.pinned_at
        if session.history_base:
            payload["history_base"] = list(session.history_base)
        if session.artifacts:
            payload["artifacts"] = list(session.artifacts)
        # Absent means "not confirmed": a file without it is read back asking.
        if session.full_access_confirmed:
            payload["full_access_confirmed"] = True
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
