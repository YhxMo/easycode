"""Web chat route: the SPA-facing ``/api/chat`` SSE endpoint.

Extracted from ``main.py`` in B3 so the app factory only wires up the other
routes. The factory closures the endpoint needs (``cfg`` / ``store`` /
``broker``) are injected by ``register_chat``. Every event's SSE line is built
in ``easycode.web.bridge.event_to_sse`` (single serialization authority) — this
module never hand-builds a ``data: ...`` line.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from easycode.agent.loop import AgentEvent
from easycode.web.bridge import (
    ApprovalBroker,
    approval_required_sse,
    cancelled_sse,
    event_to_sse,
    session_sse,
    stream_chat_with_approval,
)
from easycode.web.session import Session
from easycode.workspace import resolve_workspace_path

if TYPE_CHECKING:
    from easycode.commands import CommandRegistry


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    secondary_roots: list[str] | None = None
    permission_mode: str | None = None
    root: str | None = None


def _attach_snapshot(sess: Session) -> None:
    """Give a session its file snapshot manager (no-op if already attached)."""
    if sess.agent.snapshot_manager is None:
        from easycode.snapshot import FileSnapshotManager

        sess.agent.snapshot_manager = FileSnapshotManager(
            sess.id, sess.agent.path_context().roots
        )


def _session_roots(sess: Session, cfg: Any) -> list[Path]:
    raw = [sess.root, *sess.secondary_roots] if sess.root else [str(cfg.root), *sess.secondary_roots]
    return [Path(p) for p in raw if p]


def _build_web_commands(roots: list[Path], skills) -> CommandRegistry:
    """Registry for the Web UI: templates + skill commands (+ builtin placeholders)."""
    from easycode.commands import Command, CommandRegistry

    reg = CommandRegistry()
    for name, desc, hint in (
        ("run", "一键改代码并汇总 diff", "[任务描述]"),
        ("undo", "撤销上一回合（消息 + 文件回滚）", ""),
        ("redo", "重做被撤销的回合", ""),
        ("skills", "列出可用 skills", ""),
        ("agents", "列出可委派的 agents", ""),
    ):
        reg.register(Command(name=name, description=desc, kind="builtin", arg_hint=hint))
    reg.discover_templates(roots)
    if skills:
        reg.add_skill_commands(skills)
    return reg


def _expand_command(message: str, sess: Session, cfg: Any) -> str:
    """Resolve a leading '/' message: templates/skills expand, builtins rejected."""

    reg = _build_web_commands(_session_roots(sess, cfg), sess.agent.skills)
    resolved = reg.resolve(message)
    if resolved is None:
        raise HTTPException(400, f"unknown command: {message.split()[0]}")
    cmd, rest = resolved
    if cmd.kind in ("template", "skill"):
        return cmd.expand(rest)
    raise HTTPException(400, f"/{cmd.name} 是终端内置命令，模板命令（.easycode/commands/*.md）与 skill 可在 Web 使用")


def register_chat(app, cfg: Any, store: Any, broker: ApprovalBroker) -> None:
    """Register the ``/api/chat`` SSE route.

    ``cfg`` / ``store`` / ``broker`` are the app-factory closures, injected here
    (never module globals) so the route stays unit-testable in isolation. The
    shared workspace helpers (``_normalise_root``, ``_project_key``,
    ``_config_dir``, ``_session_primary``) live in ``main.py`` and are imported
    lazily inside this function to avoid a module-level circular import
    (``main.py`` imports this module at load time).
    """
    from easycode.web.main import (
        _config_dir,
        _normalise_root,
        _project_key,
        _session_primary,
    )

    def _get_session(session_id: str | None, **agent_kwargs: object) -> Session:
        if session_id:
            sess = store.get(session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            _attach_snapshot(sess)
            return sess
        sess = store.create(**agent_kwargs)
        _attach_snapshot(sess)
        return sess

    def _assert_session_workspace_match(
        session_id: str, root_raw: str | None, secondary_raw: list[str] | None
    ) -> None:
        """409 when a caller claims a different project for an existing session (P1-2)."""
        sess = store.get(session_id)
        if sess is None:
            return  # _get_session surfaces the 404
        if root_raw:
            request_root = _normalise_root(root_raw)
            if _project_key(request_root) != _project_key(_session_primary(sess)):
                raise HTTPException(409, "workspace root does not match session primary")
        if secondary_raw:
            request_sec = sorted(
                str(resolve_workspace_path(p, _config_dir(cfg))) for p in secondary_raw if str(p).strip()
            )
            actual_sec = sorted(
                str(resolve_workspace_path(p, _config_dir(cfg))) for p in (sess.secondary_roots or [])
            )
            if request_sec != actual_sec:
                raise HTTPException(409, "secondary roots do not match session")

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(422, "empty message")
        if req.root and not Path(req.root).expanduser().is_dir():
            raise HTTPException(422, f"workspace root is not a directory: {req.root}")
        from easycode.approval import permission_parse

        perm_mode: str | None = None
        if req.permission_mode:
            try:
                perm_mode = permission_parse(req.permission_mode)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        kwargs: dict = {}
        if req.secondary_roots:
            kwargs["secondary_roots"] = req.secondary_roots
        if req.root:
            kwargs["root"] = str(Path(req.root).expanduser().resolve())
        if perm_mode and not req.session_id:
            kwargs["permission_mode"] = perm_mode
        # P1-2: an existing session must answer its own project — a root/secondary
        # the caller claims for it must match, else 409 (never silently ignore).
        if req.session_id:
            _assert_session_workspace_match(req.session_id, req.root, req.secondary_roots)
        try:
            sess = _get_session(req.session_id, **kwargs) if kwargs else _get_session(req.session_id)
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
        raw_message = req.message
        if raw_message.strip().startswith("/"):
            req.message = _expand_command(req.message, sess, cfg)

        async def gen():
            # Hold the per-session lock across the ENTIRE stream (including
            # approval waits) and release only after the final flush, so a
            # session's history / approval / user_times mutations can never be
            # interleaved by a concurrent request.
            await sess._lock.acquire()
            cancel_event = asyncio.Event()
            sess.cancel_event = cancel_event
            try:
                # Mutations belong under the lock so they cannot race a concurrent
                # undo/redo/permission (which is rejected with 409 while busy).
                if perm_mode:
                    sess.permission_mode = perm_mode
                    sess.agent.permission_mode = perm_mode
                if sess.title == "新会话":
                    sess.title = raw_message.strip()[:30]
                sess.user_times.append(datetime.now(UTC).isoformat())
                yield session_sse(sess.id)
                gen_it = stream_chat_with_approval(
                    sess.agent, req.message, broker, cancel_event=cancel_event, session=sess
                )
                async for kind, payload in gen_it:
                    if kind == "approval":
                        approval_id, tc, reason, scope = payload
                        yield approval_required_sse(approval_id, tc, reason, scope)
                    elif kind == "text":
                        yield event_to_sse(AgentEvent(kind="text", content=payload))
                    else:
                        yield event_to_sse(payload)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    yield cancelled_sse()
                raise
            finally:
                if sess.cancel_event is cancel_event:
                    sess.cancel_event = None
                # do not resurrect a session the user deleted while
                # the stream was in flight; record_exchange is also gated, but
                # check here so the StreamResponse settles cleanly either way.
                if store.get(sess.id) is not None:
                    store.record_exchange(sess)
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
