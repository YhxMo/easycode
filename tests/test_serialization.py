"""Web session serialization — concurrent chat/undo/redo/permission gating,
cancel special-case, and flush tmp-path uniqueness.

Covers same-session concurrent chat (which would interleave history) and
in-flight undo/redo (which would corrupt history) at the web endpoint level.
Every test runs against a throw-away HOME and an in-process ASGI app; no real
~/.easycode is read or written, and no easycode service is started.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.models.base import Provider, StreamEvent, ToolCall
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


class GateProvider(Provider):
    """One provider shared by an agent's turn.

    ``stream`` call #1 waits on ``gate`` before yielding (so the session lock is
    held deterministically while the test issues concurrent requests), then serves
    the scripted items in order. Later calls skip the gate.
    """

    def __init__(self, gate: asyncio.Event, script: list[dict], model: str = "fake/model") -> None:
        super().__init__(model)
        self.gate = gate
        self.script = list(script)
        self.calls = 0

    async def stream(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.gate is not None and self.calls == 1:
            await self.gate.wait()
        if not self.script:
            yield StreamEvent(kind="text", content="ok")
            yield StreamEvent(kind="done")
            return
        item = self.script.pop(0)
        if item.get("text"):
            for token in item["text"]:
                yield StreamEvent(kind="text", content=token)
        if item.get("tool_calls"):
            calls = [ToolCall(id=tc[0], name=tc[1], arguments=tc[2]) for tc in item["tool_calls"]]
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
        yield StreamEvent(kind="done")


def _make_cfg(tmp_path: Path) -> Config:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    return cfg


def _agent(tmp_path: Path, provider: Provider, root: Path | None = None) -> Agent:
    return Agent(provider=provider, registry=build_registry(8000), root=root or tmp_path)


async def _wait_until(pred, timeout: float = 2.0) -> None:
    """Yields to the running loop until ``pred`` is true (or timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


def test_concurrent_chat_second_409_and_history_valid(tmp_path) -> None:
    """A second chat on the same session while one is streaming must
    be rejected with 409 (busy) instead of interleaving history; after the first
    turn the history is one complete, decomposable tool-calling turn."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    cfg = _make_cfg(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        def factory(alias: str = "fake-a"):
            return _agent(
                tmp_path,
                GateProvider(
                    gate=gate,
                    script=[
                        {"tool_calls": [("c1", "glob", {"pattern": "*.py"})], "text": ""},
                        {"text": "searching"},
                    ],
                ),
            )

        store = SessionStore(cfg, tmp_path, factory)
        app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat1 = asyncio.create_task(c.post("/api/chat", json={"message": "first chat"}))
            await _wait_until(lambda: len(store.list()) == 1)
            sid = list(store.list())[0].id
            # Ensure the stream's gen() has actually acquired the session lock.
            await _wait_until(store.get(sid)._lock.locked)
            # While chat1 streams (holds the session lock), chat2 must be busy.
            r2 = await c.post("/api/chat", json={"message": "second chat", "session_id": sid})
            assert r2.status_code == 409, r2.text
            assert "busy" in r2.json()["detail"]

            gate.set()
            r1 = await chat1
            assert r1.status_code == 200, r1.text

            sess = store.get(sid)
            assert sess is not None
            roles = [m.get("role") for m in sess.messages]
            # A complete tool-calling turn: user -> assistant(tool_calls) -> tool -> assistant.
            assert roles == ["user", "assistant", "tool", "assistant"], roles
            n_user = sum(1 for m in sess.messages if m.get("role") == "user")
            assert len(sess.user_times) == n_user == 1
            fetched = (await c.get(f"/api/sessions/{sid}")).json()
            assert fetched["messages"] == sess.messages

    asyncio.run(scenario())


def test_inflight_chat_undo_redo_permission_archive_409(tmp_path) -> None:
    """While a chat stream is in flight, undo/redo/permission/archive
    and a second chat must be rejected with 409; once the turn completes the
    history is a single clean turn with user_times aligned."""
    cfg = _make_cfg(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        def factory(alias: str = "fake-a"):
            return _agent(tmp_path, GateProvider(gate=gate, script=[{"text": "done"}]))

        store = SessionStore(cfg, tmp_path, factory)
        app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await _wait_until(lambda: len(store.list()) == 1)
            sid = list(store.list())[0].id
            await _wait_until(store.get(sid)._lock.locked)

            for method, url, body in (
                ("POST", f"/api/sessions/{sid}/undo", None),
                ("POST", f"/api/sessions/{sid}/redo", None),
                ("POST", f"/api/sessions/{sid}/permission", {"mode": "allow-all"}),
                ("POST", f"/api/sessions/{sid}/archive", {"archived": True}),
                ("POST", "/api/chat", {"message": "x", "session_id": sid}),
            ):
                r = await c.request(method, url, json=body) if body is not None else await c.request(method, url)
                assert r.status_code == 409, (url, r.status_code, r.text)
                assert "busy" in r.json()["detail"], url

            gate.set()
            r1 = await chat
            assert r1.status_code == 200, r1.text

            sess = store.get(sid)
            assert sess is not None
            roles = [m.get("role") for m in sess.messages]
            assert roles == ["user", "assistant"], roles
            n_user = sum(1 for m in sess.messages if m.get("role") == "user")
            assert len(sess.user_times) == n_user == 1

    asyncio.run(scenario())


def test_cancel_during_inflight_not_gated(tmp_path) -> None:
    """A3: cancel is the deliberate exception — it does NOT grab the session lock
    and must be allowed while a chat is streaming (it just signals cancel_event)."""
    cfg = _make_cfg(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        def factory(alias: str = "fake-a"):
            return _agent(tmp_path, GateProvider(gate=gate, script=[{"text": "late"}]))

        store = SessionStore(cfg, tmp_path, factory)
        app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await _wait_until(lambda: len(store.list()) == 1)
            sid = list(store.list())[0].id
            await _wait_until(store.get(sid)._lock.locked)

            r_cancel = await c.post(f"/api/sessions/{sid}/cancel")
            assert r_cancel.status_code == 200, r_cancel.text
            assert r_cancel.json()["cancelled"] is True

            body = (await chat).text
            assert '"type": "cancelled"' in body
            sess = store.get(sid)
            assert sess is not None and sess.agent.history.last_user_index() == -1

    asyncio.run(scenario())


def test_flush_concurrent_unique_tmp_and_atomic(tmp_path) -> None:
    """_flush must never share a .tmp path (concurrent flushes write to
    distinct temp files), and concurrent flushes leave a complete, valid session
    file on disk."""
    cfg = _make_cfg(tmp_path)

    def factory(alias: str = "fake-a"):
        from tests.conftest import FakeProvider

        return _agent(tmp_path, FakeProvider(script=[{"text": "ok"}]))

    store = SessionStore(cfg, tmp_path, factory)
    s = store.create()
    sid = s.id

    # Two tmp paths for the same session are always distinct (unique suffix).
    p1 = store._tmp_path(sid)
    p2 = store._tmp_path(sid)
    assert p1 != p2

    async def flush_many() -> None:
        await asyncio.gather(*(asyncio.to_thread(store._flush, s) for _ in range(8)))

    asyncio.run(flush_many())

    data = json.loads(store._path(sid).read_text(encoding="utf-8"))
    assert data["id"] == sid
    assert data["title"] == s.title
