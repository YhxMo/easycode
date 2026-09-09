"""LiteLLM-backed Provider implementation with SSA-style tool-call accumulation."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from litellm import acompletion

from easycode.models.base import Provider, StreamEvent, ToolCall


class LiteLLMProvider(Provider):
    """Streams via ``litellm.acompletion(stream=True)``.

    Tool-call deltas arrive as fragments keyed by ``index``; arguments are
    accumulated as JSON text chunks, then parsed when the call completes.
    """

    def __init__(self, model: str, **kwargs: Any) -> None:
        super().__init__(model)
        self.kwargs = kwargs

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        try:
            response = await acompletion(
                model=self.model,
                messages=messages,
                tools=tools or None,
                stream=True,
                **self.kwargs,
            )
            async for ev in self._accumulate(response):
                yield ev
        except Exception as exc:  # noqa: BLE001 - surface any provider error
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")

    async def _accumulate(self, response: Any) -> AsyncIterator[StreamEvent]:
        calls: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"id": "", "name": "", "arguments": ""}
        )
        saw_tool_calls = False

        async for chunk in response:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta or {}
            if not (getattr(delta, "content", None) or getattr(delta, "tool_calls", None)):
                continue

            if getattr(delta, "content", None):
                content = getattr(delta, "content", None)
                if content is not None:
                    yield StreamEvent(kind="text", content=str(content))

            if getattr(delta, "tool_calls", None):
                saw_tool_calls = True
                for tc in delta.tool_calls:
                    if tc is None:
                        continue
                    idx = tc.index
                    slot = calls[idx]
                    fn = tc.function or {}
                    if tc.id:
                        slot["id"] = tc.id
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        if saw_tool_calls:
            tool_calls: list[ToolCall] = []
            for idx in sorted(calls):
                slot = calls[idx]
                args: dict[str, Any] = {}
                raw = slot["arguments"]
                if raw.strip():
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        args = {"_raw": raw}
                tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
            yield StreamEvent(kind="tool_calls", tool_calls=tool_calls)

        yield StreamEvent(kind="done")