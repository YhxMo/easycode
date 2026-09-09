"""Rich-based rendering for the REPL."""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from easycode.agent.loop import AgentEvent

console = Console()


def banner(model: str) -> None:
    console.print(
        Panel(
            "[bold cyan]Easy code[/] — CLI coding agent\n"
            f"model: [yellow]{model}[/]   commands: /help /model /exit",
            border_style="cyan",
        )
    )


def model_switched(model: str) -> None:
    console.print(f"[dim]model → {model}[/]")


def render_event(ev: AgentEvent) -> None:
    if ev.kind == "text" and ev.content:
        console.print(ev.content, end="", markup=False, highlight=False, soft_wrap=False)
    elif ev.kind == "tool_start" and ev.tool_call:
        tc = ev.tool_call
        args = ", ".join(f"{k}={v!r}" for k, v in list(tc.arguments.items())[:4])
        console.print(f"\n[bold blue]⚙ {tc.name}[/]({args})", soft_wrap=True, highlight=False)
    elif ev.kind == "tool_result" and ev.tool_result:
        if ev.tool_call and render_diff_result(ev.tool_call.name, ev.tool_result):
            return
        snippet = ev.tool_result.replace("\n", " ")[:300]
        console.print(f"[dim]  └→ {snippet}[/]", highlight=False, soft_wrap=True)
    elif ev.kind == "error":
        console.print(Panel(ev.error or "unknown error", title="error", border_style="red"))
    elif ev.kind == "cancelled":
        console.print("\n[dim]⏹ 已中断[/]")
    elif ev.kind == "done":
        console.print("\n")


def render_diff_result(tool_name: str, result: str) -> bool:
    """Render the embedded diff of edit_file/write_file results; True if rendered."""
    if tool_name not in ("edit_file", "write_file"):
        return False
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return False
    diff = data.get("diff")
    if not diff:
        return False
    label = "diff (preview)" if data.get("dry_run") else f"diff ({data.get('path', tool_name)})"
    console.print(Panel(Syntax(diff, "diff", theme="monokai", word_wrap=True), title=label, border_style="green"))
    return True


def show_diff_summary(edits: list[dict]) -> None:
    """Render a multi-file edit summary (used by /run)."""
    if not edits:
        return
    console.print(Panel(f"[bold cyan]修改汇总[/] — 共 {len(edits)} 个文件", border_style="cyan"))
    for edit in edits:
        console.print(f"\n[bold]{edit['path']}[/]")
        console.print(Syntax(edit["diff"], "diff", theme="monokai", word_wrap=True))


def print_help(models: dict[str, str], current: str) -> None:
    lines = [
        "[bold]Commands[/]",
        "  /help                  show this help",
        "  /model                 list models & current default",
        "  /model <alias>         switch model by alias",
        "  /model <a>=<litellm>   add/override alias at runtime",
        "  /exit                  quit",
        "",
        "[bold]Editing[/]",
        "  Enter       send",
        "  Alt+Enter   newline",
        "  Ctrl+C      interrupt current turn",
        "  Ctrl+D      quit",
        "",
        f"[bold]Models[/]  [yellow]current: {current}[/]",
    ]
    lines += [f"  {a}: {m}" for a, m in models.items()]
    console.print(Panel("\n".join(lines), title="Easy code", border_style="cyan"))