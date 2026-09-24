"""HTTP endpoints for workspaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.config import Config
from easycode.sandbox import sandbox_command
from easycode.web.platform import WorktreeAddError, choose_folders_via_finder, finder_supported
from easycode.web.platform import create_worktree as platform_create_worktree
from easycode.web.platform import reveal_in_finder as platform_reveal
from easycode.web.session import Session, SessionStore, project_key
from easycode.workspace import normalise_secondary, root_error


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


def _normalise_root(root: str | None) -> str | None:
    """Resolve a project root string; empty/'-' mean default project."""
    if not root or root in ("-", "default"):
        return None
    return str(Path(root).expanduser().resolve())


def _session_primary(sess: Session) -> str | None:
    """Canonical primary root for a session ('default' → None key)."""
    return _normalise_root(sess.root)


def projects_from_sessions(store: SessionStore) -> list[dict]:
    """Infer project → secondary bindings from conversation history."""
    by_key: dict[str, set[str]] = {}
    for s in store.list():
        key = project_key(_normalise_root(s.root))
        by_key.setdefault(key, set()).update(s.secondary_roots or [])
    out = [{"root": _normalise_root(k), "secondary": sorted(v)} for k, v in by_key.items()]
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
            key = project_key(p.get("root"))
            if key not in order:
                order.append(key)
            secondary.setdefault(key, set()).update(p.get("secondary") or [])
            if source is base:
                meta[key] = {k: p[k] for k in ("name", "pinned") if p.get(k)}
    out = []
    for key in order:
        root = key if key else None
        entry = {"root": root, "secondary": sorted(secondary[key])}
        entry.update(meta.get(key, {}))
        out.append(entry)
    out.sort(key=lambda p: (not p.get("pinned"), p["root"] is not None, p["root"] or ""))
    return out


def build_projects(cfg: Config, store: SessionStore) -> list[dict]:
    return merge_projects(cfg.workspace_projects, projects_from_sessions(store))


def register_workspaces(app: FastAPI, cfg: Config, store: SessionStore) -> None:
    @app.get("/api/workspaces")
    def list_workspaces() -> dict:
        """Project bindings: main root → its secondary roots.

        Merges config-persisted bindings with those inferred from session
        history. The default project is represented by root=null.
        """
        return {"default": str(cfg.root), "projects": build_projects(cfg, store)}

    @app.post("/api/workspaces/projects")
    def save_project(req: SaveProjectRequest) -> dict:
        root = _normalise_root(req.root)
        if root:
            err = root_error(Path(root))
            if err is not None:
                raise HTTPException(422, err)
        # Validate every secondary before mutating anything: all-or-nothing.
        sec_paths, sec_err = normalise_secondary(req.secondary, cfg.base_dir())
        if sec_err is not None:
            raise HTTPException(422, sec_err)
        secondary = sorted({str(p) for p in sec_paths})
        sess = None
        if req.session_id:
            sess = store.get(req.session_id)
            if sess is None:
                raise HTTPException(404, "session not found")
            # the requested root must match the session's valid primary.
            if project_key(root) != project_key(_session_primary(sess)):
                raise HTTPException(409, "project root does not match session primary")
        key = project_key(root)
        found = False
        for proj in cfg.workspace_projects:
            if project_key(proj.get("root")) == key:
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
        key = project_key(root)
        for proj in cfg.workspace_projects:
            if project_key(proj.get("root")) == key:
                return proj
        secondary = base_secondary
        if secondary is None:
            merged = next(
                (p for p in build_projects(cfg, store) if project_key(p.get("root")) == key), None
            )
            secondary = sorted(merged.get("secondary") or []) if merged else []
        entry: dict[str, Any] = {"root": root, "secondary": secondary}
        cfg.workspace_projects.append(entry)
        return entry

    @app.post("/api/workspaces/pin")
    def pin_project(req: PinProjectRequest) -> dict:
        root = _normalise_root(req.root)
        entry = _ensure_project_entry(root)
        entry["pinned"] = bool(req.pinned)
        cfg.save()
        return {
            "ok": True,
            "root": root,
            "pinned": entry.get("pinned"),
            "projects": build_projects(cfg, store),
        }

    @app.post("/api/workspaces/reveal")
    def reveal_in_finder(req: RevealRequest) -> dict:
        """Reveal the project directory in the system file browser (macOS ``open``)."""
        if not (req.root or Path(cfg.root).is_dir()):
            return {"ok": False, "supported": False, "error": "no directory"}
        path = _normalise_root(req.root) or str(cfg.root)
        return platform_reveal(path)

    @app.post("/api/workspaces/projects/remove")
    def remove_project(req: RemoveProjectRequest) -> dict:
        """Remove a project binding; with ``delete_sessions`` delete its chats too."""
        root = _normalise_root(req.root)
        key = project_key(root)
        cfg.workspace_projects = [
            p for p in cfg.workspace_projects if project_key(p.get("root")) != key
        ]
        cfg.save()
        deleted = 0
        if req.delete_sessions:
            deleted = store.delete_root(root)
        return {
            "ok": True,
            "root": root,
            "deleted_sessions": deleted,
            "projects": build_projects(cfg, store),
        }

    @app.post("/api/workspaces/archive")
    def archive_project_chats(req: RevealRequest) -> dict:
        """Archive every chat under the project (对齐 codex 归档语义)."""
        root = _normalise_root(req.root)
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
        root = _normalise_root(req.root)
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
