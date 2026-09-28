"""Chat streaming and prompt-template endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from easycode.config import Config
from easycode.permissions.boundary import resolve_workspace_path, root_error
from easycode.skills import MCP_COMMAND_PREFIX, SkillRegistry
from easycode.web.bridge import (
    ApprovalBroker,
    approval_required_sse,
    event_to_sse,
    session_sse,
    stream_chat_with_approval,
    turn_accepted_sse,
)
from easycode.web.session import Session, SessionStore

log = logging.getLogger("easycode.web.chat")


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


def _command_token(text: str) -> str:
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


if TYPE_CHECKING:
    from easycode.commands import Command, CommandRegistry


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    secondary_roots: list[str] | None = None
    permission_mode: str | None = None
    #: Required alongside ``permission_mode="allow-all"``: see
    #: ``policy.require_full_access_consent``.
    confirm_full_access: bool = False
    root: str | None = None
    #: Id of the entry the user picked in the ``/`` menu. When present the
    #: command is resolved from the registered projects rather than from the
    #: session's own scope; the text is still what gets expanded.
    command_id: str | None = None
    #: Editing: the recorded turn this message replaces. The turn and everything
    #: after it leave the conversation, and the reply starts a new branch in
    #: place. Files, shells and MCP calls the replaced turns already ran are not
    #: undone — an edit rewrites the conversation, not the workspace.
    edit_turn_id: str | None = None
    #: The revision the client read the conversation at. Required with
    #: ``edit_turn_id``: a page that has fallen behind must not rewrite a
    #: conversation that moved on under it.
    expected_revision: int | None = None


def _session_roots(sess: Session, cfg: Config) -> list[Path]:
    raw = (
        [sess.root, *sess.secondary_roots] if sess.root else [str(cfg.root), *sess.secondary_roots]
    )
    return [Path(p) for p in raw if p]


def _build_web_commands(roots: list[Path], skills) -> CommandRegistry:
    """Discover executable prompt templates and skill commands for the Web UI."""
    from easycode.commands import build_registry

    return build_registry(roots, skills)


def _draft_roots(
    root_raw: str | None,
    secondary_raw: list[str] | None,
    cfg: Config,
    store: SessionStore,
) -> list[Path]:
    """Workspace roots a brand-new session would discover commands/skills from.

    ``secondary_raw=None`` inherits the project binding; ``[]`` is explicit.
    """
    from easycode.web.routes_workspaces import _normalise_root

    root = _normalise_root(root_raw)
    secondary = [str(p) for p in store._resolve_secondary(root, secondary_raw)]
    base = Path(root) if root else Path(cfg.root)
    return [base, *(Path(p) for p in secondary)]


def _session_state(sess: Session) -> dict:
    """Everything rewriting a branch touches, for an all-or-nothing edit."""
    import copy

    return {
        "turns": copy.deepcopy(sess.turns),
        "revision": sess.revision,
        "artifacts": copy.deepcopy(sess.artifacts),
        "approval_log": copy.deepcopy(sess.approval_log),
        "history": copy.deepcopy(sess.agent.history.messages),
        "todos": copy.deepcopy(sess.agent.todos),
    }


def _restore_state(sess: Session, state: dict) -> None:
    import copy

    sess.turns = state["turns"]
    sess.revision = state["revision"]
    sess.artifacts = state["artifacts"]
    sess.approval_log = state["approval_log"]
    sess.agent.history.messages = state["history"]
    sess.agent.todos = copy.deepcopy(state["todos"])


def _expand_command(message: str, roots: list[Path], skills) -> str:
    """Resolve a leading '/' message: expand the selected prompt template or skill."""

    reg = _build_web_commands(roots, skills)
    resolved = reg.resolve(message)
    if resolved is None:
        raise HTTPException(400, f"unknown command: {message.split()[0]}")
    cmd, rest = resolved
    return cmd.expand(rest)


def _command_entries(cfg: Config, store: SessionStore) -> tuple[list[tuple[dict, Command]], list[str]]:
    """Every command the ``/`` menu can offer, with the id that selects it.

    The menu aggregates the personal directory and every registered project, so
    one name can appear several times (two projects defining it, or a project
    overriding a personal command); each entry keeps its own id and says where it
    came from. Resolving an id walks this list again rather than trusting the
    caller's path, which is what keeps an unregistered project's command from
    being executed.
    """
    from easycode.commands import personal_commands, project_commands, skill_command
    from easycode.skills import personal_skills, project_skills
    from easycode.web.routes_workspaces import build_projects

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


def _mcp_command_entries(cfg: Config, project_root: str) -> tuple[list[dict], list[str]]:
    """The MCP services one project can be asked to use, and what is broken.

    The list is configuration, not connection: opening the menu must not start
    a subprocess for every server the project has ever configured, and a server
    that happens to be down is still a service the user may ask for — the turn
    is where that becomes an error.

    Nothing here fills in a credential; see ``mcp_config.configured_servers``.
    """
    from easycode.mcp_config import MCPConfigError, configured_servers

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
    for entry, command in _command_entries(cfg, store)[0]:
        if entry["id"] != command_id:
            continue
        if command.name != name.lower():
            raise HTTPException(400, f"command does not match its id: {message.strip()}")
        _, _, rest = message.strip()[1:].partition(" ")
        return command.expand(rest.strip())
    raise HTTPException(400, f"unknown command: {command_id}")


def register_chat(app: FastAPI, cfg: Config, store: SessionStore, broker: ApprovalBroker) -> None:
    """Connect chat and command endpoints to this app's session store."""
    from easycode.web.routes_workspaces import _normalise_root, _session_primary
    from easycode.web.session import project_key

    def _get_session(session_id: str | None, **agent_kwargs: object) -> Session:
        if session_id:
            sess = store.get(session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            return sess
        return store.create(**agent_kwargs)

    def _assert_session_workspace_match(
        session_id: str, root_raw: str | None, secondary_raw: list[str] | None
    ) -> None:
        """409 when a caller claims a different project for an existing session."""
        sess = store.get(session_id)
        if sess is None:
            return  # _get_session surfaces the 404
        if root_raw:
            request_root = _normalise_root(root_raw)
            if project_key(request_root) != project_key(_session_primary(sess)):
                raise HTTPException(409, "workspace root does not match session primary")
        if secondary_raw is not None:
            request_sec = sorted(
                str(resolve_workspace_path(p, cfg.base_dir()))
                for p in secondary_raw
                if str(p).strip()
            )
            actual_sec = sorted(
                str(resolve_workspace_path(p, cfg.base_dir()))
                for p in (sess.secondary_roots or [])
            )
            if request_sec != actual_sec:
                raise HTTPException(409, "secondary roots do not match session")

    def _commands_project_root(session_id: str | None, root: str | None) -> str:
        """Which project's services a command-list request is about.

        A conversation answers with its own directory, and a caller that claims
        a different one is refused rather than quietly served the other list. A
        start page has no conversation yet, so it names a registered project —
        or nothing, which means the default one.
        """
        from easycode.mcp_config import known_projects

        if session_id:
            sess = store.get(session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            project_root = _session_primary(sess) or str(Path(cfg.root).expanduser().resolve())
            if root and _normalise_root(root) != project_root:
                raise HTTPException(409, "workspace root does not match session primary")
            return project_root
        claimed = _normalise_root(root)
        if claimed is None:
            return str(Path(cfg.root).expanduser().resolve())
        if claimed not in {p["root"] for p in known_projects(cfg, store)}:
            raise HTTPException(422, "需要一个已登记的项目目录")
        return claimed

    @app.get("/api/commands")
    def list_commands(session_id: str | None = None, root: str | None = None) -> dict:
        """Every command the ``/`` menu offers, with its source.

        Skills and templates come from all registered projects plus the personal
        directory: the menu is about what exists, and choosing an entry sends its
        id back, which the server resolves against this same list — so the menu
        can offer another project's command without the request being able to
        name an arbitrary path.

        MCP services are different. They are per project, and the effective list
        depends on which project the conversation runs in, so they are listed
        only for the one this request is about.
        """
        project_root = _commands_project_root(session_id, root)
        entries, errors = _command_entries(cfg, store)
        mcp_entries, mcp_errors = _mcp_command_entries(cfg, project_root)
        commands = [entry for entry, _ in entries] + mcp_entries
        commands.sort(key=lambda row: (row["name"], row["source_label"]))
        return {"commands": commands, "errors": [*errors, *mcp_errors]}

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(422, "empty message")
        if req.root and not Path(req.root).expanduser().is_dir():
            raise HTTPException(422, f"workspace root is not a directory: {req.root}")
        if req.edit_turn_id and not req.session_id:
            raise HTTPException(422, "edit_turn_id needs an existing session")
        if req.edit_turn_id and req.expected_revision is None:
            raise HTTPException(422, "编辑需要携带 expected_revision")
        from easycode.permissions.policy import permission_parse, require_full_access_consent

        perm_mode: str | None = None
        if req.permission_mode:
            try:
                perm_mode = permission_parse(req.permission_mode)
                require_full_access_consent(perm_mode, req.confirm_full_access)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        kwargs: dict = {}
        if req.secondary_roots is not None:
            kwargs["secondary_roots"] = req.secondary_roots
        if req.root:
            kwargs["root"] = _normalise_root(req.root)
        if perm_mode and not req.session_id:
            kwargs["permission_mode"] = perm_mode
        # an existing session must answer its own project — a root/secondary
        # the caller claims for it must match, else 409 (never silently ignore).
        if req.session_id:
            _assert_session_workspace_match(req.session_id, req.root, req.secondary_roots)
        raw_message = req.message
        existing_sess = store.get(req.session_id) if req.session_id else None
        # Which command this message is, if any. An edit keeps the one its turn
        # was sent with unless the request names another, so re-running an edited
        # `/review` prompt stays a `/review` prompt rather than becoming a plain
        # message with a stray slash in it.
        command_id = req.command_id
        if req.edit_turn_id and not command_id and existing_sess is not None:
            index = existing_sess.turn_index(req.edit_turn_id)
            if index >= 0:
                previous = existing_sess.turns[index]
                # Inherited only while the message still begins with the same
                # command: rewritten into plain text there is nothing left to
                # inherit, and rewritten into another command it has to be
                # resolved on its own terms.
                if _command_token(raw_message) == _command_token(
                    str(previous.get("raw_input") or "")
                ):
                    command_id = previous.get("command_id")
        if command_id and not raw_message.strip().startswith("/"):
            raise HTTPException(400, "command id needs a /command message")

        def _resolve_mcp_input(
            text: str, command_id: str | None, project_root: str
        ) -> ResolvedChatInput:
            """``/mcp:<service> <task>``: the service asked for, and the prompt."""
            head, _, task = text[1:].partition(" ")
            name = head[len(MCP_COMMAND_PREFIX) :].strip()
            if not name:
                raise HTTPException(400, "未知命令：MCP 命令要写出服务名，例如 /mcp:demo 任务")
            entries, errors = _mcp_command_entries(cfg, project_root)
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

        def _resolve_input(
            text: str,
            command_id: str | None,
            roots: list[Path],
            skills: SkillRegistry | None = None,
        ) -> ResolvedChatInput:
            """The model's text for ``text``, and what this message is recorded as.

            ``skills`` lets the locked path hand over the registry it just
            refreshed instead of discovering the same directories again.
            """
            stripped = text.strip()
            if stripped.startswith(f"/{MCP_COMMAND_PREFIX}"):
                # The prefix is the Web's way of asking for one service, whether
                # it was picked from the menu or typed by hand.
                return _resolve_mcp_input(stripped, command_id, str(roots[0]))
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

        def _input_roots() -> list[Path]:
            """The roots this request's ``/`` text resolves against, right now."""
            if existing_sess is not None:
                return _session_roots(existing_sess, cfg)
            # A brand-new session: resolved before creating it, so an unknown
            # command does not leave an empty session behind.
            try:
                return _draft_roots(req.root, req.secondary_roots, cfg, store)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        # Resolve once here so an unknown command — or an MCP service this
        # project does not have — is a plain HTTP error before any stream
        # starts. The text the model is actually given is resolved again inside
        # ``gen``, against the skills this session will run with by then: an
        # import that landed a moment ago must not be expanded by the registry
        # this request happened to read first. Splitting the message in two is
        # what keeps an edit honest in the transcript — the user sees the text
        # they wrote, while the model is given the command expansion of it.
        _resolve_input(raw_message, command_id, _input_roots())
        try:
            # Session creation and turn preparation read cfg/credentials; the
            # config guard keeps them from interleaving with a model/project
            # change, which would assemble the agent from a half-updated config.
            async with store.config_change():
                sess = _get_session(req.session_id, **kwargs)
                # A restored session can predate the current root rules (e.g. it
                # points at a sensitive dir): keep its history viewable, but
                # refuse to execute against an invalid workspace.
                primary_root = Path(sess.root) if sess.root else cfg.root
                root_err = root_error(primary_root)
                if root_err is not None:
                    raise HTTPException(422, f"会话工作目录无效: {root_err}")
                # A restored session without a usable credential resolves its
                # provider on first use; failure is a clear 422, not a hidden
                # mid-stream error.
                store.ensure_provider(sess)
                if req.edit_turn_id:
                    # 404 before anything is prepared: the turn must exist in
                    # *this* session, and a stale id is a client error, not a
                    # reason to quietly append instead.
                    if sess.turn_index(req.edit_turn_id) < 0:
                        raise HTTPException(404, f"turn not found: {req.edit_turn_id}")
                    if sess.revision != req.expected_revision:
                        raise HTTPException(409, "会话已被更新，请刷新后再编辑")
        except ValueError as exc:
            # Session creation can fail while building the agent (e.g. the
            # default model has no credential configured) or while validating
            # secondary roots. Surface the reason instead of a bare 500.
            raise HTTPException(422, str(exc)) from exc
        # A session already streaming (its lock is held for the
        # whole chat lifecycle) rejects a new turn with 409 instead of interleaving
        # history; gen() then acquires the lock so even the pre-check window is
        # serialized (two near-simultaneous starts run, never interleave).
        if sess._lock.locked():
            raise HTTPException(409, "session busy")

        async def gen():
            # Hold the per-session lock across the ENTIRE stream (including
            # approval waits) and release only after the final flush, so a
            # session's history / approval / turn mutations can never be
            # interleaved by a concurrent request.
            await sess._lock.acquire()
            cancel_event = asyncio.Event()
            sess.cancel_event = cancel_event
            turn: dict | None = None
            terminal = False
            error_seen = False
            try:
                # A session deleted between the route's get() and this acquire
                # must not run a turn: the request holds a stale object only.
                if store.get(sess.id) is None:
                    return
                # This session's skills may have changed since it last looked —
                # an import, or a refresh that failed when it happened — and the
                # prompt has to be built from what this turn will really run
                # with, not from what the session saw last time. Rediscovery and
                # re-expansion are both synchronous: no configuration change can
                # slip between the registry and the text expanded from it, and a
                # failure here changes nothing (no turn, no branch, no prompt).
                try:
                    sess.agent.rediscover_extensions(with_skills=cfg.skills_enabled)
                    resolved = _resolve_input(
                        raw_message,
                        command_id,
                        _session_roots(sess, cfg),
                        skills=sess.agent.skills,
                    )
                except HTTPException as exc:
                    yield event_to_sse(
                        {"type": "error", "error": str(exc.detail), "code": "command_error"}
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - a failed refresh ends the turn
                    # Nothing was prepared and nothing was accepted: the turn
                    # simply does not start, with the reason on screen.
                    yield event_to_sse(
                        {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
                    )
                    return
                # Mutations belong under the lock so they cannot race a concurrent
                # permission change (which is rejected with 409 while busy).
                if perm_mode:
                    sess.set_permission_mode(perm_mode)
                    # A chat-requested mode change must drop MCP started under
                    # the previous sandbox, same as the permission endpoint.
                    await sess.agent.invalidate_mcp_if_context_changed()
                # The branch is rewritten — and persisted — before the model is
                # asked anything: an edit that failed to reach disk must leave the
                # session it read exactly as it was, not half-rewritten in memory.
                if req.edit_turn_id:
                    index = sess.turn_index(req.edit_turn_id)
                    if index < 0:
                        yield event_to_sse(
                            {"type": "error", "error": f"turn not found: {req.edit_turn_id}"}
                        )
                        return
                    if sess.revision != req.expected_revision:
                        yield event_to_sse(
                            {"type": "error", "error": "会话已被更新，请刷新后再编辑"}
                        )
                        return
                    # The candidate branch replaces the old one only once it is on
                    # disk: an edit that could not be persisted must leave the
                    # conversation it read exactly as it was, in memory as well as
                    # on disk, and report that nothing was accepted.
                    snapshot = _session_state(sess)
                    turn = sess.replace_turns_from(
                        index, raw_message, resolved.model_input, resolved.command_id
                    )
                else:
                    snapshot = None
                    turn = sess.begin_turn(
                        raw_message, resolved.model_input, resolved.command_id
                    )
                try:
                    store.record_exchange(sess)
                except OSError as exc:
                    if snapshot is not None:
                        _restore_state(sess, snapshot)
                    yield event_to_sse({"type": "error", "error": f"保存会话失败: {exc}"})
                    return
                yield session_sse(sess.id)
                yield turn_accepted_sse(sess, turn["id"], req.edit_turn_id)
                async with aclosing(
                    stream_chat_with_approval(
                        sess.agent,
                        resolved.model_input,
                        broker,
                        cancel_event=cancel_event,
                        session=sess,
                        turn=turn,
                        required_mcp_server=resolved.mcp_server,
                    )
                ) as events:
                    async for kind, payload in events:
                        if kind == "approval":
                            approval_id, tc, reason, scope = payload
                            yield approval_required_sse(approval_id, tc, reason, scope)
                        else:
                            if payload.kind == "error":
                                # A terminal error the server produced belongs to
                                # the turn that raised it, so a reload can put it
                                # back. A user stop and a dropped connection are
                                # not failures and never land here.
                                terminal = True
                                error_seen = True
                                if payload.error:
                                    sess.record_turn_failure(
                                        payload.error, payload.code, turn["id"]
                                    )
                            if payload.kind == "done":
                                terminal = True
                            if payload.kind == "cancelled":
                                terminal = True
                            # File-tool results are kept on the session so the
                            # pane can still show them after a refresh or once
                            # compaction drops them from the history.
                            if payload.kind == "tool_result" and payload.tool_call:
                                sess.record_artifact(
                                    payload.tool_call, payload.tool_result, turn["id"]
                                )
                            yield event_to_sse(payload)
            finally:
                # Persistence failures must not keep the session lock: release
                # it in an inner finally so the next turn can still start.
                try:
                    if sess.cancel_event is cancel_event:
                        sess.cancel_event = None
                    if turn is not None and store.get(sess.id) is not None:
                        index = sess.turn_index(turn["id"])
                        if index >= 0 and sess.turns[index].get("status") == "running":
                            # The stream never reported an end of its own. A user
                            # stop is the cancel event and is not a failure; a
                            # reader that simply went away mid-turn is. Either way
                            # the turn is over, and saying so keeps a reload from
                            # showing it as still working.
                            from easycode.web.turns import CANCELLED, COMPLETED, FAILED

                            # Four ways a turn can end without having closed
                            # itself out: the model reported an error, the user
                            # stopped it, it finished normally, or the reader
                            # vanished mid-turn. Only the last one is a failure
                            # the user did not ask for.
                            if error_seen:
                                status = FAILED
                            elif cancel_event.is_set():
                                status = CANCELLED
                            elif terminal:
                                status = COMPLETED
                            else:
                                status = FAILED
                            sess.finish_turn(turn["id"], status)
                        try:
                            store.record_exchange(sess)
                        except OSError as exc:
                            # The turn already ran and its results were streamed;
                            # a failed final save must not replace that outcome
                            # with an exception, and must not keep the lock.
                            log.warning("保存会话 %s 失败: %s", sess.id, exc)
                finally:
                    sess._lock.release()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
