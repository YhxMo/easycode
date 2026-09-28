"""Skills API: listing, import scope, session refresh and the send path."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import easycode.web.routes.skills as routes_skills
from easycode.config import Config
from easycode.web.main import create_app


def make_skill(root: Path, name: str, description: str = "Test skill", body: str = "Do the thing"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )
    return directory


def build(tmp_path):
    """An app with a default project, a second project and a secondary root."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.store import SessionStore
    from tests.conftest import FakeProvider

    default = tmp_path / "default"
    other = tmp_path / "other"
    attached = tmp_path / "attached"
    for directory in (default, other, attached):
        directory.mkdir()
    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8"
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = default
    cfg.workspace_projects = [
        {"root": str(other), "secondary": []},
        {"root": str(default), "secondary": [str(attached)]},
    ]

    def factory(alias: str = "fake-a", **kw):
        root = Path(kw["root"]).resolve() if kw.get("root") else default
        secondary = [Path(p) for p in kw.get("secondary_roots") or []]
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=secondary,
            # The system prompt lists skills; these tests read them directly.
            skills=None,
        )

    store = SessionStore(cfg, default, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    return app, cfg, store, {"default": default, "other": other, "attached": attached}


def test_lists_installed_skills_with_their_scope_and_effective_flag(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    # The personal copy is shadowed by the project's: both are listed, and the
    # shadowed one says so instead of disappearing.
    make_skill(Path.home() / ".easycode" / "skills", "shared", "personal copy")
    make_skill(roots["default"] / ".easycode" / "skills", "shared", "project copy")
    make_skill(roots["default"] / ".easycode" / "skills", "only-here")
    client = TestClient(app)

    body = client.get("/api/skills").json()

    assert body["root"] == str(roots["default"])
    assert body["enabled"] is True
    assert body["install_roots"] == {
        "personal": str(Path.home() / ".easycode" / "skills"),
        "project": str(roots["default"] / ".easycode" / "skills"),
    }
    rows = {(row["name"], row["scope"]): row for row in body["skills"]}
    assert set(rows) == {("shared", "personal"), ("shared", "project"), ("only-here", "project")}
    assert rows[("shared", "personal")]["effective"] is False
    assert rows[("shared", "project")]["effective"] is True
    assert rows[("only-here", "project")]["directory"] == str(
        roots["default"] / ".easycode" / "skills" / "only-here"
    )
    assert rows[("shared", "project")]["path"].endswith("SKILL.md")
    assert body["errors"] == []


def test_broken_packages_are_reported_not_hidden(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    skills = roots["default"] / ".easycode" / "skills"
    make_skill(skills, "fine")
    (skills / "broken").mkdir(parents=True)
    (skills / "broken" / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    (skills / "loose-file.txt").write_text("x", encoding="utf-8")
    client = TestClient(app)

    body = client.get("/api/skills").json()

    assert [row["name"] for row in body["skills"]] == ["fine"]
    assert len(body["errors"]) == 2
    assert any("broken" in message for message in body["errors"])
    assert any("loose-file.txt" in message for message in body["errors"])


def test_secondary_directory_skills_belong_to_the_project(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    make_skill(roots["attached"] / ".easycode" / "skills", "from-attached")
    client = TestClient(app)

    body = client.get("/api/skills").json()

    row = next(r for r in body["skills"] if r["name"] == "from-attached")
    assert row["scope"] == "project"
    assert row["directory"] == str(roots["attached"] / ".easycode" / "skills" / "from-attached")
    # A secondary directory is somewhere the session reads, not a second place
    # to install: the install root stays the project's own.
    assert body["install_roots"]["project"] == str(
        roots["default"] / ".easycode" / "skills"
    )


def test_an_unregistered_project_is_refused(tmp_path):
    app, _cfg, _store, _roots = build(tmp_path)
    client = TestClient(app)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert client.get("/api/skills", params={"root": str(outside)}).status_code == 422
    assert (
        client.post(
            "/api/skills/import",
            json={"source_path": str(outside), "root": str(outside)},
        ).status_code
        == 422
    )


def test_a_disabled_config_still_allows_management(tmp_path):
    app, cfg, _store, roots = build(tmp_path)
    cfg.skills_enabled = False
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "late")

    assert client.get("/api/skills").json()["enabled"] is False

    r = client.post(
        "/api/skills/import",
        json={"source_path": str(source), "scope": "project"},
    )
    assert r.status_code == 201
    # Disabling skill *calls* is not a reason to refuse installing one: the
    # panel says the calls are off and the user decides what to do about it.
    assert (roots["default"] / ".easycode" / "skills" / "late" / "SKILL.md").is_file()


def test_import_publishes_and_refreshes_an_open_session(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    sess = store.create(root=str(roots["default"]))
    assert sess.agent.skills is None or "pack" not in sess.agent.skills.names()
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "pack", body="Packaged instructions")

    r = client.post(
        "/api/skills/import",
        json={"source_path": str(source), "scope": "project"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imported"]["name"] == "pack"
    assert body["imported"]["scope"] == "project"
    assert body["imported"]["effective"] is True
    assert body["root"] == str(roots["default"])
    assert body["warnings"] == []
    # The open session knows the skill without waiting for another turn.
    assert "pack" in sess.agent.skills.names()


def test_personal_import_reaches_every_session(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    other = store.create(root=str(roots["other"]))
    default = store.create(root=str(roots["default"]))
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "everywhere")

    r = client.post(
        "/api/skills/import", json={"source_path": str(source), "scope": "personal"}
    )

    assert r.status_code == 201, r.text
    assert r.json()["imported"]["scope"] == "personal"
    assert (Path.home() / ".easycode" / "skills" / "everywhere" / "SKILL.md").is_file()
    assert "everywhere" in other.agent.skills.names()
    assert "everywhere" in default.agent.skills.names()


def test_project_import_reaches_a_session_that_uses_it_as_a_secondary(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    # A session in the other project that merely reads this one's directory.
    sess = store.create(root=str(roots["other"]), secondary_roots=[str(roots["default"])])
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "shared-pack")

    r = client.post(
        "/api/skills/import",
        json={"source_path": str(source), "scope": "project", "root": str(roots["default"])},
    )

    assert r.status_code == 201, r.text
    assert "shared-pack" in sess.agent.skills.names()


def test_project_import_leaves_other_projects_alone(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    other = store.create(root=str(roots["other"]))
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "local-only")

    client.post("/api/skills/import", json={"source_path": str(source), "scope": "project"})

    assert other.agent.skills is None or "local-only" not in other.agent.skills.names()


def test_a_duplicate_import_is_a_conflict(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "twice")

    assert client.post("/api/skills/import", json={"source_path": str(source)}).status_code == 201
    again = client.post("/api/skills/import", json={"source_path": str(source)})

    assert again.status_code == 409
    assert "同名目录" in again.json()["detail"]
    # The first install is untouched.
    assert (roots["default"] / ".easycode" / "skills" / "twice" / "SKILL.md").is_file()


def test_bad_input_is_rejected_with_a_reason(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    (tmp_path / "src" / "no-skill-md").mkdir(parents=True)

    missing = client.post(
        "/api/skills/import", json={"source_path": str(tmp_path / "src" / "no-skill-md")}
    )
    assert missing.status_code == 422
    assert "SKILL.md" in missing.json()["detail"]

    absent = client.post(
        "/api/skills/import", json={"source_path": str(tmp_path / "src" / "gone")}
    )
    assert absent.status_code == 422
    assert "无法读取源文件夹" in absent.json()["detail"]

    assert not (roots["default"] / ".easycode" / "skills").exists()


async def test_a_busy_session_blocks_the_import_before_it_writes(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    sess = store.create(root=str(roots["default"]))
    source = make_skill(tmp_path / "src", "blocked")

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
        sess._lock,
    ):
        r = await client.post("/api/skills/import", json={"source_path": str(source)})
    assert r.status_code == 409
    # Nothing was published: a refused import leaves no half-installed skill.
    assert not (roots["default"] / ".easycode" / "skills").exists()


def test_import_reports_a_failed_refresh_without_failing_the_install(tmp_path, monkeypatch):
    app, _cfg, store, roots = build(tmp_path)
    sess = store.create(root=str(roots["default"]))
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "warned")

    def boom(self, *, with_skills):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("easycode.agent.loop.Agent.rediscover_extensions", boom)

    r = client.post("/api/skills/import", json={"source_path": str(source)})

    assert r.status_code == 201
    body = r.json()
    assert (roots["default"] / ".easycode" / "skills" / "warned" / "SKILL.md").is_file()
    assert len(body["warnings"]) == 1
    assert sess.id in body["warnings"][0]
    # The already-published install is not reported as a failure: importing it
    # again is not how a stale session gets fixed.
    assert body["imported"]["name"] == "warned"


async def test_a_cancelled_request_still_refreshes_what_it_published(tmp_path, monkeypatch):
    """The disk change outlives the request; the sessions must learn about it."""
    app, _cfg, store, roots = build(tmp_path)
    sess = store.create(root=str(roots["default"]))
    source = make_skill(tmp_path / "src", "slow")
    real = routes_skills.import_skill

    def slow(source_path, destination_root, scope):
        time.sleep(0.3)
        return real(source_path, destination_root, scope)

    monkeypatch.setattr(routes_skills, "import_skill", slow)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        task = asyncio.create_task(
            client.post("/api/skills/import", json={"source_path": str(source)})
        )
        # Long enough that the worker is inside the copy, short enough that it
        # is still running when the request goes away.
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert (roots["default"] / ".easycode" / "skills" / "slow" / "SKILL.md").is_file()
    assert "slow" in sess.agent.skills.names()


def test_typing_a_new_skill_sends_its_context_and_the_task(tmp_path):
    """The restore path: an open session, a fresh import, then `/name task`."""
    app, _cfg, store, roots = build(tmp_path)
    session_id = client_session(app, store, roots)
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "greet", body="Say hello in the project language")
    client.post("/api/skills/import", json={"source_path": str(source)})

    r = client.post(
        "/api/chat",
        json={"session_id": session_id, "message": "/greet 给 README 加一段问候"},
    )

    assert r.status_code == 200, r.text
    assert "event: " not in r.text  # plain SSE data lines only
    sess = store.get(session_id)
    sent = sess.agent.provider.calls[-1][-1]["content"]
    assert "Say hello in the project language" in sent
    assert "给 README 加一段问候" in sent
    # The model is told where the package lives, not just what it says.
    assert str(roots["default"] / ".easycode" / "skills" / "greet") in sent
    assert [t["status"] for t in sess.turns] == ["completed"]


def test_typing_a_new_skill_still_resolves_when_the_import_refresh_failed(tmp_path, monkeypatch):
    """A stale session recovers on the next send rather than staying broken."""
    app, _cfg, store, roots = build(tmp_path)
    session_id = client_session(app, store, roots)
    client = TestClient(app)
    source = make_skill(tmp_path / "src", "later", body="Loaded after a failure")
    from easycode.agent.loop import Agent

    real = Agent.rediscover_extensions
    calls = {"n": 0}

    def flaky(self, *, with_skills):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first refresh fails")
        return real(self, with_skills=with_skills)

    monkeypatch.setattr(Agent, "rediscover_extensions", flaky)

    imported = client.post("/api/skills/import", json={"source_path": str(source)})
    assert imported.status_code == 201
    assert imported.json()["warnings"]  # the session could not be refreshed then

    # The next message re-reads the registry inside the turn, so the skill the
    # import failed to register still resolves — and the pre-check, which reads
    # the disk, no longer rejects the text as an unknown command.
    r = client.post(
        "/api/chat", json={"session_id": session_id, "message": "/later do the work"}
    )

    assert r.status_code == 200, r.text
    sess = store.get(session_id)
    sent = sess.agent.provider.calls[-1][-1]["content"]
    assert "Loaded after a failure" in sent
    assert "do the work" in sent


def test_a_turn_that_cannot_refresh_ends_with_a_stream_error(tmp_path, monkeypatch):
    """Failing to refresh must not silently run a turn against a stale registry."""
    app, _cfg, store, roots = build(tmp_path)
    session_id = client_session(app, store, roots)
    client = TestClient(app)

    def boom(self, *, with_skills):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("easycode.agent.loop.Agent.rediscover_extensions", boom)

    r = client.post("/api/chat", json={"session_id": session_id, "message": "hello"})

    assert r.status_code == 200
    assert '"type": "error"' in r.text
    assert "registry unavailable" in r.text
    assert "turn_accepted" not in r.text
    sess = store.get(session_id)
    assert sess.turns == []  # no branch was written


def client_session(app, store, roots) -> str:
    """An open session in the default project, as the Web UI would have it."""
    sess = store.create(root=str(roots["default"]))
    return sess.id


def test_effective_flags_match_what_discovery_resolves(tmp_path):
    """The panel's ``effective`` must agree with what a conversation loads.

    Three scopes define the same name: the last project root wins, exactly as
    ``SkillRegistry.discover`` resolves it for an agent running in that project.
    """
    from easycode.extensions.skills import SkillRegistry

    app, _cfg, _store, roots = build(tmp_path)
    make_skill(Path.home() / ".easycode" / "skills", "same", "personal copy")
    make_skill(roots["default"] / ".easycode" / "skills", "same", "primary copy")
    make_skill(roots["attached"] / ".easycode" / "skills", "same", "secondary copy")
    client = TestClient(app)

    body = client.get("/api/skills").json()

    panel = {row["scope"] + ":" + row["directory"]: row["effective"] for row in body["skills"]}
    assert set(panel.values()) == {False, True}
    loaded = SkillRegistry.discover(
        [roots["default"], roots["attached"]]
    ).get("same")
    winner = [
        row for row in body["skills"] if row["effective"]
    ]
    assert len(winner) == 1
    # Same directory the agent would load, not merely the same name.
    assert winner[0]["directory"] == str(loaded.directory)


def test_one_panel_request_reads_each_skill_file_once(tmp_path, monkeypatch):
    """Listing skills must not re-read the same SKILL.md a second time."""
    app, _cfg, _store, roots = build(tmp_path)
    make_skill(roots["default"] / ".easycode" / "skills", "alpha")
    make_skill(roots["default"] / ".easycode" / "skills", "beta")
    make_skill(Path.home() / ".easycode" / "skills", "gamma")
    real = routes_skills.load_skill
    reads: list[Path] = []

    def counted(directory, source):
        reads.append(Path(directory))
        return real(directory, source)

    monkeypatch.setattr(routes_skills, "load_skill", counted)
    TestClient(app).get("/api/skills")

    assert sorted(p.name for p in reads) == ["alpha", "beta", "gamma"]


def test_picked_command_does_not_discover_skills_twice(tmp_path, monkeypatch):
    """The locked path reuses the registry it just refreshed.

    Typed ``/`` text is resolved once before the stream and again inside the
    session lock. The locked half must expand it against the registry
    ``rediscover_extensions`` just built, not read every SKILL.md again.
    """
    from easycode.extensions.skills import SkillRegistry

    app, _cfg, store, roots = build(tmp_path)
    session_id = client_session(app, store, roots)
    client = TestClient(app)
    make_skill(roots["default"] / ".easycode" / "skills", "greet", body="Say hi")
    real = SkillRegistry.discover
    seen: list[list[Path]] = []

    def counted(cls, roots_arg, user_dir=None):
        seen.append(list(roots_arg))
        return real(roots_arg, user_dir)

    monkeypatch.setattr(SkillRegistry, "discover", classmethod(counted))
    r = client.post(
        "/api/chat",
        json={"session_id": session_id, "message": "/greet 任务"},
    )

    assert r.status_code == 200, r.text
    sess = store.get(session_id)
    assert "Say hi" in sess.agent.provider.calls[-1][-1]["content"]
    # Pre-request check, then the locked path's refresh. The locked resolve
    # itself contributes none: it was handed the registry from that refresh.
    assert len(seen) == 2, seen
