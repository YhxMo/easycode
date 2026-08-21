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

    def factory(alias: str) -> Agent:
        provider = FakeProvider(script=[])
        return Agent(provider=provider, registry=build_registry(8000), root=tmp_path)

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