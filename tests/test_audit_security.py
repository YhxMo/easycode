"""Batch A security regressions (SC-1 .. SC-4) from .audit/plan.md.

- item-01 (SC-1 + SC-2): credential paths are unconditionally protected and
  reading / writing / enumerating them is rejected by every file tool.
- item-02 (SC-3): an approved shell (``force_allowed=True``) relaxes only the
  network dimension — file-write and process limits are retained; only
  ``danger-full-access`` disables the sandbox.
- item-03 (SC-4): model detail redacts ``api_key``; cross-origin state-change
  POSTs are rejected; the CLI ``web`` host stays loopback.

Tests are isolated: temporary HOME, scripted FakeProvider, no real service.
"""

from __future__ import annotations

import inspect
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.credentials import Credential, data_home, new_credential_id, save_credential
from easycode.mcp import StdioTransport
from easycode.sandbox import child_env
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from easycode.workspace import PathContext
from tests.conftest import FakeProvider


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


# ---------------------------------------------------------------- item-01 (SC-1+SC-2)


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


def test_write_file_cannot_bypass_credentials_with_force_allowed(tmp_path, monkeypatch):
    proj, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    cred = data_home() / "credentials.json"

    out = json.loads(
        build_registry(8000).execute(
            "write_file", {"path": str(cred), "content": "{}"}, proj, force_allowed=True
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
    # SC-2: a not-yet-existing credential file is still off-limits for writes.
    assert ctx.in_allowed(cred) is False
    assert ctx.is_protected(cred) is True


# ---------------------------------------------------------------- item-02 (SC-3)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_sandbox_command_force_allowed_relaxes_network_not_bare(tmp_path):
    from easycode.sandbox.macos import sandbox_command

    cmd = ["/bin/sh", "-c", "curl -I https://example.com"]
    ctx = PathContext(primary=tmp_path)
    wrapped = sandbox_command(cmd, ctx, force_allowed=True)

    assert wrapped != cmd, "force_allowed must not return the bare (unsandboxed) command"
    assert wrapped[0] == "/usr/bin/sandbox-exec"
    policy = wrapped[2]
    assert "(deny default)" in policy
    assert "(allow network*)" in policy  # only the network dimension is relaxed

    # Without force_allowed, network stays denied.
    default_wrapped = sandbox_command(cmd, ctx)
    assert "(allow network*)" not in default_wrapped[2]


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
def test_approved_shell_still_cannot_write_credentials(tmp_path, monkeypatch):
    from easycode.sandbox.macos import sandbox_command

    _, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    ctx = PathContext(primary=proj)
    cred = data_home() / "credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["/bin/sh", "-c", f"printf '{{}}' > {cred}"]
    # force_allowed=True simulates an approved shell: file-write protections remain.
    proc = subprocess.run(sandbox_command(cmd, ctx, force_allowed=True), capture_output=True, text=True)
    assert proc.returncode != 0
    assert not cred.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration is macOS-only")
@pytest.mark.parametrize("sandbox_mode", ["workspace-write", "danger-full-access"])
def test_shell_cannot_read_credentials_in_any_mode(tmp_path, monkeypatch, sandbox_mode):
    """The shell path must not bypass the file-tool credential firewall."""
    _, home = make_home_ctx(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    cred = data_home() / "credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text('{"api_key":"sk-shell-secret"}', encoding="utf-8")

    ctx = PathContext(primary=proj, sandbox_mode=sandbox_mode)
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


@pytest.mark.asyncio
async def test_stdio_mcp_uses_sanitized_env_and_explicit_values(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class DummyTransport:
        async def write(self, _data):
            return None

    async def fake_create(*command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            stdin=DummyTransport(),
            stdout=DummyTransport(),
            stderr=DummyTransport(),
            returncode=None,
        )

    async def no_read(self):
        return None

    async def no_err(self):
        return None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-secret")
    monkeypatch.setenv("MCP_SAFE_PARENT", "kept")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    transport = StdioTransport(
        command="mcp-server",
        args=["--stdio"],
        env={"MCP_EXPLICIT": "configured", "MCP_EXPLICIT_TOKEN": "intentionally-configured"},
        cwd=None,
        ctx=PathContext(primary=tmp_path),
    )
    monkeypatch.setattr(transport, "_read_loop", no_read.__get__(transport))
    monkeypatch.setattr(transport, "_err_loop", no_err.__get__(transport))

    await transport.start()
    env = captured["env"]
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
    from easycode.approval import destructive_command_reason

    assert destructive_command_reason("git status --short") is None
    assert destructive_command_reason("pytest tests/test_permissions.py -q") is None


def test_sandbox_command_force_allowed_fails_closed_non_macos(monkeypatch, tmp_path):
    import easycode.sandbox.macos as macos

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        macos.sandbox_command(["/bin/sh", "-c", "true"], PathContext(primary=tmp_path), force_allowed=True)


# ---------------------------------------------------------------- item-03 (SC-4)


def make_app(tmp_path: Path):
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
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


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
