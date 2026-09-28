"""LLM-based conversation summarizer for context compression."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from litellm import acompletion

from easycode.models.base import Message

Summarizer = Callable[[list[Message], str | None], Awaitable[str | None]]

TOOL_OUTPUT_MAX_CHARS = 2_000

SUMMARY_PROMPT = (
    "You are a context summarization agent. You are given a conversation between "
    "a user and a coding agent. Produce a structured summary in the exact format "
    "requested so another coding agent can continue the work. Keep every section, "
    "preserve exact file paths and identifiers, and prefer terse bullets. Do not "
    "continue the conversation or answer any questions in it. Respond in the same "
    "language as the conversation."
)

SUMMARY_TEMPLATE = """Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers when known.
- Do not mention the summary process or that context was compacted."""

SUMMARY_UPDATE_INSTRUCTIONS = """The <prior-summary> summarizes everything that happened before the <conversation>. Construct a new summary that combines both. The <prior-summary> is discarded after this: anything you do not carry into the new summary is lost.

When combining:
- Carry forward objectives, constraints, user directives, decisions, and parallel workstreams from the <prior-summary> even when the <conversation> does not mention them. Drop only what is finished and no longer needed.
- The <conversation> is more recent than the <prior-summary>. Where they conflict, the conversation wins: state the corrected fact and drop the old claim.
- Add new progress, decisions, constraints, and context from the conversation.
- Move completed work from "Active" to "Completed".
- If a blocker has been resolved, update the summary to reflect that while keeping any details still needed to continue the work.
- Update "Objective" and "Next Move" to reflect the current work state."""


def build_prompt(previous_summary: str | None, context: str) -> str:
    """Build the summarization prompt; merge the prior summary when present."""
    conversation = (
        f"Here is the conversation so far:\n\n<conversation>\n{context}\n</conversation>"
    )
    if not previous_summary:
        return (
            f"{conversation}\n\n"
            "Create a new anchored summary from the conversation history in the "
            "<conversation> tags above so another coding agent can continue the work.\n\n"
            f"{SUMMARY_TEMPLATE}"
        )
    return (
        f"{conversation}\n\n"
        "Here is the summary of the conversation before the <conversation> above:\n\n"
        f"<prior-summary>\n{previous_summary}\n</prior-summary>\n\n"
        f"{SUMMARY_UPDATE_INSTRUCTIONS}\n\n"
        f"{SUMMARY_TEMPLATE}"
    )


class LLMSummarizer:
    """Summarize a transcript chunk with a non-streaming LLM call.

    Callable as ``summarizer(messages, previous_summary)`` so repeated
    compactions merge into one rolling summary instead of overwriting it.
    """

    def __init__(self, model: str, max_chars: int = 8_000, **kwargs: Any) -> None:
        self.model = model
        self.max_chars = max_chars
        self.kwargs = kwargs

    async def __call__(
        self, messages: list[Message], previous_summary: str | None = None
    ) -> str | None:
        context = self._to_transcript(messages)
        prompt = build_prompt(previous_summary, context)
        try:
            resp = await acompletion(
                model=self.model,
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                **self.kwargs,
            )
            content = resp.choices[0].message.content or ""
        except Exception:  # noqa: BLE001 - never fabricate a summary
            # Returning None lets the caller fall back (e.g. trim) without
            # replacing the original messages with a fake "(summary
            # unavailable...)"" note — that note would corrupt the transcript.
            return None
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
            text = str(content)
            if len(text) > TOOL_OUTPUT_MAX_CHARS:
                text = text[:TOOL_OUTPUT_MAX_CHARS] + "\n...[truncated]"
            parts.append(f"{role}: {text}")
        return "\n---\n".join(parts)