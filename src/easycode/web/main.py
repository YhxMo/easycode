"""FastAPI app: sessions + SSE chat + model management."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from easycode.config import API_FORMATS, DEFAULT_API_FORMAT, Config, infer_api_format
from easycode.credentials import (
    Credential,
    delete_credential,
    new_credential_id,
    load_credentials,
    save_credential,
    data_home,
)
from easycode.skills import SkillRegistry
from easycode.web.bridge import ApprovalBroker, event_to_sse, stream_chat_with_approval
from easycode.web.session import Session, SessionStore

FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

FORMAT_PROVIDERS = {
    "openai_responses": "openai",
    "openai_compatible": "openai",
    "anthropic": "anthropic",
    "bedrock": "bedrock",
    "gemini": "gemini",
}


def infer_provider(model: str, api_format: str) -> str:
    """Infer the supplier independently from the wire API format."""
    prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
    if prefix and prefix != "responses":
        return prefix
    return FORMAT_PROVIDERS.get(api_format, "custom")


def _derive_api_format(model: str) -> str:
    """API format for a model when the credential stores none.

    Recognises OpenAI Responses (``responses/`` route), and the native
    Anthropic / Bedrock / Gemini formats from the wire provider (``provider``
    or the model-string prefix); everything else defaults to OpenAI-compatible
    chat completions.
    """
    return infer_api_format(model)


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

    def models_response() -> dict:
        providers: dict[str, str] = {}
        limits: dict[str, dict[str, int] | None] = {}
        credentials = load_credentials()
        for alias, spec in cfg.models.items():
            credential = credentials.get(spec.key_id) if spec.key_id else None
            providers[alias] = (
                spec.provider
                or (credential.provider if credential else None)
                or infer_provider(spec.model, spec.api_format)
            )
            limits[alias] = cfg.get_model_limits(alias)
        return {
            "default": cfg.default_model,
            "models": {alias: spec.to_value() for alias, spec in cfg.models.items()},
            "providers": providers,
            "limits": limits,
        }

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
        """Return model records without exposing API keys."""
        reloaded = Config.load(start=cfg.root)
        cfg.models = reloaded.models
        cfg.default_model = reloaded.default_model
        return models_response()

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

        api_key = (req.api_key or "").strip()
        base_url = (req.base_url or "").strip() or None
        key_id = None
        if api_key or base_url:
            key_id = new_credential_id()
            save_credential(
                Credential(
                    key_id=key_id,
                    api_key=api_key,
                    provider=req.provider,
                    base_url=base_url,
                )
            )
        entry: dict[str, str] = {"model": model, "api_format": api_format}
        if req.provider and req.provider.strip():
            entry["provider"] = req.provider.strip()
        if key_id:
            entry["key_id"] = key_id
        cfg.set_model_alias(alias, entry)
        if cfg.default_model not in cfg.models:
            cfg.set_default_model(alias)
        cfg.save()
        return models_response()

    @app.get("/api/models/{alias}")
    def get_model_detail(alias: str) -> dict:
        """Return one model's editable configuration.

        Unlike the collection endpoint, this returns the raw API key because
        the localhost-only edit dialog must be able to reveal it on demand.
        Models without a stored credential fall back to provider env vars so
        the dialog shows the effective configuration (masked until revealed).
        """
        spec = cfg.models.get(alias)
        if spec is None:
            raise HTTPException(404, f"unknown alias: {alias}")
        cred = load_credentials().get(spec.key_id) if spec.key_id else None
        api_format = spec.api_format or _derive_api_format(spec.model)
        provider = spec.provider or (cred.provider if cred and cred.provider else infer_provider(spec.model, api_format))
        api_key = cred.api_key if cred else ""
        base_url = cred.base_url if cred else None
        return {
            "alias": alias,
            "model": spec.model,
            "key_id": spec.key_id,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "api_format": api_format,
            "has_api_key": bool(api_key),
        }

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

        current_cred = load_credentials().get(spec.key_id) if spec.key_id else None
        if req.clear_key:
            if spec.key_id:
                delete_credential(spec.key_id)
            key_id = None
        else:
            next_api_key = (req.api_key if req.api_key is not None else (current_cred.api_key if current_cred else "")).strip()
            next_base_url = req.base_url if req.base_url is not None else (current_cred.base_url if current_cred else None)
            if spec.key_id or next_api_key or next_base_url:
                key_id = spec.key_id or new_credential_id()
                save_credential(
                    Credential(
                        key_id=key_id,
                        api_key=next_api_key,
                        provider=req.provider if req.provider is not None else (current_cred.provider if current_cred else None),
                        base_url=next_base_url,
                    )
                )
            else:
                key_id = None

        if target_alias != alias:
            cfg.rename_model_alias(alias, target_alias)
        provider = req.provider.strip() if req.provider is not None else spec.provider
        entry: dict[str, str] = {"model": model, "api_format": api_format}
        if provider:
            entry["provider"] = provider
        if key_id:
            entry["key_id"] = key_id
        cfg.set_model_alias(target_alias, entry)
        cfg.save()

        for sess in store.list():
            if sess.model_alias != alias:
                continue
            from easycode.cli import _rebind_agent

            _rebind_agent(sess.agent, cfg, target_alias)
            sess.model_alias = target_alias
            store.record_exchange(sess)

        return models_response()

    @app.delete("/api/models/{alias}")
    def delete_model(alias: str) -> dict:
        if alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {alias}")
        spec = cfg.models[alias]
        del cfg.models[alias]
        if spec.key_id:
            delete_credential(spec.key_id)
        if cfg.default_model == alias:
            cfg.set_default_model(next(iter(cfg.models), "deepseek-v4flash"))
        cfg.save()
        return models_response()

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
        return models_response()

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
    def archive_session(session_id: str, req: ArchiveRequest) -> dict:
        sess = store.set_archived(session_id, req.archived)
        if sess is None:
            raise HTTPException(404, "session not found")
        return {"ok": True, "archived": sess.archived}

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
        if req.session_id:
            sess = store.get(req.session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
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
        import shutil

        if not (req.root or Path(cfg.root).is_dir()):
            return {"ok": False, "supported": False, "error": "no directory"}
        path = _normalise_root(req.root, cfg.root) or str(cfg.root)
        if not Path(path).is_dir():
            return {"ok": False, "supported": False, "error": f"not a directory: {path}"}
        if not (sys.platform == "darwin" and shutil.which("open")):
            return {"ok": False, "supported": False, "error": "open is only supported on macOS"}
        import subprocess

        try:
            subprocess.run(["open", path], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "supported": False, "error": "open failed"}
        return {"ok": True, "supported": True, "path": path}

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
        import shutil
        import subprocess
        import uuid

        root = _normalise_root(req.root, cfg.root)
        if not root:
            raise HTTPException(422, "default project has no directory to worktree")
        src = Path(root)
        if not src.is_dir():
            raise HTTPException(422, f"not a directory: {root}")

        def run(cmd: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )

        if not shutil.which("git"):
            raise HTTPException(422, "git is not installed")
        is_repo = run(["git", "rev-parse", "--is-inside-work-tree"], src)
        if is_repo.returncode != 0 or is_repo.stdout.strip() != "true":
            raise HTTPException(422, "项目不是 Git 仓库，无法创建 worktree")

        try:
            head_sha = run(["git", "rev-parse", "HEAD"], src).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            raise HTTPException(422, "cannot resolve HEAD commit")
        if not head_sha:
            raise HTTPException(422, "repository has no commits")

        root_home = data_home() / "worktrees"
        root_home.mkdir(parents=True, exist_ok=True)
        slug = f"{src.name}-{uuid.uuid4().hex[:5]}"
        wt = root_home / slug
        added = run(["git", "worktree", "add", "--detach", str(wt), head_sha], src)
        if added.returncode != 0 or not (wt.is_dir() and (wt / ".git").exists() or (wt / ".git").is_file()):
            raise HTTPException(500, f"git worktree add failed: {added.stderr[:200]}")

        notes: list[str] = []

        # best-effort: apply uncommitted changes from the source checkout
        diff = run(["git", "diff", "HEAD"], src)
        if diff.returncode == 0 and diff.stdout.strip():
            # git apply needs stdin; use a temp patch file instead
            patch = wt.parent / f"{slug}.patch"
            patch.write_text(diff.stdout, encoding="utf-8")
            res = run(["git", "apply", str(patch)], wt, timeout=15)
            if res.returncode == 0:
                notes.append("applied uncommitted changes")
            try:
                patch.unlink(missing_ok=True)
            except OSError:
                pass

        # .worktreeinclude: copy gitignored files listed at the repo root
        include_file = src / ".worktreeinclude"
        if include_file.is_file():
            for raw in include_file.read_text(encoding="utf-8").splitlines():
                rel = raw.strip()
                if not rel or rel.startswith("#") or rel.startswith("!"):
                    continue
                s = src / rel
                if not s.exists() or s.is_symlink():
                    continue
                d = wt / rel
                if d.exists():
                    continue
                try:
                    if s.is_dir():
                        import shutil as _sh

                        _sh.copytree(s, d, symlinks=False, dirs_exist_ok=False)
                    else:
                        d.parent.mkdir(parents=True, exist_ok=True)
                        import shutil as _sh

                        _sh.copy2(s, d)
                except OSError:
                    pass
            notes.append("copied .worktreeinclude")

        # best-effort setup script (代码对齐 codex 的 .codex/setup.sh)
        setup = wt / ".easycode" / "setup.sh"
        if setup.is_file():
            try:
                run(["bash", str(setup)], wt, timeout=300)
                notes.append("ran .easycode/setup.sh")
            except (OSError, subprocess.TimeoutExpired) as exc:
                notes.append(f"setup.sh failed: {exc}")

        entry = _ensure_project_entry(str(wt.resolve()))
        if not entry.get("name"):
            entry["name"] = slug
        cfg.save()
        return {
            "ok": True,
            "root": str(wt.resolve()),
            "name": entry.get("name"),
            "notes": notes,
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
        sess.user_times.append(datetime.now(timezone.utc).isoformat())

        async def gen():
            cancel_event = asyncio.Event()
            sess.cancel_event = cancel_event
            try:
                yield "data: " + json.dumps(
                    {"type": "session", "session_id": sess.id}, ensure_ascii=False
                ) + "\n\n"
                gen_it = stream_chat_with_approval(
                    sess.agent, req.message, broker, cancel_event=cancel_event, session=sess
                )
                async for kind, payload in gen_it:
                    if kind == "approval":
                        approval_id, tc, reason, scope = payload
                        data = json.dumps(
                            {
                                "type": "approval_required",
                                "approval_id": approval_id,
                                "reason": reason,
                                "scope": scope,
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

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

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
