"""Workspaces tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.web.main import create_app


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


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


def test_primary_root_validated_like_other_roots(tmp_path):
    """DEC-T5: the primary root must pass root_error; worktrees stay exempt."""
    from easycode.agent.loop import Agent
    from easycode.credentials import data_home
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from easycode.workspace import root_error
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
