"""Phase 6 tests: AgentSpec parsing, discovery, project-level override, task tool."""

from __future__ import annotations

import json

import pytest

from easycode.agent.loop import Agent
from easycode.agents import AgentRegistry
from easycode.frontmatter import FrontmatterError, parse_frontmatter, parse_spec
from easycode.tools import build_registry
from tests.conftest import FakeProvider


def test_parse_frontmatter_valid():
    text = """---
name: planner
description: Plan architecture
model: test-model
tools:
  - read_file
  - grep
mode: subagent
temperature: 0.2
---

You are a planner.
"""
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "planner"
    assert meta["description"] == "Plan architecture"
    assert meta["model"] == "test-model"
    assert meta["tools"] == ["read_file", "grep"]
    assert meta["mode"] == "subagent"
    assert meta["temperature"] == 0.2
    assert body.strip() == "You are a planner."


def test_parse_spec_missing_description():
    text = """---
name: incomplete
---
No description here.
"""
    with pytest.raises(FrontmatterError, match="missing required"):
        parse_spec(text, default_name="incomplete", required=("description",))


def test_agent_discovery_and_override(tmp_path):
    user_dir = tmp_path / "user_agents"
    user_dir.mkdir(parents=True)
    proj_dir = tmp_path / "proj" / ".easycode" / "agents"
    proj_dir.mkdir(parents=True)

    # User agent
    (user_dir / "helper.md").write_text(
        """---
description: User helper
---
User prompt
""",
        encoding="utf-8",
    )

    # Project agent with same name to test override
    (proj_dir / "helper.md").write_text(
        """---
description: Project helper
tools: read_file, glob
---
Project prompt
""",
        encoding="utf-8",
    )

    # Project unique agent
    (proj_dir / "reviewer.md").write_text(
        """---
description: Code reviewer
mode: subagent
---
Review prompt
""",
        encoding="utf-8",
    )

    reg = AgentRegistry.discover([tmp_path / "proj"], user_dir=user_dir)
    assert sorted(reg.names()) == ["helper", "reviewer"]

    helper = reg.get("helper")
    assert helper is not None
    assert helper.description == "Project helper"
    assert helper.source == "project"
    assert helper.tools == ["read_file", "glob"]
    assert helper.system == "Project prompt"

    reviewer = reg.get("reviewer")
    assert reviewer is not None
    assert reviewer.description == "Code reviewer"
    assert reviewer.delegatable is True


@pytest.mark.asyncio
async def test_task_tool_delegation(tmp_path):
    user_dir = tmp_path / "agents"
    user_dir.mkdir()
    (user_dir / "coder.md").write_text(
        """---
description: Write code
model: deepseek-v4flash
---
You write code.
""",
        encoding="utf-8",
    )
    reg = AgentRegistry.discover([], user_dir=user_dir)

    subagent_called = []

    def fake_factory(model: str) -> Agent:
        sub = Agent(
            provider=FakeProvider(script=[{"text": "code generated"}]),
            registry=build_registry(8000),
            root=tmp_path,
        )
        subagent_called.append(model)
        return sub

    script = [
        {
            "tool_calls": [
                ("t1", "task", {"agent": "coder", "prompt": "generate fibonacci"})
            ],
            "text": "",
        },
        {"text": "Task finished."},
    ]

    main_agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
        subagent_factory=fake_factory,
    )

    events = [ev async for ev in main_agent.respond("please run coder")]
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert len(results) == 1
    data = json.loads(results[0])
    assert data["status"] == "ok"
    assert data["agent"] == "coder"
    assert data["result"] == "code generated"
    assert "deepseek-v4flash" in subagent_called


@pytest.mark.asyncio
async def test_task_tool_unknown_agent_error(tmp_path):
    from easycode.agents import AgentSpec

    reg = AgentRegistry({"known": AgentSpec(name="known", description="exists")})
    script = [
        {
            "tool_calls": [
                ("t1", "task", {"agent": "nonexistent", "prompt": "do work"})
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
    )

    events = [ev async for ev in agent.respond("run")]
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert len(results) == 1
    data = json.loads(results[0])
    assert data["status"] == "error"
    assert "unknown agent" in data["message"]


@pytest.mark.asyncio
async def test_subagent_tool_whitelist_enforced(tmp_path):
    """A spec's `tools` allow-list is enforced at execution, not just in schemas."""
    user_dir = tmp_path / "agents"
    user_dir.mkdir()
    (user_dir / "reader.md").write_text(
        """---
description: Read-only helper
tools: read_file
---
You only read files.
""",
        encoding="utf-8",
    )
    reg = AgentRegistry.discover([], user_dir=user_dir)

    target = tmp_path / "blocked.txt"
    sub_script = [
        {"tool_calls": [("s1", "write_file", {"path": str(target), "content": "nope"})]},
        {"text": "done"},
    ]
    subs: list[Agent] = []

    def fake_factory(_model: str) -> Agent:
        sub = Agent(
            provider=FakeProvider(script=sub_script),
            registry=build_registry(8000),
            root=tmp_path,
        )
        subs.append(sub)
        return sub

    script = [
        {"tool_calls": [("t1", "task", {"agent": "reader", "prompt": "write it"})]},
        {"text": "finished"},
    ]
    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
        subagent_factory=fake_factory,
    )

    events = [ev async for ev in agent.respond("delegate")]
    result = json.loads(next(e.tool_result for e in events if e.kind == "tool_result"))

    assert result["status"] == "ok"
    assert result["result"] == "done"
    assert not target.exists()
    sub_tools = [m for m in subs[0].history.payload() if m["role"] == "tool"]
    assert any('"rejected": true' in str(m.get("content")) for m in sub_tools)


def test_subagent_inherits_model_credentials(tmp_path):
    from easycode.agent.builtin_tools import make_subagent
    from easycode.agent.loop import Agent
    from easycode.models.litellm_provider import LiteLLMProvider
    from easycode.tools import build_registry

    provider = LiteLLMProvider("demo", api_key="test-key", api_base="http://localhost/v1")
    parent = Agent(provider=provider, registry=build_registry(8000), root=tmp_path)
    child = make_subagent(parent)
    assert child.provider.model == parent.provider.model
    assert child.provider.kwargs == parent.provider.kwargs
    assert child.history is not parent.history
