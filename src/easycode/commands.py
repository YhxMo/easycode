"""Slash command registry: built-ins + user markdown template commands.

User commands (Claude Code / opencode style)::

    .easycode/commands/{name}.md       # project (any workspace root)
    ~/.easycode/commands/{name}.md     # personal

Frontmatter: ``description`` (required), ``argument-hint`` (optional).
Body is a prompt template supporting ``$ARGUMENTS`` / ``$ARGS`` (full rest)
and ``$1``..``$9`` (positional arguments).

Skills (``SKILL.md`` files) are registered as commands too and take priority
over template commands with the same name; built-in names are reserved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from easycode.frontmatter import parse_spec
from easycode.skills import SkillRegistry, skill_context

log = logging.getLogger(__name__)


@dataclass
class Command:
    name: str
    description: str
    kind: str  # "builtin" | "template" | "skill"
    body: str = ""
    arg_hint: str = ""
    source: str = "builtin"  # "builtin" | "project" | "user"
    handler: Callable[..., Any] | None = None

    def expand(self, rest: str) -> str:
        """Substitute $ARGUMENTS/$1..$9 into a template body.

        A skill body is instructions, not a template: one that names no
        placeholder would otherwise swallow the task typed after ``/name``, so
        the task is appended to it. A template command keeps its own meaning —
        there the body is the whole prompt — and a skill that does use
        placeholders keeps the plain substitution it already had.
        """
        if not self.body:
            return rest
        placeholders = ("$ARGUMENTS", "$ARGS", *(f"${i}" for i in range(1, 10)))
        used = any(p in self.body for p in placeholders)
        text = self.body.replace("$ARGUMENTS", rest).replace("$ARGS", rest)
        args = rest.split()
        for i in range(1, 10):
            if f"${i}" in text:
                text = text.replace(f"${i}", args[i - 1] if i <= len(args) else "")
        if used or not rest or self.kind != "skill":
            return text
        return f"{text}\n\n## 用户任务\n\n{rest}"


def build_registry(
    roots: list[Path],
    skills: SkillRegistry | None,
    builtins: tuple[tuple[str, str], ...] = (),
) -> CommandRegistry:
    """Registry with optional builtins + user templates + skill commands."""
    reg = CommandRegistry()
    for name, desc in builtins:
        reg.register(Command(name=name, description=desc, kind="builtin"))
    reg.discover_templates(roots)
    if skills:
        reg.add_skill_commands(skills)
    return reg


def skill_command(skill) -> Command:
    """Expose one skill as the ``/name`` command that loads it.

    The body is the skill's context, not the raw markdown: running a skill by
    hand must hand the model the same resource directory ``use_skill`` does.
    """
    return Command(
        name=skill.name,
        description=skill.description,
        kind="skill",
        body=skill_context(skill),
        arg_hint="",
        source=skill.source,
    )


def read_template(path: Path, source: str) -> Command | None:
    """One user markdown template, or None when the file cannot be used."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_spec(text, default_name=path.stem, required=("description",))
    except Exception as exc:  # noqa: BLE001 - one bad file must not break discovery
        log.warning("skip command file %s: %s", path, exc)
        return None
    return Command(
        name=str(meta["name"]).strip().lower(),
        description=str(meta["description"]).strip(),
        kind="template",
        body=body.strip(),
        arg_hint=str(meta.get("argument-hint") or meta.get("arg_hint") or "").strip(),
        source=source,
    )


def personal_commands(user_dir: Path | None = None) -> list[Command]:
    """Personal prompt templates (``~/.easycode/commands`` by default)."""
    root = user_dir or Path.home() / ".easycode" / "commands"
    if not root.is_dir():
        return []
    return [cmd for cmd in (read_template(p, "user") for p in sorted(root.glob("*.md"))) if cmd]


def project_commands(roots: list[Path]) -> list[Command]:
    """Prompt templates under these roots' ``.easycode/commands``."""
    out: list[Command] = []
    for root in roots:
        proj = root / ".easycode" / "commands"
        if not proj.is_dir():
            continue
        out += [cmd for cmd in (read_template(p, "project") for p in sorted(proj.glob("*.md"))) if cmd]
    return out


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._reserved: set[str] = set()

    def register(self, cmd: Command) -> None:
        name = cmd.name.strip().lower()
        if cmd.kind == "builtin":
            self._reserved.add(name)
        self._commands[name] = cmd

    def get(self, name: str) -> Command | None:
        return self._commands.get(name.strip().lower())

    def list(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def resolve(self, raw: str) -> tuple[Command, str] | None:
        """'  /name rest'  -> (command, rest); None when not a command."""
        text = raw.strip()
        if not text.startswith("/"):
            return None
        cmd_name, _, rest = text[1:].partition(" ")
        cmd = self.get(cmd_name.strip())
        if cmd is None:
            return None
        return cmd, rest.strip()

    def discover_templates(self, roots: list[Path], user_dir: Path | None = None) -> None:
        """Register user markdown commands (project wins over personal)."""
        user = user_dir or Path.home() / ".easycode" / "commands"
        if user.is_dir():
            for p in sorted(user.glob("*.md")):
                self._add_template(p, source="user")
        for root in roots:
            proj = root / ".easycode" / "commands"
            if proj.is_dir():
                for p in sorted(proj.glob("*.md")):
                    self._add_template(p, source="project")

    def add_skill_commands(self, skills: SkillRegistry) -> None:
        """Expose skills as commands; skills beat templates but not built-ins."""
        for skill in skills.list():
            if skill.name in self._reserved:
                continue
            self._commands[skill.name] = skill_command(skill)

    def _add_template(self, path: Path, source: str) -> None:
        cmd = read_template(path, source)
        if cmd is None:
            return
        name = cmd.name
        if name in self._reserved:
            return
        if name in self._commands and self._commands[name].kind == "skill":
            return  # skill takes priority
        self._commands[name] = cmd