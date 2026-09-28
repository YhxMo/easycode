"""Security regression tests.

- Credential paths are unconditionally protected and
  reading / writing / enumerating them is rejected by every file tool.
- An approved shell (``grant=ToolGrant(network_allowed=True)``) relaxes only the
  network dimension — file-write and process limits are retained; only
  ``danger-full-access`` disables the sandbox.
- Model detail redacts ``api_key``; cross-origin state-change
  POSTs are rejected; the CLI ``web`` host stays loopback.

Tests are isolated: temporary HOME, scripted FakeProvider, no real service.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.credentials import Credential, data_home, new_credential_id, save_credential
from easycode.permissions.boundary import PathContext, ToolGrant
from easycode.permissions.sandbox import child_env
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.middleware import _origin_is_local
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider

# ----------------------------------------------------------------


def make_home_ctx(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(workspace_root, data_home)`` with distinct HOME/workspace dirs."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj, home


def test_read_file_rejects_credential_path(tmp_path, monkeypatch):
    proj, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cred = data_home() / "credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(json.dumps({"k": {"api_key": "sk-super-secret"}}), encoding="utf-8")

    out = json.loads(build_registry(8000).execute("read_file", {"path": str(cred)}, proj))
    assert out["status"] == "error"
    assert "protected" in out["message"]
    assert "sk-super-secret" not in json.dumps(out)


def test_write_file_rejected_creating_credential(tmp_path, monkeypatch):
    proj, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cred = data_home() / "credentials.json"

    out = json.loads(
        build_registry(8000).execute("write_file", {"path": str(cred), "content": "{}"}, proj)
    )
    assert out["status"] == "error"
    assert not cred.exists()


def test_write_file_cannot_bypass_credentials_with_grant(tmp_path, monkeypatch):
    proj, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cred = data_home() / "credentials.json"

    out = json.loads(
        build_registry(8000).execute(
            "write_file", {"path": str(cred), "content": "{}"}, proj, grant=ToolGrant(network_allowed=True)
        )
    )
    assert out["status"] == "error"
    assert not cred.exists()


def test_grep_and_glob_skip_credential_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cred = data_home() / "credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(json.dumps({"k": {"api_key": "sk-super-secret"}}), encoding="utf-8")

    g = json.loads(build_registry(8000).execute("grep", {"pattern": "sk-super-secret"}, home))
    assert g["status"] == "ok"
    assert g["matches"] == []

    gl = json.loads(build_registry(8000).execute("glob", {"pattern": "**/*.json"}, home))
    assert all("credentials.json" not in m for m in gl["matches"])


def test_in_allowed_false_for_credentials_even_when_missing(tmp_path, monkeypatch):
    proj, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    ctx = PathContext(primary=proj)
    cred = data_home() / "credentials.json"
    assert not cred.exists()
    # a not-yet-existing credential file is still off-limits for writes.
    assert ctx.in_allowed(cred) is False
    assert ctx.is_protected(cred) is True


# ----------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_sandbox_command_grant_relaxes_network_not_bare(tmp_path):
    from easycode.permissions.sandbox.macos import sandbox_command

    cmd = ["/bin/sh", "-c", "curl -I https://example.com"]
    ctx = PathContext(primary=tmp_path)
    wrapped = sandbox_command(cmd, ctx, grant=ToolGrant(network_allowed=True))

    assert wrapped != cmd, "a grant must not return the bare (unsandboxed) command"
    assert wrapped[0] == "/usr/bin/sandbox-exec"
    policy = wrapped[2]
    assert "(deny default)" in policy
    assert "(allow network*)" in policy  # only the network dimension is relaxed

    # Without a grant, network stays denied.
    default_wrapped = sandbox_command(cmd, ctx)
    assert "(allow network*)" not in default_wrapped[2]


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_approved_shell_still_cannot_write_credentials(tmp_path, monkeypatch):
    from easycode.permissions.sandbox.macos import sandbox_command

    _, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    ctx = PathContext(primary=proj)
    cred = data_home() / "credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["/bin/sh", "-c", f"printf '{{}}' > {cred}"]
    # grant=ToolGrant(network_allowed=True) simulates an approved shell: file-write protections remain.
    proc = subprocess.run(sandbox_command(cmd, ctx, grant=ToolGrant(network_allowed=True)), capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert not cred.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_cannot_read_credentials_in_workspace_write(tmp_path, monkeypatch):
    """The shell path must not bypass the file-tool credential firewall.

    ``danger-full-access`` is deliberately absent here: it drops the firewall
    for every tool at once (see
    ``test_file_tools_and_shell_reach_protected_paths_under_allow_all``), and a
    mode where the shell is open but the tools are not would be the surprising
    combination, not this one.
    """
    _, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    cred = data_home() / "credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text('{"api_key":"sk-shell-secret"}', encoding="utf-8")

    ctx = PathContext(primary=proj)
    result = json.loads(
        build_registry(8000).execute(
            "execute_shell", {"command": f"cat {cred}"}, proj, ctx
        )
    )

    assert result["status"] == "ok"
    assert result["exit_code"] != 0
    assert "sk-shell-secret" not in json.dumps(result)


def test_child_env_strips_parent_secrets_but_keeps_runtime_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-parent-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("EASYCODE_TEST_VALUE", "kept")

    env = child_env()

    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["EASYCODE_TEST_VALUE"] == "kept"


@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
@pytest.mark.asyncio
async def test_mcp_child_env_is_sanitized(monkeypatch, tmp_path):
    """A secret-bearing parent variable must not reach an MCP server process,
    while explicit ``mcp_servers`` env values are layered on top."""
    import contextlib

    from easycode.extensions.mcp import client as mcp_module

    captured: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def fake_stdio(params):
        captured["params"] = params
        raise RuntimeError("no child process in this test")
        yield  # pragma: no cover - never reached

    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-secret")
    monkeypatch.setenv("MCP_SAFE_PARENT", "kept")
    monkeypatch.setattr(mcp_module, "stdio_client", fake_stdio)

    connection = mcp_module.MCPConnection(
        mcp_module.MCPServerConfig.parse(
            "demo",
            {
                "command": "mcp-server",
                "args": ["--stdio"],
                "env": {
                    "MCP_EXPLICIT": "configured",
                    "MCP_EXPLICIT_TOKEN": "intentionally-configured",
                },
            },
        ),
        PathContext(primary=tmp_path),
    )
    with pytest.raises(RuntimeError):
        await connection.start()

    env = captured["params"].env
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
    assert env["MCP_SAFE_PARENT"] == "kept"
    assert env["MCP_EXPLICIT"] == "configured"
    assert env["MCP_EXPLICIT_TOKEN"] == "intentionally-configured"


def test_destructive_shell_commands_are_denied_before_execution(tmp_path):
    registry = build_registry(8000)
    ctx = PathContext(primary=tmp_path)
    for command in (
        "rm -rf /",
        "git reset --hard HEAD",
        "git clean -fd",
        "git push --force origin main",
    ):
        result = json.loads(registry.execute("execute_shell", {"command": command}, tmp_path, ctx))
        assert result["status"] == "error"
        assert result["rejected"] is True
        assert result["category"] == "destructive"


def test_destructive_denylist_is_off_under_danger_full_access(tmp_path):
    from easycode.permissions.policy import SANDBOX_DANGER_FULL_ACCESS

    registry = build_registry(8000)
    ctx = PathContext(primary=tmp_path, sandbox_mode=SANDBOX_DANGER_FULL_ACCESS)
    # tmp_path is not a git repo, so git fails on its own; the point is that
    # the command RUNS instead of being rejected by the destructive denylist.
    result = json.loads(
        registry.execute("execute_shell", {"command": "git reset --hard"}, tmp_path, ctx)
    )
    assert result["status"] == "ok"
    assert result.get("rejected") is not True


def test_writable_roots_declaration_is_not_gated_under_danger_full_access(tmp_path):
    from easycode.permissions.policy import SANDBOX_DANGER_FULL_ACCESS

    registry = build_registry(8000)
    ext = tmp_path / "ext"
    ext.mkdir()
    ctx = PathContext(primary=tmp_path, sandbox_mode=SANDBOX_DANGER_FULL_ACCESS)
    # No grant exists under allow-all; the declaration must be ignored rather
    # than failing the command with "not granted by approval".
    result = json.loads(
        registry.execute(
            "execute_shell",
            {"command": "echo ok", "writable_roots": [str(ext)]},
            tmp_path,
            ctx,
        )
    )
    assert result["status"] == "ok"
    assert "writable_roots" not in result.get("message", "")


def test_definitive_deny_reason_respects_sandbox_mode(tmp_path):
    from easycode.models.base import ToolCall
    from easycode.permissions.approval import definitive_deny_reason
    from easycode.permissions.policy import SANDBOX_DANGER_FULL_ACCESS

    tc = ToolCall(id="t1", name="execute_shell", arguments={"command": "rm -rf /"})
    assert definitive_deny_reason(tc, PathContext(primary=tmp_path)) is not None
    assert (
        definitive_deny_reason(tc, PathContext(primary=tmp_path, sandbox_mode=SANDBOX_DANGER_FULL_ACCESS))
        is None
    )


def test_secondary_root_keeps_workspace_access_but_protects_metadata(tmp_path):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    (secondary / ".git").mkdir()
    (secondary / ".easycode").mkdir()
    registry = build_registry(8000)
    ctx = PathContext(primary=primary, secondary=[secondary])

    writable = json.loads(
        registry.execute(
            "write_file",
            {"path": str(secondary / "module.py"), "content": "value = 1\n"},
            primary,
            ctx,
        )
    )
    assert writable["status"] == "ok"
    assert (secondary / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    assert ctx.classify(secondary / "module.py") == "workspace"

    for protected in (secondary / ".git" / "config", secondary / ".easycode" / "state.json"):
        result = json.loads(
            registry.execute(
                "write_file", {"path": str(protected), "content": "blocked"}, primary, ctx
            )
        )
        assert result["status"] == "error"
        assert not protected.exists()


def test_normal_shell_commands_are_not_in_destructive_denylist(tmp_path):
    from easycode.permissions.approval import destructive_command_reason

    assert destructive_command_reason("git status --short") is None
    assert destructive_command_reason("pytest tests/test_permissions.py -q") is None


def test_file_tools_keep_protected_paths_closed_in_workspace_write(tmp_path):
    """The sandboxed presets keep the permanent write boundaries: neither a
    workspace-write context nor an approval grant may open them."""
    from easycode.permissions.boundary import CONFIG_FILENAME

    (tmp_path / ".git").mkdir()
    (tmp_path / ".easycode").mkdir()
    ctx = PathContext(primary=tmp_path)
    registry = build_registry(8000)

    for rel in (".git/config", ".easycode/blocked.txt", CONFIG_FILENAME):
        target = tmp_path / rel
        result = json.loads(
            registry.execute("write_file", {"path": str(target), "content": "x"}, tmp_path, ctx)
        )
        assert result["status"] == "error", rel
        assert result["protected"] is True, rel
        assert not target.exists(), rel


def test_sandbox_command_grant_fails_closed_non_macos(monkeypatch, tmp_path):
    import easycode.permissions.sandbox.macos as macos

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        macos.sandbox_command(["/bin/sh", "-c", "true"], PathContext(primary=tmp_path), grant=ToolGrant(network_allowed=True))


# ----------------------------------------------------------------


def make_app(tmp_path: Path, bind_host: str = "127.0.0.1", bind_port: int = 8000):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **agent_kwargs):
        root = agent_kwargs.get("root") or tmp_path
        return Agent(
            provider=FakeProvider(script=[]),
            registry=build_registry(8000),
            root=Path(root),
        )

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(
        create_app(
            cfg=cfg,
            session_store=store,
            static_dir=tmp_path / "no-dist",
            bind_host=bind_host,
            bind_port=bind_port,
        )
    )


def test_model_detail_hides_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    key_id = new_credential_id()
    save_credential(Credential(key_id=key_id, api_key="sk-super-secret", base_url="https://api.x"))
    cfg.set_model_alias("fake-a", {"model": "fake/a", "key_id": key_id})

    def factory(alias: str, **agent_kwargs):
        return Agent(
            provider=FakeProvider(script=[]),
            registry=build_registry(8000),
            root=tmp_path,
        )

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    r = client.get("/api/models/fake-a")
    assert r.status_code == 200
    data = r.json()
    assert "api_key" not in data
    assert "sk-super-secret" not in json.dumps(data)
    assert data["has_api_key"] is True
    assert data["key_tail"] == "cret"


def test_cross_origin_post_rejected(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/chat", json={"message": "hi"}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403


def test_local_origin_post_allowed(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/chat", json={"message": "hi"}, headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


def test_cli_web_host_default_loopback():
    from typer.main import get_command

    from easycode.cli import app as cli_app

    web_cmd = get_command(cli_app).commands["web"]
    sig = inspect.signature(web_cmd.callback)
    assert sig.parameters["host"].default == "127.0.0.1"


# ------------------------------------------------------ origin guard tightening
# Origin gate: the control plane must only accept state-change requests from the
# loopback bind's exact same origin (plus the known dev-frontend origins). The
# three old escape hatches (no-Origin always allowed, Origin host == Host host,
# any loopback host on any port) must all be closed.

def test_origin_is_local_no_origin_loopback_only():
    """No-Origin (CLI/curl/own tests) is allowed ONLY on a loopback bind."""
    assert _origin_is_local(None, "127.0.0.1", 8000) is True
    assert _origin_is_local(None, "localhost", 8000) is True
    assert _origin_is_local(None, "::1", 8000) is True
    # Non-loopback bind (e.g. `--host 0.0.0.0`) rejects a missing Origin.
    assert _origin_is_local(None, "0.0.0.0", 8000) is False


def test_origin_host_equals_host_header_equivalent_rejected(tmp_path):
    """Origin hole #1: Origin host == Host hostname must NOT pass (DNS-rebinding)."""
    assert _origin_is_local("http://evil.example.com:8000", "127.0.0.1", 8000) is False
    client = make_app(tmp_path)
    r = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"Host": "evil.example.com:8000", "Origin": "http://evil.example.com:8000"},
    )
    assert r.status_code == 403


def test_origin_localhost_arbitrary_port_rejected(tmp_path):
    """Origin hole #2: any loopback host on any port must no longer pass; the
    bound PORT must match (5273 is not the bound port)."""
    assert _origin_is_local("http://localhost:4173", "127.0.0.1", 8000) is False
    client = make_app(tmp_path)
    r = client.post("/api/chat", json={"message": "hi"}, headers={"Origin": "http://localhost:4173"})
    assert r.status_code == 403


def test_origin_exact_same_origin_accepted(tmp_path):
    """A same-origin request (http/https, same host+port as the listener) passes;
    loopback hostname aliasing (localhost<->127.0.0.1) is kept so the built
    frontend is usable whether the user visits ``localhost`` or ``127.0.0.1``."""
    assert _origin_is_local("http://127.0.0.1:8000", "127.0.0.1", 8000) is True
    assert _origin_is_local("https://127.0.0.1:8000", "127.0.0.1", 8000) is True
    assert _origin_is_local("http://localhost:8000", "127.0.0.1", 8000) is True
    # A different port on the same loopback host is still rejected.
    assert _origin_is_local("http://127.0.0.1:9000", "127.0.0.1", 8000) is False
    client = make_app(tmp_path)
    r = client.post("/api/chat", json={"message": "hi"}, headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


def test_origin_known_dev_frontend_allowed():
    """Dev Vite server origins remain a documented, loopback-only exception."""
    assert _origin_is_local("http://localhost:5173", "127.0.0.1", 8000) is True
    assert _origin_is_local("http://127.0.0.1:5173", "127.0.0.1", 8000) is True


def test_nonloopback_bind_requires_token(tmp_path, monkeypatch):
    """Non-loopback bind (--host 0.0.0.0) demands a bearer token; no-Origin and
    bad tokens are fail-closed. Only a valid EASYCODE_WEB_TOKEN reaches the API."""
    monkeypatch.setenv("EASYCODE_WEB_TOKEN", "sekrit")
    client = make_app(tmp_path, bind_host="0.0.0.0")
    # No Origin on a non-loopback bind is rejected outright.
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 403
    ok_origin = {"Origin": "http://127.0.0.1:8000"}
    # Valid Origin but no / wrong token -> 401.
    assert client.post("/api/chat", json={"message": "hi"}, headers=ok_origin).status_code == 401
    assert (
        client.post(
            "/api/chat", json={"message": "hi"}, headers={**ok_origin, "Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    # Correct token passes through to the API.
    assert (
        client.post(
            "/api/chat",
            json={"message": "hi"},
            headers={**ok_origin, "Authorization": "Bearer sekrit"},
        ).status_code
        == 200
    )


def test_nonloopback_bind_fails_closed_without_token_env(tmp_path, monkeypatch):
    """If EASYCODE_WEB_TOKEN is unset, a non-loopback bind rejects every
    state-change request (security-first fail-closed)."""
    monkeypatch.delenv("EASYCODE_WEB_TOKEN", raising=False)
    client = make_app(tmp_path, bind_host="0.0.0.0")
    r = client.post("/api/chat", json={"message": "hi"}, headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code in (401, 403)
    assert r.status_code != 200


# ------------------------------------------------------------- worktree safety
# create_worktree: `.worktreeinclude` entries must resolve INSIDE the source and
# worktree roots (absolute paths / `..` escapes are rejected with a warning), and
# the repository-controlled `.easycode/setup.sh` must run through the SAME seatbelt
# boundary as `execute_shell` — no unsandboxed escape hatch.

import subprocess as _sp


def _mk_repo_tmp(tmp_path: Path, name: str) -> Path:
    """Minimal committed git repo for worktree endpoint tests (isolated)."""
    repo = tmp_path / name
    repo.mkdir()
    _sp.run(["git", "init", "-q"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    _sp.run(["git", "add", "f.txt"], cwd=repo, check=True)
    _sp.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_worktreeinclude_rejects_escaping_parent_entry(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo_tmp(tmp_path, "proj-wt-escape")
    # Hostile listing tries to copy a SIBLING file (outside the repo root).
    secret = tmp_path / "secret"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (repo / ".worktreeinclude").write_text("../secret\n", encoding="utf-8")
    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})
    assert r.status_code == 200, r.text
    data = r.json()
    warnings = data.get("warnings", [])
    assert any("secret" in w or "../secret" in w for w in warnings), warnings
    wt = Path(data["root"])
    # Not copied into the worktree, not smuggled into the worktree parent dir.
    assert not (wt / "secret").exists()
    assert not (data_home() / "worktrees" / "secret").exists()
    # The host file is untouched (never read into the tree).
    assert secret.read_text(encoding="utf-8") == "TOP SECRET"


def test_worktreeinclude_rejects_absolute_path_entry(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo_tmp(tmp_path, "proj-wt-abs")
    (repo / ".worktreeinclude").write_text("/etc/hosts\n", encoding="utf-8")
    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})
    assert r.status_code == 200, r.text
    data = r.json()
    warnings = data.get("warnings", [])
    assert any("/etc/hosts" in w for w in warnings), warnings
    assert not (Path(data["root"]) / "hosts").exists()


def test_worktreeinclude_copies_valid_entries(tmp_path):
    client = make_app(tmp_path)
    repo = _mk_repo_tmp(tmp_path, "proj-wt-ok")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide", encoding="utf-8")
    (repo / "notes.md").write_text("notes", encoding="utf-8")
    (repo / ".worktreeinclude").write_text("docs/\nnotes.md\n# a comment\n", encoding="utf-8")
    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})
    assert r.status_code == 200, r.text
    data = r.json()
    wt = Path(data["root"])
    assert (wt / "docs" / "guide.md").read_text(encoding="utf-8") == "guide"
    assert (wt / "notes.md").read_text(encoding="utf-8") == "notes"
    assert not data.get("warnings"), data.get("warnings")


def test_worktree_setup_script_runs_through_sandbox_command(tmp_path, monkeypatch):
    import easycode.web.routes_workspaces as main_mod

    client = make_app(tmp_path)
    repo = _mk_repo_tmp(tmp_path, "proj-wt-setup")
    (repo / ".easycode").mkdir()
    (repo / ".easycode" / "setup.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    _sp.run(["git", "add", ".easycode/setup.sh"], cwd=repo, check=True)
    _sp.run(["git", "commit", "-qm", "add setup"], cwd=repo, check=True)

    calls: list[list[str]] = []

    def fake_sandbox(command: list[str], ctx, **kw):
        calls.append(list(command))
        # A portable no-op so the endpoint proceeds without a real seatbelt profile.
        return ["sh", "-c", "exit 0"]

    monkeypatch.setattr(main_mod, "sandbox_command", fake_sandbox)

    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})
    assert r.status_code == 200, r.text
    data = r.json()
    wt = Path(data["root"])
    setup_path = str(wt / ".easycode" / "setup.sh")
    # The repo script was handed to the sandbox wrapper, not run bare.
    assert any(cmd == ["bash", setup_path] for cmd in calls), calls
    assert any("ran .easycode/setup.sh" in n for n in data.get("notes", [])), data.get("notes")


def test_worktree_setup_script_fails_closed_when_sandbox_unavailable(tmp_path, monkeypatch):
    import easycode.web.routes_workspaces as main_mod

    client = make_app(tmp_path)
    repo = _mk_repo_tmp(tmp_path, "proj-wt-setup-nosand")
    (repo / ".easycode").mkdir()
    (repo / ".easycode" / "setup.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    _sp.run(["git", "add", ".easycode/setup.sh"], cwd=repo, check=True)
    _sp.run(["git", "commit", "-qm", "add setup"], cwd=repo, check=True)

    def unsupported(command: list[str], ctx, **kw):
        raise RuntimeError("workspace sandbox is currently supported only on macOS")

    monkeypatch.setattr(main_mod, "sandbox_command", unsupported)

    r = client.post("/api/workspaces/worktree", json={"root": str(repo)})
    assert r.status_code == 200, r.text
    data = r.json()
    warnings = " ".join(data.get("warnings", []))
    assert any(tok in warnings for tok in ("macOS", "Seatbelt", "sandbox")), warnings
    # the script was NOT run (fail-closed, no unsandboxed fallback)
    wt = Path(data["root"])
    assert (wt / ".easycode" / "setup.sh").is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
@pytest.mark.parametrize("sandbox_mode", ["workspace-write", "danger-full-access"])
def test_managed_worktree_root_is_usable_in_every_mode(tmp_path, monkeypatch, sandbox_mode):
    """A worktree under ~/.easycode/worktrees must be readable/writable by its
    own shell in every permission mode (allow-all used to deny the whole data
    home, breaking even `pwd`)."""
    _, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    worktree = data_home() / "worktrees" / "repo"
    worktree.mkdir(parents=True)
    (worktree / "source.py").write_text("x = 1\n", encoding="utf-8")
    ctx = PathContext(primary=worktree, sandbox_mode=sandbox_mode)

    result = json.loads(
        build_registry(8000).execute(
            "execute_shell",
            {"command": "pwd && cat source.py && printf 'y=2\\n' > own.txt && cat own.txt"},
            worktree,
            ctx,
        )
    )
    assert result["status"] == "ok", result
    assert result["exit_code"] == 0, result
    assert (worktree / "own.txt").read_text(encoding="utf-8") == "y=2\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_worktree_exception_keeps_state_dirs_denied(tmp_path, monkeypatch):
    """The worktree exception must not open sessions, global extensions or
    credentials to model-originated shells (full access, which drops the whole
    firewall by design, is covered in ``test_permissions``)."""
    _, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    worktree = data_home() / "worktrees" / "repo"
    worktree.mkdir(parents=True)
    sessions = data_home() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.json").write_text('{"secret":"session-record"}', encoding="utf-8")
    cred = data_home() / "credentials.json"
    cred.write_text('{"api_key":"sk-worktree-secret"}', encoding="utf-8")
    ctx = PathContext(primary=worktree)
    registry = build_registry(8000)

    for command, needle in (
        (f"cat {sessions / 's.json'}", "session-record"),
        (f"cat {cred}", "sk-worktree-secret"),
        (f"printf x > {sessions / 'new.json'}", None),
    ):
        result = json.loads(
            registry.execute("execute_shell", {"command": command}, worktree, ctx)
        )
        assert result["status"] == "ok", (command, result)
        assert result["exit_code"] != 0, (command, result)
        if needle is not None:
            assert needle not in json.dumps(result), command
    assert not (sessions / "new.json").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_grant_root_shell_cannot_write_metadata(tmp_path):
    """A granted external writable root stays writable, but its
    .git/.easycode/config remain write-protected in the Seatbelt policy."""
    proj = tmp_path / "proj"
    proj.mkdir()
    ext = tmp_path / "ext"
    (ext / ".git" / "hooks").mkdir(parents=True)
    ctx = PathContext(primary=proj)
    grant = ToolGrant(writable_roots=(ext,))
    command = (
        f"printf ok > {ext}/ok.txt; "
        f"printf x > {ext}/.git/hooks/x; "
        f"printf y > {ext}/easycode.config.json"
    )

    result = json.loads(
        build_registry(8000).execute(
            "execute_shell",
            {"command": command, "writable_roots": [str(ext)]},
            proj,
            ctx,
            grant=grant,
        )
    )
    assert result["status"] == "ok", result
    assert (ext / "ok.txt").read_text(encoding="utf-8") == "ok"
    assert not (ext / ".git" / "hooks" / "x").exists()
    assert not (ext / "easycode.config.json").exists()


# -------------------------------------------------- finder prompt sanitization
# ``choose_folders_via_finder`` splices a user-controlled ``prompt`` into an
# ``osascript -e`` AppleScript literal. Only printable, trimmed, length-capped
# text may reach the literal — never a raw newline or control character that
# could break the ``choose folder`` statement, and quotes/backslashes stay
# escaped so the value cannot escape the string literal.


def test_finder_prompt_sanitizer_strips_controls_and_caps():
    import easycode.web.platform as platform_mod

    raw = "a\nb\tc\x00d" + "x" * 1000
    out = platform_mod._sanitize_finder_prompt(raw)
    assert "\n" not in out
    assert "\t" not in out
    assert "\x00" not in out
    # length-capped for the script literal
    assert len(out) == platform_mod.FINDER_PROMPT_MAX_LEN
    # leading/trailing whitespace is trimmed
    assert platform_mod._sanitize_finder_prompt("   hi  ") == "hi"


@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
def test_finder_prompt_script_no_newline_and_quotes_escaped(tmp_path, monkeypatch):
    import subprocess as sp

    import easycode.web.routes_workspaces as main_mod

    monkeypatch.setattr(main_mod, "finder_supported", lambda: True)
    captured: dict[str, list[str]] = {}

    class DummyProc:
        returncode = 0
        stdout = "/tmp/some/folder\n"
        stderr = ""

    def fake_run(cmd, **kw):
        captured["argv"] = cmd
        return DummyProc()

    monkeypatch.setattr(sp, "run", fake_run)

    hostile = '选目录\n注入 do shell script "touch /tmp/pwned" 结束'
    main_mod.choose_folders_via_finder(multiple=False, prompt=hostile)
    argv = captured["argv"]
    assert argv[0] == "osascript"
    script = argv[2]  # the single ``-e`` argument
    # No raw newline / control character reaches the interpreter argument.
    assert "\n" not in script
    assert "\r" not in script
    # The intended plain text survives.
    assert "选目录" in script
    # The hostile text stays inert: its quotes are escaped into the literal.
    assert "do shell script" in script
    assert '\\"touch' in script, script
    # The prompt renders as a single AppleScript string literal (one open+close).
    assert script.startswith('POSIX path of (choose folder with prompt "')


# ------------------------------------------------------- data_home write denial
# The shell sandbox (workspace-write) lists ``data_home()`` in ``writable_roots``
# so the app's Python can persist sessions/credentials, but that must NOT make
# ``~/.easycode`` a shell write target — a model-originated shell could otherwise
# silently create/delete ``~/.easycode/sessions/*`` bypassing SessionStore.


def test_secret_policy_denies_data_home_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from easycode.permissions.sandbox.macos import _secret_policy

    dh = data_home()
    policy = _secret_policy(include_write=True, protect_all_data_home=True)
    lit = str(dh.resolve())
    assert f'(deny file-read* (subpath "{lit}"))' in policy
    # The read denial alone did not stop a shell truncating/creating files under
    # ~/.easycode; a write denial must accompany it.
    assert f'(deny file-write* (subpath "{lit}"))' in policy


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_sandbox_command_profile_denies_data_home_write(tmp_path, monkeypatch):
    from easycode.permissions.sandbox.macos import sandbox_command

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    ctx = PathContext(primary=proj)
    wrapped = sandbox_command(["/bin/sh", "-c", "true"], ctx)
    policy = wrapped[2]
    lit = str(data_home().resolve())
    assert f'(deny file-read* (subpath "{lit}"))' in policy
    assert f'(deny file-write* (subpath "{lit}"))' in policy


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_shell_cannot_write_sessions_in_workspace_write(tmp_path, monkeypatch):
    """A plain workspace-write shell cannot create/modify ~/.easycode/sessions."""
    proj, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    ctx = PathContext(primary=proj)  # default sandbox_mode = workspace-write
    sessions = data_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    target = sessions / "evil.json"
    target.write_text("ORIGINAL", encoding="utf-8")  # must be left untouched

    result = json.loads(
        build_registry(8000).execute(
            "execute_shell", {"command": f"printf '{{}}' > {target}"}, proj, ctx
        )
    )
    assert result["status"] == "ok"  # the shell ran at all
    assert result["exit_code"] != 0  # the write was denied by Seatbelt
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# ---------------------------------------------------------------------------
# SC-105: ask/auto-review 模式下，read_file 目标在项目目录之外（如 ~/.ssh）
# 必须先经审批，不能静默读取外部敏感文件。
# ---------------------------------------------------------------------------

def test_read_file_external_requires_approval_ask_mode(tmp_path, monkeypatch):
    from easycode.models.base import ToolCall
    from easycode.permissions.approval import needs_approval
    from easycode.permissions.boundary import PathContext

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # pytest 的 tmp 在 OS 临时目录下；把 tempdir 指到别处，outside 才是真正的 external
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home / "tdir"))
    (home / "tdir").mkdir()
    ext = home / "outside"  # HOME 之下、项目/临时目录/data_home 之外 -> classify=external
    ext.mkdir()
    secret = ext / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ok.txt").write_text("fine", encoding="utf-8")
    ctx = PathContext(primary=proj)

    assert needs_approval(ToolCall(id="a", name="read_file", arguments={"path": str(secret)}), ctx, "ask") is True
    # 项目内读取保持免批
    assert needs_approval(ToolCall(id="b", name="read_file", arguments={"path": "ok.txt"}), ctx, "ask") is False
    # allow-all 保持不问
    assert needs_approval(ToolCall(id="c", name="read_file", arguments={"path": str(secret)}), ctx, "allow-all") is False


@pytest.mark.asyncio
async def test_read_file_external_denied_without_approval(tmp_path, monkeypatch):
    """agent 级：ask 模式下外部读取触发审批；拒绝后内容不得落地。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home / "tdir"))
    (home / "tdir").mkdir()
    ext = home / "outside"
    ext.mkdir()
    secret = ext / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()

    script = [{"tool_calls": [("c1", "read_file", {"path": str(secret)})]}, {"text": "done"}]
    agent = Agent(FakeProvider(script=script), build_registry(8_000), proj)
    prompts: list[str] = []

    async def approval(tc, *_args):
        prompts.append(tc.name)
        return False

    agent.approval_handler = approval
    events = [event async for event in agent.respond("read it")]
    result = next(event.tool_result for event in events if event.kind == "tool_result")

    assert prompts == ["read_file"]
    assert "top-secret" not in result


@pytest.mark.asyncio
async def test_read_file_external_allowed_after_approval(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home / "tdir"))
    (home / "tdir").mkdir()
    ext = home / "outside"
    ext.mkdir()
    secret = ext / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()

    script = [{"tool_calls": [("c1", "read_file", {"path": str(secret)})]}, {"text": "done"}]
    agent = Agent(FakeProvider(script=script), build_registry(8_000), proj)

    async def approval(_tc, *_args):
        return True

    agent.approval_handler = approval
    events = [event async for event in agent.respond("read it")]
    result = next(event.tool_result for event in events if event.kind == "tool_result")

    assert json.loads(result)["status"] == "ok"
    assert "top-secret" in result


@pytest.mark.asyncio
async def test_mcp_command_follows_the_session_sandbox_mode(monkeypatch, tmp_path):
    """MCP 子进程走与 Shell 相同的 ``sandbox_command``：沙箱模式包 Seatbelt，
    完全访问按原命令启动（环境清洗两边都在）。"""
    import contextlib

    from easycode.extensions.mcp import client as mcp_module
    from easycode.permissions.policy import SANDBOX_DANGER_FULL_ACCESS

    captured: list = []

    @contextlib.asynccontextmanager
    async def fake_stdio(params):
        captured.append(params)
        raise RuntimeError("no child process in this test")
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(mcp_module, "stdio_client", fake_stdio)
    conf = mcp_module.MCPServerConfig.parse("demo", {"command": "mcp-server", "args": ["--stdio"]})

    full = PathContext(primary=tmp_path, sandbox_mode=SANDBOX_DANGER_FULL_ACCESS)
    with pytest.raises(RuntimeError):
        await mcp_module.MCPConnection(conf, full).start()
    assert captured[-1].command == "mcp-server"
    assert captured[-1].args == ["--stdio"]

    if sys.platform == "darwin":
        with pytest.raises(RuntimeError):
            await mcp_module.MCPConnection(conf, PathContext(primary=tmp_path)).start()
        assert captured[-1].command.endswith("sandbox-exec")
