from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.models.base import Provider, StreamEvent
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Audit isolation: every test runs against a throw-away HOME so the
    SessionStore never reads or writes the real ~/.easycode/."""
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return root


class SlowProvider(Provider):
    """Streams text after a sleep so the turn can be cancelled mid-flight."""

    def __init__(self, model: str = "fake/model") -> None:
        super().__init__(model)

    async def stream(self, messages, tools=None):
        await asyncio.sleep(0.2)
        yield StreamEvent(kind="text", content="late answer")
        yield StreamEvent(kind="done")


def _agent(repo: Path, provider) -> Agent:
    return Agent(provider=provider, registry=build_registry(8000), root=repo)


def test_web_cancel_endpoint(repo: Path) -> None:
    """Cancel endpoint stops an in-flight chat; same event loop (ASGI transport)."""
    import httpx

    from easycode.web.session import SessionStore as SS

    cfg = Config.load()
    created: list[Agent] = []

    def factory(alias: str = "fake", **kw):
        agent = _agent(repo, SlowProvider())
        created.append(agent)
        return agent

    store = SS(cfg, repo, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await asyncio.sleep(0.08)  # let the chat start streaming
            sid = list(store.list())[0].id
            r = await c.post(f"/api/sessions/{sid}/cancel")
            assert r.status_code == 200
            assert r.json()["cancelled"] is True
            body = (await chat).text
            assert '"type": "cancelled"' in body
            # the interrupted prompt remains available for a follow-up
            sess = store.get(sid)
            assert sess is not None and sess.agent.history.messages[0] == {
                "role": "user",
                "content": "hi",
            }

    asyncio.run(scenario())


def test_deleted_session_not_resurrected_by_stream(repo: Path) -> None:
    """deleting a session while its chat stream is still in flight
    must not let the stream's ``finally`` re-write the session to disk. After
    the stream ends the .json is gone and ``load_all`` finds nothing."""
    import httpx

    from easycode.web.session import SessionStore as SS

    cfg = Config.load()
    created: list[Agent] = []

    def factory(alias: str = "fake", **kw):
        agent = _agent(repo, SlowProvider())
        created.append(agent)
        return agent

    store = SS(cfg, repo, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await asyncio.sleep(0.05)  # let the chat start streaming
            sid = list(store.list())[0].id
            path = store._path(sid)
            assert path.exists()
            r = await c.delete(f"/api/sessions/{sid}")
            assert r.status_code == 200
            assert not path.exists()
            # stream completes after deletion; it must NOT recreate the file
            await chat
            assert not path.exists()
            # restart-time load_all must find an empty store (no resurrection)
            store.load_all()
            assert store.get(sid) is None

    asyncio.run(scenario())


def test_first_chat_yields_session_event(repo: Path) -> None:
    from tests.conftest import FakeProvider

    cfg = Config.load()
    created: list[Agent] = []

    def factory(alias: str = "fake", **kw):
        agent = Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=repo,
        )
        created.append(agent)
        return agent

    store = SessionStore(cfg, repo, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist"))
    with client:
        r = client.post("/api/chat", json={"message": "hi"})
        body = r.text
        assert '"type": "session"' in body and '"session_id"' in body


async def test_cancel_preserves_completed_edit_and_can_continue(tmp_path):
    from tests.conftest import FakeProvider
    from tests.helpers_history import assert_valid_tool_protocol

    waiting = asyncio.Event()

    class EditThenWait(FakeProvider):
        async def stream(self, messages, tools=None):
            if len(self.calls) == 1:
                waiting.set()
                await asyncio.Event().wait()
            async for event in super().stream(messages, tools):
                yield event

    provider = EditThenWait(
        script=[
            {
                "tool_calls": [("edit", "write_file", {"path": "kept.txt", "content": "saved"})],
            }
        ]
    )
    agent = _agent(tmp_path, provider)

    async def consume():
        return [event async for event in agent.respond("write a file")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "kept.txt").read_text() == "saved"
    assert_valid_tool_protocol(agent.history.messages)
    assert any(m.get("tool_call_id") == "edit" for m in agent.history.messages)

    agent.provider = FakeProvider(script=[{"text": "The edit is still present."}])
    events = [event async for event in agent.respond("continue")]
    assert events[-1].kind == "done"
    assert_valid_tool_protocol(agent.provider.calls[0])


async def test_cancel_during_approval_completes_batch_without_running_tools(tmp_path):
    import json

    from tests.conftest import FakeProvider
    from tests.helpers_history import assert_valid_tool_protocol

    requested = asyncio.Event()

    async def approve(tc):
        requested.set()
        await asyncio.Event().wait()

    agent = Agent(
        provider=FakeProvider(
            script=[
                {
                    "tool_calls": [
                        ("first", "write_file", {"path": "first.txt", "content": "first"}),
                        ("second", "write_file", {"path": "second.txt", "content": "second"}),
                    ]
                }
            ]
        ),
        registry=build_registry(8000),
        root=tmp_path,
        permission_rules={"write_file": "ask"},
        approval_handler=approve,
    )

    async def consume():
        return [event async for event in agent.respond("write")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(requested.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
    assert_valid_tool_protocol(agent.history.messages)
    results = [m for m in agent.history.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["first", "second"]
    assert all(json.loads(m["content"])["status"] == "error" for m in results)


async def test_closing_stream_keeps_valid_tool_history(tmp_path):
    from tests.conftest import FakeProvider
    from tests.helpers_history import assert_valid_tool_protocol

    agent = _agent(
        tmp_path,
        FakeProvider(
            script=[
                {
                    "tool_calls": [
                        ("first", "write_file", {"path": "first.txt", "content": "first"}),
                        ("second", "write_file", {"path": "second.txt", "content": "second"}),
                    ]
                }
            ]
        ),
    )
    stream = agent.respond("write")
    async for event in stream:
        if event.kind == "tool_result":
            break
    await stream.aclose()
    assert (tmp_path / "first.txt").read_text() == "first"
    assert not (tmp_path / "second.txt").exists()
    assert_valid_tool_protocol(agent.history.messages)


def test_removed_undo_routes_are_unavailable(repo):
    cfg = Config.load()
    store = SessionStore(cfg, repo, lambda alias: _agent(repo, SlowProvider()))
    with TestClient(
        create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist")
    ) as client:
        session = store.create()
        assert client.post(f"/api/sessions/{session.id}/undo").status_code == 404
        assert client.post(f"/api/sessions/{session.id}/redo").status_code == 404


async def test_running_sync_tool_may_finish_after_stop(tmp_path):
    import threading

    from pydantic import BaseModel

    from easycode.tools.registry import Tool, ToolRegistry
    from tests.conftest import FakeProvider
    from tests.helpers_history import assert_valid_tool_protocol

    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class Args(BaseModel):
        pass

    def slow_write(args, *, root, ctx, grant):
        loop.call_soon_threadsafe(started.set)
        try:
            if release.wait(timeout=2):
                (root / "late.txt").write_text("completed")
            return {"status": "ok"}
        finally:
            loop.call_soon_threadsafe(finished.set)

    registry = ToolRegistry()
    registry.register(Tool("slow_write", "Write after release", Args, slow_write))
    agent = Agent(
        provider=FakeProvider(
            script=[
                {
                    "tool_calls": [("slow", "slow_write", {})],
                }
            ]
        ),
        registry=registry,
        root=tmp_path,
    )

    async def consume():
        return [event async for event in agent.respond("write")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (tmp_path / "late.txt").exists()
        assert_valid_tool_protocol(agent.history.messages)
        assert "effects may remain" in agent.history.messages[-1]["content"]
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
    assert (tmp_path / "late.txt").read_text() == "completed"
