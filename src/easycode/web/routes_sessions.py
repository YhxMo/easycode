"""HTTP endpoints for sessions."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.web.bridge import ApprovalBroker
from easycode.web.session import SessionStore


class PermissionRequest(BaseModel):
    mode: str


class ArchiveRequest(BaseModel):
    archived: bool = True


class ApprovalRequest(BaseModel):
    approve: bool = True
    always: bool = False


def register_sessions(app: FastAPI, store: SessionStore, broker: ApprovalBroker) -> None:
    @app.get("/api/sessions")
    def list_sessions(archived: int = 0) -> list[dict]:
        """Session summaries. ``archived=1`` returns only archived sessions."""
        return [s.summary for s in store.list_by_archived(bool(archived))]

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        return {
            **sess.summary,
            "messages": sess.messages,
            "approvals": list(sess.approval_log),
            "user_times": list(sess.user_times),
        }

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict:
        if not store.delete(session_id):
            raise HTTPException(404, "session not found")
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/archive")
    async def archive_session(session_id: str, req: ArchiveRequest) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        if sess._lock.locked():
            raise HTTPException(409, "session busy")
        await sess._lock.acquire()
        try:
            sess = store.set_archived(session_id, req.archived)
            if sess is None:
                raise HTTPException(404, "session not found")
            return {"ok": True, "archived": sess.archived}
        finally:
            sess._lock.release()

    @app.post("/api/sessions/{session_id}/permission")
    async def set_session_permission(session_id: str, req: PermissionRequest) -> dict:
        from easycode.approval import permission_parse

        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        if sess._lock.locked():
            raise HTTPException(409, "session busy")
        await sess._lock.acquire()
        try:
            try:
                mode = permission_parse(req.mode)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            sess.permission_mode = mode
            sess.agent.permission_mode = mode
            store.record_exchange(sess)
            return {"id": sess.id, "permission_mode": mode}
        finally:
            sess._lock.release()

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
