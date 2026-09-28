"""Conversation turns: the full record the display reads, kept apart from the model context.

A session persists two things that answer different questions. ``history_base``
plus one entry of ``TurnRecord.messages`` per turn is the *whole* conversation —
what the user said and everything the model did in reply — and it never shrinks.
The agent's :class:`~easycode.agent.context.History` is the *model context*: the
same messages after compaction and pruning reshaped them for the next call, and
it is free to drop whatever no longer fits.

Keeping them apart is what lets an earlier user message be edited. Rewriting the
model context alone would save a transcript that no longer contains the turns it
was built from, so a replayed or reloaded session could not show — or rebuild —
the branch the user picked.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from easycode.agent.context import History

#: ``status`` values a turn can hold. A turn that never left ``running`` was
#: interrupted (a crash, a kill); restoring one repairs it before it is used.
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"


def new_turn_id() -> str:
    """A stable id for one turn; never a list index or a timestamp."""
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TurnRecord:
    """One user turn, as persisted.

    ``messages`` holds the messages this turn added to the model history —
    including assistant ``tool_calls`` and their results — copied at insertion
    time so a later compaction that replaces them in the live history cannot
    rewrite the record. ``todos_before`` is the task list as it stood when the
    turn started, which is what editing this turn must restore.
    """

    id: str
    created_at: str
    raw_input: str
    model_input: str
    messages: list[dict] = field(default_factory=list)
    status: str = RUNNING
    todos_before: list[dict] = field(default_factory=list)
    command_id: str | None = None
    #: Terminal errors this turn ended with (``{"message", "code"?}``); the
    #: server records them here so a reload puts the error back where it happened.
    failures: list[dict] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        raw_input: str,
        model_input: str,
        todos: list[dict] | None = None,
        command_id: str | None = None,
    ) -> TurnRecord:
        return cls(
            id=new_turn_id(),
            created_at=now_iso(),
            raw_input=raw_input,
            model_input=model_input,
            command_id=command_id,
            todos_before=copy.deepcopy(list(todos or [])),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "raw_input": self.raw_input,
            "model_input": self.model_input,
            "messages": self.messages,
            "status": self.status,
            "todos_before": self.todos_before,
            "command_id": self.command_id,
            "failures": self.failures,
        }



class TurnRecorder:
    """Appends every message this turn adds to the model history onto its record.

    Bound to the session agent's :class:`History` for the duration of one turn.
    Only an explicit ``add`` is captured, so trimming, condensing and restoring
    history leave the record alone: a summary replacing a run of messages is not
    a new chat message, and recording it as one would put the model's own
    bookkeeping in front of the user.

    It writes to the persisted ``dict`` shape rather than a detached object, so
    what the agent captures is exactly what the session file will hold.
    """

    def __init__(self, record: dict) -> None:
        self.record = record

    def on_add(self, message: dict) -> None:
        # A copy, not a reference: compaction replaces message objects, and the
        # snapshot must survive whatever the live history does next.
        self.record.setdefault("messages", []).append(copy.deepcopy(message))


def _status_of(data: dict) -> str:
    return str(data.get("status") or COMPLETED)


def _failure_entry(failure: dict) -> dict:
    out = {"message": str(failure.get("message") or "")}
    if failure.get("code"):
        out["code"] = str(failure["code"])
    return out


def project_detail(turns: list[dict], history_base: list[dict]) -> dict:
    """The display projection: every message of the conversation, in order.

    ``messages`` is what the transcript renders and its per-message ``turn_id``
    is how the view names the turn an item belongs to — the user message carries
    the turn it opened, and everything the model did in reply sits after it, so
    the pair survives a reload, a compaction and an edit.

    ``failures`` says what a terminal error was and which turn it ended; whether
    a turn is still working is the session's own state, not this projection's.
    """
    # The baseline is the model context's own head, and a compaction summary in
    # it is the model's bookkeeping, not something the user said.
    messages: list[dict] = [dict(m) for m in history_base if not History.is_summary(m)]
    failures: list[dict] = []
    for turn in turns:
        tid = str(turn.get("id") or "")
        status = _status_of(turn)
        entries = turn.get("messages") or []
        for message in entries:
            copy_msg = dict(message)
            copy_msg["turn_id"] = tid
            messages.append(copy_msg)
        if not entries and turn.get("raw_input"):
            # The turn ended before its input reached the history (MCP setup
            # failed, the request was refused): the prompt the user typed still
            # belongs in the transcript, and is still what an edit targets.
            messages.append({"role": "user", "content": turn.get("raw_input"), "turn_id": tid})
        if status in (FAILED, CANCELLED):
            for failure in turn.get("failures") or []:
                failures.append({"turn_id": tid, **_failure_entry(failure)})
    return {"messages": messages, "failures": failures}


def turn_statuses(turns: list[dict]) -> list[dict]:
    """Compact per-turn metadata for the client (ids and terminal states only)."""
    out: list[dict] = []
    for turn in turns:
        entry: dict[str, Any] = {
            "id": str(turn.get("id") or ""),
            "created_at": turn.get("created_at"),
            "status": _status_of(turn),
        }
        if turn.get("command_id"):
            entry["command_id"] = turn["command_id"]
        out.append(entry)
    return out


def split_legacy(
    messages: list[dict],
    user_times: list[str],
    failures: list[dict],
    session_id: str,
) -> tuple[list[dict], list[dict]]:
    """Reconstruct turns from a session written before turns were recorded.

    Everything from the first user message on becomes a turn; a summary or a
    stray system message in front of it is kept as ``history_base`` — it is the
    only thing an already-compacted session still carries of what came before,
    and it is not a user turn.

    A turn's prompt is the surviving user content, because the command the user
    originally picked was never stored: inventing one would make an edit silently
    run a command the user never chose. ``todos_before`` cannot be recovered at
    all (the task list is only written in its final state), so it stays empty
    rather than back-filling later work into an earlier turn's starting point.

    The ids are derived from the session and the turn's position, so reading the
    same file twice yields the same ids even if nothing has been written back
    yet — a reloaded session must not renumber the turns a client is holding.
    """
    first_user = next(
        (i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("content")),
        None,
    )
    if first_user is None:
        return ([dict(m) for m in messages], [])
    history_base = [dict(m) for m in messages[:first_user]]
    groups: list[list[dict]] = []
    for message in messages[first_user:]:
        if message.get("role") == "user" and groups and groups[-1]:
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(dict(message))
    failures_by_time = {str(f.get("time")): f for f in failures if f.get("time")}
    turns = [
        _legacy_turn(
            entries,
            f"r{index}-{session_id}",
            user_times[index] if index < len(user_times) else None,
            failures_by_time,
        )
        for index, entries in enumerate(groups)
    ]
    return (history_base, turns)


def _legacy_turn(
    messages: list[dict],
    turn_id: str,
    created_at: str | None,
    failures_by_time: dict[str, dict],
) -> dict:
    """One reconstructed turn; ``created_at`` is merely a label for display."""
    prompt = ""
    for message in messages:
        if message.get("role") == "user" and message.get("content"):
            prompt = str(message["content"])
            break
    record = TurnRecord(
        id=turn_id,
        created_at=created_at or now_iso(),
        raw_input=prompt,
        model_input=prompt,
        messages=[dict(m) for m in messages],
        status=COMPLETED,
    )
    failure = failures_by_time.get(str(created_at)) if created_at else None
    if failure:
        record.failures = [_failure_entry(failure)]
    return record.to_dict()


def full_messages(history_base: list[dict], turns: list[dict]) -> list[dict]:
    """Every message of the conversation, in order: the model context rebuilt."""
    out: list[dict] = [dict(m) for m in history_base]
    for turn in turns:
        out.extend(dict(m) for m in turn.get("messages") or [])
    return out


def rebuild_history(
    history: History, turns: list[dict], todos: list[dict], *, before: int | None = None
) -> None:
    """Restore the model context to the state *before* one of the turns ran.

    Editing a turn means the messages it produced — and every turn after it —
    must not reach the model again. Replaying the retained turns verbatim is
    what guarantees the replaced branch is absent rather than merely unread;
    compaction then runs on the next call and reshapes it as usual.

    ``before`` names the turn being replaced; the turns ahead of it are replayed
    and it is left out. ``None`` replays all of them (the state after the last).
    """
    kept = turns if before is None else turns[:before]
    history.messages = [dict(m) for turn in kept for m in turn.get("messages") or []]
    if before is None or before >= len(turns):
        todos[:] = copy.deepcopy(list(todos))
    else:
        target = turns[before]
        todos[:] = copy.deepcopy(list(target.get("todos_before") or []))


def repair_interrupted(turn: dict) -> bool:
    """Give an interrupted turn a terminal status and close its open tool calls.

    A session file written by a process that stopped mid-turn holds a ``running``
    record whose last assistant message may still declare tool calls with no
    results — a history the provider rejects. Appending the missing results and
    marking the turn cancelled is what makes such a session continuable, and it
    reports whether anything changed so the caller can persist the repair.
    """
    if _status_of(turn) != RUNNING:
        return False
    messages = turn.get("messages") or []
    answered = {
        str(m.get("tool_call_id"))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            cid = str(call.get("id") or "")
            if not cid or cid in answered:
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": str((call.get("function") or {}).get("name") or ""),
                    "content": (
                        '{"status": "error", "message": "Turn interrupted before this tool '
                        'reported a result. If it started, its effects may remain."}'
                    ),
                }
            )
            answered.add(cid)
    turn["status"] = CANCELLED
    return True
