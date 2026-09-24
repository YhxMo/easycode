"""The task list (`update_todos`) and session pinning."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.approval import needs_approval
from easycode.config import Config
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider


def make_app(tmp_path: Path) -> TestClient:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    store = SessionStore(
        cfg,
        tmp_path,
        lambda alias, **kw: Agent(
            provider=FakeProvider(script=[]),
            registry=build_registry(8000),
            root=Path(kw.get("root") or tmp_path),
        ),
    )
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def make_agent(tmp_path: Path, script: list[dict]) -> Agent:
    return Agent(provider=FakeProvider(script=script), registry=build_registry(8000), root=tmp_path)


# ------------------------------------------------------------------ tool list


def test_update_todos_is_offered_and_needs_no_approval(tmp_path):
    agent = make_agent(tmp_path, [])
    assert "update_todos" in agent.available_tool_names()
    assert "update_todos" in {s["function"]["name"] for s in agent.tool_schemas()}
    assert "update_todos" in agent._tools_desc()

    from easycode.agent.loop import ToolCall

    call = ToolCall(id="t1", name="update_todos", arguments={"todos": []})
    assert needs_approval(call, agent.path_context(), "ask") is False


async def test_tool_emits_one_todo_event_after_the_batch(tmp_path):
    agent = make_agent(
        tmp_path,
        [
            {
                "text": "",
                "tool_calls": [
                    (
                        "c1",
                        "update_todos",
                        {
                            "todos": [
                                {"text": "读代码", "status": "completed"},
                                {"text": "改代码", "status": "in_progress"},
                            ]
                        },
                    )
                ],
            },
            {"text": "done"},
        ],
    )
    events = [ev async for ev in agent.respond("go")]

    todo = [ev for ev in events if ev.kind == "todo"]
    assert len(todo) == 1
    assert json.loads(todo[0].content or "[]") == [
        {"text": "读代码", "status": "completed"},
        {"text": "改代码", "status": "in_progress"},
    ]
    # the tool result still goes back to the model as a normal tool message
    assert any(ev.kind == "tool_result" for ev in events)


async def test_invalid_todos_become_a_tool_error_result(tmp_path):
    agent = make_agent(
        tmp_path,
        [{"text": "", "tool_calls": [("c1", "update_todos", {"todos": [{"text": "x", "status": "nope"}]})]}, {"text": "fixed"}],
    )
    events = [ev async for ev in agent.respond("go")]
    assert not [ev for ev in events if ev.kind == "todo"]
    result = next(ev.tool_result for ev in events if ev.kind == "tool_result")
    assert '"status": "error"' in (result or "")


# ------------------------------------------------------------ session state


def _sse_events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data:")]


def test_chat_publishes_and_persists_the_task_list(tmp_path):
    client = make_app(tmp_path)
    client.app.state.store.agent_factory = lambda alias, **kw: Agent(
        provider=FakeProvider(
            script=[
                {
                    "text": "",
                    "tool_calls": [
                        (
                            "c1",
                            "update_todos",
                            {"todos": [{"text": "写测试", "status": "pending"}]},
                        )
                    ],
                },
                {"text": "ok"},
            ]
        ),
        registry=build_registry(8000),
        root=tmp_path,
    )
    session = client.app.state.store.create(root=str(tmp_path))

    r = client.post("/api/chat", json={"session_id": session.id, "message": "go"})
    payloads = _sse_events(r.text)

    todo = [p for p in payloads if p["type"] == "todo"]
    assert todo and todo[0]["todos"] == [{"text": "写测试", "status": "pending"}]
    # persisted on the session and returned by the detail endpoint
    assert session.todos == [{"text": "写测试", "status": "pending"}]
    assert client.get(f"/api/sessions/{session.id}").json()["todos"] == [
        {"text": "写测试", "status": "pending"}
    ]
    # and it survives a reload from disk
    reloaded = make_app(tmp_path)
    assert reloaded.get(f"/api/sessions/{session.id}").json()["todos"] == [
        {"text": "写测试", "status": "pending"}
    ]


def test_legacy_session_files_default_to_no_todos_and_unpinned(tmp_path):
    import os

    sessions = tmp_path / ".easycode" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "old.json").write_text(
        json.dumps({"id": "old", "title": "旧会话", "model_alias": "fake-a"}),
        encoding="utf-8",
    )
    client = make_app(tmp_path)

    detail = client.get("/api/sessions/old").json()
    assert detail["todos"] == []
    assert "pinned" not in detail
    # the old file still works after a write
    assert client.post("/api/sessions/old/pin", json={"pinned": True}).status_code == 200
    stored = json.loads((sessions / "old.json").read_text(encoding="utf-8"))
    assert stored["pinned"] is True
    assert stored["todos"] == []
    assert os.path.exists(sessions / "old.json")


# -------------------------------------------------------------------- pinning


def test_pin_and_unpin_round_trip(tmp_path):
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    assert "pinned" not in client.get("/api/sessions").json()[0]

    pinned = client.post(f"/api/sessions/{session.id}/pin", json={"pinned": True}).json()
    assert pinned["pinned"] is True
    assert pinned["pinned_at"]

    listed = next(s for s in client.get("/api/sessions").json() if s["id"] == session.id)
    assert listed["pinned"] is True
    # ...and it is on disk, not just in memory
    reloaded = make_app(tmp_path)
    assert reloaded.get(f"/api/sessions/{session.id}").json()["pinned"] is True

    unpinned = client.post(f"/api/sessions/{session.id}/pin", json={"pinned": False}).json()
    assert "pinned" not in unpinned
    assert client.app.state.store.get(session.id).pinned_at is None


def test_pin_unknown_session_is_404(tmp_path):
    client = make_app(tmp_path)
    assert client.post("/api/sessions/nope/pin", json={"pinned": True}).status_code == 404
