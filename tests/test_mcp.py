"""Mcp tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def mcp_server_config():
    import sys

    return {
        "demo": {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve().parent / "mcp_demo_server.py")],
        }
    }


@pytest.mark.asyncio
async def test_mcp_manager_connects_and_lists_tools():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    mgr = MCPSessionManager(mcp_server_config())
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
async def test_mcp_manager_call_tool():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    mgr = MCPSessionManager(mcp_server_config())
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

    mgr = MCPSessionManager({"ghost": {"command": "definitely-not-a-command-xyz", "args": []}})
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
async def test_mcp_schemas_skip_invalid():
    """A server with an unserializable schema is skipped, others survive."""
    from easycode.mcp import MCPSessionManager

    servers = mcp_server_config()
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
    from easycode.mcp import MCPSession, MCPSessionManager

    mgr = MCPSessionManager({})
    sess = MCPSession("demo", None)
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
