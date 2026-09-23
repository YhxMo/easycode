"""CLI entry-point smoke test without a real model or interactive terminal."""

from typer.testing import CliRunner

from easycode import cli
from easycode.agent.loop import Agent
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
