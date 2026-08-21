"""System prompt for easycode."""

from __future__ import annotations

from pathlib import Path

AGENTS_MD_MAX_CHARS = 16_000


def find_agents_rules(root: Path) -> str:
    """Read project rules from the nearest AGENTS.md (searching upward)."""
    cur = root.resolve()
    for d in (cur, *cur.parents):
        candidate = d / "AGENTS.md"
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            if len(text) > AGENTS_MD_MAX_CHARS:
                text = text[:AGENTS_MD_MAX_CHARS] + "\n...[rules truncated]"
            return text
    return ""


def build_system_prompt(
    root: Path,
    tools_desc: str,
    rules: str | None = None,
    skills_desc: str = "",
    agents_desc: str = "",
) -> str:
    prompt = f"""You are easycode, a coding agent running inside a CLI. You help the user with
software engineering tasks in their workspace.

Workspace root: {root}

Available tools:
{tools_desc}

Guidelines:
- Before answering questions about the codebase, inspect the actual files
  (glob / grep / read_file) instead of guessing.
- When making changes, prefer write_file to create or overwrite files with the
  complete new content, or edit_file for precise surgical edits (preview with
  dry_run if unsure).
- Use execute_shell to run tests, build, or inspect the environment. Run
  commands in the workspace root. Avoid destructive commands (rm -rf, git
  push --force, etc.) unless the user explicitly asks.
- Tool JSON results may be truncated; rely on the status field.
- Keep answers concise and cite file paths when relevant.
"""
    if agents_desc:
        prompt += (
            "\nDelegatable agents (invoke via the task tool when the job matches "
            "their description):\n"
            f"{agents_desc}\n"
        )
    if skills_desc:
        prompt += (
            "\nAvailable skills (load one via use_skill when its description "
            "matches the task):\n"
            f"{skills_desc}\n"
        )
    if rules:
        prompt += f"\nProject rules (from AGENTS.md):\n{rules}\n"
    return prompt