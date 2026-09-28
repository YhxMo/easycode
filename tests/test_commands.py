"""Phase 6 tests: CommandRegistry, Markdown template commands, $ARGUMENTS expansion, dynamic /help."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.extensions.commands import Command, CommandRegistry
from easycode.extensions.skills import Skill, SkillRegistry
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.store import SessionStore
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
    # A skill registered as a command carries its skill context, not the raw
    # body: the two loading paths must name the same resource directory.
    assert cmd.body == "# Skill: lint\n\nSkill body"


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

    r = client.get("/api/commands")
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


def test_commands_menu_lists_every_registered_scope(tmp_path, monkeypatch):
    """The `/` menu aggregates all registered projects and the personal directory.

    The same name can come from more than one place, so each entry keeps its own
    id and names where it came from instead of collapsing to a single winner.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    for proj in (proj_a, proj_b):
        cmds = proj / ".easycode" / "commands"
        cmds.mkdir(parents=True)
        (cmds / "shared.md").write_text(
            f"---\ndescription: shared in {proj.name}\n---\nBody of {proj.name}\n",
            encoding="utf-8",
        )
    personal = tmp_path / ".easycode" / "commands"
    personal.mkdir(parents=True)
    (personal / "personal.md").write_text(
        "---\ndescription: personal one\n---\nPersonal body\n", encoding="utf-8"
    )
    # The default workspace is its own directory, so its commands are listed
    # alongside the personal ones without being the same file.
    default = tmp_path / "default"
    (default / ".easycode" / "commands").mkdir(parents=True)
    (default / ".easycode" / "commands" / "default-cmd.md").write_text(
        "---\ndescription: in the default workspace\n---\nDefault body\n", encoding="utf-8"
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = default

    def factory(alias: str, **kwargs):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj_a)

    store = SessionStore(cfg, tmp_path, factory)
    store.create(root=str(proj_a))
    store.create(root=str(proj_b))

    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    listing = client.get("/api/commands").json()["commands"]
    shared = [c for c in listing if c["name"] == "shared"]
    assert len(shared) == 2
    assert {c["source_label"] for c in shared} == {"a", "b"}
    assert len({c["id"] for c in shared}) == 2
    assert all(c["source"] == "project" for c in shared)
    assert all(c["id"] for c in listing)
    assert [c["source_label"] for c in listing if c["name"] == "personal"] == ["个人"]
    # The default workspace is in the menu even though no session uses it yet.
    assert [c["source_label"] for c in listing if c["name"] == "default-cmd"] == ["default"]


def test_picked_command_expands_from_the_project_it_came_from(tmp_path, monkeypatch):
    """A menu selection runs the chosen project's command in this session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    cmds = proj_b / ".easycode" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "only-b.md").write_text(
        "---\ndescription: only in b\n---\nTemplate $ARGUMENTS\n", encoding="utf-8"
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = proj_a
    cfg.workspace_projects = [{"root": str(proj_b), "secondary": []}]

    def factory(alias: str, **kwargs):
        root = kwargs.get("root") or proj_a
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=Path(root),
        )

    store = SessionStore(cfg, proj_a, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    # Created after the app: create_app restores sessions from disk, which would
    # replace this in-memory one (and its provider) with a copy.
    sess = store.create(root=str(proj_a))

    entry = next(c for c in client.get("/api/commands").json()["commands"] if c["name"] == "only-b")
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "/only-b hello", "session_id": sess.id, "command_id": entry["id"]},
        )
        assert r.status_code == 200, r.text
    # The body came from the other project; the turn ran in this session.
    sent = sess.agent.provider.calls[0][-1]["content"]
    assert sent == "Template hello"

    # A menu entry is resolved against the commands registered now, and the
    # text has to still be that command.
    assert (
        client.post(
            "/api/chat",
            json={"message": "/only-b x", "session_id": sess.id, "command_id": "project:/gone:only-b"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/chat",
            json={"message": "/other x", "session_id": sess.id, "command_id": entry["id"]},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/chat",
            json={"message": "not a command", "session_id": sess.id, "command_id": entry["id"]},
        ).status_code
        == 400
    )


def test_typed_command_still_resolves_in_the_session_scope(tmp_path, monkeypatch):
    """Typing `/name` by hand keeps the current project's and personal rules.

    The menu offers every project's commands, so a name that exists twice is
    picked from the menu; without a pick, the session's own scope decides.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    other = tmp_path / "other"
    for folder, body in ((proj, "Proj body"), (other, "Other body")):
        cmds = folder / ".easycode" / "commands"
        cmds.mkdir(parents=True)
        (cmds / "run.md").write_text(
            f"---\ndescription: run here\n---\n{body} $ARGUMENTS\n", encoding="utf-8"
        )
    cfg = Config.load(start=tmp_path)
    cfg.root = proj

    def factory(alias: str, **kwargs):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=Path(kwargs.get("root") or proj),
        )

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    # Created after the app: create_app restores sessions from disk, which would
    # replace this in-memory one (and its provider) with a copy.
    sess = store.create(root=str(proj))
    other_sess = store.create(root=str(other))

    with client:
        r = client.post("/api/chat", json={"message": "/run now", "session_id": sess.id})
        assert r.status_code == 200, r.text
    assert sess.agent.provider.calls[0][-1]["content"] == "Proj body now"

    with client:
        r = client.post("/api/chat", json={"message": "/run now", "session_id": other_sess.id})
        assert r.status_code == 200, r.text
    assert other_sess.agent.provider.calls[0][-1]["content"] == "Other body now"


def test_secondary_change_refreshes_agent_skills_and_commands(tmp_path, monkeypatch):
    """Changing a session's secondary roots re-discovers skills/agents and
    rebuilds the system prompt, so a typed command and the agent share one scope."""
    monkeypatch.setenv("HOME", str(tmp_path))

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.store import SessionStore
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
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=proj,
        )

    store = SessionStore(cfg, proj, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    with client:
        sess = store.create(root=str(proj), secondary_roots=[])
        # The secondary directory's command is not part of this session yet.
        before = client.post("/api/chat", json={"message": "/sec-cmd x", "session_id": sess.id})
        assert before.status_code == 400
        assert sess.agent.skills is None or "demo" not in sess.agent.skills.names()

        r = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [str(sec)], "session_id": sess.id},
        )
        assert r.status_code == 200, r.text

        after = client.post("/api/chat", json={"message": "/sec-cmd x", "session_id": sess.id})
        assert after.status_code == 200, after.text
        assert "demo" in sess.agent.skills.names()
        assert "demo skill" in sess.agent.history.payload()[0]["content"]
    assert sess.agent.provider.calls[-1][-1]["content"] == "Do x"


def test_secondary_roots_explicit_empty_vs_inherited(tmp_path, monkeypatch):
    """A draft chat: omitted secondary_roots inherits the project binding, an
    explicit empty list creates a session with no secondary roots."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from pathlib import Path

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.store import SessionStore
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


def test_discovery_keeps_priority_and_file_order(tmp_path):
    """`discover_templates` reads through the same helpers the menu uses.

    One project template and one personal template share a name, a reserved
    name is refused, and the project's copy is what resolves.
    """
    user_cmds = tmp_path / "user_cmds"
    proj_cmds = tmp_path / "proj" / ".easycode" / "commands"
    user_cmds.mkdir(parents=True)
    proj_cmds.mkdir(parents=True)

    def template(path, description, body):
        path.write_text(
            f"---\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
        )

    template(user_cmds / "shared.md", "personal", "personal body")
    template(proj_cmds / "shared.md", "project", "project body")
    template(user_cmds / "help.md", "reserved name", "must not register")
    template(proj_cmds / "only-project.md", "project only", "project body")

    reg = CommandRegistry()
    reg.register(Command(name="help", description="Help", kind="builtin"))
    reg.discover_templates([tmp_path / "proj"], user_dir=user_cmds)

    assert reg.get("shared").description == "project"
    assert reg.get("help").kind == "builtin"
    # Filenames name the commands when the frontmatter does not.
    assert reg.get("only-project").expand("") == "project body"


def test_a_skill_command_keeps_the_name_over_a_template(tmp_path):
    """A skill registered before discovery still wins the name."""
    user_cmds = tmp_path / "user_cmds"
    user_cmds.mkdir(parents=True)
    (user_cmds / "lint.md").write_text(
        "---\ndescription: Template lint\n---\nTemplate body\n", encoding="utf-8"
    )

    reg = CommandRegistry()
    reg.add_skill_commands(
        SkillRegistry({"lint": Skill(name="lint", description="Skill lint", body="Skill body")})
    )
    reg.discover_templates([], user_dir=user_cmds)

    cmd = reg.get("lint")
    assert cmd is not None
    assert cmd.kind == "skill"
