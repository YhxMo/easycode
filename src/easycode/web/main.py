"""FastAPI app: sessions + SSE chat + model management."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from easycode.config import Config
from easycode.credentials import Credential, delete_credential, load_credentials, save_credential
from easycode.skills import SkillRegistry
from easycode.web.bridge import ApprovalBroker, event_to_sse, stream_chat_with_approval
from easycode.web.session import Session, SessionStore

FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

WEB_KEY_PREFIX = "web-"


def _attach_snapshot(sess: Session) -> None:
    """Give a session its file snapshot manager (no-op if already attached)."""
    if sess.agent.snapshot_manager is None:
        from easycode.snapshot import FileSnapshotManager

        sess.agent.snapshot_manager = FileSnapshotManager(
            sess.id, sess.agent.path_context().roots
        )


def _session_roots(sess: Session, cfg: Config) -> list[Path]:
    raw = [sess.root, *sess.secondary_roots] if sess.root else [str(cfg.root), *sess.secondary_roots]
    return [Path(p) for p in raw if p]


def _expand_command(message: str, sess: Session, cfg: Config) -> str:
    """Resolve a leading '/' message: templates/skills expand, builtins rejected."""
    from easycode.commands import CommandRegistry

    reg = _build_web_commands(_session_roots(sess, cfg), sess.agent.skills)
    resolved = reg.resolve(message)
    if resolved is None:
        raise HTTPException(400, f"unknown command: {message.split()[0]}")
    cmd, rest = resolved
    if cmd.kind in ("template", "skill"):
        return cmd.expand(rest)
    raise HTTPException(400, f"/{cmd.name} 是终端内置命令，模板命令（.easycode/commands/*.md）与 skill 可在 Web 使用")


def _build_web_commands(roots: list[Path], skills) -> "CommandRegistry":
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


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    secondary_roots: list[str] | None = None
    permission_mode: str | None = None
    root: str | None = None


class ChooseWorkspaceRequest(BaseModel):
    multiple: bool = False
    prompt: str = "选择目录"


class SaveProjectRequest(BaseModel):
    root: str | None = None
    secondary: list[str] = []
    session_id: str | None = None


class PermissionRequest(BaseModel):
    mode: str


FINDER_APPLESCRIPT = 'POSIX path of (choose folder with prompt "{prompt}"{multiple})'
FINDER_MULTIPLE_SUFFIX = " with multiple selections allowed"


def finder_supported() -> bool:
    import shutil

    return sys.platform == "darwin" and shutil.which("osascript") is not None


def choose_folders_via_finder(multiple: bool = False, prompt: str = "选择目录") -> list[str]:
    """Open a macOS Finder folder picker via osascript; [] when cancelled/unavailable."""
    import subprocess

    if not finder_supported():
        return []
    script = FINDER_APPLESCRIPT.format(
        prompt=prompt.replace('"', '\\"'),
        multiple=FINDER_MULTIPLE_SUFFIX if multiple else "",
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out = (proc.stdout or "").strip()
    if not out:
        return []
    paths: list[str] = []
    for line in out.splitlines():
        p = line.strip().strip('"')
        if p:
            paths.append(p)
    return paths


def _normalise_root(root: str | None, default: Path) -> str | None:
    """Resolve a project root string; empty/'-' mean default project."""
    if not root or root in ("-", "default"):
        return None
    return str(Path(root).expanduser().resolve())


def _project_key(root: str | None) -> str:
    return root or ""


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
    """Union project bindings by root key; ``base`` (config) wins ordering."""
    merged: dict[str, set[str]] = {}
    order: list[str] = []
    for source in (base, extra):
        for p in source:
            key = _project_key(p.get("root"))
            if key not in merged:
                order.append(key)
            merged.setdefault(key, set()).update(p.get("secondary") or [])
    out = []
    for key in order:
        root = None if not key else key
        out.append({"root": root, "secondary": sorted(merged[key])})
    out.sort(key=lambda p: (p["root"] is not None, p["root"] or ""))
    return out


def build_projects(cfg: Config, store: SessionStore) -> list[dict]:
    return merge_projects(cfg.workspace_projects, projects_from_sessions(store, cfg.root))


class ApprovalRequest(BaseModel):
    approve: bool = True


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


def create_app(
    cfg: Config | None = None,
    session_store: SessionStore | None = None,
    static_dir: Path | None = None,
    approval_broker: ApprovalBroker | None = None,
) -> FastAPI:
    """App factory. ``session_store``/``approval_broker`` injectable for tests."""
    from easycode.agent.loop import Agent  # noqa: F401
    from easycode.cli import make_agent

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

    @app.get("/api/models")
    def get_models() -> dict:
        """Masked model list: values are str or {model, key_id} — never api_key."""
        reloaded = Config.load(start=cfg.root)
        cfg.models = reloaded.models
        cfg.default_model = reloaded.default_model
        return {
            "default": cfg.default_model,
            "models": {alias: spec.to_value() for alias, spec in cfg.models.items()},
        }

    @app.post("/api/models/add")
    def add_model(req: AddModelRequest) -> dict:
        alias = req.alias.strip()
        if not alias or not req.model.strip():
            raise HTTPException(422, "alias and model are required")
        if req.api_key:
            key_id = f"{WEB_KEY_PREFIX}{alias}"
            save_credential(
                Credential(
                    key_id=key_id,
                    api_key=req.api_key,
                    provider=req.provider,
                    base_url=req.base_url,
                )
            )
            cfg.set_model_alias(alias, {"model": req.model, "key_id": key_id})
        else:
            cfg.set_model_alias(alias, req.model)
        if cfg.default_model not in cfg.models:
            cfg.set_default_model(alias)
        cfg.save()
        return {
            "default": cfg.default_model,
            "models": {a: spec.to_value() for a, spec in cfg.models.items()},
        }

    @app.delete("/api/models/{alias}")
    def delete_model(alias: str) -> dict:
        if alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {alias}")
        spec = cfg.models[alias]
        del cfg.models[alias]
        if spec.key_id == f"{WEB_KEY_PREFIX}{alias}":
            delete_credential(spec.key_id)
        if cfg.default_model == alias:
            cfg.set_default_model(next(iter(cfg.models), "deepseek-v4flash"))
        cfg.save()
        return {
            "default": cfg.default_model,
            "models": {a: s.to_value() for a, s in cfg.models.items()},
        }

    @app.post("/api/models")
    def set_model(req: ModelRequest) -> dict:
        if req.alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {req.alias}")
        cfg.set_default_model(req.alias)
        cfg.save()
        for sess in store.list():
            from easycode.cli import _rebind_agent

            _rebind_agent(sess.agent, cfg, req.alias)
            sess.model_alias = req.alias
        return {
            "default": cfg.default_model,
            "models": {a: spec.to_value() for a, spec in cfg.models.items()},
        }

    @app.get("/api/sessions")
    def list_sessions() -> list[dict]:
        return [s.summary for s in store.list()]

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        return {**sess.summary, "messages": sess.messages}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict:
        if not store.delete(session_id):
            raise HTTPException(404, "session not found")
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/permission")
    def set_session_permission(session_id: str, req: PermissionRequest) -> dict:
        from easycode.approval import permission_parse

        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        try:
            mode = permission_parse(req.mode)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        sess.permission_mode = mode
        sess.agent.permission_mode = mode
        store.record_exchange(sess)
        return {"id": sess.id, "permission_mode": mode}

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
        secondary = sorted({str(Path(p).expanduser().resolve()) for p in req.secondary if p.strip()})
        key = _project_key(root)
        found = False
        for proj in cfg.workspace_projects:
            if _project_key(proj.get("root")) == key:
                proj["root"] = root
                proj["secondary"] = secondary
                found = True
                break
        if not found:
            cfg.workspace_projects.append({"root": root, "secondary": secondary})
        cfg.save()
        if req.session_id:
            sess = store.get(req.session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            sess.secondary_roots = list(secondary)
            sess.agent.secondary_roots = [Path(p) for p in secondary]
            store.record_exchange(sess)
        return {"root": root, "secondary": secondary, "projects": build_projects(cfg, store)}

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
        if not broker.resolve(approval_id, req.approve):
            raise HTTPException(404, f"unknown or expired approval: {approval_id}")
        return {"ok": True, "approve": req.approve}

    @app.post("/api/sessions/{session_id}/cancel")
    def cancel_session(session_id: str) -> dict:
        """Cancel the in-flight chat for a session (session interruption)."""
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        cancelled = sess.cancel_stream()
        return {"ok": True, "cancelled": cancelled}

    @app.post("/api/sessions/{session_id}/undo")
    def undo_session(session_id: str, req: UndoRequest | None = None) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        try:
            if req is not None and req.until_user:
                summary = sess.agent.undo_to_user(req.until_user)
            else:
                summary = sess.agent.undo_turn()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.record_exchange(sess)
        return {
            "ok": True,
            "undo_available": sess.agent.undo_available(),
            "redo_available": sess.agent.redo_available(),
            **summary,
        }

    @app.post("/api/sessions/{session_id}/redo")
    def redo_session(session_id: str) -> dict:
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        try:
            summary = sess.agent.redo_turn()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.record_exchange(sess)
        return {
            "ok": True,
            "undo_available": sess.agent.undo_available(),
            "redo_available": sess.agent.redo_available(),
            **summary,
        }

    @app.post("/api/chat")
    def chat(req: ChatRequest, background: BackgroundTasks) -> StreamingResponse:
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
        sess = _get_session(req.session_id, **kwargs) if kwargs else _get_session(req.session_id)
        if perm_mode and req.session_id:
            sess.permission_mode = perm_mode
            sess.agent.permission_mode = perm_mode
            store.record_exchange(sess)
        raw_message = req.message
        if raw_message.strip().startswith("/"):
            req.message = _expand_command(req.message, sess, cfg)
        if sess.title == "新会话":
            sess.title = raw_message.strip()[:30]
            store.record_exchange(sess)

        async def gen():
            cancel_event = asyncio.Event()
            sess.cancel_event = cancel_event
            try:
                yield "data: " + json.dumps(
                    {"type": "session", "session_id": sess.id}, ensure_ascii=False
                ) + "\n\n"
                gen_it = stream_chat_with_approval(
                    sess.agent, req.message, broker, cancel_event=cancel_event
                )
                async for kind, payload in gen_it:
                    if kind == "approval":
                        approval_id, tc = payload
                        data = json.dumps(
                            {
                                "type": "approval_required",
                                "approval_id": approval_id,
                                "tool_call": {
                                    "id": tc.id,
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                },
                            },
                            ensure_ascii=False,
                        )
                        yield f"data: {data}\n\n"
                    elif kind == "text":
                        data = json.dumps({"type": "text", "content": payload}, ensure_ascii=False)
                        yield f"data: {data}\n\n"
                    else:
                        yield event_to_sse(payload)
            except asyncio.CancelledError:
                try:
                    yield "data: " + json.dumps({"type": "cancelled"}, ensure_ascii=False) + "\n\n"
                except BaseException:
                    pass
                raise
            finally:
                if sess.cancel_event is cancel_event:
                    sess.cancel_event = None
                store.record_exchange(sess)

        return StreamingResponse(gen(), media_type="text/event-stream")

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
            return FileResponse(dist / "index.html")

    return app


app = create_app()