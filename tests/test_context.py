"""History budget gate unit tests."""

from __future__ import annotations

import pytest

from easycode.agent.context import History
from tests.helpers_history import assert_valid_tool_protocol, validated_payload


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


# ----------------------------------------------------------------


def test_over_budget_large_system_trips() -> None:
    """the system prompt counts toward the budget. A big system body must
    trip over-budget on its own, not be hidden behind an empty conversation."""
    h = History(max_tokens=1000)
    h.set_system("S" * 20_000)  # ~5000 tokens heuristic, system only
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget() is True


def test_over_budget_tool_schemas_trips() -> None:
    """the tool schemas sent on every call count toward the budget via the
    ``extra`` term. Same conversation is in-budget without schemas but trips once
    a large schema is accounted for."""
    h = History(max_tokens=1000)
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget() is False
    huge_schema_tokens = 5000
    assert h.over_budget(huge_schema_tokens) is True


def test_over_budget_cheap_gate_counts_system_and_extra() -> None:
    """a large system / schema must defeat the early-exit cheap gate
    (``messages + system + extra <= 0.7 * max``) so the exact count is consulted."""
    h = History(max_tokens=1000)
    h.set_system("S" * 6_000)
    h.add({"role": "user", "content": "hi"})
    assert h.over_budget(2000) is True


# ----------------------------------------------------------------


def test_trim_drops_whole_turn_not_single_message() -> None:
    """Trimming must never split a tool_call from its result."""
    h = History(max_chars=4)
    h.add_user("u1")
    h.add({"role": "assistant", "content": "", "tool_calls": [_tc("a")]})
    h.add_tool("a", "f", "r1")
    h.add_user("u2")
    h.add_assistant("a2")
    h.trim()
    # The oldest complete turn (u1 + assistant-with-tool_call + tool) is dropped
    # wholesale; the payload never holds an orphan tool result.
    assert_no_orphan_tools(h.messages)
    assert h.messages[0]["role"] == "user"
    assert h.messages[0]["content"] == "u2"
    assert len(h.messages) <= 3
    assert not any(m.get("role") == "tool" for m in h.messages)


def test_trim_never_orphans_tool_results() -> None:
    """hard character/token trim also drops whole turns."""
    h = History(max_tokens=1_000_000, max_chars=50)
    h.add_user("u1")
    h.add({"role": "assistant", "content": "", "tool_calls": [_tc("b")]})
    h.add_tool("b", "f", "r1" + "y" * 500)
    h.add_user("u2")
    h.add_assistant("tiny")
    h.trim()
    assert_no_orphan_tools(h.messages)


def test_select_tail_start_split_does_not_orphan_tool() -> None:
    """a large assistant ``tool_calls`` + small result must cause the
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
    """a raw tool index passed to ``condense_from`` is snapped back so the
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


#
# Strict one-to-one tool protocol: every assistant tool_call id must be matched
# by exactly one tool result, consumed in declaration order, with no duplicate /
# missing / old-turn / pre-declaration result. System messages are exempt.


def _tool_result(tid: str, content: str = "r") -> dict:
    return {"role": "tool", "tool_call_id": tid, "name": "f", "content": content}


def test_protocol_accepts_simple_paired_turn() -> None:
    messages = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
        _tool_result("a", "ok"),
        {"role": "assistant", "content": "done"},
    ]
    assert_valid_tool_protocol(messages)
    # the payload (with a system message prepended) must also validate
    assert_valid_tool_protocol(
        [{"role": "system", "content": "sys"}, *messages]
    )
    assert validated_payload(messages) is messages


def test_protocol_accepts_multi_call_batch_in_order() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b")]},
        _tool_result("a", "1"),
        _tool_result("b", "2"),
        {"role": "assistant", "content": "both done"},
    ]
    assert_valid_tool_protocol(messages)


def test_protocol_system_messages_exempt() -> None:
    messages = [
        {"role": "system", "content": "Previous conversation summary (older messages were condensed):\n- x"},
        {"role": "system", "content": "[skill: check]\nbody"},
        {"role": "user", "content": "hi"},
    ]
    assert_valid_tool_protocol(messages)


def test_protocol_rejects_missing_result_for_second_call() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b")]},
        _tool_result("a", "1"),
    ]
    with pytest.raises(AssertionError):
        assert_valid_tool_protocol(messages)


def test_protocol_rejects_duplicate_result() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
        _tool_result("a", "1"),
        _tool_result("a", "2"),
    ]
    with pytest.raises(AssertionError):
        assert_valid_tool_protocol(messages)


def test_protocol_rejects_old_turn_id_reuse() -> None:
    """A result that re-uses an already-fulfilled id from an older turn must be
    rejected rather than silently matched to the new declaration."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
        _tool_result("a", "1"),
        {"role": "assistant", "content": "", "tool_calls": [_tc("b")]},
        _tool_result("a", "old-turn id"),
    ]
    with pytest.raises(AssertionError):
        assert_valid_tool_protocol(messages)


def test_protocol_rejects_result_before_declaring_assistant() -> None:
    """Interleaving: a result that appears before the assistant that declared it
    must be rejected (no pending declaration)."""
    messages = [
        _tool_result("a", "early"),
        {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
    ]
    with pytest.raises(AssertionError):
        assert_valid_tool_protocol(messages)


def test_protocol_rejects_out_of_order_results_within_batch() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tc("a"), _tc("b")]},
        _tool_result("b", "2"),
        _tool_result("a", "1"),
    ]
    with pytest.raises(AssertionError):
        assert_valid_tool_protocol(messages)


def test_protocol_survives_trim_of_adjacent_batches() -> None:
    """Whole-turn trimming that cuts across adjacent multi-call batches
    must keep the one-to-one pairing intact (never an unpaired call/result)."""
    h = History(max_chars=16)
    for i in range(3):
        h.add_user(f"u{i}")
        h.add({"role": "assistant", "content": "", "tool_calls": [_tc(f"a{i}"), _tc(f"b{i}")]})
        h.add_tool(f"a{i}", "f", f"ra{i}")
        h.add_tool(f"b{i}", "f", f"rb{i}")
    h.trim()
    assert len(h.messages) < 12
    assert_valid_tool_protocol(h.messages)
    assert_valid_tool_protocol(h.payload())


def test_protocol_survives_condense_cut_between_adjacent_batches() -> None:
    """A token-selected tail cut (condense_from) that lands between two
    adjacent multi-call batches must leave a fully paired tail, with the
    summary system message exempt."""
    h = History()
    for i in range(5):
        h.add_user(f"u{i}")
        h.add({"role": "assistant", "content": "", "tool_calls": [_tc(f"a{i}"), _tc(f"b{i}")]})
        h.add_tool(f"a{i}", "f", f"ra{i}")
        h.add_tool(f"b{i}", "f", f"rb{i}")
    start = h.select_tail_start(10_000, tail_turns=2)
    assert start is not None
    assert h.condense_from("SUMMARY", start) is True
    assert_valid_tool_protocol(h.messages)
    assert History.is_summary(h.messages[0])  # summary exempt, but present
