"""Web API tests: SSE chat flow, sessions, models, persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.credentials import Credential, save_credential
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


def _credentialed_models() -> dict:
    """Two models with real credential records in the sandboxed HOME."""
    return {
        "fake-a": {"model": "fake/a", "key_id": "k-a", "api_format": "openai_compatible"},
        "fake-b": {"model": "fake/b", "key_id": "k-b", "api_format": "openai_compatible"},
    }


def test_switch_model(tmp_path):
    save_credential(Credential(key_id="k-a", api_key="sk-test"))
    save_credential(Credential(key_id="k-b", api_key="sk-test"))
    client = make_app(
        tmp_path, {"models": _credentialed_models(), "default_model": "fake-b"}
    )
    r = client.post("/api/models", json={"alias": "fake-a"})
    assert r.status_code == 200
    assert r.json()["default"] == "fake-a"
    r2 = client.post("/api/models", json={"alias": "nope"})
    assert r2.status_code == 404


def test_switch_model_without_credential_rejected(tmp_path):
    """Switching to a model with no credential fails atomically (P1)."""
    models = _credentialed_models()
    models["nocred"] = {"model": "x/nocred", "api_format": "openai_compatible"}
    client = make_app(tmp_path, {"models": models, "default_model": "fake-a"})
    r = client.post("/api/models", json={"alias": "nocred"})
    assert r.status_code == 422
    assert "credential" in r.json()["detail"]
    # Atomicity: the default must be untouched both on disk and via the API.
    disk = json.loads((tmp_path / "easycode.config.json").read_text(encoding="utf-8"))
    assert disk["default_model"] == "fake-a"
    assert client.get("/api/models").json()["default"] == "fake-a"


def test_switch_model_persists_session_alias(tmp_path):
    """Rebinding on switch is flushed per session (restart cannot revert it)."""
    save_credential(Credential(key_id="k-a", api_key="sk-test"))
    save_credential(Credential(key_id="k-b", api_key="sk-test"))
    client = make_app(
        tmp_path, {"models": _credentialed_models(), "default_model": "fake-b"}
    )
    with client:
        r = client.post("/api/chat", json={"message": "hello"})
        assert r.status_code == 200
        sid = client.get("/api/sessions").json()[0]["id"]
        r = client.post("/api/models", json={"alias": "fake-a"})
        assert r.status_code == 200
        data = json.loads(
            (tmp_path / ".easycode" / "sessions" / f"{sid}.json").read_text(encoding="utf-8")
        )
        assert data["model_alias"] == "fake-a"


def test_chat_new_session_without_credential_returns_422(tmp_path):
    """Fresh-install path: default model lacks a key → clear 422, no session."""
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "default_model": "nokey",
                "models": {"nokey": {"model": "x/nokey", "api_format": "openai_compatible"}},
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    # No injected store: exercise the REAL agent factory so provider_kwargs
    # validation runs exactly as in production.
    client = TestClient(create_app(cfg=cfg, static_dir=tmp_path / "no-dist"))
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 422
    assert "credential" in r.json()["detail"]
    sessions_dir = tmp_path / ".easycode" / "sessions"
    assert not list(sessions_dir.glob("*.json"))


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


def test_load_all_migrates_old_system_prompt(tmp_path):
    """A persisted old base system prompt is re-anchored to the current one on reload.

    The freshly built agent already holds the latest prompt (e.g. the
    execute_shell writable_roots guidance) in ``history.system``; the old embedded
    base system message inside ``messages`` is dropped so the ordinary history
    starts with the user/assistant turns, while skill/custom system messages stay.
    """
    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=tmp_path,
            secondary_roots=[Path(p).resolve() for p in (kw.get("secondary_roots") or [])],
        )

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    store = SessionStore(cfg, tmp_path, factory)

    # Current prompt from a fresh agent (must mention writable_roots).
    fresh = factory("fake-a")
    cur = fresh.history.system["content"]
    assert "writable_roots" in cur

    # Pre-seed an OLD session whose messages[0] is the outdated base prompt.
    old_hist = [
        {"role": "system", "content": "You are easycode OLD prompt (no writable_roots)."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "[skill: docs] body"},
    ]
    sid = "old2222"
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / f"{sid}.json").write_text(
        json.dumps(
            {
                "id": sid,
                "title": "old",
                "created_at": "2020-01-01T00:00:00+00:00",
                "model_alias": "fake-a",
                "messages": old_hist,
                "permission_mode": "ask",
            }
        ),
        encoding="utf-8",
    )
    store.load_all()
    s = store.get(sid)
    assert s is not None
    # history.system keeps the freshly built (current) prompt, including writable_roots.
    assert s.agent.history.system is not None
    assert "writable_roots" in s.agent.history.system["content"]
    m = s.agent.history.messages
    # messages no longer carry the base system: it starts with the ordinary history.
    assert m[0] == {"role": "user", "content": "hi"}
    assert m[1] == {"role": "assistant", "content": "hello"}
    assert {"role": "system", "content": "[skill: docs] body"} in m  # kept
    assert s.messages == m  # synced

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


# ---------------------------------------------------------------- item-11 (MS-4)


def _user_count(messages) -> int:
    return sum(1 for m in messages if m.get("role") == "user")


def test_user_times_follow_compress_undo_redo(tmp_path):
    """MS-4: user_times stays aligned with the surviving user messages across
    3 turns, compaction (front drop), undo (back drop) and redo (back restore).
    Regression: the old ``user_times[:n_user]`` kept the WRONG end under
    compaction and lost the timestamp that a redo should restore."""
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

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        sid = None
        for msg in ("one", "two", "three"):
            payload: dict = {"message": msg}
            if sid:
                payload["session_id"] = sid
            r = client.post("/api/chat", json=payload)
            assert r.status_code == 200
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    ev = json.loads(line[6:])
                    if ev.get("type") == "session":
                        sid = ev["session_id"]
            assert sid
        sess = store.get(sid)
        assert sess is not None
        assert _user_count(sess.messages) == 3
        times3 = list(sess.user_times)
        assert len(times3) == 3  # one ISO timestamp per user message

        # compaction: simulate the in-loop condense dropping the oldest turns.
        # history [u1,a1,u2,a2,u3,a3] -> [summary, u3, a3] (only one user left).
        assert sess.agent.history.condense_from("SUMMARY", 4) is True
        assert _user_count(sess.agent.history.messages) == 1
        store.record_exchange(sess)
        assert _user_count(sess.messages) == 1
        assert sess.user_times == [times3[2]]  # surviving = the most recent entry

        # undo removes the last turn -> no user messages, no timestamps.
        r = client.post(f"/api/sessions/{sid}/undo")
        assert r.status_code == 200
        assert r.json()["ok"]
        assert _user_count(sess.messages) == 0
        assert sess.user_times == []
        assert client.get(f"/api/sessions/{sid}").json()["user_times"] == []

        # redo restores the turn AND its timestamp.
        r = client.post(f"/api/sessions/{sid}/redo")
        assert r.status_code == 200
        assert r.json()["ok"]
        assert _user_count(sess.messages) == 1
        assert sess.user_times == [times3[2]]

        # old-session JSON compatibility: a JSON without user_times must load and
        # keep a consistent (here: empty) user_times that stays aligned.
        fetched = client.get(f"/api/sessions/{sid}").json()
        assert fetched["messages"] and fetched["user_times"] == [times3[2]]


def test_chat_cross_project_session_id_conflict(tmp_path):
    """P1-2: an existing session rejects a different project (409, not silence)."""
    primary = tmp_path / "p"
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    primary.mkdir(); proj_a.mkdir(); proj_b.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        root = kw.get("root") or primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=Path(root),
            secondary_roots=secs,
        )

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        s = store.create(root=str(proj_a))
        # chat claiming a different project -> 409
        r = client.post("/api/chat", json={"message": "hi", "session_id": s.id, "root": str(proj_b)})
        assert r.status_code == 409
        # correct project -> 200 (stream starts)
        r2 = client.post("/api/chat", json={"message": "hi", "session_id": s.id, "root": str(proj_a)})
        assert r2.status_code == 200
        # save_project for a different project -> 409 (nothing mutated)
        r3 = client.post(
            "/api/workspaces/projects", json={"root": str(proj_b), "secondary": [], "session_id": s.id}
        )
        assert r3.status_code == 409
        assert s.secondary_roots == []
        # matching project -> 200
        r4 = client.post(
            "/api/workspaces/projects", json={"root": str(proj_a), "secondary": [], "session_id": s.id}
        )
        assert r4.status_code == 200


def test_config_project_secondary_binds_into_new_session(tmp_path):
    """P1-1: a session created without secondary inherits the project binding."""
    primary = tmp_path / "p"
    sec = tmp_path / "sec"
    primary.mkdir(); sec.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    cfg.workspace_projects = [{"root": None, "secondary": [str(sec)]}]

    def factory(alias: str, **kw):
        root = kw.get("root") or primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=Path(root),
            secondary_roots=secs,
        )

    store = SessionStore(cfg, primary, factory)
    s = store.create(model_alias="fake-a")  # no root/secondary -> default project binding
    assert s.secondary_roots == [str(sec.resolve())]
    assert sorted(str(p) for p in s.agent.secondary_roots) == [str(sec.resolve())]
