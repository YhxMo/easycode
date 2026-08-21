"""P5.5: agent-level undo/redo, cancellation rollback, Web cancel/undo/redo endpoints."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.models.base import Provider, StreamEvent, ToolCall
from easycode.snapshot import FileSnapshotManager
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from easycode.config import Config

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


class EditProvider(Provider):
    """One turn: edit app.py via edit_file, then answer."""

    def __init__(self, model: str = "fake/model") -> None:
        super().__init__(model)
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="edit_file",
                        arguments={
                            "path": "app.py",
                            "old_string": "hello",
                            "new_string": "hello world",
                        },
                    )
                ],
            )
        else:
            yield StreamEvent(kind="text", content="done editing")
        yield StreamEvent(kind="done")


class SlowProvider(Provider):
    """Streams text after a sleep so the turn can be cancelled mid-flight."""

    def __init__(self, model: str = "fake/model") -> None:
        super().__init__(model)

    async def stream(self, messages, tools=None):
        await asyncio.sleep(0.2)
        yield StreamEvent(kind="text", content="late answer")
        yield StreamEvent(kind="done")


class TwoTurnProvider(Provider):
    """Respond 1: edit hello→hello world then answer. Respond 2: edit again."""

    def __init__(self, model: str = "fake/model") -> None:
        super().__init__(model)
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="edit_file",
                        arguments={
                            "path": "app.py",
                            "old_string": "hello",
                            "new_string": "hello world",
                        },
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(kind="text", content="done")
        elif self.calls == 3:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="edit_file",
                        arguments={
                            "path": "app.py",
                            "old_string": "hello world",
                            "new_string": "hello world!",
                        },
                    )
                ],
            )
        else:
            yield StreamEvent(kind="text", content="done2")
        yield StreamEvent(kind="done")


def _agent(repo: Path, provider) -> Agent:
    agent = Agent(provider=provider, registry=build_registry(8000), root=repo)
    return agent


class Sink:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def run(self, agent: Agent, msg: str) -> None:
        async for ev in agent.respond(msg):
            self.kinds.append(ev.kind)


async def test_agent_undo_redo(repo: Path) -> None:
    agent = _agent(repo, EditProvider())
    agent.snapshot_manager = FileSnapshotManager("s", agent.path_context().roots)

    sink = Sink()
    await sink.run(agent, "edit the file")
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello world')\n"
    assert agent.undo_available()

    summary = agent.undo_turn()
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert not agent.undo_available()
    assert agent.redo_available()

    agent.redo_turn()
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello world')\n"
    assert len(agent.history.messages) == len(sink.kinds)  # messages restored


async def test_agent_undo_working_tree_without_git(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    agent = _agent(tmp_path, EditProvider())
    agent.snapshot_manager = FileSnapshotManager("s", agent.path_context().roots)
    await Sink().run(agent, "edit")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('hello world')\n"  # tool ran
    agent.undo_turn()
    # content snapshots work without a git repository
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert agent.history.last_user_index() == -1
    assert agent.redo_available()
    agent.redo_turn()
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('hello world')\n"


async def test_cancellation_rolls_back_partial_history() -> None:
    agent = _agent(Path("."), SlowProvider())
    agent.snapshot_manager = FileSnapshotManager("s", agent.path_context().roots)
    task = asyncio.create_task(Sink().run(agent, "hello"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # user message from the cancelled turn must be rolled back
    assert agent.history.last_user_index() == -1
    assert not agent.undo_available()


def _web_client(repo: Path) -> tuple[TestClient, SessionStore, Agent]:
    from easycode.web.session import SessionStore as SS

    cfg = Config.load()
    created: list[Agent] = []

    def factory(alias: str = "fake", **kw):
        agent = _agent(repo, EditProvider())
        created.append(agent)
        return agent

    store = SS(cfg, repo, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist"))
    return client, store, created[0]


def test_web_undo_redo_endpoints(repo: Path) -> None:
    client, store, first = _web_client(repo)
    with client:
        r = client.post("/api/chat", json={"message": "edit it"})
        assert r.status_code == 200
        assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello world')\n"

        sid = list(store.list())[0].id
        r = client.post(f"/api/sessions/{sid}/undo")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] and data["restored"]
        assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello')\n"

        detail = client.get(f"/api/sessions/{sid}").json()
        assert all(m["role"] != "user" for m in detail["messages"])

        r = client.post(f"/api/sessions/{sid}/redo")
        assert r.status_code == 200
        assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello world')\n"


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
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            chat = asyncio.create_task(c.post("/api/chat", json={"message": "hi"}))
            await asyncio.sleep(0.08)  # let the chat start streaming
            sid = list(store.list())[0].id
            r = await c.post(f"/api/sessions/{sid}/cancel")
            assert r.status_code == 200
            assert r.json()["cancelled"] is True
            body = (await chat).text
            assert '"type": "cancelled"' in body
            # the interrupted turn's messages were rolled back
            sess = store.get(sid)
            assert sess is not None and sess.agent.history.last_user_index() == -1

    asyncio.run(scenario())


async def test_agent_undo_to_user(repo: Path) -> None:
    """Roll back to before the 1st prompt: both turns' edits reverted, 0 prompts left."""
    agent = _agent(repo, TwoTurnProvider())
    agent.snapshot_manager = FileSnapshotManager("s", agent.path_context().roots)
    await Sink().run(agent, "turn one")
    await Sink().run(agent, "turn two")
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello world!')\n"
    assert agent.history.last_user_index() == 4  # user2 present

    agent.undo_to_user(1)
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    users = [m for m in agent.history.messages if m.get("role") == "user"]
    assert users == []

    agent.undo_to_user(0) if False else None  # noqa
    with pytest.raises(RuntimeError):
        agent.undo_to_user(3)  # out of range


def test_web_undo_until_user(repo: Path) -> None:
    from tests.conftest import FakeProvider

    cfg = Config.load()
    created: list[Agent] = []

    def factory(alias: str = "fake", **kw):
        agent = _agent(repo, TwoTurnProvider())
        created.append(agent)
        return agent

    store = SessionStore(cfg, repo, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist"))
    with client:
        client.post("/api/chat", json={"message": "turn one"})
        sid = list(store.list())[0].id
        client.post("/api/chat", json={"message": "turn two", "session_id": sid})
        assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello world!')\n"

        r = client.post(f"/api/sessions/{sid}/undo", json={"until_user": 1})
        assert r.status_code == 200
        assert r.json()["ok"]
        assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello')\n"

        detail = client.get(f"/api/sessions/{sid}").json()
        users = [m for m in detail["messages"] if m["role"] == "user"]
        assert users == []


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