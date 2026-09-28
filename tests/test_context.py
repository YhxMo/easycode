"""History budget gate unit tests."""

from __future__ import annotations

import random

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


def test_character_statistics_preserve_content_and_tool_arguments() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "ab中🙂",
            "tool_calls": [
                {"function": {"arguments": "xyz中"}},
                {"function": {"arguments": None}},
            ],
        },
        {"role": "user", "content": None},
        {"role": "user", "content": ["a"]},
    ]
    # 4 content chars + 4 argument chars + 5 in str(["a"]); 3 non-ASCII.
    assert History._char_stats(messages) == (13, 3)
    assert History.estimate_messages_tokens(messages) == 6
    history = History(messages=messages)
    assert history.estimate_chars() == 13
    assert History.estimate_text_tokens("ab中🙂") == 3


def test_over_budget_char_backstop() -> None:
    h = History()
    h.max_tokens = 1_000_000
    h.max_chars = 100
    h.add({"role": "user", "content": "x" * 200})
    assert h.over_budget() is True


def test_dense_code_token_count_beats_chars_over_four() -> None:
    """10B: dense ASCII code tokenizes far denser than the 4-chars/token
    heuristic; the exact token count must drive the gate (the character
    ceiling is a memory backstop, far away in this case)."""
    code = "x+=1;" * 10_000
    h = History(max_tokens=15_000, max_chars=10_000_000)
    h.add({"role": "user", "content": code})
    assert h.estimate_tokens() > h.estimate_messages_tokens(h.messages)
    assert h.over_budget() is True


def test_large_tool_output_counts_toward_token_budget() -> None:
    """Tool outputs are token-accounted like any other message content."""
    output = "\n".join(f"item-{i}: value {i * 7}" for i in range(4000))
    h = History(max_tokens=10_000, max_chars=10_000_000)
    h.add_user("q")
    h.add({"role": "assistant", "content": "", "tool_calls": [_tc("a")]})
    h.add_tool("a", "f", output)
    assert h.over_budget() is True


def test_char_backstop_is_independent_of_model_budget() -> None:
    """The character ceiling stays an explicit memory guard: a history that
    fits a huge token budget still trips once it exceeds ``max_chars``."""
    h = History(max_tokens=10_000_000, max_chars=50_000)
    h.add({"role": "user", "content": "x" * 60_000})
    assert h.estimate_tokens() < h.max_tokens
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


def test_message_estimate_matches_the_joined_text_estimate() -> None:
    """The list-based estimate must equal estimating the concatenated text.

    Rounding the ASCII 4-chars/token allowance once per message would inflate
    a long transcript by a token per message, so the rounding has to happen
    once for the whole list — which is what joining the text does.
    """
    messages = [
        {"role": "user", "content": "ab中🙂"},
        {"role": "assistant", "content": "ab中🙂"},
        {"role": "user", "content": "a"},
    ]
    joined = "".join(str(m.get("content") or "") for m in messages)
    assert History.estimate_messages_tokens(messages) == 6
    assert History.estimate_messages_tokens(messages) == History.estimate_text_tokens(joined)


def _split_turn_start_reference(
    history: History, start: int, end: int, remaining: int
) -> int | None:
    """The previous implementation: estimate each suffix from scratch.

    Kept as the equivalence oracle for the accumulating version — linear
    scaling is only acceptable if it picks the same cut every time.
    """
    if end - start <= 1:
        return None
    for s in range(start + 1, end):
        if history.messages[s].get("role") == "tool":
            continue
        if history.estimate_messages_tokens(history.messages[s:end]) <= remaining:
            return s
    return None


def test_tail_split_hand_computed():
    #  "abcd" is 1 token, each CJK char is 1 token.
    messages = [
        {"role": "user", "content": "abcd"},
        {"role": "assistant", "content": "中"},
        {"role": "user", "content": "abcd"},
    ]
    h = History(messages=[dict(m) for m in messages])
    # suffix [2:] = 1 token; [1:] = 2; budget 1 takes the latest cut.
    assert h._split_turn_start(0, 3, 1) == 2
    assert h._split_turn_start(0, 3, 2) == 1
    # Nothing fits: no cut at all.
    assert h._split_turn_start(0, 3, 0) is None
    # A single candidate is never split (the caller keeps the whole turn).
    assert h._split_turn_start(0, 1, 100) is None


def test_tail_split_skips_a_cut_that_would_orphan_a_tool_result():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]
    h = History(messages=[dict(m) for m in messages])

    # A cut at 3 would orphan the tool result at 2, so the earliest valid cut
    # that fits wins instead.
    assert h._split_turn_start(0, 4, 1_000) == 1
    # Too small for anything but the orphaned cut: no cut at all.
    assert h._split_turn_start(0, 4, 0) is None


def test_tail_split_matches_the_previous_implementation():
    """Randomized equivalence against the re-estimating version (fixed seed)."""
    random.seed(20260928)
    styles = ("ascii", "cjk", "mixed", "tool")
    for _ in range(500):
        count = random.randrange(2, 30)
        style = random.choice(styles)
        messages = []
        for i in range(count):
            if style == "tool":
                role, content = ("tool" if i % 3 == 0 else "assistant"), "y" * 3
            elif style == "cjk":
                role, content = ("user" if i % 2 else "assistant"), "中" * 10
            elif style == "mixed":
                role, content = "assistant", "ab" * i + "中"
            else:
                role, content = ("user" if i % 2 else "assistant"), "x" * 40
            messages.append({"role": role, "content": content})
        h = History(messages=[dict(m) for m in messages])
        for remaining in (0, 1, 10, 100, 1_000, 10**9):
            assert h._split_turn_start(0, count, remaining) == _split_turn_start_reference(
                h, 0, count, remaining
            ), (style, count, remaining, messages)
