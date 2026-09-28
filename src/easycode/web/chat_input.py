"""What a chat request's text means: which command, and what the model is given.

The ``/`` menu is a catalogue of everything selectable across the registered
projects and the personal directory, plus the current project's MCP services.
An entry is selected by a stable id rather than by path, and resolving one walks
this catalogue again — so a request can only ever describe something the menu is
already offering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import HTTPException

from easycode.config import Config
from easycode.extensions.skills import MCP_COMMAND_PREFIX, SkillRegistry
from easycode.web.session import SessionStore

if TYPE_CHECKING:
    from easycode.extensions.commands import Command, CommandRegistry


@dataclass(frozen=True)
class ResolvedChatInput:
    """What one request turns into: the model's input, and what it is recorded as."""

    model_input: str
    #: The command this message is, for the transcript and for an edit's re-send.
    command_id: str | None
    #: The MCP service this message asks to be handled with, if any.
    mcp_server: str | None = None


def mcp_command_id(project_root: str, server: str) -> str:
    """Stable id of one project's MCP service entry.

    Both parts are percent-encoded whole: a root is an absolute path, and an id
    that could be split on its own separators would let a request name another
    project's service.
    """
    return f"mcp:{quote(project_root, safe='')}:{quote(server, safe='')}"


def command_token(text: str) -> str:
    """The command name a message writes, or "" when it names none."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return ""
    return stripped.split(maxsplit=1)[0]


def _mcp_model_input(name: str, task: str) -> str:
    """What the model is given for ``/mcp:<name> <task>``.

    The service name is JSON-encoded, so a name that happens to contain quotes
    or newlines cannot turn into instructions of its own.
    """
    return (
        f"The user asked for this task to be handled with the MCP service {json.dumps(name)}. "
        "Prefer that service's tools for it. This is a preference, not a restriction: "
        "if none of its tools fits the task, use whatever else does and say what you used.\n\n"
        f"{task}"
    )


def _parse_mcp_command_id(command_id: str) -> tuple[str, str] | None:
    """``mcp:<root>:<server>`` back into its parts, or None if it is not one."""
    from urllib.parse import unquote

    parts = command_id.split(":")
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    return unquote(parts[1]), unquote(parts[2])


def _build_web_commands(roots: list[Path], skills) -> CommandRegistry:
    """Discover executable prompt templates and skill commands for the Web UI."""
    from easycode.extensions.commands import build_registry

    return build_registry(roots, skills)


def draft_roots(
    root_raw: str | None,
    secondary_raw: list[str] | None,
    cfg: Config,
    store: SessionStore,
) -> list[Path]:
    """Workspace roots a brand-new session would discover commands/skills from.

    ``secondary_raw=None`` inherits the project binding; ``[]`` is explicit.
    """
    from easycode.web.projects import normalise_root

    root = normalise_root(root_raw)
    secondary = [str(p) for p in store._resolve_secondary(root, secondary_raw)]
    base = Path(root) if root else Path(cfg.root)
    return [base, *(Path(p) for p in secondary)]


def _expand_command(message: str, roots: list[Path], skills) -> str:
    """Resolve a leading '/' message: expand the selected prompt template or skill."""

    reg = _build_web_commands(roots, skills)
    resolved = reg.resolve(message)
    if resolved is None:
        raise HTTPException(400, f"unknown command: {message.split()[0]}")
    cmd, rest = resolved
    return cmd.expand(rest)


def command_entries(cfg: Config, store: SessionStore) -> tuple[list[tuple[dict, Command]], list[str]]:
    """Every command the ``/`` menu can offer, with the id that selects it.

    The menu aggregates the personal directory and every registered project, so
    one name can appear several times (two projects defining it, or a project
    overriding a personal command); each entry keeps its own id and says where it
    came from. Resolving an id walks this list again rather than trusting the
    caller's path, which is what keeps an unregistered project's command from
    being executed.
    """
    from easycode.extensions.commands import personal_commands, project_commands, skill_command
    from easycode.extensions.skills import personal_skills, project_skills
    from easycode.web.projects import build_projects

    out: list[tuple[dict, Command]] = []
    errors: list[str] = []

    def add(command: Command, label: str, root_key: str) -> None:
        if command.name.startswith(MCP_COMMAND_PREFIX):
            # The prefix is how the Web writes "use this MCP service", so a
            # skill or template under it could never be selected — and saying
            # nothing would leave the user with a file that does not work.
            errors.append(
                f"「{command.name}」（{label}）以 {MCP_COMMAND_PREFIX} 开头，"
                f"该前缀保留给 MCP 服务命令，请改名"
            )
            return
        out.append(
            (
                {
                    "id": f"{command.source}:{root_key}:{command.name}",
                    "name": command.name,
                    "description": command.description,
                    "kind": command.kind,
                    "argument_hint": command.arg_hint,
                    "source": command.source,
                    "source_label": label,
                },
                command,
            )
        )

    for command in personal_commands():
        add(command, "个人", "")
    if cfg.skills_enabled:
        for skill in personal_skills():
            add(skill_command(skill), "个人", "")
    # The default workspace is where a new conversation starts even before it is
    # saved as a project, so it is always a candidate; a saved binding for the
    # same directory comes first and keeps its own name.
    candidates = [*build_projects(cfg, store), {"root": None, "secondary": []}]
    # One entry per directory: the same project can reach this list twice (as the
    # default workspace and as a saved binding spelled differently), and the menu
    # must not offer the same command twice.
    seen: set[str] = set()
    for project in candidates:
        resolved = str(Path(project.get("root") or cfg.root).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        label = str(project.get("name") or Path(resolved).name)
        roots = [Path(resolved), *(Path(p) for p in project.get("secondary") or [])]
        for command in project_commands(roots):
            add(command, label, resolved)
        if cfg.skills_enabled:
            for skill in project_skills(roots):
                add(skill_command(skill), label, resolved)
    # By name, so the same name's entries sit together, each with its source.
    out.sort(key=lambda item: (item[1].name, item[0]["source_label"]))
    return out, errors


def mcp_command_entries(cfg: Config, project_root: str) -> tuple[list[dict], list[str]]:
    """The MCP services one project can be asked to use, and what is broken.

    The list is configuration, not connection: opening the menu must not start
    a subprocess for every server the project has ever configured, and a server
    that happens to be down is still a service the user may ask for — the turn
    is where that becomes an error.

    Nothing here fills in a credential; see ``mcp_config.configured_servers``.
    """
    from easycode.extensions.mcp.config import MCPConfigError, configured_servers

    try:
        servers = configured_servers(cfg.mcp_servers, project_root)
    except MCPConfigError as exc:
        # A configuration nobody can read is reported instead of being shown as
        # a project with no services at all.
        return [], [str(exc)]
    project_name = _project_label(cfg, project_root)
    out: list[dict] = []
    for server in servers:
        if not server.config.enabled:
            continue
        source = {"personal": "user", "app": "app", "project": "project"}[server.scope]
        if server.scope == "project":
            label = f"{project_name} · 项目"
        elif server.scope == "app":
            label = "应用启动配置"
        else:
            label = "个人"
        out.append(
            {
                "id": mcp_command_id(project_root, server.name),
                "name": f"{MCP_COMMAND_PREFIX}{server.name}",
                "description": f"使用 {server.name} 服务完成任务",
                "kind": "mcp",
                "argument_hint": "任务描述",
                "source": source,
                "source_label": label,
                "mcp_server": server.name,
                "project_root": project_root,
            }
        )
    return out, []


def _project_label(cfg: Config, project_root: str) -> str:
    """What to call a project in the menu: its configured name, else the folder."""
    for project in cfg.workspace_projects:
        resolved = str(Path(project.get("root") or cfg.root).expanduser().resolve())
        if resolved == project_root:
            return str(project.get("name") or Path(project_root).name)
    return Path(project_root).name


def _expand_by_id(command_id: str, message: str, cfg: Config, store: SessionStore) -> str:
    """Expand a command the user picked from the menu.

    The id is resolved against the commands registered *now*, so a project that
    was removed since the menu was fetched no longer expands anything; and the
    text has to be that command, so an id left over from an edited prompt cannot
    expand something the user has typed over.
    """
    name = message.strip()[1:].partition(" ")[0].strip()
    for entry, command in command_entries(cfg, store)[0]:
        if entry["id"] != command_id:
            continue
        if command.name != name.lower():
            raise HTTPException(400, f"command does not match its id: {message.strip()}")
        _, _, rest = message.strip()[1:].partition(" ")
        return command.expand(rest.strip())
    raise HTTPException(400, f"unknown command: {command_id}")


def _resolve_mcp_input(
    text: str, command_id: str | None, project_root: str, cfg: Config
) -> ResolvedChatInput:
    """``/mcp:<service> <task>``: the service asked for, and the prompt."""
    head, _, task = text[1:].partition(" ")
    name = head[len(MCP_COMMAND_PREFIX) :].strip()
    if not name:
        raise HTTPException(400, "未知命令：MCP 命令要写出服务名，例如 /mcp:demo 任务")
    entries, errors = mcp_command_entries(cfg, project_root)
    match = next((row for row in entries if row["mcp_server"] == name), None)
    if match is None:
        if errors:
            # A configuration nobody can read is not the same as a
            # project with no services: say which one it is.
            raise HTTPException(422, errors[0])
        raise HTTPException(400, f"当前项目没有可用的 MCP 服务: {name}")
    if command_id and command_id != match["id"]:
        claimed = _parse_mcp_command_id(command_id)
        if claimed is not None and claimed[0] != project_root:
            raise HTTPException(409, "所选 MCP 服务属于另一个项目")
        raise HTTPException(400, f"MCP 服务与所选条目不一致: {name}")
    task = task.strip()
    if not task:
        raise HTTPException(422, "请补充需要该 MCP 服务完成的任务")
    return ResolvedChatInput(
        model_input=_mcp_model_input(name, task),
        command_id=match["id"],
        mcp_server=name,
    )


def resolve_input(
    text: str,
    command_id: str | None,
    roots: list[Path],
    cfg: Config,
    store: SessionStore,
    skills: SkillRegistry | None = None,
) -> ResolvedChatInput:
    """The model's text for ``text``, and what this message is recorded as.

    ``skills`` lets the locked path hand over the registry it just refreshed
    instead of discovering the same directories again.
    """
    stripped = text.strip()
    if stripped.startswith(f"/{MCP_COMMAND_PREFIX}"):
        # The prefix is the Web's way of asking for one service, whether
        # it was picked from the menu or typed by hand.
        return _resolve_mcp_input(stripped, command_id, str(roots[0]), cfg)
    if not stripped.startswith("/"):
        return ResolvedChatInput(model_input=text, command_id=command_id)
    if command_id:
        # A menu selection is resolved against the registered commands, so
        # the body can come from another project — while it still runs in
        # this session's own directory and permission scope.
        return ResolvedChatInput(
            model_input=_expand_by_id(command_id, text, cfg, store),
            command_id=command_id,
        )
    if skills is None and cfg.skills_enabled:
        skills = SkillRegistry.discover(roots)
    return ResolvedChatInput(
        model_input=_expand_command(stripped, roots, skills), command_id=command_id
    )
