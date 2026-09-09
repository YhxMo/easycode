"""REPL CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from easycode.agent.loop import Agent
from easycode.agentfactory import (
    apply_api_format as apply_api_format,  # re-export: tests import these from cli
)
from easycode.agentfactory import (
    build_provider as build_provider,
)
from easycode.agentfactory import (
    make_agent,
    rebind_agent,
)
from easycode.agentfactory import (
    provider_kwargs as provider_kwargs,
)
from easycode.approval import approval_key
from easycode.config import Config
from easycode.ui.render import (
    banner,
    console,
    model_switched,
    render_event,
    show_diff_summary,
)

if TYPE_CHECKING:
    from easycode.commands import CommandRegistry

# Backwards-compatible alias: older callers (and any tests) referenced the
# private name; keep it resolvable on this module.
_rebind_agent = rebind_agent

app = typer.Typer(help="easycode — a CLI coding agent", no_args_is_help=False)





def build_commands(agent: Agent, roots: list[Path]) -> CommandRegistry:
    """CommandRegistry with built-ins + user templates + skill commands."""
    from easycode.commands import Command, CommandRegistry

    reg = CommandRegistry()
    for name, desc in (
        ("help", "show this help"),
        ("exit", "quit the REPL"),
        ("model", "list / switch / add model aliases"),
        ("run", "execute a change task and summarize diffs"),
        ("undo", "undo the last turn (messages + file changes)"),
        ("redo", "redo the last undone turn"),
        ("skills", "list available skills"),
        ("agents", "list delegatable agents"),
    ):
        reg.register(Command(name=name, description=desc, kind="builtin"))
    reg.discover_templates(roots)
    if agent.skills:
        reg.add_skill_commands(agent.skills)
    return reg


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
    from easycode.agent.loop import ToolCall

    async def approval_handler(tc: ToolCall) -> bool:
        key = approval_key(tc)
        if key in always_allow:
            return True
        console.print(f"[yellow]⚠ 需要批准:[/] {tc.name} {json.dumps(tc.arguments, ensure_ascii=False)}")
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
            current = cfg.default_model
            continue
        try:
            await run_turn(agent, text)
        except KeyboardInterrupt:
            console.print("\n[dim]interrupted[/]")


async def _cli_main(cfg: Config, current: str, workdir: Path, secondary_root: list[Path] | None) -> None:
    ctx = cfg.path_context(root=workdir, secondary=[str(p) for p in (secondary_root or [])])
    agent = make_agent(
        cfg,
        current,
        workdir,
        secondary_roots=ctx.secondary,
        extra_safe_dirs=[Path(d).expanduser() for d in cfg.extra_safe_dirs],
    )
    from easycode.snapshot import FileSnapshotManager

    agent.snapshot_manager = FileSnapshotManager(f"cli-{os.getpid()}", agent.path_context().roots)
    commands = build_commands(agent, ctx.roots)
    console.print()
    banner(cfg.resolve_model(current))
    asyncio.run(repl_loop(cfg, agent, current, commands))


@app.command()
def main(
    model: Annotated[str | None, typer.Option("--model", "-m", help="model alias to start with")] = None,
    root: Annotated[Path | None, typer.Option("--root", "-r", help="workspace root (default: cwd)")] = None,
    secondary_root: Annotated[list[Path] | None, typer.Option("--secondary-root", help="extra workspace root (repeatable)")] = None,
    permission: Annotated[str | None, typer.Option("--permission", help="permission mode: ask/auto-review/allow-all")] = None,
) -> None:
    cfg = Config.load(start=root)
    if permission:
        from easycode.approval import permission_parse

        cfg.permission_mode = permission_parse(permission)
    current = model or cfg.default_model
    workdir = (root or Path.cwd()).resolve()
    _cli_main(cfg, current, workdir, secondary_root)


@app.command()
def web(
    host: Annotated[str, typer.Option("--host", help="bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="bind port")] = 8000,
    root: Annotated[Path | None, typer.Option("--root", "-r", help="workspace root (default: cwd)")] = None,
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


async def handle_command(raw: str, cfg: Config, agent: Agent, current: str, commands) -> bool | None:
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

    if cmd.name == "/help" or cmd.name == "help":
        return _cmd_help(cfg, current, commands, agent)
    if cmd.name in ("exit", "quit"):
        return False
    if cmd.name == "run":
        return await _cmd_run(agent, rest)
    if cmd.name == "model":
        return _cmd_model(cfg, agent, current, rest)
    if cmd.name == "undo":
        return _cmd_undo(agent)
    if cmd.name == "redo":
        return _cmd_redo(agent)
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
                marker = f" [dim]({a.model or 'inherit'})[/]" if a.model else ""
                console.print(f"  [cyan]{a.name}[/]{marker}: {a.description}")
        else:
            console.print("[dim]没有可用 agent（在 .easycode/agents/<name>.md 创建）[/]")
        return None
    console.print(f"[red]unknown builtin: /{cmd.name}[/]")
    return None


def _cmd_help(cfg: Config, current: str, commands, agent) -> None:
    from rich.panel import Panel

    lines = ["[bold]Commands[/]"]
    for c in commands.list():
        hint = f" {c.arg_hint}" if c.arg_hint else ""
        badge = {"builtin": "", "template": " [dim](custom)[/]", "skill": " [dim](skill)[/]"}[c.kind]
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
    prev_hook = agent.hook

    def hook(name: str, tc, result: str) -> None:
        if name in ("edit_file", "write_file"):
            try:
                data = json.loads(result)
            except json.JSONDecodeError:
                return
            if data.get("status") == "ok" and data.get("diff"):
                edits.append({"path": data.get("path", "?"), "diff": data.get("diff", "")})
        if prev_hook:
            prev_hook(name, tc, result)

    agent.hook = hook
    console.print(f"[bold cyan]/run[/] {task}")
    try:
        await run_turn(agent, task)
    finally:
        agent.hook = prev_hook
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
            cfg.set_default_model(alias)
            cfg.save()
            _rebind_agent(agent, cfg, alias)
            model_switched(cfg.resolve_model(alias))
        else:
            console.print(f"[red]unknown alias: {alias} (use /model <alias>=<name> to add)[/]")
        return
    return


def _cmd_undo(agent) -> None:
    try:
        summary = agent.undo_turn()
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        return
    _render_rollback(summary)
    return


def _cmd_redo(agent) -> None:
    try:
        summary = agent.redo_turn()
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        return
    _render_rollback(summary)
    return


def _render_rollback(summary: dict) -> None:
    restored = summary.get("restored") or []
    if summary.get("message_only"):
        console.print("[dim]回滚了消息（非 git 仓库，文件未改动）[/]")
    elif restored:
        console.print(f"[bold cyan]已回滚文件（{len(restored)}）[/]")
        for p in restored:
            console.print(f"  ↺ {p}")
    else:
        console.print("[dim]已回滚（无文件改动）[/]")


if __name__ == "__main__":
    app()
