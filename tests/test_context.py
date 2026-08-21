"""History budget gate unit tests."""

from __future__ import annotations

from easycode.agent.context import History


def test_over_budget_cheap_gate_not_fooled_by_cjk() -> None:
    """A CJK-heavy transcript is over its token budget even when the cheap
    char-based gate would under-count it."""
    h = History()
    h.max_tokens = 32_000
    h.max_chars = 400_000
    for _ in range(2):
        h.add({"role": "user", "content": "一" * 20_000})
    assert h.estimate_tokens() > h.max_tokens
    assert h.over_budget() is True


def test_over_budget_small_history_is_false() -> None:
    h = History()
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget() is False


def test_over_budget_char_backstop() -> None:
    h = History()
    h.max_tokens = 1_000_000
    h.max_chars = 100
    h.add({"role": "user", "content": "x" * 200})
    assert h.over_budget() is True