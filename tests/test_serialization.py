"""Concurrent chat, permission changes, and session persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.models.base import Provider
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.store import SessionStore
from tests.helpers_web import GateProvider, wait_until


def _make_cfg(tmp_path: Path) -> Config:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    return cfg


def _agent(tmp_path: Path, provider: Provider, root: Path | None = None) -> Agent:
    return Agent(provider=provider, registry=build_registry(8000), root=root or tmp_path)


def test_concurrent_chat_second_409_and_history_valid(tmp_path) -> None:
    """A second chat on the same session while one is streaming must
    be rejected with 409 (busy) instead of interleaving history; after the first
    turn the history is one complete, decomposable tool-calling turn."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    cfg = _make_cfg(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        def factory(alias: str = "fake-a", **_):
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
            await wait_until(lambda: len(store.list()) == 1)
            sid = list(store.list())[0].id
            # Ensure the stream's gen() has actually acquired the session lock.
            await wait_until(store.get(sid)._lock.locked)
            # While chat1 streams (holds the session lock), chat2 must be busy.
            r2 = await c.post("/api/chat", json={"message": "second chat", "session_id": sid})
            assert r2.status_code == 409, r2.text
            assert "busy" in r2.json()["detail"]

            gate.set()
            r1 = await chat1
            assert r1.status_code == 200, r1.text

            sess = store.get(sid)
            assert sess is not None
            roles = [m.get("role") for m in sess.agent.history.messages]
            # A complete tool-calling turn: user -> assistant(tool_calls) -> tool -> assistant.
            assert roles == ["user", "assistant", "tool", "assistant"], roles
            assert [t["raw_input"] for t in sess.turns] == ["first chat"]
            # The detail route serves the conversation the turns record; the
            # context cache (equal here, since nothing was compacted) is separate.
            fetched = (await c.get(f"/api/sessions/{sid}")).json()
            assert [m["content"] for m in fetched["messages"] if m["role"] == "user"] == [
                "first chat"
            ]

    asyncio.run(scenario())


def test_inflight_chat_permission_archive_409(tmp_path) -> None:
    """While a chat stream is in flight, permission/archive
    and a second chat must be rejected with 409; once the turn completes the
    history is a single clean turn with user_times aligned."""
    cfg = _make_cfg(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        def factory(alias: str = "fake-a", **_):
            return _agent(tmp_path, GateProvider(gate=gate, script=[{"text": "done"}]))

        store = SessionStore(cfg, tmp_path, factory)
        app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await wait_until(lambda: len(store.list()) == 1)
            sid = list(store.list())[0].id
            await wait_until(store.get(sid)._lock.locked)

            for method, url, body in (
                (
                    "POST",
                    f"/api/sessions/{sid}/permission",
                    {"mode": "allow-all", "confirm_full_access": True},
                ),
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
            roles = [m.get("role") for m in sess.agent.history.messages]
            assert roles == ["user", "assistant"], roles
            # The conversation is recorded as one complete turn.
            assert [t["raw_input"] for t in sess.turns] == ["hi"]
            assert sess.turns[0]["status"] == "completed"

    asyncio.run(scenario())


def test_prune_does_not_mutate_recorded_snapshot(tmp_path) -> None:
    """DEC-W2: the recorded snapshot shares message dicts with the live
    history, so pruning must replace message objects, not edit them in place."""
    from easycode.agent.compaction import PRUNED_OUTPUT

    cfg = _make_cfg(tmp_path)

    def factory(alias: str = "fake-a", **_):
        from tests.conftest import FakeProvider

        return _agent(tmp_path, FakeProvider(script=[]))

    store = SessionStore(cfg, tmp_path, factory)
    sess = store.create()
    h = sess.agent.history
    big = "x" * 200_000
    h.add_user("first")
    h.add_assistant(
        "", [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    )
    h.add_tool("c1", "read_file", big)
    h.add_assistant("done")
    h.add_user("second")
    h.add_assistant("ok")
    h.add_user("third")
    h.add_assistant("ok")
    store.record_exchange(sess)
    snapshot_before = list(sess.messages)

    sess.agent.compactor.prune(h)

    # The old snapshot keeps the original tool output...
    assert sess.messages == snapshot_before
    assert big in sess.messages[2]["content"]
    # ...while the live history has the pruned copy.
    assert h.messages[2]["content"] == PRUNED_OUTPUT
    assert h.messages[2] is not sess.messages[2]

    # Persisting again records the pruned view.
    store.record_exchange(sess)
    assert PRUNED_OUTPUT in sess.messages[2]["content"]


def test_cancel_during_inflight_not_gated(tmp_path) -> None:
    """A3: cancel is the deliberate exception — it does NOT grab the session lock
    and must be allowed while a chat is streaming (it just signals cancel_event)."""
    cfg = _make_cfg(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        def factory(alias: str = "fake-a", **_):
            return _agent(tmp_path, GateProvider(gate=gate, script=[{"text": "late"}]))

        store = SessionStore(cfg, tmp_path, factory)
        app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await wait_until(lambda: len(store.list()) == 1)
            sid = list(store.list())[0].id
            await wait_until(store.get(sid)._lock.locked)

            r_cancel = await c.post(f"/api/sessions/{sid}/cancel")
            assert r_cancel.status_code == 200, r_cancel.text
            assert r_cancel.json()["cancelled"] is True

            body = (await chat).text
            assert '"type": "cancelled"' in body
            sess = store.get(sid)
            assert sess is not None and sess.agent.history.messages == [{"role": "user", "content": "hi"}]

    asyncio.run(scenario())


def test_flush_concurrent_unique_tmp_and_atomic(tmp_path) -> None:
    """A flush must never share a .tmp path (concurrent flushes write to
    distinct temp files), and concurrent flushes leave a complete, valid session
    file on disk."""
    cfg = _make_cfg(tmp_path)

    def factory(alias: str = "fake-a", **_):
        from tests.conftest import FakeProvider

        return _agent(tmp_path, FakeProvider(script=[{"text": "ok"}]))

    store = SessionStore(cfg, tmp_path, factory)
    s = store.create()
    sid = s.id

    # Two tmp paths for the same session are always distinct (unique suffix).
    from easycode.web.persistence import tmp_path

    p1 = tmp_path(store._path(sid), sid)
    p2 = tmp_path(store._path(sid), sid)
    assert p1 != p2

    async def flush_many() -> None:
        await asyncio.gather(*(asyncio.to_thread(store._flush, s) for _ in range(8)))

    asyncio.run(flush_many())

    data = json.loads(store._path(sid).read_text(encoding="utf-8"))
    assert data["id"] == sid
    assert data["title"] == s.title
