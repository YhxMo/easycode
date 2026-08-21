"""Agentic-loop tests using FakeProvider (no network)."""

from __future__ import annotations

from pathlib import Path

from easycode.agent.loop import Agent, AgentEvent
from easycode.tools import build_registry
from tests.conftest import FakeProvider


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


async def test_max_iterations_guard(tmp_path):
    agent, _ = make_agent(tmp_path, [])
    agent.provider.script = [
        {"tool_calls": [("c", "glob", {"pattern": "*"})], "text": ""}
    ] * 15
    events = [ev async for ev in agent.respond("loop")]
    assert any(e.kind == "error" and e.error and "max tool iterations" in e.error for e in events)


async def test_concurrent_turns_isolated(tmp_path):
    """Two agents (sessions) running interleaved in the same loop."""
    a1, p1 = make_agent(tmp_path, [{"text": "A"}])
    a2, p2 = make_agent(tmp_path, [{"text": "B"}])
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


async def test_edit_file_model_view_strips_diff_review_keeps(tmp_path):
    """P7-1: same for edit_file — the auto-review diff stays complete."""
    import json as _json

    f = tmp_path / "e.py"
    f.write_text("def one():\n    return 1\n", encoding="utf-8")
    script = [
        {"tool_calls": [("c1", "edit_file", {"path": "e.py", "old_string": "return 1", "new_string": "return 42"})], "text": ""},
        {"text": "changed"},
    ]
    agent, provider = make_agent(tmp_path, script)
    agent.permission_mode = "auto-review"
    events = await collect(agent, "edit")

    history_tools = [m for m in agent.history.messages if m.get("role") == "tool"]
    assert history_tools
    assert "diff" not in history_tools[0]["content"]

    reviews = [e for e in events if e.kind == "review"]
    assert reviews
    changes = _json.loads(reviews[0].content)["changes"]
    assert "return 42" in changes[0]["diff"]