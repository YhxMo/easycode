"""LLM-based conversation summarizer for context compression."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import litellm
from litellm import acompletion

from easycode.models.base import Message

Summarizer = Callable[[list[Message]], Awaitable[str]]

SUMMARY_PROMPT = (
    "Summarize the following conversation between a user and a coding agent. "
    "Keep it concise but preserve: user goals, key decisions, files/paths touched, "
    "code changes made, tool results that matter, and open follow-ups. "
    "Write in the same language as the conversation. Output only the summary."
)


class LLMSummarizer:
    """Summarize a transcript chunk with a non-streaming LLM call."""

    def __init__(self, model: str, max_chars: int = 8_000) -> None:
        self.model = model
        self.max_chars = max_chars

    async def summarize(self, messages: list[Message]) -> str:
        transcript = self._to_transcript(messages)
        try:
            resp = await acompletion(
                model=self.model,
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": transcript},
                ],
            )
            content = resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - degrade to a fallback note
            content = f"(summary unavailable: {type(exc).__name__})"
        if len(content) > self.max_chars:
            content = content[: self.max_chars] + "…[truncated]"
        return content

    @staticmethod
    def _to_transcript(messages: list[Message]) -> str:
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if m.get("tool_calls"):
                calls = [
                    f"{tc.get('function', {}).get('name', '?')}({tc.get('function', {}).get('arguments', '')})"
                    for tc in m["tool_calls"]
                ]
                content = (str(content) + "\n[tool calls] " + "; ".join(calls)).strip()
            parts.append(f"{role}: {content}")
        return "\n---\n".join(parts)