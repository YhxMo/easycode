"""The task list (`update_todos`) and session pinning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.permissions.approval import needs_approval
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


async def test_tool_announces_the_list_right_after_its_own_result(tmp_path):
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
    # announced after that call's own result, never between a call and its outcome
    kinds = [ev.kind for ev in events]
    assert kinds.index("todo") == kinds.index("tool_result") + 1
    assert agent.todos == [
        {"text": "读代码", "status": "completed"},
        {"text": "改代码", "status": "in_progress"},
    ]


async def test_committed_list_survives_a_cancelled_batch(tmp_path):
    """The reported bug: the list was queued for after the whole batch, so a
    batch cancelled while waiting on a later approval dropped it even though
    its own tool result was already a success."""
    import asyncio

    from tests.conftest import FakeProvider
    from tests.helpers_history import assert_valid_tool_protocol

    requested = asyncio.Event()

    async def approve(tc, _reason, _key):
        requested.set()
        await asyncio.Event().wait()

    agent = Agent(
        provider=FakeProvider(
            script=[
                {
                    "tool_calls": [
                        (
                            "todos",
                            "update_todos",
                            {"todos": [{"text": "写代码", "status": "in_progress"}]},
                        ),
                        ("write", "write_file", {"path": "out.txt", "content": "x"}),
                    ]
                }
            ]
        ),
        registry=build_registry(8000),
        root=tmp_path,
        permission_rules={"write_file": "ask"},
        approval_handler=approve,
    )

    seen = []

    async def consume():
        async for event in agent.respond("go"):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(requested.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    results = [json.loads(m["content"]) for m in agent.history.messages if m["role"] == "tool"]
    assert results[0]["status"] == "ok"
    assert agent.todos == [{"text": "写代码", "status": "in_progress"}]
    assert [ev.kind for ev in seen if ev.kind == "todo"]
    assert not (tmp_path / "out.txt").exists()
    assert_valid_tool_protocol(agent.history.messages)


async def test_cleared_list_is_committed_and_announced(tmp_path):
    agent = make_agent(tmp_path, [])
    agent.todos = [{"text": "旧的", "status": "pending"}]
    from easycode.agent.builtin_tools import run_builtin
    from easycode.agent.loop import ToolCall

    result = await run_builtin(agent, ToolCall(id="c1", name="update_todos", arguments={"todos": []}))
    assert json.loads(result)["status"] == "ok"
    assert agent.todos == []
    assert agent._todos_event("update_todos", result) is not None


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


async def test_cancelled_turn_still_persists_the_committed_list(tmp_path):
    """Cancel while a later call in the same batch waits for approval: the
    list update_todos already committed must survive the interrupted turn and
    still be there when the session is reopened."""
    import asyncio

    import httpx

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias="fake-a", **kw):
        return Agent(
            provider=FakeProvider(
                script=[
                    {
                        "tool_calls": [
                            (
                                "todos",
                                "update_todos",
                                {"todos": [{"text": "写代码", "status": "in_progress"}]},
                            ),
                            ("write", "write_file", {"path": "out.txt", "content": "x"}),
                        ]
                    }
                ]
            ),
            registry=build_registry(8000),
            root=Path(kw.get("root") or tmp_path),
            permission_rules={"write_file": "ask"},
        )

    store = SessionStore(cfg, tmp_path, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    session = store.create(root=str(tmp_path))

    async def scenario() -> str:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(
                c.post("/api/chat", json={"session_id": session.id, "message": "go"})
            )
            # wait until the batch reached the approval its second call needs
            for _ in range(200):
                await asyncio.sleep(0.01)
                if session.agent.todos:
                    break
            assert session.agent.todos == [{"text": "写代码", "status": "in_progress"}]
            r = await c.post(f"/api/sessions/{session.id}/cancel")
            assert r.json()["cancelled"] is True
            return (await chat).text

    body = await scenario()
    assert '"type": "cancelled"' in body
    assert not (tmp_path / "out.txt").exists()

    stored = json.loads((store.dir / f"{session.id}.json").read_text(encoding="utf-8"))
    assert stored["todos"] == [{"text": "写代码", "status": "in_progress"}]

    reloaded = make_app(tmp_path)
    assert reloaded.get(f"/api/sessions/{session.id}").json()["todos"] == [
        {"text": "写代码", "status": "in_progress"}
    ]
