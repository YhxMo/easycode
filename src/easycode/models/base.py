"""Core message/tool-call types and the Provider protocol (OpenAI-format messages)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
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