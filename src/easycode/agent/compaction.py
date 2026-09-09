"""Context-compaction subsystem.

Owns context-budget invariants: the compaction gate before every provider
call, prune thresholds, rolling-summary merge, and schema-token accounting;
constants, the :class:`Compactor`, and :class:`BudgetExceededError`.

The :class:`Compactor` holds a back-reference to the owning :class:`~easycode.agent.loop.Agent`
so it reads *live* ``history`` / ``compaction`` / ``summarizer`` values. This is
deliberate: tests and callers mutate ``agent.summarizer`` and replace
``agent.history`` *after* construction, so any snapshot taken at ``__init__``
would go stale.
"""

from __future__ import annotations

from typing import Any

from easycode.agent.summarizer import LLMSummarizer, Summarizer

#: Context-compaction defaults (aligned with opencode `compaction` config).
COMPACTION_DEFAULTS: dict[str, Any] = {
    "auto": True,
    "buffer": 20_000,
    "preserve_recent_tokens": None,
    "tail_turns": None,
    "prune": True,
    "summary_max_chars": 8_000,
}

#: clear old tool outputs once >PRUNE_PROTECT tokens accumulate (min PRUNE_MINIMUM freed)
PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
PRUNED_OUTPUT = "[Tool output cleared]"
PROTECTED_TOOL_OUTPUTS = {"use_skill", "task"}


class BudgetExceededError(Exception):
    """The completion payload exceeds the provider's context budget.

    Raised by ``Compactor._raise_if_over_budget`` *before* ``provider.stream``
    when compaction could not bring the payload back under budget (invariant
    #5: over-budget must fail explicitly instead of relying on the provider to
    reject it). The message carries the measured usage vs. the limit and a hint
    to shorten the conversation or disable auto-compaction.
    """


def _loop() -> Any:
    """Return the ``easycode.agent.loop`` module at call time.

    ``PRUNE_MINIMUM`` / ``PRUNE_PROTECT`` are imported (and re-exported) by
    ``loop``, and existing tests monkeypatch them on the ``loop`` module
    (``monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)``). Reading them through
    the ``loop`` namespace here keeps those tests green without a top-level
    circular import: by the time a :class:`Compactor` method runs, ``loop`` is
    fully loaded.
    """
    import easycode.agent.loop as _lp  # deferred: avoids top-level cycle

    return _lp


class Compactor:
    """Compacts ``agent.history`` when the context budget is exceeded.

    Only touches ``agent.history`` / ``agent.compaction`` (plus the read-only
    ``agent.summarizer`` / ``agent.model_limits`` / ``agent.max_context_tokens``
    and the ``agent._tool_schema_tokens()`` estimate). ``Agent`` keeps thin
    delegating methods so the loop's internal call sites and the existing test
    monkeypatch points stay intact.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    # -- live view of the owning agent ---------------------------------------

    @property
    def agent(self) -> Any:
        return self._agent

    @property
    def history(self) -> Any:
        return self._agent.history

    @property
    def compaction(self) -> dict[str, Any]:
        return self._agent.compaction

    @property
    def summarizer(self) -> Summarizer | None:
        return self._agent.summarizer

    @property
    def model_limits(self) -> dict[str, int] | None:
        return self._agent.model_limits

    @property
    def max_context_tokens(self) -> int:
        return self._agent.max_context_tokens

    def _tool_schema_tokens(self) -> int:
        return self._agent._tool_schema_tokens()

    # -- budget model ---------------------------------------------------------

    def _usable_tokens(self) -> int:
        """Usable context budget = model window − reserved output buffer.

        When the model's limits are unknown (fallback), the whole
        ``max_context_tokens`` is usable — there is no output figure to reserve.
        """
        limits = self.model_limits
        if not limits:
            return self.max_context_tokens
        context = limits["context"]
        output = limits["output"] or 0
        buffer = int(self.compaction.get("buffer") or 0)
        reserved = min(buffer, output) if output else buffer
        return max(0, context - reserved)

    def _preserve_recent_tokens(self) -> int:
        explicit = self.compaction.get("preserve_recent_tokens")
        if explicit is not None:
            return int(explicit)
        return max(2_000, min(15_000, int(self._usable_tokens() * 0.25)))

    async def _summarize(self, messages: list[dict]) -> str | None:
        if self.summarizer is None:
            return None
        if isinstance(self.summarizer, LLMSummarizer):
            return await self.summarizer.summarize(messages, previous_summary=self.history.summary)
        return await self.summarizer(messages)

    # -- prune -----------------------------------------------------------------

    def _prune_tool_outputs(self) -> None:
        """Clear the outputs of old completed tool calls to free context (opencode prune).

        Protects the most recent two user turns, existing summaries, and the
        ``use_skill``/``task`` tools; only clears once there are at least
        ``PRUNE_PROTECT`` tokens of older tool output and the freed amount
        exceeds ``PRUNE_MINIMUM``.
        """
        if not self.compaction.get("prune", True):
            return
        prune_protect = _loop().PRUNE_PROTECT
        prune_minimum = _loop().PRUNE_MINIMUM
        pruned_output = _loop().PRUNED_OUTPUT
        protected = _loop().PROTECTED_TOOL_OUTPUTS
        turns = 0
        total = 0
        pruned = 0
        to_clear: list[dict] = []
        for m in reversed(self.history.messages):
            role = m.get("role")
            if role == "user":
                turns += 1
            if turns < 2:
                continue
            if self.history.is_summary(m):
                break
            if role != "tool":
                continue
            if m.get("name") in protected:
                continue
            content = str(m.get("content") or "")
            if not content or content == pruned_output:
                continue
            size = self.history.estimate_messages_tokens([m])
            total += size
            if total <= prune_protect:
                continue
            pruned += size
            to_clear.append(m)
        if pruned > prune_minimum:
            for m in to_clear:
                m["content"] = pruned_output

    # -- condense ---------------------------------------------------------------

    async def _condense_if_over_budget(self) -> None:
        """Compact history when over budget (honors ``compaction.auto``).

        When ``auto`` is False the user opted out of automatic compaction, so
        nothing is summarized or trimmed here even when the budget is exceeded.
        ``extra`` is the token cost of the tool schemas sent on every call, so a
        large tool set also counts toward the budget.
        """
        if not self.compaction.get("auto", True):
            return
        extra = self._tool_schema_tokens()
        if not self.history.over_budget(extra):
            return
        self._prune_tool_outputs()
        if not self.history.over_budget(extra):
            return
        tail_start = self.history.select_tail_start(
            self._preserve_recent_tokens(), self.compaction.get("tail_turns")
        )
        if tail_start is None or self.summarizer is None:
            self.history.trim()
            return
        summary = await self._summarize(self.history.messages[:tail_start])
        if summary is None or not self.history.condense_from(summary, tail_start):
            self.history.trim()

    def _raise_if_over_budget(self) -> None:
        """Final budget gate (invariant #5): fail before the provider call.

        ``_condense_if_over_budget`` already tried to compact the history, so by
        the time we reach this point an over-budget payload can no longer be
        reduced — there is no tool boundary to cut, or compression is disabled
        (``compaction.auto=False``). Waiting for the provider to reject it would
        both waste a round-trip and silently depend on vendor behavior, so we
        raise instead.

        ``extra`` (the tool schemas sent on every call) is included so a bloated
        tool set also trips the gate.
        """
        extra = self._tool_schema_tokens()
        if not self.history.over_budget(extra):
            return
        used = self.history.estimate_tokens() + extra
        limit = self.history.max_tokens
        raise BudgetExceededError(
            f"上下文超出预算：约 {used} tokens > 上限 {limit} tokens，自动压缩无法降级。"
            "请缩短对话或关闭上下文压缩（compaction auto=false）后重试。"
        )


__all__ = [
    "COMPACTION_DEFAULTS",
    "PROTECTED_TOOL_OUTPUTS",
    "PRUNED_OUTPUT",
    "PRUNE_MINIMUM",
    "PRUNE_PROTECT",
    "BudgetExceededError",
    "Compactor",
]
