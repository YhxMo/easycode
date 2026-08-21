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


def test_select_tail_start_keeps_recent_turns() -> None:
    h = History()
    for i in range(4):
        h.add_user("user " + "x" * 50)
        h.add_assistant("assistant " + "y" * 10)
    assert h.select_tail_start(10_000) is None  # everything fits
    start = h.select_tail_start(40)  # ~ two recent turns fit
    assert start == 4
    assert h.messages[start]["role"] == "user"


def test_select_tail_start_tail_turns_cap() -> None:
    h = History()
    for i in range(5):
        h.add_user("u " + "x" * 10)
        h.add_assistant("a")
    # only the last turn is considered with tail_turns=1, so the cut is at 8
    assert h.select_tail_start(10_000, tail_turns=1) == 8


def test_condense_from_tracks_summary() -> None:
    h = History()
    for i in range(4):
        h.add_user(f"u{i}")
        h.add_assistant(f"a{i}")
    assert h.condense_from("SUMMARY", 4) is True
    assert h.summary == "SUMMARY"
    assert h.messages[0]["role"] == "system"
    assert len(h.messages) == 5  # summary + 4 tail
    assert h.messages[-1]["content"] == "a3"


def test_trim_preserves_summary_message() -> None:
    h = History(max_tokens=10)
    h.add_user("x" * 1000)
    h.add_user("m1")
    h.add_user("m2")
    h.condense_from("S", 1)  # summary + [m1, m2]
    h.trim()
    assert h.messages[0]["role"] == "system"
    assert History.is_summary(h.messages[0])