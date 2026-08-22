"""Web session management: multi-session isolation + disk persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from easycode.agent.loop import Agent
from easycode.credentials import data_home

AgentFactory = Callable[[str], Agent]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    permission_mode: str = "ask"  # ask | auto-review | allow-all
    cancel_event: Any | None = None  # asyncio.Event; set by the cancel endpoint
    always_allow: list[str] = field(default_factory=list)  # approval_key() scopes, persists
    approval_log: list[dict] = field(default_factory=list)  # resolved approval records
    user_times: list[str] = field(default_factory=list)  # ISO timestamps per user message
    archived: bool = False  # hidden from the sidebar main list (对齐 codex 归档)

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

    def __init__(self, cfg, root: Path, agent_factory: AgentFactory) -> None:
        self.cfg = cfg
        self.root = root
        self.agent_factory = agent_factory
        self.dir = data_home() / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}

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
        secondary = list(secondary_roots or [])
        if root is not None:
            agent_kwargs["root"] = root
        if secondary:
            agent_kwargs["secondary_roots"] = secondary
        agent = self.agent_factory(alias, **agent_kwargs) if agent_kwargs else self.agent_factory(alias)
        mode = permission_mode or self.cfg.permission_mode
        agent.permission_mode = mode
        sess = Session(
            id=sid,
            title="新会话",
            created_at=_now(),
            model_alias=alias,
            agent=agent,
            root=root,
            secondary_roots=secondary,
            permission_mode=mode,
        )
        self._sessions[sid] = sess
        self._flush(sess)
        return sess

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

    def archive_root(self, root: str | None) -> int:
        count = 0
        for sess in self._sessions.values():
            if sess.root == root:
                sess.archived = True
                count += 1
        if count:
            self._flush_many()
        return count

    def delete_root(self, root: str | None) -> int:
        """Delete every session under ``root`` (including archived)."""
        ids = [s.id for s in self._sessions.values() if s.root == root]
        for sid in ids:
            self.delete(sid)
        return len(ids)

    def delete(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
        try:
            self._path(session_id).unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def load_all(self) -> None:
        """Restore sessions persisted on disk (survives browser refresh / restart)."""
        for p in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                root = data.get("root")
                secondary = list(data.get("secondary_roots") or [])
                permission_mode = data.get("permission_mode") or self.cfg.permission_mode
                agent_kwargs: dict = {}
                if root:
                    agent_kwargs["root"] = root
                if secondary:
                    agent_kwargs["secondary_roots"] = secondary
                agent = self.agent_factory(
                    data.get("model_alias") or self.cfg.default_model, **agent_kwargs
                )
                agent.permission_mode = permission_mode
                sess = Session(
                    id=data["id"],
                    title=data.get("title", "新会话"),
                    created_at=data.get("created_at", _now()),
                    model_alias=data.get("model_alias", self.cfg.default_model),
                    agent=agent,
                    messages=list(data.get("messages") or []),
                    root=root,
                    secondary_roots=secondary,
                    permission_mode=permission_mode,
                    always_allow=list(data.get("always_allow") or []),
                    approval_log=list(data.get("approval_log") or []),
                    user_times=list(data.get("user_times") or []),
                    archived=bool(data.get("archived")),
                )
                sess.agent.history.messages = list(sess.messages)
                self._sessions[sess.id] = sess
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue

    def record_exchange(self, session: Session) -> None:
        """Persist current history after a turn."""
        session.messages = list(session.agent.history.messages)
        n_user = sum(1 for m in session.messages if m.get("role") == "user")
        session.user_times = list(session.user_times[:n_user])
        self._flush(session)

    def _flush_many(self) -> None:
        for sess in self._sessions.values():
            self._flush(sess)

    def _flush(self, session: Session) -> None:
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
        tmp = self._path(session.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(session.id))
