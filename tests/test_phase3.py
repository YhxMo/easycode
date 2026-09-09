"""Phase 3 tests: AGENTS.md rules, context compression, parallel sub-agents."""

from __future__ import annotations

import json
from pathlib import Path

from easycode.agent.context import History
from easycode.agent.loop import Agent
from easycode.agent.system import find_agents_rules
from easycode.tools import build_registry
from tests.conftest import FakeProvider


def make_agent(tmp_path: Path, script: list[dict] | None = None, **kw) -> Agent:
    return Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        **kw,
    )


# ---------- P3-2 AGENTS.md ----------


async def test_agents_md_loaded_into_system_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\nalways use tabs\n", encoding="utf-8")
    agent = make_agent(tmp_path, [{"text": "ok"}])
    payload = agent.history.payload()
    assert "Project rules" in payload[0]["content"]
    assert "always use tabs" in payload[0]["content"]


async def test_agents_md_found_upward(tmp_path):
    rules_dir = tmp_path / "pkg"
    rules_dir.mkdir()
    (tmp_path / "AGENTS.md").write_text("no emojis in code", encoding="utf-8")
    assert "no emojis in code" in find_agents_rules(rules_dir)


async def test_no_agents_md_no_rules_section(tmp_path):
    agent = make_agent(tmp_path, [{"text": "ok"}])
    assert "Project rules" not in agent.history.payload()[0]["content"]


# ---------- P3-3 context compression ----------


def test_history_condense_replaces_old_messages():
    h = History(max_chars=1_000_000, max_messages=100)
    for i in range(30):
        h.add_user(f"user {i}")
        h.add_assistant(f"answer {i}")
    ok = h.condense("EARLY SUMMARY", keep_recent=5)
    assert ok is True
    roles = [m["role"] for m in h.messages]
    assert roles[0] == "system" and "EARLY SUMMARY" in h.messages[0]["content"]
    assert len(h.messages) == 6  # summary + 5 recent (last pair)
    assert h.messages[-1]["content"] == "answer 29"


def test_history_condense_noop_when_few_messages():
    h = History()
    h.add_user("hi")
    h.add_assistant("yo")
    assert h.condense("S", keep_recent=5) is False


async def test_agent_condenses_when_over_budget(tmp_path):
    calls = []

    async def fake_summarizer(messages) -> str:
        calls.append(messages)
        return "COMPRESSED"

    agent = make_agent(tmp_path, [{"text": "final"}], summarizer=fake_summarizer)
    # tiny budgets force compression and keep only one recent turn verbatim
    agent.history.max_chars = 1_000
    agent.compaction["preserve_recent_tokens"] = 50
    for i in range(10):
        agent.history.add_user("line: " + "x" * 200)
        agent.history.add_assistant("reply: " + "y" * 200)
    events = [ev async for ev in agent.respond("wrap up")]
    assert any(ev.kind == "done" for ev in events)
    assert calls, "summarizer should have been invoked"
    payload = agent.history.payload()
    assert any("COMPRESSED" in str(m.get("content")) for m in payload)


# ---------- P3-1 parallel sub-agents ----------


async def test_parallel_tasks_runs_subagents(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    sub_1 = Agent(
        provider=FakeProvider(script=[{"text": "result-one"}]),
        registry=build_registry(8000),
        root=tmp_path,
        include_parallel_tool=False,
    )
    sub_2 = Agent(
        provider=FakeProvider(script=[{"text": "result-two"}]),
        registry=build_registry(8000),
        root=tmp_path,
        include_parallel_tool=False,
    )
    made = iter([sub_1, sub_2])

    def factory(model: str) -> Agent:
        return next(made)

    main = make_agent(
        tmp_path,
        [
            {
                "tool_calls": [
                    (
                        "pc1",
                        "parallel_tasks",
                        {
                            "tasks": [
                                {"name": "t1", "prompt": "do a"},
                                {"name": "t2", "prompt": "do b"},
                            ]
                        },
                    )
                ],
                "text": "",
            },
            {"text": "all done"},
        ],
        subagent_factory=factory,
    )
    events = [ev async for ev in main.respond("parallel please")]
    kinds = [e.kind for e in events]
    assert kinds.count("tool_start") == 1
    assert kinds.count("tool_result") == 1
    assert kinds[-1] == "done"

    result_ev = next(e for e in events if e.kind == "tool_result")
    data = json.loads(result_ev.tool_result)
    assert data["status"] == "ok"
    assert data["count"] == 2
    by_name = {r["name"]: r["result"] for r in data["results"]}
    assert by_name == {"t1": "result-one", "t2": "result-two"}

    # each sub-agent ran exactly once with its own fresh history
    assert len(sub_1.provider.calls) == 1
    assert len(sub_2.provider.calls) == 1


async def test_parallel_tasks_in_main_schema(tmp_path):
    agent = make_agent(tmp_path, [{"text": "x"}])
    names = [s["function"]["name"] for s in agent.tool_schemas()]
    assert "parallel_tasks" in names


async def test_parallel_tasks_disabled_optionally(tmp_path):
    agent = make_agent(tmp_path, [{"text": "x"}], include_parallel_tool=False)
    names = [s["function"]["name"] for s in agent.tool_schemas()]
    assert "parallel_tasks" not in names


async def test_parallel_tasks_subagent_error_reported(tmp_path):
    fails = Agent(
        provider=FakeProvider(script=[{"error": "boom"}]),
        registry=build_registry(8000),
        root=tmp_path,
        include_parallel_tool=False,
    )
    main = make_agent(
        tmp_path,
        [
            {
                "tool_calls": [
                    ("pc1", "parallel_tasks", {"tasks": [{"name": "t1", "prompt": "x"}]})
                ],
                "text": "",
            },
            {"text": "ok"},
        ],
        subagent_factory=lambda m: fails,
    )
    events = [ev async for ev in main.respond("go")]
    result_ev = next(e for e in events if e.kind == "tool_result")
    data = json.loads(result_ev.tool_result)
    assert data["results"][0]["name"] == "t1"
    assert "error" in data["results"][0]