"""History budget gate unit tests."""

from __future__ import annotations

from easycode.agent.context import History


def _tc(tid: str, args: str = "{}") -> dict:
    return {"id": tid, "type": "function", "function": {"name": "f", "arguments": args}}


def assert_no_orphan_tools(messages) -> None:
    """Every ``tool`` result must be preceded by an assistant that declared it."""
    declared: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            declared |= {tc["id"] for tc in m["tool_calls"]}
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in declared, f"orphan tool result: {m['tool_call_id']}"


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


# ---------------------------------------------------------------- item-10 (MS-8)


def test_over_budget_large_system_trips() -> None:
    """MS-8: the system prompt counts toward the budget. A big system body must
    trip over-budget on its own, not be hidden behind an empty conversation."""
    h = History(max_tokens=1000)
    h.set_system("S" * 20_000)  # ~5000 tokens heuristic, system only
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget() is True


def test_over_budget_tool_schemas_trips() -> None:
    """MS-8: the tool schemas sent on every call count toward the budget via the
    ``extra`` term. Same conversation is in-budget without schemas but trips once
    a large schema is accounted for."""
    h = History(max_tokens=1000)
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget() is False
    huge_schema_tokens = 5000
    assert h.over_budget(huge_schema_tokens) is True


def test_over_budget_cheap_gate_counts_system_and_extra() -> None:
    """MS-8: a large system / schema must defeat the early-exit cheap gate
    (``messages + system + extra <= 0.7 * max``) so the exact count is consulted."""
    h = History(max_tokens=1000)
    h.set_system("S" * 6_000)
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget(2000) is True


# ---------------------------------------------------------------- item-04 (MS-1+MS-2)


def test_add_trims_by_whole_turn_not_single_message() -> None:
    """MS-1: popping past ``max_messages`` must never split a tool_call from
    its result. Old code popped one message at a time (pop(0)), which could
    leave an orphaned ``tool`` result behind (leaving ``[tool, u2, a2]``)."""
    h = History(max_messages=3)
    h.add_user("u1")
    h.add({"role": "assistant", "content": "", "tool_calls": [_tc("a")]})
    h.add_tool("a", "f", "r1")
    h.add_user("u2")
    h.add_assistant("a2")
    # The oldest complete turn (u1 + assistant-with-tool_call + tool) is dropped
    # wholesale; the payload never holds an orphan tool result.
    assert_no_orphan_tools(h.messages)
    assert h.messages[0]["role"] == "user"
    assert h.messages[0]["content"] == "u2"
    assert len(h.messages) <= 3
    assert not any(m.get("role") == "tool" for m in h.messages)


def test_trim_never_orphans_tool_results() -> None:
    """MS-1: hard character/token trim also drops whole turns."""
    h = History(max_tokens=1_000_000, max_chars=50)
    h.add_user("u1")
    h.add({"role": "assistant", "content": "", "tool_calls": [_tc("b")]})
    h.add_tool("b", "f", "r1" + "y" * 500)
    h.add_user("u2")
    h.add_assistant("tiny")
    h.trim()
    assert_no_orphan_tools(h.messages)


def test_select_tail_start_split_does_not_orphan_tool() -> None:
    """MS-2: a large assistant ``tool_calls`` + small result must cause the
    token split to land on the assistant (not the ``tool``), so condensing
    never leaves an orphaned tool result."""
    h = History()
    h.add_user("u0")
    h.add({"role": "assistant", "content": "big", "tool_calls": [_tc("x", "A" * 800)]})
    h.add_tool("x", "f", "small")
    h.add_user("u1")
    h.add_assistant("b")
    h.add_user("u2")
    h.add_assistant("c")

    start = h.select_tail_start(120)  # small budget → forces a split mid-turn0
    assert start is not None, "expected a split index for a small budget"
    # never begin the kept tail with a tool result (that would orphan it)
    assert h.messages[start]["role"] != "tool"

    assert h.condense_from("SUMMARY", start) is True
    assert_no_orphan_tools(h.messages)


def test_condense_from_coalesces_tool_boundary() -> None:
    """MS-2: a raw tool index passed to ``condense_from`` is snapped back so the
    kept tail never starts with an orphaned tool result."""
    h = History()
    h.add_user("u0")
    h.add({"role": "assistant", "content": "", "tool_calls": [_tc("c")]})
    h.add_tool("c", "f", "r0")
    h.add_user("u1")
    h.add_assistant("a1")
    # tail_start=2 points straight at the ``tool`` message → must snap back to the
    # assistant (index 1) so the tool result stays paired.
    assert h.condense_from("S", 2) is True
    assert_no_orphan_tools(h.messages)
    assert h.messages[1]["role"] == "assistant"
    assert h.messages[1].get("tool_calls")
    assert h.messages[2]["role"] == "tool"
