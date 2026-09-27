"""HTTP endpoints for sessions."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.web.bridge import ApprovalBroker
from easycode.web.session import SessionStore, idle_sessions, run_mutation


class PermissionRequest(BaseModel):
    mode: str
    #: Required to enter ``allow-all``: the client states the user confirmed the
    #: risk dialog, so a misclick cannot reach full host access on its own.
    confirm_full_access: bool = False


class CreateSessionRequest(BaseModel):
    """A new, empty session. ``secondary_roots=None`` inherits the project binding."""

    root: str | None = None
    secondary_roots: list[str] | None = None
    permission_mode: str | None = None
    confirm_full_access: bool = False


class SessionWorkspaceRequest(BaseModel):
    """A session's working directories. ``secondary_roots=None`` inherits the
    target project's binding, the same way creating a session there would."""

    root: str | None = None
    secondary_roots: list[str] | None = None


class ArchiveRequest(BaseModel):
    archived: bool = True


class PinRequest(BaseModel):
    pinned: bool = True


class ApprovalRequest(BaseModel):
    approve: bool = True
    always: bool = False


def register_sessions(app: FastAPI, store: SessionStore, broker: ApprovalBroker) -> None:
    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest) -> dict:
        """Create an empty session without sending anything.

        The Web UI opens a conversation as soon as the user asks for one, so the
        tab it shows is backed by a real session from the start. Root rules are
        the same as a chat-created session's: an invalid root or secondary entry
        is a 422, and a failed agent build (e.g. no usable default model) leaves
        no session behind.
        """
        from easycode.policy import permission_parse, require_full_access_consent
        from easycode.web.routes_workspaces import _normalise_root

        kwargs: dict = {}
        if req.root:
            kwargs["root"] = _normalise_root(req.root)
        if req.secondary_roots is not None:
            kwargs["secondary_roots"] = req.secondary_roots
        if req.permission_mode:
            try:
                mode = permission_parse(req.permission_mode)
                require_full_access_consent(mode, req.confirm_full_access)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            kwargs["permission_mode"] = mode
        try:
            async with store.config_change():
                sess = store.create(**kwargs)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return sess.summary

    @app.get("/api/sessions")
    def list_sessions(archived: int = 0) -> list[dict]:
        """Session summaries. ``archived=1`` returns only archived sessions."""
        return [s.summary for s in store.list_by_archived(bool(archived))]

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        # ``messages`` is the whole conversation rebuilt from its turn records —
        # not the model context, which compaction is free to shorten. The live
        # lists (approvals, artifacts) are per-session and already complete.
        detail = sess.projection()
        return {
            **sess.summary,
            "messages": detail["messages"],
            "approvals": list(sess.approval_log),
            "turn_failures": detail["failures"],
            "turns": detail["turn_status"],
            "revision": detail["revision"],
            "busy": sess.running,
            "todos": list(sess.todos),
            "artifacts": list(sess.artifacts),
        }

    @app.post("/api/sessions/{session_id}/workspace")
    async def set_session_workspace(session_id: str, req: SessionWorkspaceRequest) -> dict:
        """Move a session that has not started to another project directory.

        Only a blank session may move: once a turn has run, everything the pane
        reports — records, previews, the working-tree diff — describes one
        directory, so a started session answers 409 instead of a half-switch.
        The new root and its secondary directories are validated before anything
        is written, and the move is applied under the session's own lock, so a
        refused request leaves the session exactly as it was.
        """
        from easycode.web.routes_workspaces import _normalise_root, build_projects
        from easycode.workspace import root_error

        cfg = store.cfg
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        root = _normalise_root(req.root)
        if root:
            err = root_error(Path(root))
            if err is not None:
                raise HTTPException(422, err)

        def mutate(roots: list[Path]) -> dict:
            sess.root = root
            sess.secondary_roots = [str(p) for p in roots]
            sess.agent.root = Path(root) if root else cfg.root
            sess.agent.secondary_roots = list(roots)
            # Agents/skills and the system prompt follow the new scope, exactly
            # as they do when a session's secondary roots change.
            sess.agent.rediscover_extensions(with_skills=cfg.skills_enabled)
            store.record_exchange(sess)
            return {"session": sess.summary, "projects": build_projects(cfg, store)}

        async with store.config_change(), idle_sessions([sess]):
            # Re-checked under the lock: a turn that started after the request
            # was read owns this session, and moving it now would strand it.
            if not sess.is_blank:
                raise HTTPException(409, "会话已经开始，工作目录不能再修改")
            # The project binding this inherits is read under the same guard as
            # every other config reader, so a concurrent project edit cannot be
            # half applied to this session.
            try:
                secondary = store._resolve_secondary(root, req.secondary_roots)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            try:
                return await run_mutation(mutate, secondary)
            finally:
                await sess.agent.invalidate_mcp_if_context_changed()

    @app.get("/api/sessions/{session_id}/changes")
    async def session_changes(session_id: str) -> dict:
        """Live uncommitted changes in the repositories this session runs in.

        Read from disk on every request: it describes the working tree as it is
        now, including whatever was already uncommitted before this conversation
        started, so it is never presented as the session's own work.
        """
        import asyncio

        from easycode.web.git import session_changes as collect

        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        return await asyncio.to_thread(collect, sess)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, blank_only: int = 0) -> dict:
        """Delete a session; ``blank_only=1`` refuses one that started a turn.

        The blank form is what closing an untouched tab calls: the answer says
        whether the session was actually removed, and "nothing to remove" — the
        session is gone, or it has already begun a turn — is reported as such
        rather than as an error, because the caller only closes a view either
        way.
        """
        try:
            deleted = await store.delete(session_id, blank_only=bool(blank_only))
        except OSError as exc:
            # The session file is still on disk: report a failure the caller can
            # retry instead of a success that would resurrect on reload.
            raise HTTPException(500, f"删除会话文件失败: {exc}") from exc
        if not deleted and blank_only:
            return {"ok": True, "deleted": False}
        if not deleted:
            raise HTTPException(404, "session not found")
        return {"ok": True, "deleted": True}

    @app.post("/api/sessions/{session_id}/archive")
    async def archive_session(session_id: str, req: ArchiveRequest) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        async with idle_sessions([sess]):
            sess = store.set_archived(session_id, req.archived)
            if sess is None:
                raise HTTPException(404, "session not found")
            return {"ok": True, "archived": sess.archived}

    @app.post("/api/sessions/{session_id}/pin")
    async def pin_session(session_id: str, req: PinRequest) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        async with idle_sessions([sess]):
            pinned = store.set_pinned(session_id, req.pinned)
            if pinned is None:
                raise HTTPException(404, "session not found")
            return {"ok": True, **pinned.summary}

    @app.post("/api/sessions/{session_id}/permission")
    async def set_session_permission(session_id: str, req: PermissionRequest) -> dict:
        from easycode.policy import permission_parse, require_full_access_consent

        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        async with idle_sessions([sess]):
            try:
                mode = permission_parse(req.mode)
                require_full_access_consent(mode, req.confirm_full_access)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            sess.set_permission_mode(mode)
            # The MCP process was launched with the old sandbox; drop it so the
            # next turn reconnects under the new mode.
            await sess.agent.invalidate_mcp_if_context_changed()
            store.record_exchange(sess)
            return {"id": sess.id, "permission_mode": mode}

    @app.post("/api/approval/{approval_id}")
    def resolve_approval(approval_id: str, req: ApprovalRequest) -> dict:
        if not broker.resolve(approval_id, req.approve, req.always):
            raise HTTPException(404, f"unknown or expired approval: {approval_id}")
        return {"ok": True, "approve": req.approve, "always": req.always}

    @app.post("/api/sessions/{session_id}/cancel")
    def cancel_session(session_id: str) -> dict:
        """Cancel the in-flight chat for a session (session interruption)."""
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        cancelled = sess.cancel_stream()
        return {"ok": True, "cancelled": cancelled}
