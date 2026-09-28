"""The session file format: what one session looks like on disk.

Kept apart from the store so the on-disk shape can be read on its own: every
field that is not written has a documented default, which is what lets a file
written by an earlier version still restore.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from easycode.agent.turns import full_messages, now_iso, repair_interrupted, split_legacy
from easycode.permissions.policy import PERM_ALLOW_ALL, PERM_ASK
from easycode.web.artifacts import records_from_messages
from easycode.web.session import AgentFactory, Session


def restore(data: dict, *, cfg: Any, agent_factory: AgentFactory) -> tuple[Session, bool]:
    """Rebuild one persisted session; the flag says the file must be rewritten.

    A file recording full access without the confirmation flag predates the
    gate: it comes back asking, and the caller writes the downgrade out so the
    user is asked exactly once.
    """
    root = data.get("root")
    secondary = list(data.get("secondary_roots") or [])
    agent_kwargs: dict = {}
    if root:
        agent_kwargs["root"] = root
    if secondary:
        agent_kwargs["secondary_roots"] = secondary
    alias = data.get("model_alias") or cfg.default_model
    agent = agent_factory(alias, **agent_kwargs)
    agent.model_alias = alias
    mode = data.get("permission_mode") or cfg.permission_mode
    confirmed = bool(data.get("full_access_confirmed"))
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
        artifacts = records_from_messages(agent.history.messages, agent.path_context().resolve)
    sess = Session(
        id=data["id"],
        title=data.get("title", "新会话"),
        created_at=data.get("created_at", now_iso()),
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
    return sess, downgraded


def to_payload(session: Session) -> dict:
    """The on-disk record of one session.

    Only what differs from a default is written, so a file stays readable and a
    key that is absent keeps its documented meaning.
    """
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
    return payload


def tmp_path(path: Path, session_id: str) -> Path:
    """A per-flush temp path unique to this process/call.

    Concurrent ``_flush`` calls for the same session must never write to a
    shared ``.tmp`` file (two ``write_text`` to one path can interleave and
    move a corrupt/partial file into place). A unique suffix — pid + fresh
    uuid — makes every flush write to its own temp file; the atomic
    ``replace`` then leaves a complete, valid session file.
    """
    return path.with_name(f"{session_id}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
