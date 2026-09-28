"""Agentic-loop tests using FakeProvider (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycode.agent.events import AgentEvent
from easycode.agent.loop import Agent
from easycode.models.base import StreamEvent, ToolCall
from easycode.tools import build_registry
from tests.conftest import FakeProvider
from tests.helpers_history import ValidatingFakeProvider, assert_valid_tool_protocol


async def collect(agent: Agent, user_input: str) -> list[AgentEvent]:
    return [ev async for ev in agent.respond(user_input)]


def make_agent(tmp_path: Path, script: list[dict] | None) -> tuple[Agent, FakeProvider]:
    provider = FakeProvider(script=script)
    agent = Agent(
        provider=provider,
        registry=build_registry(8000),
        root=tmp_path,
    )
    return agent, provider


async def test_simple_answer(tmp_path):
    agent, provider = make_agent(tmp_path, [{"text": "hello world"}])
    events = await collect(agent, "hi")
    text = "".join(e.content or "" for e in events if e.kind == "text")
    assert text == "hello world"
    assert provider.calls[0][-1]["role"] == "user"
    assert agent.history.messages[-1]["role"] == "assistant"
    assert agent.history.messages[-1]["content"] == "hello world"


async def test_history_keeps_reader_friendly_text(tmp_path):
    """Assistant history must be the raw joined text - token pieces must not
    be joined with newlines (previous bug rendered history one token per line)."""
    script = [
        {
            "tool_calls": [("c1", "glob", {"pattern": "*.py"})],
            "text": "",
        },
        {"text": "found some files"},
    ]
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    agent, _ = make_agent(tmp_path, script)
    await collect(agent, "list")
    assert agent.history.messages[-1]["role"] == "assistant"
    assert agent.history.messages[-1]["content"] == "found some files"
    assert "\n" not in agent.history.messages[-1]["content"]


async def test_tool_call_then_answer(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# note\n", encoding="utf-8")
    script = [
        {
            "tool_calls": [("call_1", "glob", {"pattern": "*.py"})],
            "text": "",
        },
        {"text": "found: a.py"},
    ]
    agent, provider = make_agent(tmp_path, script)
    events = await collect(agent, "list python files")
    final = "".join(e.content or "" for e in events if e.kind == "text")
    assert final == "found: a.py"
    assert len(provider.calls) == 2
    roles = [m["role"] for m in provider.calls[1]]
    assert "tool" in roles
    tool_msg = next(m for m in provider.calls[1] if m["role"] == "tool")
    assert tool_msg["name"] == "glob"
    assert "a.py" in tool_msg["content"]


async def test_real_tool_execution_in_loop(tmp_path):
    (tmp_path / "x.py").write_text("value = 42\n", encoding="utf-8")
    script = [
        {
            "tool_calls": [("c1", "grep", {"pattern": "value"})],
            "text": "",
        },
        {"text": "done"},
    ]
    agent, _ = make_agent(tmp_path, script)
    events = await collect(agent, "search")
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert results and "value = 42" in results[0]
    kinds = [e.kind for e in events]
    assert "tool_start" in kinds and "done" in kinds


async def test_provider_error_reported(tmp_path):
    agent, _ = make_agent(tmp_path, [{"error": "boom"}])
    events = await collect(agent, "hi")
    errs = [e for e in events if e.kind == "error"]
    assert errs and "boom" in errs[0].error


# ----------------------------------------------------------------


async def test_large_tool_schema_counts_toward_budget(tmp_path, monkeypatch):
    """a large tool schema (sent on every completion) counts toward the
    context budget, so compaction fires even though the message history is tiny."""
    agent, _ = make_agent(tmp_path, [])
    agent.history.max_tokens = 1000
    agent.history.max_chars = 10_000_000
    big = {
        "type": "function",
        "function": {
            "name": "huge",
            "description": "x" * 20_000,
            "parameters": {"type": "object"},
        },
    }
    monkeypatch.setattr(agent, "tool_schemas", lambda: [big])

    assert agent._tool_schema_tokens(agent.tool_schemas()) > 0

    called: list[str] = []
    agent.summarizer = fake_summarizer_that_marks(called)
    monkeypatch.setattr(agent.history, "trim", lambda: called.append("trim"))
    await agent.compactor.condense(agent.history, agent.summarizer, agent._tool_schema_tokens(agent.tool_schemas()))

    assert called, "a large tool schema must count toward the budget and trip compaction"


# ----------------------------------------------------------------


async def test_compaction_auto_false_skips_condense(tmp_path, monkeypatch):
    """`compaction.auto=False` must suppress automatic compaction even
    when the history is well over budget — neither summarize nor trim fires."""
    agent, _ = make_agent(tmp_path, [])
    agent.compaction["auto"] = False
    agent.history.max_tokens = 10
    agent.history.max_chars = 10
    agent.history.add_user("x" * 1000)
    assert agent.history.over_budget() is True

    called: list[str] = []
    agent.summarizer = fake_summarizer_that_marks(called)
    monkeypatch.setattr(agent.history, "trim", lambda: called.append("trim"))
    before = list(agent.history.messages)

    await agent.compactor.condense(agent.history, agent.summarizer, agent._tool_schema_tokens(agent.tool_schemas()))

    assert called == []  # no summarize, no trim
    assert agent.history.messages == before  # history untouched


def fake_summarizer_that_marks(called: list[str]):
    async def _summarize(messages, previous_summary=None):
        called.append("summarize")
        return "S"
    return _summarize


async def test_compaction_auto_true_still_condenses(tmp_path, monkeypatch):
    """default `auto=True` keeps compacting when over budget
    (compaction must fire through summarize or trim)."""
    agent, _ = make_agent(tmp_path, [])
    agent.compaction["auto"] = True
    agent.history.max_tokens = 10
    agent.history.max_chars = 10
    agent.history.add_user("x" * 1000)

    called: list[str] = []
    agent.summarizer = fake_summarizer_that_marks(called)
    monkeypatch.setattr(agent.history, "trim", lambda: called.append("trim"))
    await agent.compactor.condense(agent.history, agent.summarizer, agent._tool_schema_tokens(agent.tool_schemas()))

    assert called, "expected compaction to fire when auto=True and over budget"


# ----------------------------------------------------------------


async def test_summary_failure_falls_back_without_injecting_degrade_text(tmp_path):
    """when the summarizer returns None (LLM failure) the loop must NOT
    replace original messages with a fabricated summary note; it falls back to
    the conservative trim path instead."""
    agent, _ = make_agent(tmp_path, [])
    agent.history.max_tokens = 8000
    agent.history.max_chars = 10_000_000
    for i in range(12):
        agent.history.add_user(f"u{i}" + "x" * 3000)
        agent.history.add_assistant("a" + "y" * 1000)
    assert agent.history.over_budget() is True

    log: list[str] = []

    async def failing_summarize(messages, previous_summary=None):
        log.append("summarize")
        return  # simulate provider failure surfaced as None

    agent.summarizer = failing_summarize
    await agent.compactor.condense(agent.history, agent.summarizer, agent._tool_schema_tokens(agent.tool_schemas()))

    assert "summarize" in log  # compaction attempted
    # no fabricated "(summary unavailable...)" note, and no injected summary
    for m in agent.history.messages:
        assert "summary unavailable" not in str(m.get("content") or "")
    assert agent.history.summary is None


async def test_model_can_finish_after_more_than_twelve_tool_rounds(tmp_path):
    script = [
        {"tool_calls": [(f"c{i}", "glob", {"pattern": "*"})]}
        for i in range(15)
    ] + [{"text": "finished"}]
    agent, provider = make_agent(tmp_path, script)

    events = await collect(agent, "loop")

    assert len(provider.calls) == 16
    assert sum(event.kind == "tool_result" for event in events) == 15
    assert [event.content for event in events if event.kind == "text"] == list("finished")
    assert not any(event.kind == "error" for event in events)
    assert events[-1].kind == "done"


async def test_explicit_iteration_limit_ends_the_turn_with_a_coded_error(tmp_path):
    """A configured ceiling stops the turn itself — it is never reported as a
    finished task, and the work already done stays in history."""
    script = [{"tool_calls": [(f"c{i}", "glob", {"pattern": "*"})]} for i in range(5)]
    script.append({"text": "never reached"})
    agent, provider = make_agent(tmp_path, script)
    agent.max_tool_iterations = 3

    events = await collect(agent, "loop")

    assert len(provider.calls) == 3
    errors = [e for e in events if e.kind == "error"]
    assert [e.code for e in errors] == ["tool_iteration_limit"]
    assert "max_tool_iterations=3" in (errors[0].error or "")
    assert events[-1].kind == "done"
    assert not any(e.kind == "text" for e in events)
    # the ceiling cancels nothing: every executed call keeps its result.
    assert sum(e.kind == "tool_result" for e in events) == 3
    assert_valid_tool_protocol(agent.history.messages)


async def test_approved_tool_result_tells_the_model_it_was_approved(tmp_path):
    """`in_allowed: false` means "outside the workspace", not "no approval": the
    model-visible result must carry the approval fact separately."""
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("external\n", encoding="utf-8")
    inside = root / "inside.txt"
    inside.write_text("internal\n", encoding="utf-8")
    script = [
        {"tool_calls": [("c1", "read_file", {"path": str(target)})]},
        {"tool_calls": [("c2", "read_file", {"path": str(inside)})]},
        {"text": "done"},
    ]
    agent, _ = make_agent(root, script)
    # An explicit rule, so the prompt does not depend on how the platform
    # classifies a path that happens to live under the OS temp dir (which is
    # where pytest puts tmp_path).
    agent.permission_rules = {"read_file": {f"{outside}/*": "ask"}}
    asked: list[str] = []

    async def approve(tc: ToolCall, reason: str, identity: str) -> bool:
        asked.append(tc.name)
        return True

    agent.approval_handler = approve
    await collect(agent, "read both")

    assert asked == ["read_file"]
    tool_messages = [m for m in agent.history.messages if m["role"] == "tool"]
    approved = json.loads(tool_messages[0]["content"])
    assert approved["approved_by_user"] is True
    assert approved["status"] == "ok" and "external" in approved["content"]
    # An in-workspace call nobody approved must not claim an approval.
    assert "approved_by_user" not in json.loads(tool_messages[1]["content"])


async def test_protected_write_is_rejected_without_offering_an_approval(tmp_path):
    """A hard-protected target can never be written, so the turn must not ask
    the user to approve it — the decision would be unenforceable."""
    (tmp_path / ".easycode").mkdir()
    target = tmp_path / ".easycode" / "blocked.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(target), "content": "x"})]},
        {"text": "stopped"},
    ]
    agent, _ = make_agent(tmp_path, script)
    asked: list[str] = []

    async def approve(tc: ToolCall, reason: str, identity: str) -> bool:
        asked.append(tc.name)
        return True

    agent.approval_handler = approve
    events = await collect(agent, "write it")

    assert asked == []
    result = json.loads(next(e.tool_result for e in events if e.kind == "tool_result"))
    assert result["status"] == "error"
    assert result["rejected"] is True
    assert "受保护路径" in result["reason"]
    assert not target.exists()


async def test_concurrent_turns_isolated(tmp_path):
    """Two agents (sessions) running interleaved in the same loop."""
    a1, _p1 = make_agent(tmp_path, [{"text": "A"}])
    a2, _p2 = make_agent(tmp_path, [{"text": "B"}])
    t1 = collect(a1, "q1")
    t2 = collect(a2, "q2")
    e1, e2 = await t1, await t2
    assert "".join(e.content or "" for e in e1) == "A"
    assert "".join(e.content or "" for e in e2) == "B"
    assert a1.history.messages[-1]["content"] == "A"
    assert a2.history.messages[-1]["content"] == "B"


async def test_write_file_model_view_strips_diff(tmp_path):
    """P7-1: history/model payload must not re-feed the diff, but the UI
    tool_result event keeps it."""
    script = [
        {"tool_calls": [("c1", "write_file", {"path": "a.txt", "content": "line1\nline2\n"})], "text": ""},
        {"text": "done"},
    ]
    agent, provider = make_agent(tmp_path, script)
    events = await collect(agent, "write")

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "line1\nline2\n"
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert results and "diff" in results[0]

    history_tools = [m for m in agent.history.messages if m.get("role") == "tool"]
    assert history_tools
    assert "diff" not in history_tools[0]["content"]
    assert '"status": "ok"' in history_tools[0]["content"]

    payload_tools = [m for m in provider.calls[1] if m["role"] == "tool"]
    assert payload_tools
    assert "diff" not in payload_tools[0]["content"]


async def test_auto_review_ignores_reads_and_dry_runs(tmp_path):
    """Auto-review collects only real changes: no reads, no dry-run previews."""
    import json as _json

    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    script = [
        {
            "tool_calls": [
                ("c1", "read_file", {"path": "f.txt"}),
                (
                    "c2",
                    "edit_file",
                    {
                        "path": "f.txt",
                        "old_string": "old",
                        "new_string": "new",
                        "dry_run": True,
                    },
                ),
                ("c3", "write_file", {"path": "f.txt", "content": "real change\n"}),
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    agent, _provider = make_agent(tmp_path, script)
    agent.permission_mode = "auto-review"
    events = await collect(agent, "change it")

    reviews = [e for e in events if e.kind == "review"]
    assert len(reviews) == 1
    changes = _json.loads(reviews[0].content)["changes"]
    assert len(changes) == 1
    assert changes[0]["tool"] == "write_file"
    assert changes[0]["path"] == "f.txt"
    assert "real change" in changes[0]["diff"]


async def test_edit_file_model_view_strips_diff_review_keeps(tmp_path):
    """P7-1: same for edit_file — the auto-review diff stays complete."""
    import json as _json

    f = tmp_path / "e.py"
    f.write_text("def one():\n    return 1\n", encoding="utf-8")
    script = [
        {"tool_calls": [("c1", "edit_file", {"path": "e.py", "old_string": "return 1", "new_string": "return 42"})], "text": ""},
        {"text": "changed"},
    ]
    agent, _provider = make_agent(tmp_path, script)
    agent.permission_mode = "auto-review"
    events = await collect(agent, "edit")

    history_tools = [m for m in agent.history.messages if m.get("role") == "tool"]
    assert history_tools
    assert "diff" not in history_tools[0]["content"]

    reviews = [e for e in events if e.kind == "review"]
    assert reviews
    changes = _json.loads(reviews[0].content)["changes"]
    assert "return 42" in changes[0]["diff"]


# ----------------------------------------------------------------


class _RaiseAfterToolProvider(FakeProvider):
    """Yields a ``tool_calls`` batch, then raises mid-stream (provider bug)."""

    async def stream(self, messages, tools=None):
        self.calls.append(list(messages))
        yield StreamEvent(kind="tool_calls", tool_calls=[ToolCall(id="t1", name="glob", arguments={"pattern": "*.py"})])
        yield StreamEvent(kind="text", content="full")
        raise RuntimeError("provider blew up mid-turn")


async def test_provider_exception_keeps_partial_text(tmp_path):
    provider = _RaiseAfterToolProvider()
    agent = Agent(provider=provider, registry=build_registry(8000), root=tmp_path)
    events = []
    with pytest.raises(RuntimeError, match="provider blew up"):
        async for event in agent.respond("hello"):
            events.append(event)
    assert agent.history.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "full"},
    ]
    assert any(event.kind == "error" for event in events)
    assert_valid_tool_protocol(agent.history.messages)


# ---------------------------------------------- tool protocol one-to-one check
# Validate every payload the loop hands to the provider at the FakeProvider
# exit; each recorded call must satisfy the one-to-one tool protocol.


async def test_loop_payloads_are_valid_one_to_one(tmp_path):
    """A full tool round-trip must hand the provider payloads in which
    every assistant tool_call id is matched by exactly one in-order result."""
    (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    script = [
        {
            "tool_calls": [
                ("c1", "glob", {"pattern": "*.py"}),
                ("c2", "grep", {"pattern": "def f"}),
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    provider = ValidatingFakeProvider(script=script)
    agent = Agent(provider=provider, registry=build_registry(8000), root=tmp_path)
    events = await collect(agent, "find and inspect")

    assert len(provider.calls) == 2  # first call: [system,user]; second: with tools
    # every payload was validated at the provider exit already (no raise), and
    # the retained history is itself a fully-paired sequence.
    assert_valid_tool_protocol(agent.history.messages)
    final = "".join(e.content or "" for e in events if e.kind == "text")
    assert final == "done"


async def test_validating_provider_rejects_missing_result_at_exit(tmp_path):
    """The validating provider refuses a malformed payload (a declared
    call with no matching result) at the FakeProvider exit boundary."""
    provider = ValidatingFakeProvider(script=[])
    bad = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
    ]
    with pytest.raises(AssertionError):
        [ev async for ev in provider.stream(bad)]


async def test_validating_provider_rejects_duplicate_result_at_exit(tmp_path):
    """The validating provider refuses a payload with a duplicated
    result at the FakeProvider exit boundary."""
    provider = ValidatingFakeProvider(script=[])
    bad = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "a", "name": "f", "content": "1"},
        {"role": "tool", "tool_call_id": "a", "name": "f", "content": "2"},
    ]
    with pytest.raises(AssertionError):
        [ev async for ev in provider.stream(bad)]
