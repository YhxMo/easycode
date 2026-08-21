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