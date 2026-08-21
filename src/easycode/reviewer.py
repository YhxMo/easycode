"""Independent pre-execution reviewer for sandbox escalation requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from easycode.models.base import Provider, ToolCall

REVIEW_SYSTEM = """You review one sandbox escalation requested by a coding agent.
Approve only when the action is necessary for the user's explicit task and has a narrow,
reversible impact. Deny credential access or exfiltration, broad destructive changes,
security weakening, persistence, and actions whose scope is not justified.
Return JSON only: {"approve": true|false, "rationale": "brief reason"}.
"""


@dataclass(frozen=True)
class ReviewDecision:
    approve: bool
    rationale: str


class AutoReviewer:
    def __init__(self, provider: Provider, timeout: float = 60.0) -> None:
        self.provider = provider
        self.timeout = timeout

    async def review(
        self,
        tool_call: ToolCall,
        reason: str,
        transcript: list[dict[str, Any]],
    ) -> ReviewDecision:
        import asyncio

        recent = [
            {"role": m.get("role"), "content": str(m.get("content") or "")[:2000]}
            for m in transcript[-12:]
            if m.get("role") in ("user", "assistant", "tool")
        ]
        request = {
            "reason": reason,
            "tool": tool_call.name,
            "arguments": tool_call.arguments,
            "recent_transcript": recent,
        }

        async def collect() -> str:
            parts: list[str] = []
            messages = [
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ]
            async for event in self.provider.stream(messages, tools=[]):
                if event.kind == "text" and event.content:
                    parts.append(event.content)
                elif event.kind == "error":
                    raise RuntimeError(event.error or "reviewer error")
            return "".join(parts)

        try:
            raw = await asyncio.wait_for(collect(), timeout=self.timeout)
            data = _parse_json(raw)
            if not isinstance(data.get("approve"), bool):
                raise ValueError("reviewer response lacks boolean approve")
            return ReviewDecision(bool(data["approve"]), str(data.get("rationale") or "no rationale"))
        except Exception as exc:  # noqa: BLE001 - reviewer failures must fail closed
            return ReviewDecision(False, f"automatic review failed: {type(exc).__name__}: {exc}")


def _parse_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        if value.lstrip().startswith("json"):
            value = value.lstrip()[4:].lstrip()
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("reviewer response is not an object")
    return data
