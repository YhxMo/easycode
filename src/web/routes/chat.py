"""Chat streaming and prompt-template endpoints."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from easycode.agent.turns import CANCELLED, COMPLETED, FAILED
from easycode.config import Config
from easycode.permissions.boundary import resolve_workspace_path, root_error
from easycode.permissions.policy import permission_parse, require_full_access_consent
from easycode.web.bridge import (
    ApprovalBroker,
    approval_required_sse,
    event_to_sse,
    session_sse,
    stream_chat_with_approval,
    turn_accepted_sse,
)
from easycode.web.chat_input import (
    command_entries,
    command_token,
    draft_roots,
    mcp_command_entries,
    resolve_input,
)
from easycode.web.locks import project_key
from easycode.web.projects import known_projects, normalise_root, session_primary
from easycode.web.session import Session
from easycode.web.store import SessionStore

log = logging.getLogger("easycode.web.chat")


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


def register_chat(app: FastAPI, cfg: Config, store: SessionStore, broker: ApprovalBroker) -> None:
    """Connect chat and command endpoints to this app's session store."""
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
            request_root = normalise_root(root_raw)
            if project_key(request_root) != project_key(session_primary(sess)):
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
        if session_id:
            sess = store.get(session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            project_root = session_primary(sess) or str(Path(cfg.root).expanduser().resolve())
            if root and normalise_root(root) != project_root:
                raise HTTPException(409, "workspace root does not match session primary")
            return project_root
        claimed = normalise_root(root)
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
        entries, errors = command_entries(cfg, store)
        mcp_entries, mcp_errors = mcp_command_entries(cfg, project_root)
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
            kwargs["root"] = normalise_root(req.root)
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
                if command_token(raw_message) == command_token(
                    str(previous.get("raw_input") or "")
                ):
                    command_id = previous.get("command_id")
        if command_id and not raw_message.strip().startswith("/"):
            raise HTTPException(400, "command id needs a /command message")


        def _input_roots() -> list[Path]:
            """The roots this request's ``/`` text resolves against, right now."""
            if existing_sess is not None:
                return _session_roots(existing_sess, cfg)
            # A brand-new session: resolved before creating it, so an unknown
            # command does not leave an empty session behind.
            try:
                return draft_roots(req.root, req.secondary_roots, cfg, store)
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
        resolve_input(raw_message, command_id, _input_roots(), cfg, store)
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
                    resolved = resolve_input(
                        raw_message,
                        command_id,
                        _session_roots(sess, cfg),
                        cfg,
                        store,
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
                    snapshot = sess.snapshot_state()
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
                        sess.restore_state(snapshot)
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
