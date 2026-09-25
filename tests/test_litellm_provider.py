"""LiteLLM stream accumulation: text passthrough + tool-call fragment merging."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from easycode.models.base import ToolCall
from easycode.models.litellm_provider import LiteLLMProvider


class _ToolCallDelta:
    def __init__(
        self, index: int, id: str | None = None, name: str | None = None, arguments: str | None = None
    ) -> None:
        self.index = index
        self.id = id
        self.function: dict[str, str] = {}
        if name:
            self.function["name"] = name
        if arguments:
            self.function["arguments"] = arguments


class _Delta:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, delta: _Delta | None) -> None:
        self.choices = [type("_Choice", (), {"delta": delta})()] if delta is not None else []


async def _stream(chunks: list[_Chunk]) -> AsyncIterator[_Chunk]:
    for chunk in chunks:
        yield chunk


async def _collect(chunks: list[_Chunk]):
    provider = LiteLLMProvider("fake/model")
    return [event async for event in provider._accumulate(_stream(chunks))]


async def test_text_and_fragmented_tool_call_events() -> None:
    """One text delta + two tool_call fragments produce text, merged call, done."""
    chunks = [
        _Chunk(_Delta(content="hello ")),
        _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, id="call_1", name="glob", arguments='{"pat')])),
        _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, arguments='tern": "*.py"}')])),
        _Chunk(_Delta()),  # empty delta is skipped
    ]

    events = await _collect(chunks)

    assert [event.kind for event in events] == ["text", "tool_calls", "done"]
    assert events[0].content == "hello "
    assert events[1].tool_calls == [
        ToolCall(id="call_1", name="glob", arguments={"pattern": "*.py"})
    ]


async def test_no_tool_calls_emits_only_text_and_done() -> None:
    events = await _collect([_Chunk(_Delta(content="x")), _Chunk(_Delta())])

    assert [event.kind for event in events] == ["text", "done"]
