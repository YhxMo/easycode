"""Codex-aligned execution policy and macOS sandbox regressions."""

from __future__ import annotations

import json
import shlex
import socket
import sys
from pathlib import Path

import pytest

from easycode.agent.loop import Agent
from easycode.models.base import ToolCall
from easycode.policy import ExecutionPolicy, cap_permission
from easycode.reviewer import ReviewDecision
from easycode.tools import build_registry
from easycode.workspace import PathContext
from tests.conftest import FakeProvider


def _shell(root: Path, ctx: PathContext, command: str, **extra) -> dict:
    result = build_registry(20_000).execute(
        "execute_shell", {"command": command, **extra}, root, ctx
    )
    return json.loads(result)


def test_permission_presets_map_to_codex_controls():
    ask = ExecutionPolicy.from_preset("ask")
    assert (ask.sandbox_mode, ask.approval_policy, ask.approvals_reviewer) == (
        "workspace-write",
        "on-request",
        "user",
    )
    auto = ExecutionPolicy.from_preset("auto-review")
    assert auto.sandbox_mode == "workspace-write"
    assert auto.approvals_reviewer == "auto-review"
    full = ExecutionPolicy.from_preset("allow-all")
    assert full.sandbox_mode == "danger-full-access"
    assert full.approval_policy == "never"


def test_subagent_permission_is_capped():
    assert cap_permission("ask", "allow-all") == "ask"
    assert cap_permission("auto-review", "allow-all") == "auto-review"
    assert cap_permission("allow-all", "ask") == "ask"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_can_write_primary_and_bound_secondary(tmp_path):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    ctx = PathContext(primary=primary, secondary=[secondary])

    first = _shell(primary, ctx, "printf primary > inside.txt")
    second = _shell(primary, ctx, f"printf secondary > {secondary / 'inside.txt'}")

    assert first["exit_code"] == 0
    assert second["exit_code"] == 0
    assert (primary / "inside.txt").read_text() == "primary"
    assert (secondary / "inside.txt").read_text() == "secondary"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_cannot_write_outside_workspace(tmp_path):
    primary = tmp_path / "primary"
    outside = tmp_path / "outside.txt"
    primary.mkdir()

    result = _shell(primary, PathContext(primary=primary), f"printf blocked > {outside}")

    assert result["exit_code"] != 0
    assert not outside.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_cannot_write_protected_project_metadata(tmp_path):
    primary = tmp_path / "primary"
    protected = primary / ".git"
    protected.mkdir(parents=True)

    result = _shell(primary, PathContext(primary=primary), "printf blocked > .git/config")

    assert result["exit_code"] != 0
    assert not (protected / "config").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_network_is_blocked_by_default(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    code = f"import socket; socket.create_connection(('127.0.0.1', {port}), 1)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    try:
        result = _shell(primary, PathContext(primary=primary), command)
        assert result["exit_code"] != 0
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_read_only_blocks_workspace_write_and_full_access_allows_external_write(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    target = primary / "blocked.txt"
    readonly = PathContext(primary=primary, sandbox_mode="read-only")
    blocked = _shell(primary, readonly, "printf no > blocked.txt")
    assert blocked["exit_code"] != 0
    assert not target.exists()

    outside = tmp_path / "outside.txt"
    full = PathContext(primary=primary, sandbox_mode="danger-full-access")
    allowed = _shell(primary, full, f"printf yes > {outside}")
    assert allowed["exit_code"] == 0
    assert outside.read_text() == "yes"


@pytest.mark.asyncio
async def test_auto_reviewer_denies_before_external_write(tmp_path):
    outside = tmp_path.parent / "review-denied.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "no"})]},
        {"text": "done"},
    ]
    agent = Agent(FakeProvider(script=script), build_registry(8_000), tmp_path, permission_mode="auto-review")

    async def deny(*_args):
        return ReviewDecision(False, "scope is not justified")

    agent.review_handler = deny
    events = [event async for event in agent.respond("write outside")]

    assert not outside.exists()
    review = next(event for event in events if event.kind == "review")
    assert json.loads(review.content)["approved"] is False


@pytest.mark.asyncio
async def test_auto_reviewer_can_approve_exact_external_write(tmp_path):
    outside = tmp_path.parent / "review-approved.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "ok"})]},
        {"text": "done"},
    ]
    agent = Agent(FakeProvider(script=script), build_registry(8_000), tmp_path, permission_mode="auto-review")

    async def approve(*_args):
        return ReviewDecision(True, "explicit task requires this exact file")

    agent.review_handler = approve
    try:
        events = [event async for event in agent.respond("write outside")]
        assert outside.read_text() == "ok"
        review = next(event for event in events if event.kind == "review")
        assert json.loads(review.content)["approved"] is True
    finally:
        outside.unlink(missing_ok=True)


# ---------------------------------------------------------------- approval helpers


def test_approval_scope_and_key_for_file_tools():
    from easycode.approval import approval_key, approval_scope

    tc = ToolCall(id="c1", name="write_file", arguments={"path": "/tmp/easycode-test-project/foo.txt", "content": "x"})
    assert approval_scope(tc) == "/tmp/easycode-test-project/*"
    assert approval_key(tc) == "write_file:/tmp/easycode-test-project/*"

    shell = ToolCall(id="c2", name="execute_shell", arguments={"command": "curl -I https://example.com"})
    assert approval_scope(shell) == "curl -I https://example.com"
    assert approval_key(shell) == "execute_shell:curl -I https://example.com"

    mcp = ToolCall(id="c3", name="mcp__filesystem__read", arguments={})
    assert approval_scope(mcp) == "mcp__filesystem__read"
    assert approval_key(mcp) == "mcp__filesystem__read"


def test_approval_reason_is_categorical(tmp_path):
    from easycode.approval import approval_reason

    ctx = PathContext(primary=tmp_path)
    external = ToolCall(id="c1", name="write_file", arguments={"path": str(tmp_path.parent / "x.txt"), "content": "x"})
    assert approval_reason(external, ctx) == "访问项目目录之外的文件"

    network = ToolCall(id="c2", name="execute_shell", arguments={"command": "curl -I https://example.com"})
    assert approval_reason(network, ctx) == "执行疑似联网命令"

    (tmp_path / ".git").mkdir()
    protected = ToolCall(id="c3", name="write_file", arguments={"path": str(tmp_path / ".git" / "config"), "content": "x"})
    assert approval_reason(protected, ctx) == "修改受保护目录 (.git/.easycode)"


def test_system_dir_exempt_but_credentials_protected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from easycode.credentials import data_home

    proj = tmp_path / "proj"
    proj.mkdir()
    dh = data_home()
    (dh / "sessions").mkdir(parents=True, exist_ok=True)
    ctx = PathContext(primary=proj)

    # ~/.easycode/** is now approval-exempt (matches README claim).
    assert ctx.in_allowed(dh / "sessions" / "s.json") is True

    # but credentials.json stays protected once it exists.
    cred = dh / "credentials.json"
    cred.write_text("{}", encoding="utf-8")
    assert ctx.in_allowed(cred) is False
