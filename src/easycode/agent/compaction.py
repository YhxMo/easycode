"""Context budget, old tool-output pruning, and rolling summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from easycode.agent.context import History
from easycode.agent.summarizer import Summarizer

COMPACTION_DEFAULTS: dict[str, Any] = {
    "auto": True,
    "buffer": 20_000,
    "preserve_recent_tokens": None,
    "tail_turns": None,
    "prune": True,
    "summary_max_chars": 8_000,
}
PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
PRUNED_OUTPUT = "[Tool output cleared]"
PROTECTED_TOOL_OUTPUTS = {"use_skill", "task"}


class BudgetExceededError(Exception):
    """The completion payload cannot fit within the model context window."""


@dataclass
class Compactor:
    settings: dict[str, Any]

    def usable_tokens(self, max_context_tokens: int, model_limits: dict[str, int] | None) -> int:
        if not model_limits:
            return max_context_tokens
        output = model_limits["output"] or 0
        buffer = int(self.settings.get("buffer") or 0)
        reserved = min(buffer, output) if output else buffer
        return max(0, model_limits["context"] - reserved)

    def preserve_recent_tokens(self, budget: int) -> int:
        explicit = self.settings.get("preserve_recent_tokens")
        if explicit is not None:
            return int(explicit)
        return max(2_000, min(15_000, int(budget * 0.25)))

    def prune(self, history: History) -> None:
        """Clear the outputs of old completed tool calls to free context (opencode prune).

        Protects the most recent two user turns, existing summaries, and the
        ``use_skill``/``task`` tools; only clears once there are at least
        ``PRUNE_PROTECT`` tokens of older tool output and the freed amount
        exceeds ``PRUNE_MINIMUM``.
        """
        if not self.settings.get("prune", True):
            return
        turns = 0
        total = 0
        pruned = 0
        to_clear: list[int] = []
        messages = history.messages
        for idx in range(len(messages) - 1, -1, -1):
            m = messages[idx]
            role = m.get("role")
            if role == "user":
                turns += 1
            if turns < 2:
                continue
            if history.is_summary(m):
                break
            if role != "tool":
                continue
            if m.get("name") in PROTECTED_TOOL_OUTPUTS:
                continue
            content = str(m.get("content") or "")
            if not content or content == PRUNED_OUTPUT:
                continue
            size = history.estimate_messages_tokens([m])
            total += size
            if total <= PRUNE_PROTECT:
                continue
            pruned += size
            to_clear.append(idx)
        if pruned > PRUNE_MINIMUM:
            # Replace the message objects instead of mutating them in place:
            # Session.messages keeps a completed-turn snapshot that shares
            # these dictionaries, so an in-place edit would rewrite history.
            for idx in to_clear:
                messages[idx] = {**messages[idx], "content": PRUNED_OUTPUT}

    async def condense(
        self, history: History, summarizer: Summarizer | None, extra: int = 0
    ) -> None:
        if not self.settings.get("auto", True) or not history.over_budget(extra):
            return
        self.prune(history)
        if not history.over_budget(extra):
            return
        tail_start = history.select_tail_start(
            self.preserve_recent_tokens(history.max_tokens), self.settings.get("tail_turns")
        )
        if tail_start is not None and summarizer is not None:
            messages = history.messages[:tail_start]
            # The prior summary is passed separately as ``previous_summary``;
            # keep it out of the transcript so it is not duplicated.
            if messages and history.is_summary(messages[0]):
                messages = messages[1:]
            summary = await summarizer(messages, history.summary)
            if summary and history.condense_from(summary, tail_start):
                return
        history.trim()

    @staticmethod
    def check_budget(history: History, extra: int = 0) -> None:
        if history.over_budget(extra):
            used = history.estimate_tokens() + extra
            raise BudgetExceededError(
                f"上下文超出预算：约 {used} tokens > 上限 {history.max_tokens} tokens。"
                "请缩短输入、减少工具数量或开启新会话。"
            )

    async def prepare(
        self, history: History, summarizer: Summarizer | None, extra: int = 0
    ) -> None:
        await self.condense(history, summarizer, extra)
        self.check_budget(history, extra)
