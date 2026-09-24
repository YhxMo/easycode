"""Chat streaming and prompt-template endpoints."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
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
)
from easycode.web.session import Session, SessionStore
from easycode.workspace import resolve_workspace_path

if TYPE_CHECKING:
    from easycode.commands import CommandRegistry


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    secondary_roots: list[str] | None = None
    permission_mode: str | None = None
    root: str | None = None


def _session_roots(sess: Session, cfg: Config) -> list[Path]:
    raw = (
        [sess.root, *sess.secondary_roots] if sess.root else [str(cfg.root), *sess.secondary_roots]
    )
    return [Path(p) for p in raw if p]


def _build_web_commands(roots: list[Path], skills) -> CommandRegistry:
    """Discover executable prompt templates and skill commands for the Web UI."""
    from easycode.commands import build_registry

    return build_registry(roots, skills)


def _new_session_roots(req: ChatRequest, cfg: Config, store: SessionStore) -> list[Path]:
    """Workspace roots a brand-new session would discover commands/skills from."""
    root = str(Path(req.root).expanduser().resolve()) if req.root else None
    secondary = [str(p) for p in store._resolve_secondary(root, req.secondary_roots)]
    if root:
        return [Path(root), *(Path(p) for p in secondary)]
    return [Path(cfg.root), *(Path(p) for p in secondary)]


def _expand_command(message: str, roots: list[Path], skills) -> str:
    """Resolve a leading '/' message: expand the selected prompt template or skill."""

    reg = _build_web_commands(roots, skills)
    resolved = reg.resolve(message)
    if resolved is None:
        raise HTTPException(400, f"unknown command: {message.split()[0]}")
    cmd, rest = resolved
    return cmd.expand(rest)


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
        if secondary_raw:
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
        """Executable template and skill commands for autocomplete."""
        roots = [cfg.root]
        for s in store.list():
            for p in [s.root, *s.secondary_roots] if s.root else list(s.secondary_roots):
                if p:
                    roots.append(Path(p))
        roots = list(dict.fromkeys(Path(r).resolve() for r in roots))
        skills = SkillRegistry.discover(roots) if cfg.skills_enabled else None
        reg = _build_web_commands(roots, skills)
        return {
            "commands": [
                {
                    "name": c.name,
                    "description": c.description,
                    "kind": c.kind,
                    "argument_hint": c.arg_hint,
                    "source": c.source,
                }
                for c in reg.list()
            ]
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(422, "empty message")
        if req.root and not Path(req.root).expanduser().is_dir():
            raise HTTPException(422, f"workspace root is not a directory: {req.root}")
        from easycode.policy import permission_parse

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
            kwargs["root"] = _normalise_root(req.root)
        if perm_mode and not req.session_id:
            kwargs["permission_mode"] = perm_mode
        # an existing session must answer its own project — a root/secondary
        # the caller claims for it must match, else 409 (never silently ignore).
        if req.session_id:
            _assert_session_workspace_match(req.session_id, req.root, req.secondary_roots)
        raw_message = req.message
        is_command = raw_message.strip().startswith("/")
        if is_command and not req.session_id:
            # Resolve the command BEFORE creating the session: an unknown command
            # must not leave an empty session behind.
            try:
                roots = _new_session_roots(req, cfg, store)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            skills = SkillRegistry.discover(roots) if cfg.skills_enabled else None
            req.message = _expand_command(req.message, roots, skills)
        try:
            sess = _get_session(req.session_id, **kwargs)
            # A session restored without a usable credential resolves its
            # provider on first use; failure is a clear 422, not a hidden
            # mid-stream error.
            store.ensure_provider(sess)
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
        if is_command and req.session_id:
            req.message = _expand_command(
                req.message, _session_roots(sess, cfg), sess.agent.skills
            )

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
                # permission change (which is rejected with 409 while busy).
                if perm_mode:
                    sess.agent.permission_mode = perm_mode
                if sess.title == "新会话":
                    sess.title = raw_message.strip()[:30]
                sess.user_times.append(datetime.now(UTC).isoformat())
                yield session_sse(sess.id)
                async with aclosing(
                    stream_chat_with_approval(
                        sess.agent, req.message, broker, cancel_event=cancel_event, session=sess
                    )
                ) as events:
                    async for kind, payload in events:
                        if kind == "approval":
                            approval_id, tc, reason, scope = payload
                            yield approval_required_sse(approval_id, tc, reason, scope)
                        else:
                            yield event_to_sse(payload)
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
