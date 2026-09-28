"""Workspaces tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.web.main import create_app


def test_session_create_with_root_uses_session_root(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir()
    project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    agent_roots: list[str] = []

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        agent_roots.append(str(root))
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root
        )

    store = SessionStore(cfg, primary, factory)
    s = store.create(root=str(project))
    assert s.root == str(project)
    assert agent_roots[-1] == str(project)
    assert s.summary["root"] == str(project)


def test_session_root_persistence_roundtrip(tmp_path):
    import json as _json

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir()
    project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(_json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root
        )

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(root=str(project))
    store2 = SessionStore(cfg, primary, factory)
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.root == str(project)
    assert str(project) in (store2.dir / f"{s.id}.json").read_text(encoding="utf-8")


def test_default_project_session_loads(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    import uuid

    sid = uuid.uuid4().hex[:12]
    (store.dir / f"{sid}.json").write_text(
        json.dumps({"id": sid, "title": "old", "messages": [], "model_alias": "fake-a"}),
        encoding="utf-8",
    )
    store.load_all()
    s = store.get(sid)
    assert s is not None
    assert s.root is None
    assert "root" not in s.summary


def test_workspaces_endpoints(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir()
    project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root
        )

    store = SessionStore(cfg, primary, factory)
    store.create(root=str(project))
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.get("/api/workspaces")
        assert r.status_code == 200
        data = r.json()
        assert data["default"] == str(primary)
        roots = [p["root"] for p in data["projects"]]
        assert str(project) in roots
        # 手动添加端点已删除：POST /api/workspaces 返回 405/404 而非 200
        gone = client.post("/api/workspaces", json={"path": str(project)})
        assert gone.status_code in (404, 405)


def test_chat_root_creates_session_in_project(tmp_path):
    """First message with root creates a session bound to that project."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir()
    project.mkdir()
    agents: list[Agent] = []
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = str(Path(kw.get("root")).resolve()) if kw.get("root") else str(primary)
        agent = Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=Path(root),
        )
        agents.append(agent)
        return agent

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/chat", json={"message": "hello", "root": str(project)})
        assert r.status_code == 200
        bad = client.post("/api/chat", json={"message": "x", "root": str(tmp_path / "missing")})
        assert bad.status_code == 422
    sid = store.list()[0].id
    assert store.get(sid).root == str(project)
    assert agents[0].root == project.resolve()


def test_session_secondary_roots_persist(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    primary.mkdir()
    project.mkdir()
    sec_a.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    built: list[Agent] = []

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        sec = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        agent = Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=sec,
        )
        built.append(agent)
        return agent

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(root=str(project), secondary_roots=[str(sec_a)])
    assert s.secondary_roots == [str(sec_a.resolve())]
    assert built[-1].secondary_roots == [sec_a.resolve()]
    assert s.summary["secondary_roots"] == [str(sec_a.resolve())]

    store2 = SessionStore(cfg, tmp_path / "p2", factory)
    (tmp_path / "p2").mkdir(exist_ok=True)
    store2.dir = store1.dir  # same disk dir
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.secondary_roots == [str(sec_a.resolve())]
    assert restored.root == str(project)


def test_chat_root_with_secondary_roots(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    primary.mkdir()
    project.mkdir()
    sec_a.mkdir()
    agents: list[Agent] = []
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        sec = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        agent = Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=sec,
        )
        agents.append(agent)
        return agent

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "hi", "root": str(project), "secondary_roots": [str(sec_a)]},
        )
        assert r.status_code == 200
    s = store.list()[0]
    assert s.secondary_roots == [str(sec_a)]
    assert [str(x) for x in agents[0].secondary_roots] == [str(sec_a)]


def test_choose_workspaces_endpoint(tmp_path, monkeypatch):
    import easycode.web.routes_workspaces as m

    monkeypatch.setattr(m, "finder_supported", lambda: True)
    monkeypatch.setattr(
        m,
        "choose_folders_via_finder",
        lambda multiple=False, prompt="选择目录": ["/tmp/a", "/tmp/b"] if multiple else ["/tmp/a"],
    )
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/workspaces/choose", json={"multiple": False})
        assert r.status_code == 200
        assert r.json()["paths"] == ["/tmp/a"]
        r2 = client.post("/api/workspaces/choose", json={"multiple": True, "prompt": "x"})
        assert r2.status_code == 200
        assert r2.json()["paths"] == ["/tmp/a", "/tmp/b"]


def test_choose_unsupported_returns_empty(tmp_path, monkeypatch):
    import easycode.web.routes_workspaces as m

    monkeypatch.setattr(m, "finder_supported", lambda: False)
    monkeypatch.setattr(
        m, "choose_folders_via_finder", lambda multiple=False, prompt="选择目录": []
    )
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    r = client.post("/api/workspaces/choose", json={"multiple": False})
    assert r.status_code == 200
    data = r.json()
    assert data["paths"] == []
    assert data["supported"] is False


def test_workspaces_projects_from_sessions_and_save(tmp_path):
    """GET merges config + session bindings; POST /projects persists per-root."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    proj = tmp_path / "proj"
    sec = tmp_path / "sec"
    primary.mkdir()
    proj.mkdir()
    sec.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=secs,
        )

    store = SessionStore(cfg, primary, factory)
    # config 预置绑定（default project 挂 sec）
    cfg.workspace_projects = [{"root": None, "secondary": [str(sec)]}]
    # 会话历史推断 (proj → sec)
    store.create(root=str(proj), secondary_roots=[str(sec)])

    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.get("/api/workspaces")
        data = r.json()
        projects = data["projects"]
        by_root = {p["root"]: p for p in projects}
        assert by_root[None]["secondary"] == [str(sec)]
        assert by_root[str(proj)]["secondary"] == [str(sec)]

        # save: proj 换一组次目录并持久化到 config
        r2 = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [str(sec), str(primary)]},
        )
        assert r2.status_code == 200
        assert sorted(r2.json()["secondary"]) == sorted([str(sec), str(primary)])

        # 无效主目录
        r3 = client.post(
            "/api/workspaces/projects", json={"root": str(tmp_path / "missing"), "secondary": []}
        )
        assert r3.status_code == 422

        # default project 绑定持久化
        r4 = client.post("/api/workspaces/projects", json={"root": None, "secondary": [str(proj)]})
        assert r4.status_code == 200
        assert r4.json()["secondary"] == [str(proj)]

    # config 落盘包含 projects
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "projects" in raw.get("workspace", {})
    projs = raw["workspace"]["projects"]
    assert any(p.get("root") == str(proj) for p in projs)
    assert any(p.get("root") is None for p in projs)


def test_spa_fallback_no_405_on_api_posts(tmp_path):
    """Production static serving must not turn unknown /api POSTs into 405s."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=dist))
    with client:
        # 未注册的 /api POST 现在返回 404 JSON，而不是 405
        r = client.post("/api/definitely-not-a-route", json={})
        assert r.status_code == 404
        # SPA fallback 对 GET 生效
        r2 = client.get("/")
        assert r2.status_code == 200
        assert "app" in r2.text
        # 真实 approve 端点恢复可用
        r3 = client.post("/api/approval/nonexistent", json={"approve": True})
        assert r3.status_code == 404  # 未知 id 依然 404，但路由本身存在


def test_save_project_with_session_id_updates_session(tmp_path):
    """Editing secondary roots on a locked session updates its agent + disk state."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    proj = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    sec_b = tmp_path / "sec_b"
    primary.mkdir()
    proj.mkdir()
    sec_a.mkdir()
    sec_b.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=secs,
        )

    store = SessionStore(cfg, primary, factory)
    s = store.create(root=str(proj), secondary_roots=[str(sec_a)])
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [str(sec_a), str(sec_b)], "session_id": s.id},
        )
        assert r.status_code == 200
        assert sorted(r.json()["secondary"]) == sorted([str(sec_a), str(sec_b)])

        # 会话会话本身被同步
        sess = store.get(s.id)
        assert sorted(sess.secondary_roots) == sorted([str(sec_a), str(sec_b)])
        assert sorted(str(p) for p in sess.agent.secondary_roots) == sorted(
            [str(sec_a), str(sec_b)]
        )

        # 落盘包含新绑定
        raw = json.loads((store.dir / f"{s.id}.json").read_text(encoding="utf-8"))
        assert sorted(raw["secondary_roots"]) == sorted([str(sec_a), str(sec_b)])

        # 未知 session → 404
        r2 = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [], "session_id": "nope"},
        )
        assert r2.status_code == 404


def test_root_error_rejects_sensitive_descendants_and_symlinks(tmp_path, monkeypatch):
    """DEC-T5: the whole media path is checked, not only the last name."""
    from easycode.credentials import data_home
    from easycode.permissions.boundary import root_error

    proj = tmp_path / "proj"
    objects = proj / ".git" / "objects"
    objects.mkdir(parents=True)
    skills = proj / ".easycode" / "skills"
    skills.mkdir(parents=True)

    assert root_error(objects) is not None
    assert root_error(skills) is not None

    link = tmp_path / "link-to-objects"
    link.symlink_to(objects)
    assert root_error(link) is not None

    # A managed worktree stays valid (including its subdirectories)...
    worktree = data_home() / "worktrees" / "repo"
    sub = worktree / "pkg"
    sub.mkdir(parents=True)
    assert root_error(worktree) is None
    assert root_error(sub) is None
    # ...but the worktrees container and the data home itself do not.
    assert root_error(data_home() / "worktrees") is not None
    assert root_error(data_home()) is not None
    # A project's own .easycode/worktrees gets no worktree exception.
    fake = tmp_path / "other" / ".easycode" / "worktrees" / "x"
    fake.mkdir(parents=True)
    assert root_error(fake) is not None


def test_shell_grant_root_keeps_sensitive_children_protected(tmp_path):
    """An approval grant for an external directory must not lift the
    .git/.easycode/config protections inside that directory."""
    from easycode.permissions.boundary import CONFIG_FILENAME, PathContext, ToolGrant

    proj = tmp_path / "proj"
    proj.mkdir()
    ext = tmp_path / "ext"
    (ext / ".git").mkdir(parents=True)
    (ext / ".easycode").mkdir()
    ctx = PathContext(primary=proj)
    grant = ToolGrant(writable_roots=(ext,))

    assert ctx.grant_granted(ext / "ok.txt", grant) is True
    assert ctx.grant_granted(ext / ".git" / "config", grant) is False
    assert ctx.grant_granted(ext / ".easycode" / "state.json", grant) is False
    assert ctx.grant_granted(ext / CONFIG_FILENAME, grant) is False


def test_primary_root_validated_like_other_roots(tmp_path):
    """DEC-T5: the primary root must pass root_error; worktrees stay exempt."""
    from easycode.agent.loop import Agent
    from easycode.credentials import data_home
    from easycode.permissions.boundary import root_error
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    cfg = Config.load(start=proj)
    cfg.root = proj

    def factory(alias: str, **kw):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=proj)

    store = SessionStore(cfg, proj, factory)

    assert root_error(proj / ".git") is not None
    with pytest.raises(ValueError, match="sensitive directory"):
        store.create(root=str(proj / ".git"))

    # permanent worktrees under the data dir are legitimate roots
    worktree = data_home() / "worktrees" / "repo"
    worktree.mkdir(parents=True)
    assert root_error(worktree) is None
    sess = store.create(root=str(worktree))
    assert sess.root == str(worktree)


# --------------------------------------------------- 空白会话的工作目录

def _project_app(tmp_path, projects=None, script=None):
    """A store + client whose config carries the given project bindings.

    ``projects`` are workspace entries (``root`` → ``secondary``) as the config
    persists them, so a session created for one of them inherits its binding the
    way a real project does.
    """
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "primary"
    primary.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    if projects:
        cfg.workspace_projects = projects

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(
            provider=FakeProvider(script=list(script or [])),
            registry=build_registry(8000),
            root=root,
            secondary_roots=secs,
        )

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    return client, store


def test_blank_session_moves_and_inherits_the_target_project(tmp_path):
    """A conversation that has not started may be pointed at another project.

    The move carries that project's bound secondary directories with it (the
    same binding a session created there would get) and is written to disk, so a
    reload finds the conversation where it was moved to.
    """
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    sec_b = tmp_path / "sec_b"
    for d in (proj_a, proj_b, sec_b):
        d.mkdir()
    client, store = _project_app(
        tmp_path, projects=[{"root": str(proj_b), "secondary": [str(sec_b)]}]
    )
    with client:
        sess = store.create(root=str(proj_a))
        r = client.post(f"/api/sessions/{sess.id}/workspace", json={"root": str(proj_b)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session"]["root"] == str(proj_b)
        assert body["session"]["secondary_roots"] == [str(sec_b)]
        assert body["session"]["started"] is False
        # The project list is rebuilt, so the sidebar can show the new place.
        assert any(p["root"] == str(proj_b) for p in body["projects"])

    # The live session and its agent moved together.
    assert sess.root == str(proj_b)
    assert sess.secondary_roots == [str(sec_b)]
    assert str(sess.agent.root) == str(proj_b)
    assert [str(p) for p in sess.agent.secondary_roots] == [str(sec_b)]

    # Persisted, and restored where it was moved to.
    raw = json.loads((store.dir / f"{sess.id}.json").read_text(encoding="utf-8"))
    assert raw["root"] == str(proj_b)
    assert raw["secondary_roots"] == [str(sec_b)]
    from easycode.web.session import SessionStore

    restored = SessionStore(store.cfg, store.root, store.agent_factory)
    restored.dir = store.dir
    restored.load_all()
    reloaded = restored.get(sess.id)
    assert reloaded is not None
    assert reloaded.root == str(proj_b)
    assert reloaded.secondary_roots == [str(sec_b)]


def test_blank_session_moves_back_to_the_default_project(tmp_path):
    """``root: null`` is the default project, not a missing value: moving there
    drops the primary root and takes the default project's own binding."""
    proj = tmp_path / "a"
    proj.mkdir()
    sec = tmp_path / "sec"
    sec.mkdir()
    client, store = _project_app(tmp_path, projects=[{"root": None, "secondary": [str(sec)]}])
    with client:
        sess = store.create(root=str(proj))
        r = client.post(f"/api/sessions/{sess.id}/workspace", json={"root": None})
        assert r.status_code == 200, r.text
        # A session in the default project has no primary of its own.
        assert "root" not in r.json()["session"]
        assert r.json()["session"]["secondary_roots"] == [str(sec)]
    assert sess.root is None
    assert str(sess.agent.root) == str(client.app.state.store.cfg.root)
    raw = json.loads((store.dir / f"{sess.id}.json").read_text(encoding="utf-8"))
    assert "root" not in raw


def test_started_session_refuses_to_move(tmp_path):
    """Once a turn has run, the conversation's directory is fixed: records,
    previews and the tree report all describe that one directory."""
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    client, store = _project_app(tmp_path, script=[{"text": "ok"}])
    with client:
        sess = store.create(root=str(proj_a))
        assert client.post(
            "/api/chat", json={"message": "hi", "session_id": sess.id}
        ).status_code == 200
        assert sess.is_blank is False

        r = client.post(f"/api/sessions/{sess.id}/workspace", json={"root": str(proj_b)})
        assert r.status_code == 409
        # Nothing was applied: neither in memory nor on disk.
        assert sess.root == str(proj_a)
        assert str(sess.agent.root) == str(proj_a)
        raw = json.loads((store.dir / f"{sess.id}.json").read_text(encoding="utf-8"))
        assert raw["root"] == str(proj_a)


@pytest.mark.asyncio
async def test_running_session_refuses_to_move(tmp_path):
    """A turn in flight owns the conversation: the move is refused before
    anything is written, without cancelling the turn to find out."""
    import httpx

    proj_b = tmp_path / "b"
    proj_b.mkdir()
    client, store = _project_app(tmp_path)
    app = client.app
    sess = store.create()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        async with sess._lock:
            r = await http.post(f"/api/sessions/{sess.id}/workspace", json={"root": str(proj_b)})
        assert r.status_code == 409
    assert sess.root is None
    assert store.get(sess.id) is sess


def test_move_rejects_a_bad_root_or_secondary_without_partial_update(tmp_path):
    """Validation happens before anything is written: a refused move leaves the
    session exactly where it was."""
    proj = tmp_path / "a"
    proj.mkdir()
    (proj / ".git").mkdir()
    missing = tmp_path / "missing"
    client, store = _project_app(tmp_path)
    with client:
        sess = store.create(root=str(proj))
        # A protected directory is refused outright.
        r = client.post(f"/api/sessions/{sess.id}/workspace", json={"root": str(proj / ".git")})
        assert r.status_code == 422
        # So is a secondary root that is not there.
        r2 = client.post(
            f"/api/sessions/{sess.id}/workspace",
            json={"root": None, "secondary_roots": [str(missing)]},
        )
        assert r2.status_code == 422
        # Unknown session.
        r3 = client.post("/api/sessions/nope/workspace", json={"root": None})
        assert r3.status_code == 404

    assert sess.root == str(proj)
    assert str(sess.agent.root) == str(proj)
    assert sess.secondary_roots == []


def test_merge_projects_union_metadata_and_pinning():
    """Config entries keep their naming; history only adds secondaries."""
    from easycode.web.routes_workspaces import merge_projects

    base = [
        {"root": None, "secondary": ["/a"], "name": "默认"},
        {"root": "/p", "secondary": [], "name": "项目", "pinned": True},
    ]
    extra = [
        # Repeats a base project with another secondary: merged, not duplicated.
        {"root": "/p", "secondary": ["/b"]},
        {"root": "/p", "secondary": ["/c"]},
        {"root": "/q", "secondary": []},
        {"root": None, "secondary": ["/d"]},
    ]

    out = merge_projects(base, extra)

    # No new key, no duplicate, and the pinned config entry sorts first.
    assert [p["root"] for p in out] == ["/p", None, "/q"]
    pinned, default, only_history = out
    assert pinned["secondary"] == ["/b", "/c"]
    assert pinned["name"] == "项目"
    assert pinned["pinned"] is True
    assert default["secondary"] == ["/a", "/d"]
    assert default["name"] == "默认"
    assert "pinned" not in default
    assert "name" not in only_history


def _worktree_client(tmp_path):
    """An app whose default project is the throwaway root."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8"
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **kw):
        root = Path(kw["root"]).resolve() if kw.get("root") else tmp_path
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
        )

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def _git_repo(tmp_path, name="repo"):
    import subprocess

    repo = tmp_path / name
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t.t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_worktree_applies_uncommitted_changes_through_stdin(tmp_path):
    """Local edits arrive in the new worktree, including odd file names."""
    client = _worktree_client(tmp_path)
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("hello\nchanged\n", encoding="utf-8")
    odd = repo / "带 空格 的文件.txt"
    odd.write_text("中文内容\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "-N", odd.name], cwd=repo, check=True)

    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})

    assert r.status_code == 200, r.text
    data = r.json()
    assert "applied uncommitted changes" in data["notes"]
    wt = Path(data["root"])
    assert (wt / "f.txt").read_text(encoding="utf-8") == "hello\nchanged\n"
    assert (wt / "带 空格 的文件.txt").read_text(encoding="utf-8") == "中文内容\n"
    # The patch travelled through stdin: nothing is left next to the worktree.
    assert list(wt.parent.glob("*.patch")) == []


def test_worktree_skips_apply_when_there_is_nothing_to_apply(tmp_path, monkeypatch):
    """A clean checkout must not run a diff at all."""
    client = _worktree_client(tmp_path)
    repo = _git_repo(tmp_path)
    import easycode.web.platform as platform

    seen: list[list[str]] = []
    real_run = platform.subprocess.run

    def spy(cmd, **kwargs):
        seen.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(platform.subprocess, "run", spy)
    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})

    assert r.status_code == 200, r.text
    assert r.json()["notes"] == []
    assert not any("apply" in cmd for cmd in seen), seen


def test_worktree_survives_a_patch_that_does_not_apply(tmp_path, monkeypatch):
    """A failing apply is best-effort: the worktree is still created."""
    client = _worktree_client(tmp_path)
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("hello\nchanged\n", encoding="utf-8")
    import easycode.web.platform as platform

    real_run = platform.subprocess.run

    def corrupting(cmd, **kwargs):
        res = real_run(cmd, **kwargs)
        if cmd[:3] == ["git", "diff", "HEAD"]:
            res.stdout = "this is not a patch\n"
        return res

    monkeypatch.setattr(platform.subprocess, "run", corrupting)
    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["notes"] == []
    # The worktree exists at the committed state, untouched by the bad patch.
    assert (Path(data["root"]) / "f.txt").read_text(encoding="utf-8") == "hello\n"
