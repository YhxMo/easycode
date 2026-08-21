"""Shared fixtures: FakeProvider for offline loop tests."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from easycode.models.base import Provider, StreamEvent, ToolCall


class FakeProvider(Provider):
    """A scripted provider.

    ``script`` is a list of turn descriptions::

        [{"text": "let me look...", "tool_calls": [("t1", "glob", {"pattern": "*.py"})]},
         {"text": "here is the answer"}]

    Each entry is served on one ``stream()`` call, in order.
    """

    def __init__(
        self,
        model: str = "fake/model",
        script: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(model)
        self.script = list(script or [])
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        if not self.script:
            yield StreamEvent(kind="text", content="ok")
            yield StreamEvent(kind="done")
            return
        item = self.script.pop(0)
        if "error" in item:
            yield StreamEvent(kind="error", error=item["error"])
            yield StreamEvent(kind="done")
            return
        if item.get("text"):
            for token in item["text"]:
                yield StreamEvent(kind="text", content=token)
        if item.get("tool_calls"):
            calls = [
                ToolCall(id=tc[0], name=tc[1], arguments=tc[2])
                for tc in item["tool_calls"]
            ]
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
        yield StreamEvent(kind="done")


@pytest.fixture
def fake_provider():
    return FakeProvider