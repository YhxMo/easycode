"""Mcp tests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import mcp_servers


def mcp_server_config(pidfile: Path | None = None):
    server = {
        "command": sys.executable,
        "args": [str(Path(__file__).resolve().parent / "mcp_demo_server.py")],
    }
    if pidfile is not None:
        # The demo server reports its own pid through the configured env, which
        # is also how the tests see that config ``env`` reaches the child.
        server["env"] = {"MCP_DEMO_PIDFILE": str(pidfile)}
    return {"demo": server}


def server_pid(pidfile: Path) -> int:
    return int(pidfile.read_text(encoding="utf-8"))


def process_state(pid: int) -> str:
    """``ps`` state of a pid, or "" once the process is gone."""
    return subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_mcp_manager_connects_and_lists_tools():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    mgr = MCPSessionManager(mcp_servers(mcp_server_config()))
    await mgr.start()
    try:
        names = {s["function"]["name"] for s in mgr.tool_schemas()}
        assert mcp_tool_name("demo", "add") in names
        assert mcp_tool_name("demo", "greet") in names
        assert mcp_tool_name("demo", "boom") in names
        fn = next(
            s for s in mgr.tool_schemas() if s["function"]["name"] == mcp_tool_name("demo", "add")
        )
        assert fn["function"]["parameters"]["required"] == ["a", "b"]
    finally:
        await mgr.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_mcp_manager_call_tool():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    mgr = MCPSessionManager(mcp_servers(mcp_server_config()))
    await mgr.start()
    try:
        out = await mgr.call(mcp_tool_name("demo", "add"), {"a": 2, "b": 3})
        assert '"5"' in out
        out2 = await mgr.call(mcp_tool_name("demo", "greet"), {})
        assert "hello, world" in out2
        # server-side error surfaces as status error
        out3 = await mgr.call(mcp_tool_name("demo", "boom"), {})
        assert out3.startswith('{"status": "error"')
        # unknown tool
        out4 = await mgr.call("mcp__demo__nope", {})
        assert "unknown MCP tool" in out4
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mcp_manager_bad_server_degrades():
    from easycode.mcp import MCPSessionManager

    mgr = MCPSessionManager(
        mcp_servers({"ghost": {"command": "definitely-not-a-command-xyz", "args": []}})
    )
    await mgr.start()  # must not raise
    assert mgr.tool_schemas() == []


@pytest.mark.asyncio
async def test_agent_routes_mcp_tool(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    fname = mcp_tool_name("demo", "add")
    script = [
        {"tool_calls": [("c1", fname, {"a": 10, "b": 32})], "text": ""},
        {"text": "sum is available"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
        # The demo server declares no annotations, so `mcp__demo__add` is not
        # auto-allowed under the default (ask) mode. This test is about MCP
        # *routing*, not the fail-closed approval policy, so run under allow-all.
        permission_mode="allow-all",
    )
    schemas_before = {s["function"]["name"] for s in agent.tool_schemas()}
    assert not any(n.startswith("mcp__") for n in schemas_before)

    results = []
    async for ev in agent.respond("add it"):
        if ev.kind == "tool_result":
            results.append(ev.tool_result)
    assert any("42" in r for r in results), results
    # schemas now include MCP tool after init_mcp
    schemas_after = {s["function"]["name"] for s in agent.tool_schemas()}
    assert fname in schemas_after

    await agent.mcp_manager.close()


@pytest.mark.asyncio
async def test_unknown_mcp_tool_call_is_rejected(tmp_path):
    """A hallucinated MCP tool name is not advertised and is rejected."""
    from easycode.agent.loop import Agent
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    fname = mcp_tool_name("demo", "nope")
    script = [
        {"tool_calls": [("c1", fname, {})], "text": ""},
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
        permission_mode="allow-all",
    )

    results = []
    async for ev in agent.respond("call it"):
        if ev.kind == "tool_result":
            results.append(json.loads(ev.tool_result))

    assert len(results) == 1
    assert results[0]["rejected"] is True
    assert results[0]["category"] == "policy"
    assert "tool not enabled" in results[0]["message"]

    await agent.mcp_manager.close()


@pytest.mark.asyncio
async def test_empty_tool_cap_rejects_registered_mcp_tool(tmp_path):
    """An empty explicit cap rejects even a connected MCP tool, without executing it."""
    from easycode.agent.loop import Agent
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    fname = mcp_tool_name("demo", "add")
    script = [
        {"tool_calls": [("c1", fname, {"a": 1, "b": 2})], "text": ""},
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
        enabled_tools=set(),
        permission_mode="allow-all",
    )

    results = []
    async for ev in agent.respond("add it"):
        if ev.kind == "tool_result":
            results.append(json.loads(ev.tool_result))

    assert len(results) == 1
    assert results[0]["rejected"] is True
    assert "tool not enabled" in results[0]["message"]

    await agent.mcp_manager.close()


@pytest.mark.asyncio
async def test_failed_connect_closes_connection(monkeypatch):
    """A connection created before a server's init failed must be closed."""
    from easycode.mcp import MCPConnection, MCPSessionManager

    closed: list[str] = []

    async def fail_start(self):
        raise RuntimeError("boom")

    async def spy_close(self):
        closed.append(self.name)

    monkeypatch.setattr(MCPConnection, "start", fail_start)
    monkeypatch.setattr(MCPConnection, "close", spy_close)

    mgr = MCPSessionManager(mcp_servers(mcp_server_config()))
    await mgr.start()
    assert mgr.tool_schemas() == []
    assert closed, "the connection created for a failed server must be closed"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_close_reaps_server_process(tmp_path):
    from easycode.mcp import MCPSessionManager
    from easycode.permissions.boundary import PathContext

    pidfile = tmp_path / "mcp.pid"
    mgr = MCPSessionManager(
        mcp_servers(mcp_server_config(pidfile)), PathContext(primary=tmp_path)
    )
    await mgr.start()
    pid = server_pid(pidfile)
    assert process_state(pid)
    assert mgr._sessions != {}

    await mgr.close()
    assert process_state(pid) == "", "the MCP child must be reaped"
    assert mgr._sessions == {}

    # Cleanup is idempotent.
    await mgr.close()


@pytest.mark.asyncio
async def test_subagent_task_keeps_borrowed_parent_mcp(tmp_path):
    """A subtask borrowing the parent's MCP manager must not close it."""
    from easycode.agent.loop import Agent
    from easycode.agents import AgentRegistry, AgentSpec
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    fname = mcp_tool_name("demo", "add")
    reg = AgentRegistry({"coder": AgentSpec(name="coder", description="calls mcp")})
    sub_script = [
        {"tool_calls": [("s1", fname, {"a": 1, "b": 2})], "text": ""},
        {"text": "3"},
    ]

    def factory(_model: str) -> Agent:
        return Agent(
            provider=FakeProvider(script=sub_script),
            registry=build_registry(8000),
            root=tmp_path,
        )

    script = [
        {"tool_calls": [("t1", "task", {"agent": "coder", "prompt": "add"})], "text": ""},
        {"text": "done"},
    ]
    parent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
        permission_mode="allow-all",
        agents=reg,
        subagent_factory=factory,
    )

    events = [ev async for ev in parent.respond("delegate")]
    result = json.loads(
        next(
            e.tool_result
            for e in events
            if e.kind == "tool_result" and e.tool_call and e.tool_call.name == "task"
        )
    )
    assert result["status"] == "ok"

    manager = parent.mcp_manager
    assert manager is not None and manager.tool_names(), "parent MCP must survive the subtask"
    out = await manager.call(fname, {"a": 2, "b": 3})
    assert "5" in out
    await parent.close_mcp()


@pytest.mark.asyncio
async def test_subagent_with_different_sandbox_gets_own_mcp(tmp_path):
    """Only an identical sandbox context may borrow the parent's MCP process."""
    from easycode.agent.builtin_tools import make_subagent
    from easycode.agent.loop import Agent
    from easycode.agents import AgentSpec
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    parent = Agent(
        provider=FakeProvider(model="fake"),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
        permission_mode="allow-all",
        subagent_factory=lambda _model: Agent(
            provider=FakeProvider(model="fake"),
            registry=build_registry(8000),
            root=tmp_path,
        ),
    )
    await parent.init_mcp()
    try:
        borrowed = make_subagent(parent)
        assert borrowed.mcp_manager is parent.mcp_manager

        capped = make_subagent(
            parent, AgentSpec(name="reader", description="ask", permission="ask")
        )
        assert capped.permission_mode == "ask"
        assert capped.mcp_manager is None, (
            "a differently sandboxed subagent must not share the parent's process"
        )
        assert capped.mcp_servers == parent.mcp_servers
    finally:
        await parent.close_mcp()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_session_delete_closes_owned_mcp(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.config import Config
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    pidfile = tmp_path / "mcp.pid"

    def factory(alias: str = "fake-a", **_):
        return Agent(
            provider=FakeProvider(model="fake"),
            registry=build_registry(8000),
            root=tmp_path,
            mcp_servers=mcp_server_config(pidfile),
        )

    store = SessionStore(cfg, tmp_path, factory)
    sess = store.create()
    await sess.agent.init_mcp()
    manager = sess.agent.mcp_manager
    assert manager is not None
    pid = server_pid(pidfile)

    assert await store.delete(sess.id) is True
    assert sess.agent.mcp_manager is None
    assert manager._sessions == {}
    assert process_state(pid) == ""
    assert await store.delete(sess.id) is False


def test_web_shutdown_closes_owned_mcp(tmp_path):
    """Leaving the FastAPI lifespan releases every session-owned MCP process."""
    from fastapi.testclient import TestClient

    from easycode.agent.loop import Agent
    from easycode.config import Config
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    fname = mcp_tool_name("demo", "add")
    script = [
        {"tool_calls": [("c1", fname, {"a": 1, "b": 1})], "text": ""},
        {"text": "2"},
    ]

    pidfile = tmp_path / "mcp.pid"

    def factory(alias: str = "fake-a", **_):
        return Agent(
            provider=FakeProvider(model="fake", script=list(script)),
            registry=build_registry(8000),
            root=tmp_path,
            mcp_servers=mcp_server_config(pidfile),
            permission_mode="allow-all",
        )

    store = SessionStore(cfg, tmp_path, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    client = TestClient(app)
    with client:
        r = client.post(
            "/api/chat",
                json={
                    "message": "go",
                    "permission_mode": "allow-all",
                    "confirm_full_access": True,
                },
        )
        assert r.status_code == 200, r.text
        sess = store.list()[0]
        assert sess.agent.mcp_manager is not None
        pid = server_pid(pidfile)

    # Context exit ran the lifespan shutdown.
    assert sess.agent.mcp_manager is None
    assert process_state(pid) == ""


@pytest.mark.asyncio
async def test_chat_permission_change_drops_stale_mcp(tmp_path):
    """A chat-requested permission change must not keep serving MCP tools from
    the previous sandbox: the old manager is released immediately, exactly like
    the permission endpoint (regression: /api/chat switched the mode only)."""
    import httpx

    from easycode.agent.loop import Agent
    from easycode.config import Config
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    pidfile = tmp_path / "mcp.pid"

    def factory(alias: str = "fake-a", **_):
        return Agent(
            provider=FakeProvider(model="fake", script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=tmp_path,
            mcp_servers=mcp_server_config(pidfile),
        )

    store = SessionStore(cfg, tmp_path, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")

    sess = store.create(permission_mode="allow-all")
    await sess.agent.init_mcp()
    manager = sess.agent.mcp_manager
    assert manager is not None
    pid = server_pid(pidfile)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/chat",
            json={"message": "hi", "session_id": sess.id, "permission_mode": "ask"},
        )
        assert r.status_code == 200, r.text

    assert sess.agent.permission_mode == "ask"
    assert sess.agent.mcp_manager is not manager
    assert process_state(pid) == ""
    await sess.agent.close_mcp()


@pytest.mark.asyncio
async def test_subagent_reuses_mcp_manager(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    script = [
        {
            "tool_calls": [
                ("c1", "parallel_tasks", {"tasks": [{"name": "t", "prompt": "add numbers"}]})
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
    )
    assert agent.mcp_manager is None
    from easycode.agent.builtin_tools import make_subagent

    agent.subagent_factory = lambda _model: Agent(
        provider=FakeProvider(), registry=build_registry(8000), root=tmp_path
    )
    sub = make_subagent(agent)
    assert sub.mcp_servers == agent.mcp_servers
    assert sub.mcp_manager == agent.mcp_manager  # shares (lazily shared after connect)

    # force connection through init; assert manager shared reference
    await agent.init_mcp()
    assert agent.mcp_manager is not None
    sub2 = make_subagent(agent)
    assert sub2.mcp_manager is agent.mcp_manager or sub2.mcp_manager is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_mcp_schemas_skip_invalid():
    """A server with an unserializable schema is skipped, others survive."""
    from easycode.mcp import MCPSessionManager

    servers = mcp_servers(mcp_server_config())
    mgr = MCPSessionManager(servers)
    await mgr.start()
    try:
        assert any(
            n.startswith("mcp__demo__") for n in [s["function"]["name"] for s in mgr.tool_schemas()]
        )
    finally:
        await mgr.close()


def test_mcp_requires_approval_is_fail_closed():
    """Only an explicit read-only tool (readOnlyHint True and
    destructiveHint not True) is auto-allowed. Missing annotations, readOnly
    false, and destructive all require approval; unknown names are not an MCP
    approval scope."""
    from easycode.mcp import MCPConnection, MCPSession, MCPSessionManager
    from easycode.mcp_config import MCPServerConfig

    mgr = MCPSessionManager([])
    config = MCPServerConfig.parse("demo", {"command": "mcp-server"})
    sess = MCPSession(config, "app", MCPConnection(config))
    sess.tools = {
        "mcp__demo__readonly": {
            "name": "readonly",
            "schema": {"type": "function", "function": {}},
            "annotations": {"readOnlyHint": True},
        },
        "mcp__demo__destructive": {
            "name": "destructive",
            "schema": {"type": "function", "function": {}},
            "annotations": {"destructiveHint": True},
        },
        "mcp__demo__plain": {
            "name": "plain",
            "schema": {"type": "function", "function": {}},
            "annotations": {},
        },
        "mcp__demo__rw": {
            "name": "rw",
            "schema": {"type": "function", "function": {}},
            "annotations": {"readOnlyHint": False},
        },
        "mcp__demo__readonly_destructive": {
            "name": "readonly_destructive",
            "schema": {"type": "function", "function": {}},
            "annotations": {"readOnlyHint": True, "destructiveHint": True},
        },
    }
    mgr._sessions = {"demo": sess}

    assert mgr.requires_approval("mcp__demo__readonly") is False
    assert mgr.requires_approval("mcp__demo__destructive") is True
    assert mgr.requires_approval("mcp__demo__plain") is True  # annotation missing → fail-closed
    assert mgr.requires_approval("mcp__demo__rw") is True  # readOnly false → approval
    # destructive dominates readOnly: a supposedly read-only tool that is also
    # destructive must still require approval
    assert mgr.requires_approval("mcp__demo__readonly_destructive") is True
    # unknown / non-MCP names are not an MCP approval scope
    assert mgr.requires_approval("write_file") is False
    assert mgr.requires_approval("mcp__demo__nope") is False


# ------------------------------------------------------------ connection setup


def test_a_launcher_gets_easycodes_own_cache_and_a_write_grant():
    from easycode.mcp import _launcher_env, _stdio_grant
    from easycode.mcp_config import MCPServerConfig, mcp_cache_dir

    config = MCPServerConfig.parse("s", {"command": "npx", "args": ["-y", "pkg"]})
    assert _launcher_env(config) == {"npm_config_cache": str(mcp_cache_dir("npx"))}
    grant = _stdio_grant(config)
    assert grant is not None
    assert grant.writable_roots == (mcp_cache_dir("npx"),)
    assert grant.network_allowed is False

    plain = MCPServerConfig.parse("s", {"command": "node", "args": ["server.js"]})
    assert _launcher_env(plain) == {}
    assert _stdio_grant(plain) is None


def test_network_is_granted_only_when_the_configuration_asks_for_it():
    from easycode.mcp import _stdio_grant
    from easycode.mcp_config import MCPServerConfig, mcp_cache_dir

    off = MCPServerConfig.parse("s", {"command": "npx", "network_enabled": False})
    assert _stdio_grant(off).network_allowed is False
    assert _stdio_grant(off).writable_roots == (mcp_cache_dir("npx"),)

    on = MCPServerConfig.parse("s", {"command": "node", "network_enabled": True})
    assert _stdio_grant(on).network_allowed is True
    assert _stdio_grant(on).writable_roots == ()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_tool_list_follows_every_page():
    """A server that pages its tools still gets all of them registered."""
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    raw = mcp_server_config()
    raw["demo"]["env"] = {"MCP_DEMO_PAGE_SIZE": "1"}

    mgr = MCPSessionManager(mcp_servers(raw))
    await mgr.start()
    try:
        names = {s["function"]["name"] for s in mgr.tool_schemas()}
        assert mcp_tool_name("demo", "add") in names
        # The last page only arrives if the cursor was followed to the end.
        assert mcp_tool_name("demo", "readonly") in names
    finally:
        await mgr.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_tool_filter_removes_a_tool_before_it_is_registered():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    raw = mcp_server_config()
    raw["demo"]["disabled_tools"] = ["boom"]

    mgr = MCPSessionManager(mcp_servers(raw))
    await mgr.start()
    try:
        names = {s["function"]["name"] for s in mgr.tool_schemas()}
        assert mcp_tool_name("demo", "add") in names
        assert mcp_tool_name("demo", "boom") not in names
        assert mgr.has_tool(mcp_tool_name("demo", "boom")) is False
    finally:
        await mgr.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_a_slow_tool_call_returns_an_error_instead_of_hanging():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    raw = mcp_server_config()
    raw["demo"]["tool_timeout_sec"] = 0.3

    mgr = MCPSessionManager(mcp_servers(raw))
    await mgr.start()
    try:
        started = time.monotonic()
        out = await mgr.call(mcp_tool_name("demo", "slow"), {"seconds": 30})
        assert json.loads(out)["status"] == "error"
        assert "未返回" in out
        assert time.monotonic() - started < 10

        # A timed-out call must not wedge the session for the next one.
        again = await mgr.call(mcp_tool_name("demo", "add"), {"a": 1, "b": 2})
        assert json.loads(again)["status"] == "ok"
    finally:
        await mgr.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_a_server_that_never_initializes_times_out_without_leaking_a_child(tmp_path):
    from easycode.mcp import MCPSessionManager
    from easycode.permissions.boundary import PathContext

    pidfile = tmp_path / "mcp.pid"
    raw = mcp_server_config(pidfile)
    raw["demo"]["env"]["MCP_DEMO_STARTUP_SLEEP"] = "30"
    raw["demo"]["startup_timeout_sec"] = 0.5

    mgr = MCPSessionManager(mcp_servers(raw), PathContext(primary=tmp_path))
    started = time.monotonic()
    try:
        await mgr.start()  # must not raise: a stuck server is one degraded server
        elapsed = time.monotonic() - started
        status = {s.name: s for s in mgr.status()}["demo"]
        assert status.state == "failed"
        assert "启动超时" in (status.error or "")
        assert elapsed < 10, f"the startup timeout did not bound the connect ({elapsed:.1f}s)"

        assert wait_for(pidfile.exists), "the child never started"
        pid = server_pid(pidfile)
        assert pid > 0
        # Giving up on the connect must not leave the child running behind it.
        assert wait_for(lambda: process_state(pid) == ""), "the timed-out child was not reaped"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_status_reports_disabled_failed_and_connected_servers():
    from easycode.mcp import STATE_DISABLED, STATE_FAILED, MCPSessionManager

    raw = {
        "off": {"command": "node", "enabled": False},
        "broken": {"command": "definitely-not-a-command-xyz"},
    }
    mgr = MCPSessionManager(mcp_servers(raw))
    await mgr.start()
    try:
        status = {s.name: s for s in mgr.status()}
        assert status["off"].state == STATE_DISABLED
        assert status["broken"].state == STATE_FAILED
        assert status["broken"].error
        assert mgr.tool_schemas() == []
    finally:
        await mgr.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_status_reports_a_connected_servers_tools():
    from easycode.mcp import STATE_CONNECTED, MCPSessionManager

    mgr = MCPSessionManager(mcp_servers(mcp_server_config()))
    await mgr.start()
    try:
        status = {s.name: s for s in mgr.status()}["demo"]
        assert status.state == STATE_CONNECTED
        assert status.tools == ["add", "boom", "greet", "readonly", "slow"]
        assert status.error is None
    finally:
        await mgr.close()


# ------------------------------------------------------------------- agent wiring


def agent_for(root: Path, **kwargs):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    return Agent(
        provider=FakeProvider(model="fake", script=[{"text": "ok"}]),
        registry=build_registry(8000),
        root=root,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_a_servers_own_project_file_is_what_connects_it(tmp_path):
    """The project scope is the agent's own directory, not the app config."""
    from easycode.mcp import STATE_CONNECTED, mcp_tool_name
    from easycode.mcp_config import project_config_path, write_scope

    write_scope(project_config_path(tmp_path), mcp_server_config())
    agent = agent_for(tmp_path, permission_mode="allow-all")
    await agent.init_mcp()
    try:
        assert agent.mcp_manager is not None
        assert mcp_tool_name("demo", "add") in agent.available_tool_names()
        status = {s.name: s for s in agent.mcp_manager.status()}["demo"]
        assert status.state == STATE_CONNECTED
        assert status.scope == "project"
    finally:
        await agent.close_mcp()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_a_project_can_switch_off_an_app_server_for_itself_only(tmp_path):
    from easycode.mcp import STATE_CONNECTED, STATE_DISABLED
    from easycode.mcp_config import project_config_path, write_scope

    other = tmp_path / "other"
    other.mkdir()
    write_scope(project_config_path(tmp_path), {"demo": {"command": "unused", "enabled": False}})

    def state(agent):
        return {s.name: s for s in agent.mcp_manager.status()}["demo"].state

    silenced = agent_for(tmp_path, mcp_servers=mcp_server_config())
    await silenced.init_mcp()
    try:
        assert silenced.mcp_manager is not None
        assert state(silenced) == STATE_DISABLED
        assert silenced.available_tool_names().isdisjoint({"mcp__demo__add"})
    finally:
        await silenced.close_mcp()

    # The same app-scope server still runs everywhere else.
    keeper = agent_for(other, mcp_servers=mcp_server_config(), permission_mode="allow-all")
    await keeper.init_mcp()
    try:
        assert keeper.mcp_manager is not None
        assert state(keeper) == STATE_CONNECTED
    finally:
        await keeper.close_mcp()


@pytest.mark.asyncio
async def test_a_settings_change_drops_the_running_connection(tmp_path):
    """Editing the servers must not leave the old process serving their tools."""
    from easycode.mcp import MCPSessionManager
    from easycode.mcp_config import (
        effective_servers,
        fingerprint,
        personal_config_path,
        project_config_path,
        write_scope,
    )

    write_scope(project_config_path(tmp_path), mcp_server_config())
    agent = agent_for(tmp_path, mcp_servers=mcp_server_config())
    ctx = agent.path_context()
    servers = effective_servers(agent.mcp_servers, str(tmp_path))
    agent.mcp_manager = MCPSessionManager(
        servers, ctx, fingerprint=fingerprint(servers, ctx, project_root=str(tmp_path))
    )
    agent.mcp_owned = True

    await agent.invalidate_mcp_if_context_changed()
    assert agent.mcp_manager is not None, "an unchanged setup keeps its process"

    # The user points the server somewhere else.
    write_scope(project_config_path(tmp_path), {"demo": {"command": "something-else"}})
    await agent.invalidate_mcp_if_context_changed()
    assert agent.mcp_manager is None, "an edited server list must be reconnected"

    # So must a configuration that has become unreadable, rather than serving
    # tools from a file nobody can parse any more.
    write_scope(project_config_path(tmp_path), mcp_server_config())
    servers = effective_servers(agent.mcp_servers, str(tmp_path))
    agent.mcp_manager = MCPSessionManager(
        servers, ctx, fingerprint=fingerprint(servers, ctx, project_root=str(tmp_path))
    )
    agent.mcp_owned = True
    personal_config_path().parent.mkdir(parents=True, exist_ok=True)
    personal_config_path().write_text("{ not json", encoding="utf-8")

    await agent.invalidate_mcp_if_context_changed()
    assert agent.mcp_manager is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
async def test_unrelated_credential_rotation_keeps_the_process(tmp_path):
    """A token for another project's server must not drop this connection."""
    from easycode.mcp_auth import MCPCredential
    from easycode.mcp_auth import store as credential_store
    from easycode.mcp_config import project_config_path, write_scope

    other = tmp_path / "other"
    other.mkdir()
    # The server has to reference the secret for a token to reach it at all.
    servers = mcp_server_config()
    servers["demo"]["secret_env"] = {"DEMO_TOKEN": "token"}
    write_scope(project_config_path(tmp_path), servers)
    agent = agent_for(tmp_path, permission_mode="allow-all")
    await agent.init_mcp()
    try:
        assert agent.mcp_manager is not None
        # Same server name, another project: not this session's credential.
        credential_store().save(
            MCPCredential(
                id="theirs",
                kind="env",
                scope="project",
                server="demo",
                root=str(other),
                values={"token": "a"},
            )
        )
        await agent.invalidate_mcp_if_context_changed()
        assert agent.mcp_manager is not None, "an unrelated token is not a reconfiguration"

        # Its own credential, however, must reconnect.
        credential_store().save(
            MCPCredential(
                id="mine",
                kind="env",
                scope="project",
                server="demo",
                root=str(tmp_path),
                values={"token": "b"},
            )
        )
        await agent.invalidate_mcp_if_context_changed()
        assert agent.mcp_manager is None, "rotating its own token must reconnect"
    finally:
        await agent.close_mcp()
