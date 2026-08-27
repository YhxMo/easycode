"""P5.5: agent-level undo/redo, cancellation rollback, Web cancel/undo/redo endpoints."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from easycode.agent.loop import Agent
from easycode.models.base import Provider, StreamEvent, ToolCall
from easycode.snapshot import FileSnapshotManager
from easycode.tools import build_registry
from easycode.tools.registry import ToolRegistry, tool
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from easycode.config import Config

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Audit isolation: every test runs against a throw-away HOME so the
    SessionStore never reads or writes the real ~/.easycode/."""
    monkeypatch.setenv("HOME", str(tmp_path))


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


def _web_client(repo: Path) -> tuple[TestClient, SessionStore]:
    from easycode.web.session import SessionStore as SS

    cfg = Config.load()

    def factory(alias: str = "fake", **kw):
        return _agent(repo, EditProvider())

    store = SS(cfg, repo, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=repo / "no-dist"))
    return client, store


def test_web_undo_redo_endpoints(repo: Path) -> None:
    client, store = _web_client(repo)
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


# ---------------------------------------------------------------- item-08 (MS-7 / SC-7)


def test_deleted_session_not_resurrected_by_stream(repo: Path) -> None:
    """MS-7/SC-7: deleting a session while its chat stream is still in flight
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
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
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


# ---------------------------------------------------------------- item-09 (MS-6)


class SlowWriteProvider(Provider):
    """Call 1 requests a write_file; the continuation sleeps forever (to cancel)."""

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
                        name="write_file",
                        arguments={"path": "a.txt", "content": "hello"},
                    )
                ],
            )
        else:
            await asyncio.sleep(30)
        yield StreamEvent(kind="done")


class ShellProvider(Provider):
    """Call 1 requests a shell command; the continuation sleeps forever."""

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
                        name="execute_shell",
                        arguments={"command": "echo hi > out.txt"},
                    )
                ],
            )
        else:
            await asyncio.sleep(30)
        yield StreamEvent(kind="done")


def _slow_write_registry() -> ToolRegistry:
    """A write_file tool that sleeps before writing, so it is in-flight when cancelled."""

    class WriteArgs(BaseModel):
        path: str
        content: str

    @tool("write_file", "slow write that sleeps before writing", WriteArgs)
    def _slow_write(args: WriteArgs, *, root: Path, ctx=None, force_allowed=False, grant=None):
        time.sleep(0.5)
        p = root / args.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args.content, encoding="utf-8")
        return json.dumps({"status": "ok", "path": args.path})

    reg = ToolRegistry(8000)
    reg.register(_slow_write)
    return reg


async def _consume(agent: Agent, msg: str) -> None:
    async for _ev in agent.respond(msg):
        pass


async def test_cancel_drains_slow_write_and_rolls_back(tmp_path: Path) -> None:
    """MS-6: a slow write_file that is still in its worker thread when the turn
    is cancelled must be drained before the snapshot pre-state is restored, so
    the late write is rolled back instead of re-dirtying the file."""
    agent = Agent(provider=SlowWriteProvider(), registry=_slow_write_registry(), root=tmp_path)
    agent.snapshot_manager = FileSnapshotManager("s", agent.path_context().roots)
    assert not (tmp_path / "a.txt").exists()

    task = asyncio.create_task(_consume(agent, "write slowly"))
    await asyncio.sleep(0.1)  # write_file is now sleeping in the worker thread
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # the late write landed (thread finished) but was rolled back to pre-state
    assert not (tmp_path / "a.txt").exists()
    assert agent.history.last_user_index() == -1
    assert not agent.undo_available()


async def test_cancel_shell_reports_residual_risk(tmp_path: Path) -> None:
    """MS-6: a shell command that already ran cannot have its file effects
    rolled back; the cancellation must surface that residual risk instead of
    pretending the turn was cleanly undone."""
    agent = Agent(provider=ShellProvider(), registry=build_registry(8000), root=tmp_path)
    agent.snapshot_manager = FileSnapshotManager("s", agent.path_context().roots)
    assert not agent._shell_tools_ran

    task = asyncio.create_task(_consume(agent, "write via shell"))
    await asyncio.sleep(0.3)  # shell already wrote out.txt; continuation sleeps
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # the shell wrote the file and it is NOT rolled back (untracked side effect)
    assert (tmp_path / "out.txt").exists()
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "hi"
    # the turn's partial history was rolled back, but the residual risk is reported
    assert agent.history.last_user_index() == -1
    assert agent._last_cancel_note is not None
    assert "shell" in agent._last_cancel_note
    assert "回滚" in agent._last_cancel_note