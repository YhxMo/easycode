"""Chat streaming and prompt-template endpoints."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from easycode.config import Config
from easycode.skills import SkillRegistry
from easycode.web.bridge import (
    ApprovalBroker,
    approval_required_sse,
    event_to_sse,
    session_sse,
    stream_chat_with_approval,
    turn_accepted_sse,
)
from easycode.web.session import Session, SessionStore
from easycode.workspace import resolve_workspace_path, root_error

log = logging.getLogger("easycode.web.chat")

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


def _command_entries(cfg: Config, store: SessionStore) -> list[tuple[dict, Command]]:
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

    def add(command: Command, label: str, root_key: str) -> None:
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
    return out


def _expand_by_id(command_id: str, message: str, cfg: Config, store: SessionStore) -> str:
    """Expand a command the user picked from the menu.

    The id is resolved against the commands registered *now*, so a project that
    was removed since the menu was fetched no longer expands anything; and the
    text has to be that command, so an id left over from an edited prompt cannot
    expand something the user has typed over.
    """
    name = message.strip()[1:].partition(" ")[0].strip()
    for entry, command in _command_entries(cfg, store):
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

    @app.get("/api/commands")
    def list_commands() -> dict:
        """Every command and skill the ``/`` menu offers, with its source.

        The menu is the same for every conversation: all registered projects
        plus the personal directory. Choosing an entry sends its id back with
        the message, and the server resolves that id against this same list —
        so the menu can offer another project's command without the request
        being able to name an arbitrary path.
        """
        return {"commands": [entry for entry, _ in _command_entries(cfg, store)]}

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
        from easycode.policy import permission_parse, require_full_access_consent

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
                command_id = existing_sess.turns[index].get("command_id")
        if command_id and not raw_message.strip().startswith("/"):
            raise HTTPException(400, "command id needs a /command message")

        def _resolve_message(text: str) -> str:
            """The text the model is given for ``text`` in this session's scope."""
            if not text.strip().startswith("/"):
                return text
            if command_id:
                # A menu selection is resolved against the registered commands, so
                # the body can come from another project — while it still runs in
                # this session's own directory and permission scope.
                return _expand_by_id(command_id, text, cfg, store)
            if existing_sess is not None:
                # Typed by hand rather than picked from the menu: the current
                # project's and the personal commands are what resolve it.
                return _expand_command(
                    text, _session_roots(existing_sess, cfg), existing_sess.agent.skills
                )
            # A brand-new session: resolve before creating it, so an unknown
            # command does not leave an empty session behind.
            try:
                roots = _draft_roots(req.root, req.secondary_roots, cfg, store)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            skills = SkillRegistry.discover(roots) if cfg.skills_enabled else None
            return _expand_command(text, roots, skills)

        # Splitting the message in two is what keeps an edit honest in the
        # transcript: the user sees the text they wrote, while the model is given
        # the command expansion of it.
        model_input = _resolve_message(raw_message)
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
                    turn = sess.replace_turns_from(index, raw_message, model_input, command_id)
                else:
                    snapshot = None
                    turn = sess.begin_turn(raw_message, model_input, command_id)
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
                        model_input,
                        broker,
                        cancel_event=cancel_event,
                        session=sess,
                        turn=turn,
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
