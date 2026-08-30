"""FastAPI app: sessions + SSE chat + model management."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from easycode.config import API_FORMATS, DEFAULT_API_FORMAT, Config
from easycode.sandbox import sandbox_command
from easycode.skills import SkillRegistry
from easycode.web import services
from easycode.web.bridge import ApprovalBroker
from easycode.web.platform import (
    FINDER_PROMPT_MAX_LEN,
    WorktreeAddError,
    _sanitize_finder_prompt,
    choose_folders_via_finder,
    create_worktree as platform_create_worktree,
    finder_supported,
    reveal_in_finder as platform_reveal,
)
from easycode.web.routes_chat import _build_web_commands, register_chat
from easycode.web.session import Session, SessionStore
from easycode.workspace import normalise_secondary

FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


#: Known frontend serving origins (dev Vite server) that a state-change request
#: may legitimately originate from, in addition to the listener's exact same
#: origin. These are loopback-only and mirror the CORS allow-list below.
_KNOWN_FRONTEND_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
}

logger = logging.getLogger(__name__)


def _normalise_host(host: str) -> str:
    """Lowercase a host, strip an IPv6 ``[]`` wrapper and any zone id."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if "%" in h:  # IPv6 zone id, e.g. fe80::1%lo0
        h = h.split("%", 1)[0]
    return h


def _is_loopback_host(host: str) -> bool:
    """True for loopback hostnames/addresses (localhost, 127.0.0.0/8, ::1)."""
    h = _normalise_host(host)
    return h.startswith("127.") or h in ("localhost", "::1", "0:0:0:0:0:0:0:1")


def _bind_is_loopback(bind_host: str) -> bool:
    """True when the *listening* address is loopback-only.

    ``0.0.0.0`` and any non-loopback address bind the control plane to the
    network, so they count as non-loopback (they demand a bearer token).
    """
    return _is_loopback_host(bind_host)


def _origin_is_local(origin: str | None, bind_host: str = "127.0.0.1", bind_port: int = 8000) -> bool:
    """True when a state-change request may proceed on origin geometry alone.

    Tightened rules:
    - Missing Origin/Referer (curl, non-browser tooling, our own tests) is
      accepted ONLY when the listener is bound to loopback.
    - A present Origin/Referer is accepted only when it is the exact same origin
      as the listener (http/https + same host family + same port), or it is one
      of the known dev-frontend origins.
    - The old equivalences are removed: an Origin whose host equals the Host
      header hostname (the DNS-rebinding channel) and any loopback host on an
      arbitrary port no longer pass.
    """
    if not origin:
        return _bind_is_loopback(bind_host)
    norm = origin.strip().rstrip("/")
    if norm in _KNOWN_FRONTEND_ORIGINS:
        return True
    # A Referer fallback may carry a path (e.g. http://localhost:5173/chat) that
    # still belongs to a known dev frontend source.
    if any(norm.startswith(k + "/") for k in _KNOWN_FRONTEND_ORIGINS):
        return True
    try:
        parts = urllib.parse.urlsplit(norm)
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False
    origin_host = (parts.hostname or "").strip().lower()
    if origin_host in ("", "null"):
        return False
    origin_port = parts.port or (443 if scheme == "https" else 80)
    if origin_port != bind_port:
        return False
    if _bind_is_loopback(bind_host):
        # Keep localhost<->127.0.0.1 interchangeable for real local use (the
        # built frontend is served at either), while rejecting any non-loopback
        # or cross-port origin.
        return _is_loopback_host(origin_host)
    # Non-loopback bind: bearer-token auth (checked in the middleware) is the
    # real gate. Reaching here means a well-formed http(s) origin on the bound
    # port; requests without a token never get this far on a remote bind anyway.
    return True


class _LocalOriginMiddleware:
    """Reject cross-origin state-changing requests (POST/PUT/PATCH/DELETE).

    On a non-loopback bind a valid ``Authorization: Bearer $EASYCODE_WEB_TOKEN``
    is additionally required. If ``EASYCODE_WEB_TOKEN`` is unset the control
    plane is deliberately **fail-closed** — every state-change request is
    rejected and a warning is logged at startup (security first: never expose
    the credentialed control plane to the network without an explicit token).
    """

    def __init__(self, app, bind_host: str = "127.0.0.1", bind_port: int = 8000):
        self.app = app
        self.bind_host = bind_host
        self.bind_port = bind_port
        if not _bind_is_loopback(bind_host) and not os.environ.get("EASYCODE_WEB_TOKEN"):
            logger.warning(
                "Web control plane bound to non-loopback host %r without "
                "EASYCODE_WEB_TOKEN set; state-changing requests are FAIL-CLOSED "
                "(all rejected). To use the control plane over a non-loopback "
                "bind, set EASYCODE_WEB_TOKEN and send 'Authorization: Bearer "
                "<token>' on every state-change request.",
                bind_host,
            )

    @staticmethod
    def _token_ok(headers: dict[bytes, bytes]) -> bool:
        expected = os.environ.get("EASYCODE_WEB_TOKEN")
        if not expected:
            return False
        auth = headers.get(b"authorization")
        if not auth:
            return False
        auth = auth.decode("latin-1")
        scheme, _, cred = auth.partition(" ")
        if scheme.lower() != "bearer" or not cred.strip():
            return False
        return hmac.compare_digest(cred.strip(), expected)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            origin = headers.get(b"origin")
            referer = headers.get(b"referer")
            source = None
            if origin:
                source = origin.decode("latin-1")
            elif referer:
                source = referer.decode("latin-1")
            if not _origin_is_local(source, self.bind_host, self.bind_port):
                response = JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
                await response(scope, receive, send)
                return
            if not _bind_is_loopback(self.bind_host) and not self._token_ok(headers):
                response = JSONResponse({"detail": "bearer token required"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ChooseWorkspaceRequest(BaseModel):
    multiple: bool = False
    prompt: str = "选择目录"


class SaveProjectRequest(BaseModel):
    root: str | None = None
    secondary: list[str] = []
    session_id: str | None = None
    name: str | None = None


class PinProjectRequest(BaseModel):
    root: str | None = None
    pinned: bool = True


class RevealRequest(BaseModel):
    root: str | None = None


class WorktreeRequest(BaseModel):
    root: str = ""


class RemoveProjectRequest(BaseModel):
    root: str | None = None
    delete_sessions: bool = True


class PermissionRequest(BaseModel):
    mode: str


class ArchiveRequest(BaseModel):
    archived: bool = True


def _normalise_root(root: str | None, default: Path) -> str | None:
    """Resolve a project root string; empty/'-' mean default project."""
    if not root or root in ("-", "default"):
        return None
    return str(Path(root).expanduser().resolve())


def _project_key(root: str | None) -> str:
    return root or ""


def _config_dir(cfg: Config) -> Path:
    return cfg.config_path.parent if cfg.config_path else cfg.root


def _session_primary(sess: Session, cfg: Config) -> str | None:
    """Canonical primary root for a session ('default' → cfg.root key)."""
    return _normalise_root(sess.root, cfg.root)


def projects_from_sessions(store: SessionStore, default_root: Path) -> list[dict]:
    """Infer project → secondary bindings from conversation history."""
    by_key: dict[str, set[str]] = {}
    for s in store.list():
        key = _project_key(_normalise_root(s.root, default_root))
        by_key.setdefault(key, set()).update(s.secondary_roots or [])
    out = [dict(root=_normalise_root(k, default_root), secondary=sorted(v)) for k, v in by_key.items()]
    out.sort(key=lambda p: (p["root"] is not None, p["root"] or ""))
    return out


def merge_projects(base: list[dict], extra: list[dict]) -> list[dict]:
    """Union project bindings by root key; ``base`` (config) wins ordering.

    Name/pinned metadata is preserved from the config entries (``base``);
    pinned projects sort above the rest (stable within their groups).
    """
    meta: dict[str, dict] = {}
    secondary: dict[str, set[str]] = {}
    order: list[str] = []
    for source in (base, extra):
        for p in source:
            key = _project_key(p.get("root"))
            if key not in order:
                order.append(key)
            secondary.setdefault(key, set()).update(p.get("secondary") or [])
            if source is base:
                meta[key] = {k: p[k] for k in ("name", "pinned") if p.get(k)}
    out = []
    for key in order:
        root = None if not key else key
        entry = {"root": root, "secondary": sorted(secondary[key])}
        entry.update(meta.get(key, {}))
        out.append(entry)
    out.sort(key=lambda p: (not p.get("pinned"), p["root"] is not None, p["root"] or ""))
    return out


def build_projects(cfg: Config, store: SessionStore) -> list[dict]:
    return merge_projects(cfg.workspace_projects, projects_from_sessions(store, cfg.root))


class ApprovalRequest(BaseModel):
    approve: bool = True
    always: bool = False


class UndoRequest(BaseModel):
    until_user: int | None = None  # roll back to before the nth user message (1-based)


class ModelRequest(BaseModel):
    alias: str


class AddModelRequest(BaseModel):
    alias: str
    model: str
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_format: str = DEFAULT_API_FORMAT


class UpdateModelRequest(BaseModel):
    model: str
    new_alias: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    clear_key: bool = False
    api_format: str | None = None


def create_app(
    cfg: Config | None = None,
    session_store: SessionStore | None = None,
    static_dir: Path | None = None,
    approval_broker: ApprovalBroker | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8000,
) -> FastAPI:
    """App factory. ``session_store``/``approval_broker`` injectable for tests.

    ``bind_host``/``bind_port`` are the values ``cli.web`` passes to uvicorn; the
    origin guard uses them to decide whether a state-change request is same-origin
    and whether a bearer token is required (non-loopback bind -> token required).
    They default to the loopback CLI values. To be safe this should match the
    actual listener; the CLI passes the resolved host/port through to uvicorn.
    """
    from easycode.agent.loop import Agent  # noqa: F401
    from easycode.agentfactory import make_agent

    cfg = cfg or Config.load()
    broker = approval_broker or ApprovalBroker()

    def factory(alias: str, **agent_kwargs: object) -> object:
        from pathlib import Path

        root_kw = agent_kwargs.get("root")
        session_root = Path(root_kw).resolve() if isinstance(root_kw, str) and root_kw else cfg.root
        roots = agent_kwargs.get("secondary_roots") if agent_kwargs.get("secondary_roots") else None
        secondary = [Path(r).resolve() for r in (roots or [])] or None
        return make_agent(
            cfg,
            alias,
            session_root,
            secondary_roots=secondary,
            extra_safe_dirs=[Path(d).expanduser() for d in cfg.extra_safe_dirs],
        )

    store = session_store or SessionStore(cfg, cfg.root, factory)
    store.load_all()

    app = FastAPI(title="Easy code", version="0.1.0")
    app.state.store = store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost: reject cross-origin state-change requests before CORS sees them.
    app.add_middleware(
        _LocalOriginMiddleware, bind_host=bind_host, bind_port=bind_port
    )

    @app.get("/api/models")
    def get_models() -> dict:
        """Return model records without exposing API keys."""
        reloaded = Config.load(start=cfg.root)
        cfg.models = reloaded.models
        cfg.default_model = reloaded.default_model
        return services.models_response(cfg)

    @app.post("/api/models/add")
    def add_model(req: AddModelRequest) -> dict:
        alias = req.alias.strip()
        model = req.model.strip()
        if not alias or not model:
            raise HTTPException(422, "alias and model are required")
        api_format = req.api_format
        if api_format not in API_FORMATS:
            raise HTTPException(422, f"unsupported api_format: {api_format}")
        if alias in cfg.models:
            raise HTTPException(409, f"model alias already exists: {alias}")
        return services.add_model(
            cfg,
            alias=alias,
            model=model,
            provider=req.provider,
            base_url=req.base_url,
            api_key=req.api_key,
            api_format=api_format,
        )

    @app.get("/api/models/{alias}")
    def get_model_detail(alias: str) -> dict:
        """Return one model's editable configuration.

        The raw API key is never returned to the browser — only a ``key_tail``
        hint (readability) and whether a key is configured (``has_api_key``).
        This keeps the localhost edit dialog usable without ever exposing the
        secret, matching what the collection endpoint already promises.
        """
        if cfg.models.get(alias) is None:
            raise HTTPException(404, f"unknown alias: {alias}")
        return services.get_model_detail(cfg, alias)

    @app.put("/api/models/{alias}")
    def update_model(alias: str, req: UpdateModelRequest) -> dict:
        spec = cfg.models.get(alias)
        target_alias = (req.new_alias or alias).strip()
        model = req.model.strip()
        if spec is None:
            raise HTTPException(404, f"unknown alias: {alias}")
        if not target_alias or not model:
            raise HTTPException(422, "alias and model are required")
        if target_alias != alias and target_alias in cfg.models:
            raise HTTPException(409, f"alias already exists: {target_alias}")

        api_format = req.api_format or spec.api_format
        if api_format not in API_FORMATS:
            raise HTTPException(422, f"unsupported api_format: {api_format}")
        return services.update_model(
            cfg,
            store,
            alias=alias,
            target_alias=target_alias,
            model=model,
            provider=req.provider,
            base_url=req.base_url,
            api_key=req.api_key,
            clear_key=req.clear_key,
            api_format=api_format,
        )

    @app.delete("/api/models/{alias}")
    def delete_model(alias: str) -> dict:
        if alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {alias}")
        return services.delete_model(cfg, alias=alias)

    @app.post("/api/models")
    def set_model(req: ModelRequest) -> dict:
        if req.alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {req.alias}")
        # Validate the target model BEFORE mutating anything: switching to a
        # model without a usable credential must fail atomically — no default
        # flip on disk, no partially rebound sessions (ValueError -> 422).
        try:
            return services.switch_default(cfg, store, alias=req.alias)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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

    @app.get("/api/workspaces")
    def list_workspaces() -> dict:
        """Project bindings: main root → its secondary roots.

        Merges config-persisted bindings with those inferred from session
        history. The default project is represented by root=null.
        """
        return {"default": str(cfg.root), "projects": build_projects(cfg, store)}

    @app.get("/api/commands")
    def list_commands() -> dict:
        """Slash commands for autocomplete: builtins + templates + skills."""
        roots = [cfg.root]
        for s in store.list():
            for p in ([s.root, *s.secondary_roots] if s.root else list(s.secondary_roots)):
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

    @app.post("/api/workspaces/projects")
    def save_project(req: SaveProjectRequest) -> dict:
        root = _normalise_root(req.root, cfg.root)
        if root and not Path(root).is_dir():
            raise HTTPException(422, f"not a directory: {root}")
        # Validate every secondary before mutating anything (P1-1): all-or-nothing.
        sec_paths, sec_err = normalise_secondary(req.secondary, _config_dir(cfg))
        if sec_err is not None:
            raise HTTPException(422, sec_err)
        secondary = sorted({str(p) for p in sec_paths})
        sess = None
        if req.session_id:
            sess = store.get(req.session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            # P1-2: the requested root must match the session's valid primary.
            if _project_key(root) != _project_key(_session_primary(sess, cfg)):
                raise HTTPException(409, "project root does not match session primary")
        key = _project_key(root)
        found = False
        for proj in cfg.workspace_projects:
            if _project_key(proj.get("root")) == key:
                proj["root"] = root
                proj["secondary"] = secondary
                if req.name is not None:
                    name = req.name.strip()
                    if name:
                        proj["name"] = name
                    else:
                        proj.pop("name", None)
                found = True
                break
        if not found:
            entry: dict[str, Any] = {"root": root, "secondary": secondary}
            if req.name and req.name.strip():
                entry["name"] = req.name.strip()
            cfg.workspace_projects.append(entry)
        cfg.save()
        if sess is not None:
            sess.secondary_roots = list(secondary)
            sess.agent.secondary_roots = [Path(p) for p in secondary]
            store.record_exchange(sess)
        return {"root": root, "secondary": secondary, "projects": build_projects(cfg, store)}

    def _ensure_project_entry(root: str | None, base_secondary: list[str] | None = None) -> dict:
        """Locate or create a config workspace project entry by root key."""
        key = _project_key(root)
        for proj in cfg.workspace_projects:
            if _project_key(proj.get("root")) == key:
                return proj
        secondary = base_secondary
        if secondary is None:
            merged = next((p for p in build_projects(cfg, store) if _project_key(p.get("root")) == key), None)
            secondary = sorted(merged.get("secondary") or []) if merged else []
        entry: dict[str, Any] = {"root": root, "secondary": secondary}
        cfg.workspace_projects.append(entry)
        return entry

    @app.post("/api/workspaces/pin")
    def pin_project(req: PinProjectRequest) -> dict:
        root = _normalise_root(req.root, cfg.root)
        entry = _ensure_project_entry(root)
        entry["pinned"] = bool(req.pinned)
        cfg.save()
        return {"ok": True, "root": root, "pinned": entry.get("pinned"), "projects": build_projects(cfg, store)}

    @app.post("/api/workspaces/reveal")
    def reveal_in_finder(req: RevealRequest) -> dict:
        """Reveal the project directory in the system file browser (macOS ``open``)."""
        if not (req.root or Path(cfg.root).is_dir()):
            return {"ok": False, "supported": False, "error": "no directory"}
        path = _normalise_root(req.root, cfg.root) or str(cfg.root)
        return platform_reveal(path)

    @app.post("/api/workspaces/projects/remove")
    def remove_project(req: RemoveProjectRequest) -> dict:
        """Remove a project binding; with ``delete_sessions`` delete its chats too."""
        root = _normalise_root(req.root, cfg.root)
        key = _project_key(root)
        cfg.workspace_projects = [
            p for p in cfg.workspace_projects if _project_key(p.get("root")) != key
        ]
        cfg.save()
        deleted = 0
        if req.delete_sessions:
            deleted = store.delete_root(root)
        return {"ok": True, "root": root, "deleted_sessions": deleted, "projects": build_projects(cfg, store)}

    @app.post("/api/workspaces/archive")
    def archive_project_chats(req: RevealRequest) -> dict:
        """Archive every chat under the project (对齐 codex 归档语义)."""
        root = _normalise_root(req.root, cfg.root)
        count = store.archive_root(root)
        return {"ok": True, "archived_sessions": count, "projects": build_projects(cfg, store)}

    @app.post("/api/workspaces/worktree")
    def create_worktree(req: WorktreeRequest) -> dict:
        """Create a permanent Git worktree as its own project (对齐 codex).

        Mirrors Codex's ``$CODEX_HOME/worktrees`` location via our data dir
        (data_home()/worktrees/<repo>-<slug>), detaches HEAD by resolving the
        commit sha first (avoids codex's refs/heads/HEAD bug), best-effort
        applies local changes + ``.worktreeinclude`` files, and runs
        ``.easycode/setup.sh`` if present.
        """
        root = _normalise_root(req.root, cfg.root)
        if not root:
            raise HTTPException(422, "default project has no directory to worktree")
        src = Path(root)
        if not src.is_dir():
            raise HTTPException(422, f"not a directory: {root}")
        try:
            result = platform_create_worktree(src, sandbox_command=sandbox_command)
        except WorktreeAddError as exc:
            raise HTTPException(500, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        entry = _ensure_project_entry(result["root"])
        if not entry.get("name"):
            entry["name"] = result["slug"]
        cfg.save()
        return {
            "ok": True,
            "root": result["root"],
            "name": entry.get("name"),
            "notes": result["notes"],
            "warnings": result["warnings"],
            "projects": build_projects(cfg, store),
        }

    @app.post("/api/workspaces/choose")
    def choose_workspace(req: ChooseWorkspaceRequest) -> dict:
        """Open the system folder picker (macOS Finder via osascript).

        Blocks while the user picks (or cancels), then returns the chosen
        paths; ``supported`` is False on platforms without osascript.
        """
        paths = choose_folders_via_finder(multiple=req.multiple, prompt=req.prompt)
        return {"paths": paths, "supported": finder_supported()}

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

    @app.post("/api/sessions/{session_id}/undo")
    async def undo_session(session_id: str, req: UndoRequest | None = None) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        if sess._lock.locked():
            raise HTTPException(409, "session busy")
        await sess._lock.acquire()
        try:
            try:
                if req is not None and req.until_user:
                    # Batch undo to a prior user message discards redo state (cannot be
                    # re-applied in one step), so the removed timestamps are dropped.
                    before = sum(1 for m in sess.agent.history.messages if m.get("role") == "user")
                    summary = sess.agent.undo_to_user(req.until_user)
                    removed = before - sum(
                        1 for m in sess.agent.history.messages if m.get("role") == "user"
                    )
                    sess.drop_user_times(removed)
                else:
                    # Single-turn undo is redo-able: pop the most recent timestamp and
                    # stash it so `redo_turn` can restore the user_times alignment.
                    summary = sess.agent.undo_turn()
                    sess.stash_undo_time()
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc
            store.record_exchange(sess)
        finally:
            sess._lock.release()
        return {
            "ok": True,
            "undo_available": sess.agent.undo_available(),
            "redo_available": sess.agent.redo_available(),
            **summary,
        }

    @app.post("/api/sessions/{session_id}/redo")
    async def redo_session(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        if sess._lock.locked():
            raise HTTPException(409, "session busy")
        await sess._lock.acquire()
        try:
            try:
                summary = sess.agent.redo_turn()
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc
            # A single ``redo_turn()`` re-applies exactly ONE user turn, so
            # restore exactly ONE timestamp — the one matching that re-applied
            # turn. ``_undone_user_times`` is a LIFO parallel of the agent's
            # ``_redo_stack`` (each single-turn undo appends one timestamp; the
            # most recent undo's entry sits at the end and is the first redo),
            # so pop from the end. Draining the whole stash over-restored and
            # shifted ``user_times``: 3 turns → 2 undos → 1 redo was
            # producing ``[t2,t3]`` for the surviving ``['one','two']`` instead
            # of ``[t1,t2]``, wrongly merging t3 in and losing t1.
            sess.restore_redo_time()
            store.record_exchange(sess)
        finally:
            sess._lock.release()
        return {
            "ok": True,
            "undo_available": sess.agent.undo_available(),
            "redo_available": sess.agent.redo_available(),
            **summary,
        }

    register_chat(app, cfg, store, broker)

    dist = static_dir or FRONTEND_DIST
    if dist.is_dir():
        from fastapi.responses import FileResponse

        # mounting StaticFiles at "/" turns unmatched POST /api/* into an
        # unclear 405; serve static files via a GET-only SPA fallback instead.
        @app.api_route(
            "/api/{path:path}",
            methods=["POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        def _api_unmatched(path: str) -> None:
            raise HTTPException(404, f"unknown api route: /api/{path}")

        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
