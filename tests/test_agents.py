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


def test_subagent_requires_factory(tmp_path):
    """DEC-L1: a directly constructed agent cannot delegate without a factory."""
    from easycode.agent.builtin_tools import make_subagent
    from easycode.agent.loop import Agent
    from easycode.models.litellm_provider import LiteLLMProvider
    from easycode.tools import build_registry

    parent = Agent(
        provider=LiteLLMProvider("demo", api_key="test-key"),
        registry=build_registry(8000),
        root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="subagent_factory"):
        make_subagent(parent)


def test_subagent_model_alias_resolved_with_own_credentials(tmp_path, monkeypatch):
    """A spec model alias resolves through the parent's factory: its own
    credential and a summarizer; a spec without model inherits the parent alias."""
    from easycode.agent.builtin_tools import make_subagent
    from easycode.agentfactory import make_agent
    from easycode.config import Config
    from easycode.credentials import Credential, save_credential

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    agents_dir = proj / ".easycode" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "writer.md").write_text(
        """---
description: Writes files
model: child
---
You write.
""",
        encoding="utf-8",
    )
    (agents_dir / "plain.md").write_text(
        """---
description: Inherits the parent model
---
You inherit.
""",
        encoding="utf-8",
    )
    (proj / "easycode.config.json").write_text(
        json.dumps(
            {
                "default_model": "parent",
                "models": {
                    "parent": {"model": "parent-model", "key_id": "parent-key"},
                    "child": {"model": "child-model", "key_id": "child-key"},
                },
            }
        ),
        encoding="utf-8",
    )
    creds_path = tmp_path / "home" / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="parent-key", api_key="sk-parent"), path=creds_path)
    save_credential(Credential(key_id="child-key", api_key="sk-child"), path=creds_path)

    cfg = Config.load(start=proj)
    parent = make_agent(cfg, "parent", proj)

    child = make_subagent(parent, parent.agents.get("writer"))
    assert child.provider.model == "child-model"
    assert child.provider.kwargs["api_key"] == "sk-child"
    assert child.model_alias == "child"
    assert child.summarizer is not None

    inherit = make_subagent(parent, parent.agents.get("plain"))
    assert inherit.provider.model == "parent-model"
    assert inherit.provider.kwargs["api_key"] == "sk-parent"


@pytest.mark.asyncio
async def test_task_tool_rejects_primary_agent(tmp_path):
    """DEC-C7: `mode: primary` agents cannot be delegated to."""
    from easycode.agents import AgentSpec

    reg = AgentRegistry({"main": AgentSpec(name="main", description="primary", mode="primary")})
    script = [
        {"tool_calls": [("t1", "task", {"agent": "main", "prompt": "do work"})], "text": ""},
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
    )

    events = [ev async for ev in agent.respond("run")]
    result = json.loads(next(e.tool_result for e in events if e.kind == "tool_result"))

    assert result["status"] == "error"
    assert "not delegatable" in result["message"]
