"""REPL CLI entry point."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from easycode.agent.events import file_change
from easycode.agent.factory import bind_agent, make_agent
from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.permissions.boundary import root_error
from easycode.permissions.policy import PERM_ALLOW_ALL
from easycode.ui.render import (
    banner,
    console,
    model_switched,
    render_event,
    show_diff_summary,
)

if TYPE_CHECKING:
    from easycode.extensions.commands import CommandRegistry

app = typer.Typer(help="easycode — a CLI coding agent", no_args_is_help=False)


def build_commands(agent: Agent, roots: list[Path]) -> CommandRegistry:
    """CommandRegistry with built-ins + user templates + skill commands."""
    from easycode.extensions.commands import build_registry

    return build_registry(
        roots,
        agent.skills,
        builtins=(
            ("help", "show this help"),
            ("exit", "quit the REPL"),
            ("model", "list / switch / add model aliases"),
            ("run", "execute a change task and summarize diffs"),
            ("skills", "list available skills"),
            ("agents", "list delegatable agents"),
        ),
    )


async def run_turn(agent: Agent, user_input: str) -> None:
    async for ev in agent.respond(user_input):
        render_event(ev)


async def repl_loop(cfg: Config, agent: Agent, current: str, commands) -> None:
    keys = KeyBindings()

    @keys.add(Keys.Enter)
    def _enter(event):
        event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    history_file = Path.cwd() / ".easycode_history"
    session = PromptSession(
        key_bindings=keys,
        history=FileHistory(str(history_file)),
        multiline=True,
        prompt_continuation="  ",
    )

    always_allow: set[str] = set()
    from easycode.models.base import ToolCall

    async def approval_handler(tc: ToolCall, _reason: str, key: str) -> bool:
        if key in always_allow:
            return True
        console.print(
            f"[yellow]⚠ 需要批准:[/] {tc.name} {json.dumps(tc.arguments, ensure_ascii=False)}"
        )
        try:
            answer = (await session.prompt_async("允许? [y/N/a(always)] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("a", "always"):
            always_allow.add(key)
            return True
        return False

    agent.approval_handler = approval_handler

    while True:
        try:
            user_input = await session.prompt_async(">>> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/]")
            return

        text = user_input.strip()
        if not text:
            continue
        if text.startswith("/"):
            stop = await handle_command(text, cfg, agent, current, commands)
            if stop is False:
                return
            # Track the model the agent is actually bound to, not the config
            # default (a /model switch may have rebound it).
            current = agent.provider.model
            continue
        try:
            await run_turn(agent, text)
        except KeyboardInterrupt:
            console.print("\n[dim]interrupted[/]")


def _cli_main(cfg: Config, current: str, workdir: Path, secondary_root: list[Path] | None) -> None:
    err = root_error(workdir)
    if err is not None:
        console.print(f"[red]{err}[/]")
        raise typer.Exit(code=1)
    ctx = cfg.path_context(root=workdir, secondary=[str(p) for p in (secondary_root or [])])
    agent = make_agent(cfg, current, workdir, secondary_roots=ctx.secondary)
    commands = build_commands(agent, ctx.roots)
    console.print()
    banner(cfg.resolve_model(current))

    async def _repl() -> None:
        try:
            await repl_loop(cfg, agent, current, commands)
        finally:
            # The REPL owns its agent's MCP processes; close them on exit.
            await agent.close_mcp()

    asyncio.run(_repl())


@app.command()
def main(
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="model alias to start with")
    ] = None,
    root: Annotated[
        Path | None, typer.Option("--root", "-r", help="workspace root (default: cwd)")
    ] = None,
    secondary_root: Annotated[
        list[Path] | None,
        typer.Option("--secondary-root", help="extra workspace root (repeatable)"),
    ] = None,
    permission: Annotated[
        str | None, typer.Option("--permission", help="permission mode: ask/auto-review/allow-all")
    ] = None,
    confirm_full_access: Annotated[
        bool,
        typer.Option(
            "--confirm-full-access",
            help="confirm full access up front; required when no terminal can ask",
        ),
    ] = False,
) -> None:
    cfg = Config.load(start=root)
    if permission:
        from easycode.permissions.policy import permission_parse

        cfg.permission_mode = permission_parse(permission)
    if cfg.permission_mode == PERM_ALLOW_ALL:
        _confirm_full_access(confirm_full_access)
    current = model or cfg.default_model
    workdir = (root or Path.cwd()).resolve()
    _cli_main(cfg, current, workdir, secondary_root)


def _can_prompt() -> bool:
    """True when this run has a terminal to ask a question on."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_full_access(pre_confirmed: bool) -> None:
    """Gate a CLI startup that would run in full access.

    The preset drops the workspace boundary, the approval prompts and the data
    home firewall, so it is stated out loud rather than inherited silently from
    a config file: an interactive run asks once, and a run with no terminal to
    ask must pass ``--confirm-full-access`` instead of proceeding unconfirmed.
    """
    risk = "完全访问会关闭沙箱与审批，可读写宿主机任意路径（含 ~/.easycode 与会话数据）。"
    if not pre_confirmed:
        if not _can_prompt():
            console.print(f"[red]{risk}[/]")
            console.print("[red]无终端可确认，请加 --confirm-full-access 后重试[/]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]⚠ {risk}[/]")
        try:
            approved = typer.confirm("确认以完全访问模式启动？")
        except (EOFError, KeyboardInterrupt, typer.Abort):
            approved = False
        if not approved:
            console.print("[dim]已取消[/]")
            raise typer.Exit(code=1)
        return
    console.print(f"[yellow]⚠ {risk}[/]")


@app.command()
def web(
    host: Annotated[str, typer.Option("--host", help="bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="bind port")] = 8000,
    root: Annotated[
        Path | None, typer.Option("--root", "-r", help="workspace root (default: cwd)")
    ] = None,
) -> None:
    """Start the FastAPI web UI (serves frontend/dist if built)."""
    import uvicorn

    from easycode.web.main import create_app

    if root is not None:
        import os

        os.chdir(root)

    # Pass the delegate bind host/port so the origin guard can enforce
    # same-origin on the loopback bind and require a bearer token when the
    # control plane is exposed on a non-loopback interface.
    app = create_app(bind_host=host, bind_port=port)
    banner(f"web: http://{host}:{port}  (root: {app.state.store.root})")
    uvicorn.run(app, host=host, port=port)


async def handle_command(
    raw: str, cfg: Config, agent: Agent, current: str, commands
) -> bool | None:
    """Dispatch a slash command: templates/skills expand to a prompt; builtins run."""
    resolved = commands.resolve(raw)
    if resolved is None:
        console.print(f"[red]unknown command:[/] {raw.split()[0]}  （/help 查看全部）")
        return None
    cmd, rest = resolved
    if cmd.kind in ("template", "skill"):
        prompt = cmd.expand(rest)
        console.print(f"[bold cyan]/{cmd.name}[/] {rest}")
        await run_turn(agent, prompt)
        return None

    if cmd.name == "help":
        return _cmd_help(current, commands, agent)
    if cmd.name == "exit":
        return False
    if cmd.name == "run":
        return await _cmd_run(agent, rest)
    if cmd.name == "model":
        return _cmd_model(cfg, agent, current, rest)
    if cmd.name == "skills":
        if agent.skills:
            for s in agent.skills.list():
                console.print(f"  [cyan]{s.name}[/]: {s.description}")
        else:
            console.print("[dim]没有可用 skill（在 .easycode/skills/<name>/SKILL.md 创建）[/]")
        return None
    if cmd.name == "agents":
        if agent.agents:
            for a in agent.agents.list():
                marker = f" [dim]({a.model})[/]" if a.model else ""
                console.print(f"  [cyan]{a.name}[/]{marker}: {a.description}")
        else:
            console.print("[dim]没有可用 agent（在 .easycode/agents/<name>.md 创建）[/]")
        return None
    console.print(f"[red]unknown builtin: /{cmd.name}[/]")
    return None


def _cmd_help(current: str, commands, agent) -> None:
    from rich.panel import Panel

    lines = ["[bold]Commands[/]"]
    for c in commands.list():
        hint = f" {c.arg_hint}" if c.arg_hint else ""
        badge = {"builtin": "", "template": " [dim](custom)[/]", "skill": " [dim](skill)[/]"}[
            c.kind
        ]
        lines.append(f"  /{c.name}{hint}{badge}  {c.description}")
    lines += [
        "",
        "[bold]Editing[/]",
        "  Enter       send",
        "  Alt+Enter   newline",
        "  Ctrl+C      interrupt current turn",
        "  Ctrl+D      quit",
        "",
        f"[bold]Models[/]  [yellow]current: {current}[/]",
        "",
        "[bold]Skills[/]",
    ]
    if agent.skills:
        lines += [f"  {s.name}: {s.description}" for s in agent.skills.list()]
    else:
        lines.append("  (none)")
    lines.append("\n[bold]Agents[/]")
    if agent.agents:
        lines += [f"  {a.name}: {a.description}" for a in agent.agents.list()]
    else:
        lines.append("  (none)")
    console.print(Panel("\n".join(lines), title="Easy code", border_style="cyan"))


async def _cmd_run(agent, rest) -> None:
    task = rest or "完成当前工作区的修改任务"
    edits: list[dict] = []
    console.print(f"[bold cyan]/run[/] {task}")
    async for ev in agent.respond(task):
        render_event(ev)
        if ev.kind == "tool_result" and ev.tool_call:
            change = file_change(ev.tool_call.name, ev.tool_result or "")
            if change:
                edits.append(change)
    show_diff_summary(edits)


def _cmd_model(cfg: Config, agent: Agent, current: str, rest: str) -> None:
    if not rest:
        for alias in cfg.models:
            marker = " *" if alias == cfg.default_model else ""
            console.print(f"  {alias}{marker}: {cfg.models[alias].to_display()}")
        console.print(f"\n  current: {cfg.resolve_model(current)}")
    elif "=" in rest:
        alias, _, model_str = rest.partition("=")
        alias, model_str = alias.strip(), model_str.strip()
        if not alias or not model_str:
            console.print("[red]usage: /model <alias>=<litellm-model>[/]")
            return
        cfg.set_model_alias(alias, model_str)
        if cfg.default_model == alias or cfg.default_model not in cfg.models:
            cfg.set_default_model(alias)
        cfg.save()
        model_switched(f"{alias} → {model_str}")
    else:
        alias = rest
        if alias in cfg.models:
            try:
                bind_agent(agent, cfg, alias)
            except ValueError as exc:
                console.print(f"[red]{exc}[/]")
                return
            cfg.set_default_model(alias)
            cfg.save()
            model_switched(cfg.resolve_model(alias))
        else:
            console.print(f"[red]unknown alias: {alias} (use /model <alias>=<name> to add)[/]")
        return


if __name__ == "__main__":
    app()
