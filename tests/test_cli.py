"""CLI entry-point smoke test without a real model or interactive terminal."""

import json

import pytest
from typer.testing import CliRunner

from easycode import cli
from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from tests.conftest import FakeProvider


def test_main_runs_repl(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agent = Agent(provider=FakeProvider(), registry=build_registry(8000), root=tmp_path)
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
    agent = Agent(
        provider=FakeProvider(script=script), registry=build_registry(8000), root=tmp_path
    )
    captured: list[list[dict]] = []
    monkeypatch.setattr(cli, "show_diff_summary", captured.append)

    await cli._cmd_run(agent, "make a change")

    assert len(captured) == 1
    assert len(captured[0]) == 1
    assert captured[0][0]["tool"] == "write_file"
    assert captured[0][0]["path"] == "out.txt"
