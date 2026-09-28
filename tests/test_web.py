"""Web API tests: SSE chat flow, sessions, models, persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.context import SUMMARY_PREFIX, History
from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.models.credentials import Credential, save_credential
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider


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

    def factory(alias: str, **_) -> Agent:
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


def test_session_delete_reports_file_failure_and_can_retry(tmp_path, monkeypatch):
    """A failed session-file removal must not report success: the session stays
    tracked and retryable, and only a successful delete removes both the store
    entry and the file (no resurrection on reload)."""
    import pathlib

    client = make_app(tmp_path)
    with client:
        client.post("/api/chat", json={"message": "session one"})
        sid = client.get("/api/sessions").json()[0]["id"]
        session_file = tmp_path / ".easycode" / "sessions" / f"{sid}.json"
        assert session_file.exists()

        real_unlink = pathlib.Path.unlink
        failing = {"on": True}

        def flaky_unlink(self, missing_ok=False):
            if failing["on"] and self == session_file:
                raise OSError("disk error")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(pathlib.Path, "unlink", flaky_unlink)
        r = client.delete(f"/api/sessions/{sid}")
        assert r.status_code == 500, r.text
        assert "删除会话文件失败" in r.json()["detail"]
        assert session_file.exists()
        assert [s["id"] for s in client.get("/api/sessions").json()] == [sid]

        # Retry after the transient failure succeeds and leaves nothing behind.
        failing["on"] = False
        r2 = client.delete(f"/api/sessions/{sid}")
        assert r2.status_code == 200
        assert not session_file.exists()
        assert client.get("/api/sessions").json() == []
        assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_create_blank_session_is_listed_and_blank_deletable(tmp_path):
    """「新会话」creates the conversation before anything is sent.

    It is an ordinary empty session from that moment — listed, on disk, and
    still blank — and closing its tab deletes it for good.
    """
    client = make_app(tmp_path)
    with client:
        r = client.post("/api/sessions", json={})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["title"] == "新会话"
        assert created["started"] is False
        session_file = tmp_path / ".easycode" / "sessions" / f"{created['id']}.json"
        assert session_file.exists()

        listed = client.get("/api/sessions").json()
        assert [s["id"] for s in listed] == [created["id"]]
        assert listed[0]["started"] is False
        detail = client.get(f"/api/sessions/{created['id']}").json()
        assert detail["messages"] == []
        assert detail["todos"] == []

        closed = client.delete(f"/api/sessions/{created['id']}?blank_only=1")
        assert closed.status_code == 200
        assert closed.json()["deleted"] is True
        assert client.get("/api/sessions").json() == []
        assert not session_file.exists()
        # Nothing left to remove is not an error: the caller only closes a view.
        again = client.delete(f"/api/sessions/{created['id']}?blank_only=1")
        assert again.status_code == 200
        assert again.json()["deleted"] is False


def test_create_blank_session_inherits_the_project_binding(tmp_path):
    """A blank conversation created for a project already carries its secondary
    directories, the same way a chat-created session would get them."""
    primary = tmp_path / "p"
    sec = tmp_path / "sec"
    primary.mkdir()
    sec.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    cfg.workspace_projects = [{"root": str(primary), "secondary": [str(sec)]}]

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[]),
            registry=build_registry(8000),
            root=Path(kw.get("root") or primary),
            secondary_roots=[Path(p) for p in (kw.get("secondary_roots") or [])],
        )

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        created = client.post("/api/sessions", json={"root": str(primary)}).json()
        assert created["root"] == str(primary)
        assert created["secondary_roots"] == [str(sec)]


def test_create_blank_session_rejects_an_invalid_root(tmp_path):
    """A root the rules refuse is a 422 with no session left behind."""
    client = make_app(tmp_path)
    with client:
        (tmp_path / ".git").mkdir()
        r = client.post("/api/sessions", json={"root": str(tmp_path / ".git")})
        assert r.status_code == 422
        assert client.get("/api/sessions").json() == []


def test_blank_delete_keeps_a_session_that_started(tmp_path):
    """The blank delete is decided by the server: a session whose turn has begun
    stays exactly where it is, so only the view closes."""
    client = make_app(tmp_path)
    with client:
        client.post("/api/chat", json={"message": "session one"})
        sid = client.get("/api/sessions").json()[0]["id"]

        r = client.delete(f"/api/sessions/{sid}?blank_only=1")
        assert r.status_code == 200
        assert r.json()["deleted"] is False
        listed = client.get("/api/sessions").json()
        assert [s["id"] for s in listed] == [sid]
        assert listed[0]["started"] is True

        # An explicit delete still removes it.
        assert client.delete(f"/api/sessions/{sid}").json()["deleted"] is True
        assert client.get("/api/sessions").json() == []


@pytest.mark.asyncio
async def test_blank_delete_leaves_a_running_turn_alone(tmp_path):
    """A turn in flight means the session has already started: closing its tab
    must not cancel that turn to find out."""
    import httpx

    app = make_app(tmp_path).app
    store = app.state.store
    sess = store.create()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with sess._lock:
            response = await client.delete(f"/api/sessions/{sess.id}?blank_only=1")
        assert response.status_code == 200
        assert response.json()["deleted"] is False
    assert store.get(sess.id) is sess
    assert sess._lock.locked() is False


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

    def factory(alias: str, **_) -> Agent:
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




def test_load_all_preserves_condensed_summary(tmp_path):
    """A rolling-compaction summary must survive a reload.

    The summary is the model context's own head: it is not a chat message, so it
    belongs in ``history_base`` rather than in front of the transcript. Losing it
    would silently drop everything the earlier turns were condensed into.
    """
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **_) -> Agent:
        return Agent(
            provider=FakeProvider(script=[{"text": "reply"}]),
            registry=build_registry(8000),
            root=tmp_path,
        )

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        sid = None
        for msg in ("one", "two", "three", "four"):
            payload: dict = {"message": msg}
            if sid:
                payload["session_id"] = sid
            body = client.post("/api/chat", json=payload).text
            for line in body.splitlines():
                if line.startswith("data:"):
                    ev = json.loads(line[6:])
                    if ev.get("type") == "session":
                        sid = ev["session_id"]
        sess = store.get(sid)
        assert sess is not None
        assert len(sess.turns) == 4
        # The loop already condenses older turns into a summary system message here.
        assert sess.agent.history.condense_from("SUMMARY", 2) is True
        assert History.is_summary(sess.agent.history.messages[0])
        assert sess.agent.history.summary == "SUMMARY"
        store.record_exchange(sess)
        saved = json.loads((store.dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert History.is_summary(saved["history_base"][0])

    # Reload from disk (browser refresh / service restart).
    store2 = SessionStore(cfg, tmp_path, factory)
    store2.load_all()
    restored = store2.get(sess.id)
    assert restored is not None
    m = restored.agent.history.messages
    assert m[0]["role"] == "system"
    assert History.is_summary(m[0])
    assert SUMMARY_PREFIX in str(m[0]["content"])
    # The rolling-merge field is rebuilt so the next compaction can merge, and
    # the conversation the summary replaced is still readable.
    assert restored.agent.history.summary == "SUMMARY"
    assert len(restored.turns) == 4
    detail = client.get(f"/api/sessions/{sid}").json()
    assert [msg["content"] for msg in detail["messages"] if msg["role"] == "user"] == [
        "one",
        "two",
        "three",
        "four",
    ]


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


def test_reveal_unavailable_on_non_darwin(tmp_path, monkeypatch):
    import subprocess
    import sys

    client = make_app(tmp_path)
    repo = _mk_repo(tmp_path, "proj-r")

    # Intercept only the Finder launch so the test never pops a real window
    # (pytest tmp_path lives under /private/var/folders/... on macOS).
    opened: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:1] == ["open"]:
            opened.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("easycode.web.platform.subprocess.run", fake_run)

    r = client.post("/api/workspaces/reveal", json={"root": str(repo)})
    assert r.status_code == 200
    if sys.platform != "darwin":
        assert r.json() == {"ok": False, "supported": False, "error": "open is only supported on macOS"}
        assert opened == []
    else:
        assert r.json() == {"ok": True, "supported": True, "path": str(repo)}
        assert opened == [["open", str(repo)]]


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


# ----------------------------------------------------------------


def _user_count(messages) -> int:
    return sum(1 for m in messages if m.get("role") == "user")


def test_user_times_follow_compaction(tmp_path):
    """Compacting the model context must not shorten the conversation.

    The turns are the transcript; the agent's history is only the context sent
    to the model, and condensing it is allowed to drop messages the user can
    still see and still edit.
    """
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **_) -> Agent:
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
        assert [t["raw_input"] for t in sess.turns] == ["one", "two", "three"]

        # compaction: simulate the in-loop condense dropping the oldest turns.
        # history [u1,a1,u2,a2,u3,a3] -> [summary, u3, a3] (only one user left).
        assert sess.agent.history.condense_from("SUMMARY", 4) is True
        assert _user_count(sess.agent.history.messages) == 1
        store.record_exchange(sess)
        # The context cache is the condensed one; the conversation is not.
        assert _user_count(sess.messages) == 1
        assert [t["raw_input"] for t in sess.turns] == ["one", "two", "three"]

        fetched = client.get(f"/api/sessions/{sid}").json()
        assert _user_count(fetched["messages"]) == 3


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


def test_session_without_credential_restores_and_reports_422(tmp_path):
    """DEC-W1: a session whose model lost its credential stays visible.

    Restoring must not build the provider, so the session is listed; the first
    message resolves it lazily and fails with a clear 422.
    """
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "default_model": "gone",
                "models": {"gone": {"model": "x/gone", "api_format": "openai_compatible"}},
            }
        ),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / ".easycode" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "abc123.json").write_text(
        json.dumps(
            {
                "id": "abc123",
                "title": "旧会话",
                "created_at": "2026-01-01T00:00:00+00:00",
                "model_alias": "gone",
                "permission_mode": "ask",
                "messages": [{"role": "user", "content": "hi"}],
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    client = TestClient(create_app(cfg=cfg, static_dir=tmp_path / "no-dist"))

    with client:
        sessions = client.get("/api/sessions").json()
        assert [s["id"] for s in sessions] == ["abc123"]

        r = client.post("/api/chat", json={"message": "again", "session_id": "abc123"})
        assert r.status_code == 422
        assert "credential" in r.json()["detail"]


def test_restored_session_with_sensitive_root_reports_422(tmp_path):
    """A legacy session pointing at a sensitive dir stays readable, but sending
    is refused before any tool could run in that workspace."""
    repo = tmp_path / "repo"
    objects = repo / ".git" / "objects"
    objects.mkdir(parents=True)
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    sessions_dir = tmp_path / ".easycode" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "legacy1.json").write_text(
        json.dumps(
            {
                "id": "legacy1",
                "title": "旧会话",
                "created_at": "2026-01-01T00:00:00+00:00",
                "model_alias": "fake-a",
                "permission_mode": "ask",
                "messages": [{"role": "user", "content": "old work"}],
                "root": str(objects),
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    client = TestClient(create_app(cfg=cfg, static_dir=tmp_path / "no-dist"))

    with client:
        listed = client.get("/api/sessions").json()
        assert [s["id"] for s in listed] == ["legacy1"]
        detail = client.get("/api/sessions/legacy1")
        assert detail.status_code == 200
        body = detail.json()
        assert [m["content"] for m in body["messages"]] == ["old work"]
        # The recovered turn is addressable: that id is what an edit names.
        assert body["messages"][0]["turn_id"] == body["turns"][0]["id"]

        r = client.post("/api/chat", json={"message": "run", "session_id": "legacy1"})
        assert r.status_code == 422
        assert "工作目录无效" in r.json()["detail"]


@pytest.mark.asyncio
async def test_project_archive_refuses_busy_session_before_any_write(tmp_path):
    import httpx

    app = make_app(tmp_path).app
    store = app.state.store
    sessions = [store.create(), store.create()]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with sessions[-1]._lock:
            response = await client.post("/api/workspaces/archive", json={"root": None})
        assert response.status_code == 409
    assert all(not sess.archived for sess in sessions)
    assert all(not json.loads(store._path(sess.id).read_text())["archived"] for sess in sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_archive", [False, True])
async def test_project_archive_and_delete_do_not_resurrect_session(tmp_path, monkeypatch, cancel_archive):
    import asyncio
    import threading

    import httpx

    from tests.helpers_web import wait_until

    app = make_app(tmp_path).app
    store = app.state.store
    sess = store.create()
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.create(root=str(other_root))
    entered, release = threading.Event(), threading.Event()
    original = Path.replace
    writes = []

    def paused_replace(path, target):
        writes.append(Path(target))
        if Path(target) == store._path(sess.id):
            entered.set()
            assert release.wait(5)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", paused_replace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        archive = asyncio.create_task(client.post("/api/workspaces/archive", json={"root": None}))
        delete = None
        try:
            await wait_until(entered.is_set)
            if cancel_archive:
                archive.cancel()
                await asyncio.sleep(0)
                assert not archive.done()
            delete = asyncio.create_task(client.delete(f"/api/sessions/{sess.id}"))
            await asyncio.sleep(0)
        finally:
            release.set()
            results = await asyncio.gather(
                archive, *([delete] if delete else []), return_exceptions=True
            )
        if cancel_archive:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0].status_code == 200
        assert results[1].status_code == 200
    assert not store._path(sess.id).exists()
    store.load_all()
    assert store.get(sess.id) is None
    assert store.get(other.id) is not None
    assert store._path(other.id) not in writes  # only the target project's sessions are saved


def _app_with_provider(tmp_path: Path, provider) -> tuple[TestClient, SessionStore]:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **agent_kwargs):
        root = agent_kwargs.get("root") or tmp_path
        return Agent(
            provider=provider, registry=build_registry(8000), root=Path(root)
        )

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    return client, store


def test_turn_failure_is_recorded_with_its_turn_and_restored(tmp_path):
    """A server-produced terminal error belongs to the turn that raised it, so a
    reload can put it back — and a hard failure is never a finished turn."""
    client, store = _app_with_provider(tmp_path, FakeProvider(script=[{"error": "boom"}]))
    with client:
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
        sid = client.get("/api/sessions").json()[0]["id"]
        detail = client.get(f"/api/sessions/{sid}").json()

    assert len(detail["turn_failures"]) == 1
    failure = detail["turn_failures"][0]
    assert "boom" in failure["message"]
    # The failure names its turn directly; the transcript needs no timestamp
    # lookup to place it, and an edit of that turn takes the error with it.
    assert failure["turn_id"] == detail["turns"][0]["id"]
    assert detail["turns"][0]["status"] == "failed"

    # Persisted, so a restart restores the same association.
    saved = json.loads((store.dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert saved["turns"][0]["failures"] == [{"message": failure["message"]}]
    assert saved["turns"][0]["status"] == "failed"

    restored = SessionStore(store.cfg, tmp_path, store.agent_factory)
    restored.load_all()
    again = restored.get(sid)
    assert again is not None
    assert again.turns[0]["status"] == "failed"
    assert again.turns[0]["failures"][0]["message"] == failure["message"]


def test_iteration_limit_failure_carries_its_code_to_the_client(tmp_path):
    """The stream marks an explicit ceiling with a code, and the record keeps it
    so the restored turn can offer to continue."""
    provider = FakeProvider(script=[{"tool_calls": [("c1", "glob", {"pattern": "*"})]}])
    client, store = _app_with_provider(tmp_path, provider)
    with client:
        sess = store.create()
        sess.agent.max_tool_iterations = 1
        body = client.post(
            "/api/chat", json={"message": "loop", "session_id": sess.id}
        ).text

    assert '"code": "tool_iteration_limit"' in body
    detail = client.get(f"/api/sessions/{sess.id}").json()
    assert [f["code"] for f in detail["turn_failures"]] == ["tool_iteration_limit"]
    assert detail["turn_failures"][0]["turn_id"] == detail["turns"][0]["id"]
