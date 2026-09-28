"""A web conversation: its agent, its record, and its own lock.

The in-memory object a turn runs against. How one is written to disk is
``web.persistence``; who may write to it, and when, is ``web.locks``.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easycode.agent.loop import Agent
from easycode.agent.turns import (
    RUNNING,
    TurnRecord,
    project_detail,
    rebuild_history,
    turn_statuses,
)
from easycode.permissions.policy import PERM_ALLOW_ALL

#: Creates an agent for an alias; implementations may accept extra kwargs
#: (root/secondary_roots) from ``SessionStore.create``.
AgentFactory = Callable[..., Agent]



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

    def snapshot_state(self) -> dict:
        """Everything rewriting a branch touches, for an all-or-nothing edit."""
        return {
            "turns": copy.deepcopy(self.turns),
            "revision": self.revision,
            "artifacts": copy.deepcopy(self.artifacts),
            "approval_log": copy.deepcopy(self.approval_log),
            "history": copy.deepcopy(self.agent.history.messages),
            "todos": copy.deepcopy(self.agent.todos),
        }

    def restore_state(self, state: dict) -> None:
        """Put back what :meth:`snapshot_state` captured."""
        self.turns = state["turns"]
        self.revision = state["revision"]
        self.artifacts = state["artifacts"]
        self.approval_log = state["approval_log"]
        self.agent.history.messages = state["history"]
        self.agent.todos = copy.deepcopy(state["todos"])

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
