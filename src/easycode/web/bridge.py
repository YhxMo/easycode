"""Bridge: serialize async Agent events into SSE data lines."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from easycode.agent.loop import Agent, AgentEvent
from easycode.models.base import ToolCall

if TYPE_CHECKING:
    from easycode.web.session import Session

# Throttle knobs for human-friendly streaming: coalesce token-level text into
# larger chunks (either at least TEXT_FLUSH_CHARS chars, or at most
# TEXT_FLUSH_SECONDS old) so the browser renders readable pieces instead of
# one keystroke at a time.
TEXT_FLUSH_CHARS = 64
TEXT_FLUSH_SECONDS = 0.1


def _tool_call_dict(tc: ToolCall) -> dict:
    return {"id": tc.id, "name": tc.name, "arguments": tc.arguments}


def _sse_line(payload: dict) -> str:
    """Single authority: serialize a JSON payload into one SSE ``data:`` line."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _event_payload(ev: AgentEvent) -> dict:
    payload: dict = {"type": ev.kind}
    if ev.kind == "text" and ev.content:
        payload["content"] = ev.content
    elif ev.kind == "tool_start" and ev.tool_call:
        payload["tool_call"] = _tool_call_dict(ev.tool_call)
    elif ev.kind == "tool_result" and ev.tool_call:
        payload["tool_call"] = _tool_call_dict(ev.tool_call)
        payload["result"] = ev.tool_result
    elif ev.kind == "error" and ev.error:
        payload["error"] = ev.error
        if ev.code:
            payload["code"] = ev.code
    elif ev.kind == "review" and ev.content:
        payload["content"] = ev.content
    elif ev.kind == "todo" and ev.content:
        # the live task list, so the pane can update without refetching
        payload["todos"] = json.loads(ev.content)
    elif ev.kind == "cancelled":
        payload["content"] = "cancelled"
    return payload


def event_to_sse(ev: AgentEvent | dict[str, Any]) -> str:
    """Serialize an agent event or payload dictionary as one SSE data line."""
    payload = _event_payload(ev) if isinstance(ev, AgentEvent) else dict(ev)
    return _sse_line(payload)


def session_sse(session_id: str) -> str:
    return event_to_sse({"type": "session", "session_id": session_id})


def approval_required_sse(approval_id: str, tc: ToolCall, reason: str, scope: str) -> str:
    return event_to_sse(
        {
            "type": "approval_required",
            "approval_id": approval_id,
            "reason": reason,
            "scope": scope,
            "tool_call": _tool_call_dict(tc),
        }
    )


class ApprovalBroker:
    """Resolves approval futures by id; wired to POST /api/approval/{id}."""

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout
        self._pending: dict[str, asyncio.Future[tuple[bool, bool]]] = {}

    def add(self, approval_id: str) -> asyncio.Future[tuple[bool, bool]]:
        fut: asyncio.Future[tuple[bool, bool]] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = fut
        return fut

    def remove(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approve: bool, always: bool = False) -> bool:
        fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result((approve, always))
        return True


async def stream_chat_with_approval(
    agent: Agent,
    message: str,
    broker: ApprovalBroker,
    flush_chars: int = TEXT_FLUSH_CHARS,
    flush_seconds: float = TEXT_FLUSH_SECONDS,
    cancel_event: asyncio.Event | None = None,
    session: Session | None = None,
) -> AsyncIterator[tuple[str, object]]:
    """Agent turn with human approval interleaved.

    Yields (kind, payload) pairs:
    - ("event", AgentEvent) for the normal stream; token-level text is
      coalesced into larger ``AgentEvent(kind="text")`` chunks
    - ("approval", (approval_id, tool_call, reason, scope)) when the user must decide
    - ("event", AgentEvent(kind="cancelled")) once ``cancel_event`` fires

    The agent pauses on the approval future; the caller resolves it via
    ``broker.resolve`` (or a timeout rejects it). When ``cancel_event`` is
    provided and gets set, the in-flight turn task is cancelled (the agent
    retains completed operations) and the stream ends with a ``cancelled``
    event instead of raising.

    When ``session`` is given, approvals that match the session's recorded
    ``always_allow`` scopes are auto-approved without prompting, and every
    decision/timeout is appended to the session's ``approval_log``.
    """
    q: asyncio.Queue = asyncio.Queue()
    buf: list[str] = []
    last_flush = time.monotonic()

    def give_text():
        nonlocal buf, last_flush
        text = "".join(buf)
        buf = []
        last_flush = time.monotonic()
        return ("event", AgentEvent(kind="text", content=text))

    async def run_turn() -> None:
        prev = agent.approval_handler

        async def approval_handler(tc: ToolCall, reason: str, identity: str) -> bool:
            from easycode.approval import approval_scope

            scope = approval_scope(tc)
            if session and identity in session.always_allow:
                # 'always allow' keeps its capability scope in the stored key;
                # the loop regenerates the precise target grant against the
                # executing agent's own context.
                return True
            approval_id = uuid.uuid4().hex[:12]
            fut = broker.add(approval_id)
            await q.put(("approval", (approval_id, tc, reason, scope)))
            decision = "expired"
            always = False
            try:
                approve, always = await asyncio.wait_for(fut, timeout=broker.timeout)
                decision = "approved" if approve else "denied"
                if approve and session and always and identity not in session.always_allow:
                    session.always_allow.append(identity)
            except TimeoutError:
                decision = "expired"
            finally:
                broker.remove(approval_id)
                if session is not None:
                    session.approval_log.append(
                        {
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "args": tc.arguments,
                            "reason": reason,
                            "scope": scope,
                            "decision": decision,
                            "always": bool(always and decision == "approved"),
                        }
                    )
            return decision == "approved"

        agent.approval_handler = approval_handler
        try:
            async for ev in agent.respond(message):
                await q.put(("agent", ev))
        finally:
            # Only restore the previous handler if this turn's
            # handler is STILL the current one. A concurrent turn (or a foreign
            # set) may have replaced it; unconditionally writing ``prev`` back
            # would clobber that owner and let approvals leak across turns. The
            # closure identity acts as the ownership token.
            if agent.approval_handler is approval_handler:
                agent.approval_handler = prev
            await q.put(("end", None))

    if cancel_event is None:
        # No caller-supplied cancellation: a never-set event keeps one code path.
        cancel_event = asyncio.Event()
    task = asyncio.create_task(run_turn())
    get_task = asyncio.create_task(q.get())
    watcher = asyncio.create_task(cancel_event.wait())

    try:
        while True:
            if watcher.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if buf:
                    yield give_text()
                yield ("event", AgentEvent(kind="cancelled"))
                break
            done, _ = await asyncio.wait({get_task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                continue  # re-check cancellation at loop top
            kind, payload = get_task.result()
            get_task = asyncio.create_task(q.get())
            if kind == "end":
                break
            if kind == "approval":
                if buf:
                    yield give_text()
                yield ("approval", payload)
            elif kind == "agent":
                ev: AgentEvent = payload
                if ev.kind == "text" and ev.content:
                    buf.append(ev.content)
                    size = sum(len(part) for part in buf)
                    age = time.monotonic() - last_flush
                    if size >= flush_chars or (buf and age >= flush_seconds):
                        yield give_text()
                else:
                    if buf:
                        yield give_text()
                    yield ("event", ev)
        if buf:
            yield give_text()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not get_task.done():
            get_task.cancel()
        if not watcher.done():
            watcher.cancel()
