"""Shared helpers for Web-level tests: a gated provider and an async poller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from easycode.models.base import Provider, StreamEvent, ToolCall


class GateProvider(Provider):
    """One provider shared by an agent's turn.

    ``stream`` call #1 waits on ``gate`` before yielding (so the session lock is
    held deterministically while the test issues concurrent requests), then serves
    the scripted items in order. Later calls skip the gate.
    """

    def __init__(self, gate: asyncio.Event, script: list[dict], model: str = "fake/model") -> None:
        super().__init__(model)
        self.gate = gate
        self.script = list(script)
        self.calls = 0

    async def stream(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.gate is not None and self.calls == 1:
            await self.gate.wait()
        if not self.script:
            yield StreamEvent(kind="text", content="ok")
            yield StreamEvent(kind="done")
            return
        item = self.script.pop(0)
        if item.get("text"):
            for token in item["text"]:
                yield StreamEvent(kind="text", content=token)
        if item.get("tool_calls"):
            calls = [ToolCall(id=tc[0], name=tc[1], arguments=tc[2]) for tc in item["tool_calls"]]
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
        yield StreamEvent(kind="done")


async def wait_until(pred, timeout: float = 2.0) -> None:
    """Yields to the running loop until ``pred`` is true (or timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)
