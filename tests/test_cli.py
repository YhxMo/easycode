"""CLI entry-point smoke test without a real model or interactive terminal."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycode import cli
from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from tests.conftest import FakeProvider, fake_agent


def test_main_runs_repl(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agent = fake_agent(tmp_path)
    monkeypatch.setattr(cli, "make_agent", lambda *args, **kwargs: agent)
    calls = []

    async def repl(cfg, active_agent, current, commands):
        calls.append(active_agent)
        assert "undo" not in {command.name for command in commands.list()}

    monkeypatch.setattr(cli, "repl_loop", repl)
    result = CliRunner().invoke(cli.app, ["main", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls == [agent]


@pytest.mark.asyncio
async def test_model_switch_without_credential_keeps_config(tmp_path, monkeypatch):
    """A failed model switch must not persist the new default or rebind the agent."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg_path = proj / "easycode.config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "default_model": "ok",
                "models": {
                    "ok": {"model": "openai/gpt-4o", "key_id": "k"},
                    "x": {"model": "openai/gpt-4o-mini"},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(start=proj)
    agent = Agent(
        provider=FakeProvider(model="fake/model"), registry=build_registry(8000), root=proj
    )
    commands = cli.build_commands(agent, [proj])
    before = cfg_path.read_text(encoding="utf-8")

    result = await cli.handle_command("/model x", cfg, agent, cfg.default_model, commands)

    assert result is None
    assert cfg.default_model == "ok"
    assert cfg_path.read_text(encoding="utf-8") == before
    assert agent.provider.model == "fake/model"


@pytest.mark.asyncio
async def test_run_collects_real_changes_only(tmp_path, monkeypatch):
    """/run summarizes real writes only: reads and dry-run previews are ignored."""
    (tmp_path / "out.txt").write_text("one\n", encoding="utf-8")
    script = [
        {
            "tool_calls": [
                ("c1", "write_file", {"path": "out.txt", "content": "three\n"}),
                (
                    "c2",
                    "edit_file",
                    {
                        "path": "out.txt",
                        "old_string": "one",
                        "new_string": "two",
                        "dry_run": True,
                    },
                ),
                ("c3", "read_file", {"path": "out.txt"}),
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = fake_agent(tmp_path, script)
    captured: list[list[dict]] = []
    monkeypatch.setattr(cli, "show_diff_summary", captured.append)

    await cli._cmd_run(agent, "make a change")

    assert len(captured) == 1
    assert len(captured[0]) == 1
    assert captured[0][0]["tool"] == "write_file"
    assert captured[0][0]["path"] == "out.txt"


def test_main_rejects_sensitive_root(tmp_path, monkeypatch):
    """DEC-T5: the CLI primary root goes through root_error validation."""
    monkeypatch.setenv("HOME", str(tmp_path))
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    result = CliRunner().invoke(cli.app, ["main", "--root", str(git_dir)])

    assert result.exit_code != 0
    assert "sensitive directory" in result.output


# ------------------------------------------------------------ 完全访问启动确认
# danger-full-access 会关闭沙箱与审批，因此启动时要说出来：交互式问一次，
# 没有终端可用时必须显式传 --confirm-full-access，否则拒绝启动。


def _stub_repl(monkeypatch):
    agent = fake_agent(Path.cwd())
    monkeypatch.setattr(cli, "make_agent", lambda *args, **kwargs: agent)
    started: list[str] = []

    async def repl(cfg, active_agent, current, commands):
        # ``make_agent`` is stubbed out, so the resolved config is what says
        # which mode the run would have started in.
        started.append(cfg.permission_mode)

    monkeypatch.setattr(cli, "repl_loop", repl)
    return started


def test_main_full_access_needs_confirmation_without_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)
    started = _stub_repl(monkeypatch)

    result = CliRunner().invoke(cli.app, ["main", "--root", str(tmp_path), "--permission", "allow-all"])

    assert result.exit_code == 1
    assert "--confirm-full-access" in result.output
    assert started == []


def test_main_confirm_full_access_flag_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)
    started = _stub_repl(monkeypatch)

    result = CliRunner().invoke(
        cli.app,
        ["main", "--root", str(tmp_path), "--permission", "allow-all", "--confirm-full-access"],
    )

    assert result.exit_code == 0, result.output
    assert started == ["allow-all"]
    # 该参数只确认本次运行，不会把模式写回配置文件。
    cfg_path = tmp_path / "easycode.config.json"
    assert not cfg_path.exists() or "allow-all" not in cfg_path.read_text(encoding="utf-8")


def test_main_interactive_full_access_asks_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_can_prompt", lambda: True)
    started = _stub_repl(monkeypatch)

    declined = CliRunner().invoke(
        cli.app, ["main", "--root", str(tmp_path), "--permission", "allow-all"], input="n\n"
    )
    assert declined.exit_code == 1
    assert started == []

    accepted = CliRunner().invoke(
        cli.app, ["main", "--root", str(tmp_path), "--permission", "allow-all"], input="y\n"
    )
    assert accepted.exit_code == 0, accepted.output
    assert started == ["allow-all"]


def test_main_full_access_from_config_file_needs_the_flag(tmp_path, monkeypatch):
    """配置文件里写 allow-all 与命令行选择同等对待：没有终端确认就要这个参数。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)
    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"permission": "allow-all"}), encoding="utf-8"
    )
    started = _stub_repl(monkeypatch)

    refused = CliRunner().invoke(cli.app, ["main", "--root", str(tmp_path)])
    assert refused.exit_code == 1
    assert started == []

    allowed = CliRunner().invoke(cli.app, ["main", "--root", str(tmp_path), "--confirm-full-access"])
    assert allowed.exit_code == 0, allowed.output
    assert started == ["allow-all"]


def test_main_sandboxed_modes_never_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)
    started = _stub_repl(monkeypatch)

    for mode in ("ask", "auto-review"):
        result = CliRunner().invoke(cli.app, ["main", "--root", str(tmp_path), "--permission", mode])
        assert result.exit_code == 0, (mode, result.output)
    assert started == ["ask", "auto-review"]
