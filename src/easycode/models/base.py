"""Core message/tool-call types and the Provider protocol (OpenAI-format messages)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]
"""OpenAI-format message: role/content/tool_calls/tool_call_id."""


@dataclass
class ToolCall:
    """A function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_message_content(self) -> dict[str, Any]:
        return {
            "type": "function",
            "id": self.id,
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }


@dataclass
class StreamEvent:
    """One event yielded by ``Provider.stream``."""

    kind: str  # "text" | "tool_calls" | "done" | "error"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    error: str | None = None


class Provider(ABC):
    """Abstraction over a chat LLM with streaming + tool calling."""

    model: str

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion asynchronously.

        Yields ``StreamEvent``s: token-by-token ``text`` pieces, a single
        ``tool_calls`` batch when the model requests tools, then ``done``.
        On failure, yields ``error``.
        """


class DeferredProvider(Provider):
    """Placeholder provider for a session restored without a usable model.

    ``build`` runs on the first :meth:`resolve` and may raise ``ValueError``;
    until then the agent is fully inspectable (history, permission mode,
    skills) but cannot start a turn. The caller resolves it before streaming so
    a missing credential surfaces as a clear 4xx instead of a mid-stream error.
    """

    def __init__(self, build: Callable[[], Provider]) -> None:
        super().__init__("unbound")
        self._build = build
        self._resolved: Provider | None = None

    def resolve(self) -> Provider:
        if self._resolved is None:
            self._resolved = self._build()
        return self._resolved

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        provider = self.resolve()
        async for event in provider.stream(messages, tools):
            yield event