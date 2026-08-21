"""Bridge: serialize async Agent events into SSE data lines."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator

from easycode.agent.loop import Agent, AgentEvent
from easycode.models.base import ToolCall

# Throttle knobs for human-friendly streaming: coalesce token-level text into
# larger chunks (either at least TEXT_FLUSH_CHARS chars, or at most
# TEXT_FLUSH_SECONDS old) so the browser renders readable pieces instead of
# one keystroke at a time.
TEXT_FLUSH_CHARS = 64
TEXT_FLUSH_SECONDS = 0.1


def _tool_call_dict(tc: ToolCall) -> dict:
    return {"id": tc.id, "name": tc.name, "arguments": tc.arguments}


def event_to_sse(ev: AgentEvent) -> str:
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
    elif ev.kind == "approval" and ev.tool_call:
        payload["tool_call"] = _tool_call_dict(ev.tool_call)
    elif ev.kind == "review" and ev.content:
        payload["content"] = ev.content
    elif ev.kind == "cancelled":
        payload["content"] = "cancelled"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ApprovalBroker:
    """Resolves approval futures by id; wired to POST /api/approval/{id}."""

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout
        self._pending: dict[str, asyncio.Future[bool]] = {}

    def add(self, approval_id: str) -> asyncio.Future[bool]:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = fut
        return fut

    def remove(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approve: bool) -> bool:
        fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(approve)
        return True

    def cancel_all(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()


async def stream_chat(
    agent: Agent,
    message: str,
    flush_chars: int = TEXT_FLUSH_CHARS,
    flush_seconds: float = TEXT_FLUSH_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE lines for one user message through the agent loop.

    Token-level ``text`` events are coalesced with a char-count / age
    throttle; every other event kind is passed through immediately.
    """
    buf: list[str] = []
    last_flush = time.monotonic()

    def give_text() -> str:
        nonlocal buf, last_flush
        text = "".join(buf)
        buf = []
        last_flush = time.monotonic()
        return event_to_sse(AgentEvent(kind="text", content=text))

    async for ev in agent.respond(message):
        if ev.kind == "text" and ev.content:
            buf.append(ev.content)
            size = sum(len(part) for part in buf)
            age = time.monotonic() - last_flush
            if size >= flush_chars or (buf and age >= flush_seconds):
                yield give_text()
        else:
            if buf:
                yield give_text()
            yield event_to_sse(ev)
    if buf:
        yield give_text()


async def stream_chat_with_approval(
    agent: Agent,
    message: str,
    broker: ApprovalBroker,
    flush_chars: int = TEXT_FLUSH_CHARS,
    flush_seconds: float = TEXT_FLUSH_SECONDS,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[tuple[str, object]]:
    """Agent turn with human approval interleaved.

    Yields (kind, payload) pairs:
    - ("text", chunk) / ("event", AgentEvent) for the normal stream
    - ("approval", (approval_id, tool_call_dict)) when the user must decide
    - ("event", AgentEvent(kind="cancelled")) once ``cancel_event`` fires

    The agent pauses on the approval future; the caller resolves it via
    ``broker.resolve`` (or a timeout rejects it). When ``cancel_event`` is
    provided and gets set, the in-flight turn task is cancelled (the agent
    rolls back partial history) and the stream ends with a ``cancelled``
    event instead of raising.
    """
    q: asyncio.Queue = asyncio.Queue()
    buf: list[str] = []
    last_flush = time.monotonic()

    def give_text():
        nonlocal buf, last_flush
        text = "".join(buf)
        buf = []
        last_flush = time.monotonic()
        return ("text", text)

    async def run_turn() -> None:
        prev = agent.approval_handler

        async def approval_handler(tc: ToolCall) -> bool:
            approval_id = uuid.uuid4().hex[:12]
            fut = broker.add(approval_id)
            await q.put(("approval", (approval_id, tc)))
            try:
                ok = await asyncio.wait_for(fut, timeout=broker.timeout)
            except asyncio.TimeoutError:
                ok = False
            finally:
                broker.remove(approval_id)
            return ok

        agent.approval_handler = approval_handler
        try:
            async for ev in agent.respond(message):
                await q.put(("agent", ev))
        finally:
            agent.approval_handler = prev
            await q.put(("end", None))

    task = asyncio.create_task(run_turn())
    get_task: asyncio.Task | None = asyncio.create_task(q.get()) if cancel_event else None
    watcher: asyncio.Task | None = (
        asyncio.create_task(cancel_event.wait()) if cancel_event else None
    )

    try:
        while True:
            if cancel_event is not None and watcher is not None and watcher.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if buf:
                    yield give_text()
                yield ("event", AgentEvent(kind="cancelled"))
                break
            if cancel_event is not None:
                done, _ = await asyncio.wait({get_task, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    continue  # re-check cancellation at loop top
                kind, payload = get_task.result()
                get_task = asyncio.create_task(q.get())
            else:
                kind, payload = await q.get()
            if kind == "end":
                break
            if kind == "approval":
                if buf:
                    yield give_text()
                yield ("approval", payload)
            elif kind == "agent":
                ev: AgentEvent = payload
                if ev.kind == "approval":
                    continue  # broker-driven event already yielded
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
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if get_task is not None and not get_task.done():
            get_task.cancel()
        if watcher is not None and not watcher.done():
            watcher.cancel()
        if buf:
            yield give_text()
        ty = getattr(q, "shutdown", None)
        if ty is not None:
            ty()