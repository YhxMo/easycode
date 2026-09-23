"""Codex-aligned execution policy and macOS sandbox regressions."""

from __future__ import annotations

import hashlib
import json
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from easycode.agent.builtin_tools import make_subagent
from easycode.agent.loop import Agent
from easycode.models.base import ToolCall
from easycode.policy import ExecutionPolicy, cap_permission, permission_rule_action
from easycode.reviewer import ReviewDecision
from easycode.tools import build_registry
from easycode.workspace import PathContext, ToolGrant
from tests.conftest import FakeProvider
from tests.helpers_history import assert_valid_tool_protocol


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


def test_permission_rules_use_last_matching_parameter_rule():
    rules = {
        "execute_shell": {
            "*": "ask",
            "git status*": "allow",
            "git push*": "deny",
        },
        "mcp__*": "ask",
        "write_file": {"*/protected.txt": "deny"},
    }

    assert permission_rule_action(rules, "execute_shell", "pytest -q") == "ask"
    assert permission_rule_action(rules, "execute_shell", "git status --short") == "allow"
    assert permission_rule_action(rules, "execute_shell", "git push origin main") == "deny"
    assert permission_rule_action(rules, "mcp__filesystem__read") == "ask"
    assert permission_rule_action(rules, "read_file", "src/main.py") is None
    assert permission_rule_action(rules, "write_file", "/tmp/protected.txt") == "deny"


def test_subagent_inherits_parent_permission_rules(tmp_path):
    rules = {"execute_shell": {"git push*": "deny"}}
    agent = Agent(
        FakeProvider(script=[]),
        build_registry(8_000),
        tmp_path,
        permission_rules=rules,
        subagent_factory=lambda _model: Agent(FakeProvider(script=[]), build_registry(8_000), tmp_path),
    )

    child = make_subagent(agent)

    assert child.permission_rules == rules


@pytest.mark.asyncio
async def test_permission_rule_deny_skips_approval_handler(tmp_path):
    script = [
        {"tool_calls": [("c1", "execute_shell", {"command": "git status --short"})]},
        {"text": "done"},
    ]
    agent = Agent(
        FakeProvider(script=script),
        build_registry(8_000),
        tmp_path,
        permission_rules={"execute_shell": {"git status*": "deny"}},
    )
    called = False

    async def approval(*_args):
        nonlocal called
        called = True
        return True

    agent.approval_handler = approval
    events = [event async for event in agent.respond("check status")]
    result = next(event.tool_result for event in events if event.kind == "tool_result")
    payload = json.loads(result)

    assert called is False
    assert payload["rejected"] is True
    assert payload["category"] == "policy"


@pytest.mark.asyncio
async def test_permission_rule_deny_still_applies_under_allow_all(tmp_path):
    script = [
        {"tool_calls": [("c1", "execute_shell", {"command": "git status --short"})]},
        {"text": "done"},
    ]
    agent = Agent(
        FakeProvider(script=script),
        build_registry(8_000),
        tmp_path,
        permission_mode="allow-all",
        permission_rules={"execute_shell": {"git status*": "deny"}},
    )
    events = [event async for event in agent.respond("check status")]
    result = next(event.tool_result for event in events if event.kind == "tool_result")
    payload = json.loads(result)

    assert payload["rejected"] is True
    assert payload["category"] == "policy"


@pytest.mark.asyncio
async def test_disabled_tool_call_is_rejected_at_execution(tmp_path):
    """A tool outside the advertised schema set is rejected, never executed."""
    target = tmp_path / "blocked.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(target), "content": "nope"})]},
        {"text": "ok"},
    ]
    agent = Agent(
        FakeProvider(script=script),
        build_registry(8_000),
        tmp_path,
        enabled_tools={"read_file"},
    )

    events = [event async for event in agent.respond("write it")]

    result = next(event.tool_result for event in events if event.kind == "tool_result")
    payload = json.loads(result)
    assert payload["rejected"] is True
    assert payload["category"] == "policy"
    assert "tool not enabled" in payload["message"]
    assert not target.exists()
    assert "".join(e.content for e in events if e.kind == "text") == "ok"
    assert events[-1].kind == "done"
    assert_valid_tool_protocol(agent.history.payload())


@pytest.mark.asyncio
async def test_capped_subagent_approved_external_write_succeeds(tmp_path):
    """Approval must grant against the *executing* agent's context.

    The handler mirrors the pre-fix web bridge: it derives the grant from the
    parent's (allow-all) context and returns it. The loop must ignore the
    returned grant and regenerate the precise grant for the capped subagent.
    """
    from easycode.agents import AgentRegistry, AgentSpec
    from easycode.approval import grant_for_toolcall

    root = tmp_path / "proj"
    root.mkdir()
    target = tmp_path / "outside.txt"
    registry = AgentRegistry({"writer": AgentSpec(name="writer", description="writes", permission="ask")})

    sub_script = [
        {"tool_calls": [("s1", "write_file", {"path": str(target), "content": "approved"})]},
        {"text": "done"},
    ]
    subs: list[Agent] = []

    def factory(_model: str) -> Agent:
        sub = Agent(FakeProvider(script=sub_script), build_registry(8_000), root)
        subs.append(sub)
        return sub

    script = [
        {"tool_calls": [("t1", "task", {"agent": "writer", "prompt": "write it"})]},
        {"text": "finished"},
    ]
    parent = Agent(
        FakeProvider(script=script),
        build_registry(8_000),
        root,
        permission_mode="allow-all",
        agents=registry,
        subagent_factory=factory,
    )
    asked: list[str] = []

    async def approval(tc, _reason):
        grant = grant_for_toolcall(tc, parent.path_context())
        asked.append(tc.name)
        return grant if grant else True

    parent.approval_handler = approval
    async for _ in parent.respond("delegate"):
        pass

    assert asked == ["write_file"]
    assert target.read_text() == "approved"
    assert len(subs) == 1


@pytest.mark.asyncio
async def test_destructive_command_is_not_policy_rejected_under_allow_all(tmp_path):
    script = [
        {"tool_calls": [("c1", "execute_shell", {"command": "git reset --hard"})]},
        {"text": "done"},
    ]
    agent = Agent(
        FakeProvider(script=script),
        build_registry(8_000),
        tmp_path,
        permission_mode="allow-all",
    )
    events = [event async for event in agent.respond("reset")]
    result = next(event.tool_result for event in events if event.kind == "tool_result")
    payload = json.loads(result)

    # tmp_path is not a git repo (git exits non-zero on its own), but the
    # command must RUN: no destructive policy gate under allow-all.
    assert payload.get("rejected") is not True
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_permission_rule_allow_grants_exact_external_file_write(tmp_path):
    outside = tmp_path / "secondary" / "approved.txt"
    outside.parent.mkdir()
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "approved"})]},
        {"text": "done"},
    ]
    agent = Agent(
        FakeProvider(script=script),
        build_registry(8_000),
        tmp_path / "project",
        permission_rules={"write_file": {f"*{outside.name}": "allow"}},
    )
    agent.root.mkdir()
    events = [event async for event in agent.respond("write approved file")]

    assert outside.read_text() == "approved"
    assert not any(event.kind == "approval" for event in events)


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
    # the key is `tool:readable-prefix:<sha256(full command)>` — readable for the
    # UI, but authorization equivalence depends on the full command hash.
    assert approval_key(shell) == (
        "execute_shell:curl -I https://example.com:"
        + hashlib.sha256(b"curl -I https://example.com").hexdigest()
    )

    mcp = ToolCall(id="c3", name="mcp__filesystem__read", arguments={})
    assert approval_scope(mcp) == "mcp__filesystem__read"
    assert approval_key(mcp) == "mcp__filesystem__read"


def test_approval_key_uses_full_command_hash_and_is_stable():
    """SC: 'always allow' equivalence must bind to the whole command, not the
    truncated prefix. Two commands sharing the first 80 chars but differing in
    the tail must produce different keys; an identical command must produce the
    same key on every call."""
    from easycode.approval import approval_key

    prefix80 = "echo " + ("a" * 75)  # exactly 80 characters
    c1 = prefix80 + "one"
    c2 = prefix80 + "two"
    assert len(prefix80) == 80
    assert c1[:80] == c2[:80] == prefix80
    assert c1 != c2

    k1 = approval_key(ToolCall(id="a", name="execute_shell", arguments={"command": c1}))
    k2 = approval_key(ToolCall(id="b", name="execute_shell", arguments={"command": c2}))
    assert k1 != k2  # shared prefix must not authorize a different command

    # identical command is stable across calls (and carries both prefix + digest)
    k1_again = approval_key(ToolCall(id="c", name="execute_shell", arguments={"command": c1}))
    assert k1_again == k1
    assert k1.startswith("execute_shell:" + prefix80 + ":")
    assert k1.endswith(hashlib.sha256(c1.encode("utf-8")).hexdigest())


def test_approval_reason_is_categorical(tmp_path):
    from easycode.approval import approval_reason

    ctx = PathContext(primary=tmp_path)
    external = ToolCall(id="c1", name="write_file", arguments={"path": str(tmp_path.parent / "x.txt"), "content": "x"})
    assert approval_reason(external, ctx) == "访问项目目录之外的文件"

    network = ToolCall(id="c2", name="execute_shell", arguments={"command": "curl -I https://example.com"})
    assert approval_reason(network, ctx) == "执行疑似联网命令"

    elevated = ToolCall(
        id="c2b",
        name="execute_shell",
        arguments={"command": "rm -rf /tmp/easycode-test-project", "sandbox_permissions": "require_escalated"},
    )
    assert approval_reason(elevated, ctx) == "执行需要额外权限的命令"

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


# ------------------------------------------------------------ P0-1 precise ToolGrant


def test_secondary_and_extra_safe_need_no_approval(tmp_path):
    """secondary/extra_safe directories are safe: file writes need no grant."""
    primary = tmp_path / "p"
    secondary = tmp_path / "s"
    extra = tmp_path / "e"
    primary.mkdir()
    secondary.mkdir()
    extra.mkdir()
    ctx = PathContext(primary=primary, secondary=[secondary], extra_safe_dirs=[extra])
    reg = build_registry(8000)

    r = json.loads(reg.execute("write_file", {"path": str(secondary / "a.txt"), "content": "x"}, primary, ctx))
    assert r["status"] == "ok"
    assert ctx.in_allowed(secondary / "a.txt") is True

    r2 = json.loads(reg.execute("write_file", {"path": str(extra / "b.txt"), "content": "y"}, primary, ctx))
    assert r2["status"] == "ok"
    assert ctx.in_allowed(extra / "b.txt") is True


def test_git_and_easycode_hard_denied_everywhere(tmp_path):
    """.git/.easycode are never writable by the file-tool chain, even with a grant."""
    primary = tmp_path / "p"
    primary.mkdir()
    (primary / ".git").mkdir()
    (primary / ".easycode").mkdir()
    ctx = PathContext(primary=primary)
    reg = build_registry(8000)

    assert ctx.in_allowed(primary / ".git" / "config") is False
    r = json.loads(reg.execute("write_file", {"path": str(primary / ".git" / "config"), "content": "x"}, primary, ctx, grant=ToolGrant()))
    assert r["status"] == "error"
    r2 = json.loads(reg.execute("write_file", {"path": str(primary / ".easycode" / "x"), "content": "x"}, primary, ctx, grant=ToolGrant()))
    assert r2["status"] == "error"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_grant_external_writable_root_succeeds_neighbor_fails(tmp_path):
    """A precise writable-root grant lets a shell write there, but not next door."""
    from easycode.sandbox.macos import sandbox_command

    primary = tmp_path / "p"
    ext = tmp_path / "ext"
    neighbor = tmp_path / "neighbor"
    primary.mkdir()
    ext.mkdir()
    neighbor.mkdir()
    ctx = PathContext(primary=primary)
    reg = build_registry(8000)
    grant = ToolGrant(writable_roots=(ext,))

    ok = json.loads(
        reg.execute(
            "execute_shell",
            {"command": f"printf data > {ext / 'f.txt'}", "sandbox_permissions": "require_escalated"},
            primary,
            ctx,
            grant=grant,
        )
    )
    assert ok["exit_code"] == 0
    assert (ext / "f.txt").read_text() == "data"

    bad = json.loads(
        reg.execute(
            "execute_shell",
            {"command": f"printf nope > {neighbor / 'g.txt'}", "sandbox_permissions": "require_escalated"},
            primary,
            ctx,
            grant=grant,
        )
    )
    assert bad["exit_code"] != 0
    assert not (neighbor / "g.txt").exists()

    # a network grant alone does not grant external writes (network/file separation).
    net_only = sandbox_command(["/bin/sh", "-c", f"printf nope > {ext / 'n.txt'}"], ctx, grant=ToolGrant(network_allowed=True))
    proc = subprocess.run(net_only, capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert not (ext / "n.txt").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_grant_network_and_file_dimensions_are_separate(tmp_path):
    from easycode.sandbox.macos import sandbox_command

    primary = tmp_path / "p"
    ext = tmp_path / "ext"
    primary.mkdir()
    ext.mkdir()
    ctx = PathContext(primary=primary)

    net = sandbox_command(["/bin/sh", "-c", "curl -I https://x"], ctx, grant=ToolGrant(network_allowed=True))
    assert "(allow network*)" in net[2]
    assert not any(x.startswith("-DWRITABLE_ROOT_") and x.endswith(str(ext)) for x in net)

    file_g = sandbox_command(["/bin/sh", "-c", "echo hi > out"], ctx, grant=ToolGrant(writable_roots=(ext,)))
    assert "(allow network*)" not in file_g[2]
    assert any(x.startswith("-DWRITABLE_ROOT_") and x.endswith(str(ext)) for x in file_g)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_grant_protects_git_under_grant_root(tmp_path):
    from easycode.sandbox.macos import sandbox_command

    primary = tmp_path / "p"
    groot = tmp_path / "groot"
    primary.mkdir()
    (groot / ".git").mkdir(parents=True)
    ctx = PathContext(primary=primary)
    cmd = sandbox_command(
        ["/bin/sh", "-c", f"printf x > {groot / '.git' / 'config'}"],
        ctx,
        grant=ToolGrant(writable_roots=(groot,)),
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert not (groot / ".git" / "config").exists()


# ------------------------------------------------------------ shell writable_roots (P0-1)


def test_shell_writable_roots_validation_fails_closed(tmp_path):
    """Invalid / un-granted writable_roots declaration never grants external writes."""
    from easycode.approval import needs_approval
    from easycode.models.base import ToolCall
    from easycode.tools import build_registry

    proj = tmp_path / "proj"
    ext = tmp_path / "ext"
    proj.mkdir()
    ext.mkdir()
    ctx = PathContext(primary=proj)
    reg = build_registry(8000)

    # relative root -> structured error
    out = json.loads(
        reg.execute("execute_shell", {"command": "true", "writable_roots": ["relative/dir"]}, proj, ctx)
    )
    assert out["status"] == "error" and "absolute" in out["message"]

    # plain file root -> structured error
    plain = tmp_path / "f.txt"
    plain.write_text("x")
    out2 = json.loads(
        reg.execute("execute_shell", {"command": "true", "writable_roots": [str(plain)]}, proj, ctx)
    )
    assert out2["status"] == "error" and "not a directory" in out2["message"]

    # .git root -> structured error (sensitive)
    git = tmp_path / "g"
    (git / ".git").mkdir(parents=True)
    out3 = json.loads(
        reg.execute("execute_shell", {"command": "true", "writable_roots": [str(git / ".git")]}, proj, ctx)
    )
    assert out3["status"] == "error" and "sensitive" in out3["message"]

    # declared but never granted -> fail closed (no grant passed)
    out4 = json.loads(
        reg.execute("execute_shell", {"command": "true", "writable_roots": [str(ext)]}, proj, ctx)
    )
    assert out4["status"] == "error" and "not granted" in out4["message"]

    # declaring writable_roots marks the shell as requiring approval
    tc = ToolCall(id="a", name="execute_shell", arguments={"command": "echo hi", "writable_roots": [str(ext)]})
    assert needs_approval(tc, ctx, "ask") is True
    assert needs_approval(ToolCall(id="b", name="execute_shell", arguments={"command": "echo hi"}), ctx, "ask") is False


def test_shell_grant_for_toolcall_uses_declared_roots(tmp_path):
    from easycode.approval import grant_for_toolcall
    from easycode.models.base import ToolCall

    proj = tmp_path / "proj"
    ext = tmp_path / "ext"
    proj.mkdir()
    ext.mkdir()
    ctx = PathContext(primary=proj)

    tc = ToolCall(
        id="a", name="execute_shell", arguments={"command": f"rm -rf {ext}", "writable_roots": [str(ext)]}
    )
    g = grant_for_toolcall(tc, ctx)
    assert g.writable_roots == (ext.resolve(),)
    assert g.network_allowed is False

    # escalated flips the network dimension independently
    tc2 = ToolCall(
        id="b",
        name="execute_shell",
        arguments={"command": f"rm -rf {ext}", "writable_roots": [str(ext)], "sandbox_permissions": "require_escalated"},
    )
    assert grant_for_toolcall(tc2, ctx).network_allowed is True

    # invalid declaration → no writable roots (fail closed)
    tc3 = ToolCall(id="c", name="execute_shell", arguments={"command": "echo hi", "writable_roots": ["nope/relative"]})
    assert grant_for_toolcall(tc3, ctx).writable_roots == ()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_grant_approved_explicit_root_succeeds_neighbor_fails(tmp_path):
    """A granted explicit writable root lets the shell write there but not next door."""
    from easycode.tools import build_registry

    proj = tmp_path / "p"
    ext = tmp_path / "ext"
    neighbor = tmp_path / "neighbor"
    proj.mkdir()
    ext.mkdir()
    neighbor.mkdir()
    ctx = PathContext(primary=proj)
    reg = build_registry(8000)
    grant = ToolGrant(writable_roots=(ext,))

    ok = json.loads(
        reg.execute(
            "execute_shell",
            {"command": f"printf data > {ext / 'f.txt'}", "writable_roots": [str(ext)]},
            proj,
            ctx,
            grant=grant,
        )
    )
    assert ok["status"] == "ok" and ok["exit_code"] == 0
    assert (ext / "f.txt").read_text() == "data"

    # neighbor not granted → the shell write is denied by Seatbelt
    bad = json.loads(
        reg.execute(
            "execute_shell",
            {"command": f"printf nope > {neighbor / 'g.txt'}", "writable_roots": [str(ext)]},
            proj,
            ctx,
            grant=grant,
        )
    )
    assert bad["exit_code"] != 0
    assert not (neighbor / "g.txt").exists()

    # no explicit root / no grant → external write fails closed
    no = json.loads(
        reg.execute(
            "execute_shell",
            {"command": f"printf nope > {ext / 'h.txt'}"},
            proj,
            ctx,
        )
    )
    assert no["exit_code"] != 0
    assert not (ext / "h.txt").exists()


# ------------------------------------------------------------ P0-2 hard deny / mixed invalid


def test_grant_for_toolcall_mixed_invalid_roots_no_grant(tmp_path):
    """A single invalid writable_roots entry voids the whole grant (fail closed)."""
    from easycode.approval import grant_for_toolcall
    from easycode.models.base import ToolCall

    proj = tmp_path / "proj"
    ext = tmp_path / "ext"
    proj.mkdir()
    ext.mkdir()
    ctx = PathContext(primary=proj)

    tc = ToolCall(
        id="a",
        name="execute_shell",
        arguments={"command": "rm -rf /x", "writable_roots": [str(ext), "relative/bad"]},
    )
    assert grant_for_toolcall(tc, ctx).writable_roots == ()

    # all-valid keeps every root
    tc2 = ToolCall(
        id="b",
        name="execute_shell",
        arguments={"command": "rm -rf /x", "writable_roots": [str(ext)]},
    )
    assert grant_for_toolcall(tc2, ctx).writable_roots == (ext.resolve(),)


def test_file_tool_target_git_easycode_denied_even_with_grant(tmp_path):
    """write_file to .git/.easycode is permanently denied — a grant cannot enable it."""
    from easycode.approval import grant_for_toolcall
    from easycode.models.base import ToolCall
    from easycode.tools import build_registry

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / ".easycode").mkdir()
    ctx = PathContext(primary=proj)
    reg = build_registry(8000)

    # grant_for_toolcall never grants a protected target
    tc_git = ToolCall(id="a", name="write_file", arguments={"path": str(proj / ".git" / "config"), "content": "x"})
    assert grant_for_toolcall(tc_git, ctx).writable_roots == ()
    tc_easy = ToolCall(id="b", name="write_file", arguments={"path": str(proj / ".easycode" / "x"), "content": "x"})
    assert grant_for_toolcall(tc_easy, ctx).writable_roots == ()

    # even a grant directly targeting .git cannot enable the write
    r = json.loads(
        reg.execute("write_file", {"path": str(proj / ".git" / "config"), "content": "x"}, proj, ctx, grant=ToolGrant(writable_roots=(proj / ".git",)))
    )
    assert r["status"] == "error"
    r2 = json.loads(
        reg.execute("write_file", {"path": str(proj / ".easycode" / "x"), "content": "x"}, proj, ctx, grant=ToolGrant(writable_roots=(proj,)))
    )
    assert r2["status"] == "error"


def test_extra_safe_dot_git_protected(tmp_path):
    """extra_safe_dirs stay writable, but their .git/.easycode are hard-deny."""
    from easycode.tools import build_registry

    proj = tmp_path / "proj"
    extra = tmp_path / "extra"
    proj.mkdir()
    extra.mkdir()
    (extra / ".git").mkdir()
    ctx = PathContext(primary=proj, extra_safe_dirs=[extra])
    reg = build_registry(8000)

    # normal extra_safe write is approval-free
    ok = json.loads(reg.execute("write_file", {"path": str(extra / "a.txt"), "content": "x"}, proj, ctx))
    assert ok["status"] == "ok"
    assert ctx.in_allowed(extra / "a.txt") is True

    # extra/.git is protected: not allowed, not grantable
    assert ctx.in_allowed(extra / ".git" / "config") is False
    r = json.loads(reg.execute("write_file", {"path": str(extra / ".git" / "config"), "content": "x"}, proj, ctx))
    assert r["status"] == "error"
    r2 = json.loads(
        reg.execute("write_file", {"path": str(extra / ".git" / "config"), "content": "x"}, proj, ctx, grant=ToolGrant(writable_roots=(extra,)))
    )
    assert r2["status"] == "error"

    # protected_paths covers extra/.git + .easycode but not data_home wholesale
    prot = [str(p) for p in ctx.protected_paths()]
    assert str((extra / ".git").resolve()) in prot
    assert str((extra / ".easycode").resolve()) in prot
    assert str((proj / ".git").resolve()) in prot
    assert str((proj / ".easycode").resolve()) in prot


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_extra_safe_dot_git_shell_denied(tmp_path):
    from easycode.tools import build_registry

    proj = tmp_path / "proj"
    extra = tmp_path / "extra"
    proj.mkdir()
    extra.mkdir()
    (extra / ".git").mkdir()
    ctx = PathContext(primary=proj, extra_safe_dirs=[extra])
    reg = build_registry(8000)

    ok = json.loads(reg.execute("execute_shell", {"command": f"printf hi > {extra / 'ok.txt'}"}, proj, ctx))
    assert ok["exit_code"] == 0
    assert (extra / "ok.txt").read_text() == "hi"

    bad = json.loads(reg.execute("execute_shell", {"command": f"printf x > {extra / '.git' / 'c'}"}, proj, ctx))
    assert bad["exit_code"] != 0
    assert not (extra / ".git" / "c").exists()


def test_system_prompt_mentions_writable_roots(tmp_path):
    """Guidelines tell the model to explicitly declare writable_roots (P0-1)."""
    from easycode.agent.system import build_system_prompt

    proj = tmp_path / "proj"
    proj.mkdir()
    prompt = build_system_prompt(proj, tools_desc="")
    assert "writable_roots" in prompt
    assert "require_escalated" in prompt
    assert "justification" in prompt.lower()
    # must not imply the sandbox guesses paths from the command string
    low = prompt.lower()
    assert "infer writable paths" in low
    assert "command and writable_roots" in low
