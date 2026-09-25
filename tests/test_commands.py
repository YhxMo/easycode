"""Phase 6 tests: CommandRegistry, Markdown template commands, $ARGUMENTS expansion, dynamic /help."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.commands import Command, CommandRegistry
from easycode.config import Config
from easycode.skills import Skill, SkillRegistry
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider


def test_command_template_expansion():
    cmd = Command(
        name="review",
        description="Review code",
        kind="template",
        body="Please review file $1 with focus on $2. All arguments: $ARGUMENTS",
    )
    expanded = cmd.expand("src/main.py security")
    assert expanded == "Please review file src/main.py with focus on security. All arguments: src/main.py security"


def test_command_discovery_and_override(tmp_path):
    user_cmds = tmp_path / "user_cmds"
    user_cmds.mkdir(parents=True)
    proj_cmds = tmp_path / "proj" / ".easycode" / "commands"
    proj_cmds.mkdir(parents=True)

    (user_cmds / "btw.md").write_text(
        """---
description: User btw
---
User template $ARGUMENTS
""",
        encoding="utf-8",
    )

    (proj_cmds / "btw.md").write_text(
        """---
description: Project btw
argument-hint: "[question]"
---
Project template $ARGUMENTS
""",
        encoding="utf-8",
    )

    (proj_cmds / "test.md").write_text(
        """---
description: Run tests
---
Run tests for $1
""",
        encoding="utf-8",
    )

    reg = CommandRegistry()
    reg.discover_templates([tmp_path / "proj"], user_dir=user_cmds)
    assert sorted([c.name for c in reg.list()]) == ["btw", "test"]

    btw = reg.get("btw")
    assert btw is not None
    assert btw.description == "Project btw"
    assert btw.arg_hint == "[question]"
    assert btw.expand("hello") == "Project template hello"


def test_skills_as_commands_priority():
    reg = CommandRegistry()
    reg.register(Command(name="help", description="Help", kind="builtin"))

    # Template with name 'lint'
    reg.register(Command(name="lint", description="Template lint", kind="template", body="Template body"))

    # Skill with same name 'lint'
    skills = SkillRegistry({"lint": Skill(name="lint", description="Skill lint", body="Skill body")})
    reg.add_skill_commands(skills)

    cmd = reg.get("lint")
    assert cmd is not None
    assert cmd.kind == "skill"
    assert cmd.description == "Skill lint"
    assert cmd.body == "Skill body"


def test_web_commands_endpoint(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".easycode" / "commands").mkdir(parents=True)
    (proj / ".easycode" / "commands" / "custom.md").write_text(
        """---
description: Custom command
argument-hint: "[arg]"
---
Template $ARGUMENTS
""",
        encoding="utf-8",
    )

    cfg_file = proj / "easycode.config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    cfg = Config.load(start=proj)
    cfg.root = proj

    def factory(alias: str, **kwargs):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj)

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    r = client.post("/api/commands", json={})
    assert r.status_code == 200
    cmds = r.json()["commands"]
    names = [c["name"] for c in cmds]
    assert "custom" in names
    assert "run" not in names
    assert "undo" not in names
    assert "redo" not in names

    custom = next(c for c in cmds if c["name"] == "custom")
    assert custom["description"] == "Custom command"
    assert custom["argument_hint"] == "[arg]"
    assert custom["kind"] == "template"


def test_unknown_command_does_not_create_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()

    cfg = Config.load(start=proj)
    cfg.root = proj

    def factory(alias: str, **kwargs):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj)

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    with client:
        r = client.post("/api/chat", json={"message": "/nope"})
        assert r.status_code == 400
        assert client.get("/api/sessions").json() == []

    sessions_dir = tmp_path / ".easycode" / "sessions"
    assert not list(sessions_dir.glob("*.json"))


def test_web_chat_command_expansion(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".easycode" / "commands").mkdir(parents=True)
    (proj / ".easycode" / "commands" / "ask.md").write_text(
        """---
description: Ask template
---
Expanded prompt: $ARGUMENTS
""",
        encoding="utf-8",
    )

    cfg = Config.load(start=proj)
    cfg.root = proj

    provider = FakeProvider(script=[{"text": "reply"}])

    def factory(alias: str, **kwargs):
        return Agent(provider=provider, registry=build_registry(8000), root=proj)

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    with client:
        r = client.post("/api/chat", json={"message": "/ask how are you"})
        assert r.status_code == 200

    # Verify that provider received the expanded prompt
    assert len(provider.calls) == 1
    messages = provider.calls[0]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert user_msg["content"] == "Expanded prompt: how are you"


@pytest.mark.asyncio
async def test_handle_command_smoke_help_agents_model_list(tmp_path, monkeypatch):
    """The CLI builtins that had no coverage still run without raising."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from easycode import cli

    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = Config.load(start=proj)
    agent = Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj)
    commands = cli.build_commands(agent, [proj])

    assert await cli.handle_command("/help", cfg, agent, cfg.default_model, commands) is None
    assert await cli.handle_command("/agents", cfg, agent, cfg.default_model, commands) is None
    assert await cli.handle_command("/model", cfg, agent, cfg.default_model, commands) is None


def test_commands_endpoint_scoped_to_session(tmp_path, monkeypatch):
    """DEC-C8: /api/commands matches what chat expansion sees, with two
    explicit scopes (session id, or draft root/secondary) and no union fallback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    for proj, name in ((proj_a, "only-a"), (proj_b, "only-b")):
        cmds = proj / ".easycode" / "commands"
        cmds.mkdir(parents=True)
        (cmds / f"{name}.md").write_text(
            f"---\ndescription: {name}\n---\nTemplate $ARGUMENTS\n", encoding="utf-8"
        )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **kwargs):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj_a)

    store = SessionStore(cfg, tmp_path, factory)
    sess_a = store.create(root=str(proj_a))
    store.create(root=str(proj_b))
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    scoped = client.post("/api/commands", json={"session_id": sess_a.id}).json()["commands"]
    names = [c["name"] for c in scoped]
    assert "only-a" in names
    assert "only-b" not in names

    # A new-session draft is scoped to its own root, not a union of projects.
    draft = client.post("/api/commands", json={"root": str(proj_b)}).json()["commands"]
    draft_names = [c["name"] for c in draft]
    assert "only-b" in draft_names
    assert "only-a" not in draft_names

    # An unknown session id is a 404, never a silent global scope.
    assert client.post("/api/commands", json={"session_id": "nope"}).status_code == 404


def test_secondary_change_refreshes_agent_skills_and_commands(tmp_path, monkeypatch):
    """Changing a session's secondary roots re-discovers skills/agents and
    rebuilds the system prompt, so the menu and the agent share one scope."""
    monkeypatch.setenv("HOME", str(tmp_path))

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    proj = tmp_path / "proj"
    proj.mkdir()
    sec = tmp_path / "sec"
    skill_dir = sec / ".easycode" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: demo skill\n---\nBody\n", encoding="utf-8"
    )
    cmd_dir = sec / ".easycode" / "commands"
    cmd_dir.mkdir()
    (cmd_dir / "sec-cmd.md").write_text(
        "---\ndescription: sec cmd\n---\nDo $ARGUMENTS\n", encoding="utf-8"
    )

    cfg = Config.load(start=proj)
    cfg.root = proj

    def factory(alias: str, **kwargs):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj)

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    with client:
        sess = store.create(root=str(proj), secondary_roots=[])
        before = client.post("/api/commands", json={"session_id": sess.id}).json()["commands"]
        assert "sec-cmd" not in [c["name"] for c in before]
        assert sess.agent.skills is None or "demo" not in sess.agent.skills.names()

        r = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [str(sec)], "session_id": sess.id},
        )
        assert r.status_code == 200, r.text

        after = client.post("/api/commands", json={"session_id": sess.id}).json()["commands"]
        assert "sec-cmd" in [c["name"] for c in after]
        assert "demo" in sess.agent.skills.names()
        assert "demo skill" in sess.agent.history.payload()[0]["content"]


def test_secondary_roots_explicit_empty_vs_inherited(tmp_path, monkeypatch):
    """A draft chat: omitted secondary_roots inherits the project binding, an
    explicit empty list creates a session with no secondary roots."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from pathlib import Path

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    proj = tmp_path / "proj"
    proj.mkdir()
    sec = tmp_path / "sec"
    sec.mkdir()
    cfg = Config.load(start=proj)
    cfg.root = proj
    cfg.workspace_projects = [{"root": str(proj), "secondary": [str(sec)]}]

    def factory(alias: str, **kwargs):
        root = Path(kwargs.get("root") or proj)
        secondaries = [Path(p) for p in (kwargs.get("secondary_roots") or [])]
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=secondaries,
        )

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    with client:
        # Omitting the field inherits the project's secondary binding.
        client.post("/api/chat", json={"message": "inherit", "root": str(proj)})
        inherited = [s for s in store.list() if s.title == "inherit"][0]
        assert inherited.secondary_roots == [str(sec)]

        # An explicit empty list means "no secondary roots".
        client.post(
            "/api/chat",
            json={"message": "explicit", "root": str(proj), "secondary_roots": []},
        )
        explicit = [s for s in store.list() if s.title == "explicit"][0]
        assert explicit.secondary_roots == []
