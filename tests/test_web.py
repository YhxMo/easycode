"""Web API tests: SSE chat flow, sessions, models, persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def make_app(tmp_path: Path, config_patch: dict | None = None) -> TestClient:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps(config_patch or {"models": {"fake-a": "fake/a", "fake-b": "fake/b"}}),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **agent_kwargs):
        provider = FakeProvider(script=[])
        root = agent_kwargs.get("root") or tmp_path
        return Agent(provider=provider, registry=build_registry(8000), root=Path(root))

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def test_get_models(tmp_path):
    client = make_app(tmp_path)
    r = client.get("/api/models")
    assert r.status_code == 200
    data = r.json()
    assert data["default"] == "deepseek-v4flash"
    assert "fake-a" in data["models"]


def test_switch_model(tmp_path):
    client = make_app(tmp_path, {"models": {"fake-a": "fake/a"}, "default_model": "fake-a"})
    r = client.post("/api/models", json={"alias": "fake-a"})
    assert r.status_code == 200
    assert r.json()["default"] == "fake-a"
    r2 = client.post("/api/models", json={"alias": "nope"})
    assert r2.status_code == 404


def test_chat_sse_stream(tmp_path):
    client = make_app(tmp_path)
    with client:
        # create a session first (to later find its id)
        r = client.post("/api/chat", json={"message": "hello"})
        # provider has no script -> returns "ok" text
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data:")]
        kinds = [e["type"] for e in events]
        assert "text" in kinds
        assert kinds[-1] == "done"


def test_chat_with_tool_calls(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = [
        {"tool_calls": [("c1", "glob", {"pattern": "*.py"})], "text": ""},
        {"text": "found"},
    ]

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str) -> Agent:
        provider = FakeProvider(script=list(script))
        return Agent(provider=provider, registry=build_registry(8000), root=tmp_path)

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/chat", json={"message": "list files"})
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data:")]
        kinds = [e["type"] for e in events]
        assert kinds.count("tool_start") == 1
        assert kinds.count("tool_result") == 1
        tool_ev = next(e for e in events if e["type"] == "tool_start")
        assert tool_ev["tool_call"]["name"] == "glob"
        result_ev = next(e for e in events if e["type"] == "tool_result")
        assert "a.py" in result_ev["result"]
        assert kinds[-1] == "done"


def test_sessions_list_and_delete(tmp_path):
    client = make_app(tmp_path)
    with client:
        client.post("/api/chat", json={"message": "session one"})
        r = client.get("/api/sessions")
        sessions = r.json()
        assert len(sessions) == 1
        assert sessions[0]["title"] == "session one"
        sid = sessions[0]["id"]
        r2 = client.delete(f"/api/sessions/{sid}")
        assert r2.status_code == 200
        assert client.get("/api/sessions").json() == []


def test_empty_message_rejected(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/chat", json={"message": "   "})
    assert r.status_code == 422


def test_session_persistence(tmp_path, monkeypatch):
    """Session JSON survives store reload (browser refresh)."""
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str) -> Agent:
        return Agent(
            provider=FakeProvider(script=[{"text": "reply"}]),
            registry=build_registry(8000),
            root=tmp_path,
        )

    monkeypatch.setenv("HOME", str(tmp_path))
    store1 = SessionStore(cfg, tmp_path, factory)
    s = store1.create()
    client = TestClient(create_app(cfg=cfg, session_store=store1, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/chat", json={"message": "persist me", "session_id": s.id})
        assert r.status_code == 200

    store2 = SessionStore(cfg, tmp_path, factory)
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.title == "persist me"
    assert any("persist me" in str(m.get("content")) for m in restored.messages)

# ---------------------------------------------------------------- project management (对齐 codex)

def _mk_repo(tmp_path: Path, name: str = "repo") -> Path:
    import subprocess

    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_pin_project_persists_and_orders(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-a")
    r = client.post("/api/workspaces/pin", json={"root": str(repo), "pinned": True})
    assert r.status_code == 200
    data = r.json()
    assert data["pinned"] is True
    # pinned project sorts first in the projects list
    assert data["projects"][0]["root"] == str(repo)
    assert data["projects"][0].get("pinned") is True
    # persists into config
    cfg = Config.load(start=tmp_path)
    assert cfg.workspace_projects[0].get("pinned") is True
    # unpin
    r2 = client.post("/api/workspaces/pin", json={"root": str(repo), "pinned": False})
    assert r2.json()["projects"][0].get("pinned") is None or r2.json()["projects"][0]["pinned"] is False


def test_reveal_unavailable_on_non_darwin(tmp_path):
    import sys

    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-r")
    r = client.post("/api/workspaces/reveal", json={"root": str(repo)})
    assert r.status_code == 200
    if sys.platform != "darwin":
        assert r.json() == {"ok": False, "supported": False, "error": "open is only supported on macOS"}
    else:
        assert r.json() == {"ok": True, "supported": True, "path": str(repo)}


def test_archive_project_chats_hides_them(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-a")
    # create a session under the project root
    r = client.post("/api/chat", json={"message": "hi", "root": str(repo)})
    sid = None
    for line in r.text.splitlines():
        if line.startswith("data:"):
            ev = json.loads(line[6:])
            if ev.get("type") == "session":
                sid = ev["session_id"]
    assert sid
    # visible by default
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert sid in ids
    # archive
    ra = client.post("/api/workspaces/archive", json={"root": str(repo)})
    assert ra.status_code == 200
    assert ra.json()["archived_sessions"] == 1
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert sid not in ids
    # visible with ?archived=1
    ids_archived = [s["id"] for s in client.get("/api/sessions?archived=1").json()]
    assert sid in ids_archived
    # restore
    ru = client.post(f"/api/sessions/{sid}/archive", json={"archived": False})
    assert ru.status_code == 200
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert sid in ids


def test_remove_project_deletes_sessions(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-r")
    r = client.post("/api/chat", json={"message": "hi", "root": str(repo)})
    sid = None
    for line in r.text.splitlines():
        if line.startswith("data:"):
            ev = json.loads(line[6:])
            if ev.get("type") == "session":
                sid = ev["session_id"]
    rr = client.post("/api/workspaces/projects/remove", json={"root": str(repo)})
    assert rr.status_code == 200
    assert rr.json()["deleted_sessions"] == 1
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert not any(p["root"] == str(repo) for p in rr.json()["projects"])


def test_create_worktree_registers_new_project(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-wt")
    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})
    assert r.status_code == 200, r.text
    data = r.json()
    wt_root = Path(data["root"])
    assert wt_root.is_dir()
    assert data["name"]
    # registered as a project
    assert any(p["root"] == str(wt_root.resolve()) for p in data["projects"])
    # detached HEAD: .git is a file (linked worktree)
    assert (wt_root / ".git").is_file()
    # not a git repo case
    plain = tmp_path / "plain"
    plain.mkdir()
    r2 = client.post("/api/workspaces/worktree", json={"root": str(plain)})
    assert r2.status_code == 422


def test_save_project_with_name(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-n")
    r = client.post(
        "/api/workspaces/projects",
        json={"root": str(repo), "secondary": [], "name": "我的项目"},
    )
    assert r.status_code == 200
    proj = next(p for p in r.json()["projects"] if p["root"] == str(repo))
    assert proj.get("name") == "我的项目"
    # edit rename
    r2 = client.post(
        "/api/workspaces/projects",
        json={"root": str(repo), "secondary": [], "name": "新名字"},
    )
    proj = next(p for p in r2.json()["projects"] if p["root"] == str(repo))
    assert proj.get("name") == "新名字"
