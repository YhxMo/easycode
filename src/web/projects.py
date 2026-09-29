"""Project bindings: which roots exist, what they are called, what they hold.

A project is a primary root plus the secondary roots and metadata bound to it.
Two sources describe them and they are unioned rather than merged field by field:
the configuration (which owns naming and pinning) and the conversation history
(which only knows which secondaries have actually been used).

This is also where "which directories may a request name" is decided, so the MCP
scope root and the command menu ask the same question the workspace routes do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from easycode.config import Config
from easycode.web.locks import project_key
from easycode.web.session import Session
from easycode.web.store import SessionStore


def normalise_root(root: str | None) -> str | None:
    """Resolve a project root string; empty/'-' mean default project."""
    return project_key(root) or None


def session_primary(sess: Session) -> str | None:
    """Canonical primary root for a session ('default' → None key)."""
    return normalise_root(sess.root)


def projects_from_sessions(store: SessionStore) -> list[dict]:
    """Infer project → secondary bindings from conversation history."""
    by_key: dict[str, set[str]] = {}
    for s in store.list():
        key = project_key(normalise_root(s.root))
        by_key.setdefault(key, set()).update(s.secondary_roots or [])
    out = [{"root": normalise_root(k), "secondary": sorted(v)} for k, v in by_key.items()]
    out.sort(key=lambda p: (p["root"] is not None, p["root"] or ""))
    return out


def merge_projects(base: list[dict], extra: list[dict]) -> list[dict]:
    """Union project bindings by root key; ``base`` (config) wins ordering.

    Name/pinned metadata is preserved from the config entries (``base``);
    pinned projects sort above the rest (stable within their groups).
    """
    meta: dict[str, dict] = {}
    secondary: dict[str, set[str]] = {}
    for source in (base, extra):
        for p in source:
            key = project_key(p.get("root"))
            # setdefault keeps first-seen order, so the dict's own order is
            # already "config entries first, then whatever only history knows".
            secondary.setdefault(key, set()).update(p.get("secondary") or [])
            if source is base:
                meta[key] = {k: p[k] for k in ("name", "pinned") if p.get(k)}
    out = []
    for key in secondary:
        root = key if key else None
        entry = {"root": root, "secondary": sorted(secondary[key])}
        entry.update(meta.get(key, {}))
        out.append(entry)
    out.sort(key=lambda p: (not p.get("pinned"), p["root"] is not None, p["root"] or ""))
    return out


def build_projects(cfg: Config, store: SessionStore) -> list[dict]:
    return merge_projects(cfg.workspace_projects, projects_from_sessions(store))


def known_projects(cfg, store) -> list[dict[str, Any]]:
    """Projects a request may name as the MCP scope root.

    Registered projects and the configured default workspace, never the process
    CWD: the server's own directory is not the user's project.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in [*build_projects(cfg, store), {"root": None, "secondary": []}]:
        resolved = str(Path(project.get("root") or cfg.root).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(
            {
                "root": resolved,
                "name": str(project.get("name") or Path(resolved).name),
            }
        )
    return out
