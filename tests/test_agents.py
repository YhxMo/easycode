"""Phase 6 tests: AgentSpec parsing, discovery, project-level override, task tool."""

from __future__ import annotations

import json

import pytest

from easycode.agent.loop import Agent
from easycode.extensions.frontmatter import FrontmatterError, parse_frontmatter, parse_spec
from easycode.extensions.subagents import AgentRegistry
from easycode.tools import build_registry
from tests.conftest import FakeProvider
from tests.helpers_history import assert_valid_tool_protocol


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
    from easycode.extensions.subagents import AgentSpec

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


@pytest.mark.asyncio
async def test_subagent_cannot_exceed_parent_tool_cap(tmp_path):
    """A capped parent's delegate must not gain tools the parent lacks."""
    from easycode.extensions.subagents import AgentSpec

    reg = AgentRegistry({"writer": AgentSpec(name="writer", description="writes")})
    target = tmp_path / "blocked.txt"
    sub_script = [
        {"tool_calls": [("s1", "write_file", {"path": str(target), "content": "nope"})]},
        {"text": "done"},
    ]
    subs: list[Agent] = []

    def factory(_model: str) -> Agent:
        # Mirrors make_agent: the factory hands back its own config-level set.
        sub = Agent(
            provider=FakeProvider(script=sub_script),
            registry=build_registry(8000),
            root=tmp_path,
            enabled_tools={"read_file", "write_file"},
        )
        subs.append(sub)
        return sub

    script = [
        {"tool_calls": [("t1", "task", {"agent": "writer", "prompt": "write it"})], "text": ""},
        {"text": "finished"},
    ]
    approved: list[str] = []

    async def approval(tc, _reason, _key):
        approved.append(tc.name)
        return True

    parent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
        subagent_factory=factory,
        enabled_tools={"read_file", "task"},
        approval_handler=approval,
    )

    events = [ev async for ev in parent.respond("delegate")]
    result = json.loads(next(e.tool_result for e in events if e.kind == "tool_result"))

    assert result["status"] == "ok"
    sub_tools = [m for m in subs[0].history.payload() if m["role"] == "tool"]
    assert any('"rejected": true' in str(m.get("content")) for m in sub_tools)
    assert not target.exists()
    assert approved == [], "a capped tool must be rejected before any approval"
    assert_valid_tool_protocol(subs[0].history.payload())
    assert_valid_tool_protocol(parent.history.payload())


@pytest.mark.asyncio
async def test_subagent_cannot_delegate_further(tmp_path):
    """Sub-agents get neither task nor parallel_tasks (no recursive delegation)."""
    from easycode.extensions.subagents import AgentSpec

    reg = AgentRegistry({"writer": AgentSpec(name="writer", description="writes")})
    sub_script = [
        {"tool_calls": [("s1", "task", {"agent": "writer", "prompt": "write"})], "text": ""},
        {"text": "done"},
    ]
    subs: list[Agent] = []

    def factory(_model: str) -> Agent:
        sub = Agent(
            provider=FakeProvider(script=sub_script),
            registry=build_registry(8000),
            root=tmp_path,
            agents=reg,
            enabled_tools={"read_file", "task", "parallel_tasks"},
        )
        subs.append(sub)
        return sub

    script = [
        {"tool_calls": [("t1", "task", {"agent": "writer", "prompt": "go"})], "text": ""},
        {"text": "finished"},
    ]
    parent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
        subagent_factory=factory,
        enabled_tools={"read_file", "task"},
    )

    events = [ev async for ev in parent.respond("delegate")]
    result = json.loads(next(e.tool_result for e in events if e.kind == "tool_result"))

    assert result["status"] == "ok"
    sub_tools = [m for m in subs[0].history.payload() if m["role"] == "tool"]
    assert any('"rejected": true' in str(m.get("content")) for m in sub_tools)
    assert subs[0].provider.calls, "the child must answer without delegating"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args,needle",
    [
        ("parallel_tasks", {"tasks": []}, "tasks"),
        (
            "parallel_tasks",
            {"tasks": [{"name": f"t{i}", "prompt": "x"} for i in range(7)]},
            "tasks",
        ),
        ("parallel_tasks", {"tasks": ["not-an-object"]}, "tasks.0"),
        (
            "parallel_tasks",
            {"tasks": [{"name": "t", "prompt": "x"}], "max_parallel": 0},
            "max_parallel",
        ),
        (
            "parallel_tasks",
            {"tasks": [{"name": "t", "prompt": "x"}], "max_parallel": "many"},
            "max_parallel",
        ),
        ("task", {"agent": "writer", "prompt": "   "}, "prompt"),
        ("task", {"agent": "writer"}, "prompt"),
        ("use_skill", {"name": ""}, "name"),
    ],
)
async def test_builtin_tool_argument_errors_are_results(tmp_path, name, args, needle):
    """Runtime validation mirrors the advertised schema: bad arguments become a
    tool result (no subagent starts) and the conversation continues paired."""
    from easycode.extensions.skills import Skill, SkillRegistry
    from easycode.extensions.subagents import AgentSpec
    from tests.helpers_history import assert_valid_tool_protocol

    made: list[str] = []
    reg = AgentRegistry({"writer": AgentSpec(name="writer", description="writes")})
    skills = SkillRegistry({"s": Skill(name="s", description="d", body="b")})

    def factory(model: str) -> Agent:
        made.append(model)
        return Agent(
            provider=FakeProvider(script=[{"text": "unused"}]),
            registry=build_registry(8000),
            root=tmp_path,
        )

    script = [
        {"tool_calls": [("c1", name, args)], "text": ""},
        {"text": "recovered"},
    ]
    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        agents=reg,
        skills=skills,
        subagent_factory=factory,
    )

    events = [ev async for ev in agent.respond("go")]
    result = json.loads(next(e.tool_result for e in events if e.kind == "tool_result"))

    assert result["status"] == "error"
    assert "invalid" in result["message"]
    assert needle in result["message"]
    assert made == [], "invalid arguments must not start a subagent"
    assert "".join(e.content or "" for e in events if e.kind == "text") == "recovered"
    assert_valid_tool_protocol(agent.history.payload())


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
    from easycode.agent.factory import make_agent
    from easycode.config import Config
    from easycode.models.credentials import Credential, save_credential

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
    from easycode.extensions.subagents import AgentSpec

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


def test_make_agent_carries_the_configured_iteration_limit_to_subagents(tmp_path, monkeypatch):
    """CLI, Web and delegated subagents are built by the same assembly path, so
    a ceiling configured once governs every one of them."""
    from easycode.agent.builtin_tools import make_subagent
    from easycode.agent.factory import make_agent
    from easycode.config import Config
    from easycode.extensions.subagents import AgentRegistry, AgentSpec
    from easycode.models.credentials import Credential, save_credential

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "easycode.config.json").write_text(
        json.dumps(
            {
                "default_model": "only",
                "max_tool_iterations": 7,
                "models": {"only": {"model": "only-model", "key_id": "only-key"}},
            }
        ),
        encoding="utf-8",
    )
    creds_path = tmp_path / "home" / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="only-key", api_key="sk-only"), path=creds_path)

    cfg = Config.load(start=proj)
    parent = make_agent(cfg, "only", proj)
    assert parent.max_tool_iterations == 7

    parent.agents = AgentRegistry(
        {"writer": AgentSpec(name="writer", description="writes", permission="ask")}
    )
    child = make_subagent(parent, parent.agents.get("writer"))
    assert child.max_tool_iterations == 7


def test_subtasks_follow_the_agent_into_its_new_workspace(tmp_path):
    """Delegated work inherits the agent's *current* workspace.

    The subagent factory is built with the agent, but a session that moved
    afterwards must not hand its subtasks the directories it left behind.
    """
    from easycode.agent.factory import make_agent
    from easycode.config import Config
    from easycode.models.credentials import Credential, save_credential

    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    sec_a = tmp_path / "sec_a"
    sec_b = tmp_path / "sec_b"
    for d in (proj_a, proj_b, sec_a, sec_b):
        d.mkdir()
    (tmp_path / "easycode.config.json").write_text(
        json.dumps(
            {
                "default_model": "fake",
                "models": {
                    "fake": {
                        "model": "openai/fake",
                        "api_format": "openai_compatible",
                        "key_id": "k",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    save_credential(Credential(key_id="k", api_key="sk-test"))
    cfg = Config.load(start=tmp_path)
    cfg.root = proj_a

    agent = make_agent(cfg, "fake", proj_a, [sec_a])
    sub = agent.subagent_factory("fake")
    assert sub.root == proj_a
    assert [str(p) for p in sub.secondary_roots] == [str(sec_a)]

    # The session moves: everything it delegates from now on runs where it does.
    agent.root = proj_b
    agent.secondary_roots = [sec_b]
    moved = agent.subagent_factory("fake")
    assert moved.root == proj_b
    assert [str(p) for p in moved.secondary_roots] == [str(sec_b)]
