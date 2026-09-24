"""Read-only workspace file access for the composer's @-mention menu.

The scope is the same one a turn would execute in — a session's own roots, or
the roots a new session would be created with — so the menu can only ever offer
files the agent could actually read. Nothing here needs approval: it is a
read-only listing inside the workspace, and credential / write-boundary paths
are excluded outright.

POST rather than GET for the same reason as ``/api/commands``: the query has to
distinguish "no secondary roots given, inherit the project binding" from
"explicitly no secondary roots", which a query string cannot express.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from easycode.tools.files import MAX_READ_BYTES, _iter_files
from easycode.workspace import PathContext, root_error

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


class FilesRequest(BaseModel):
    session_id: str | None = None
    root: str | None = None
    # None inherits the project binding; [] means "no secondary roots".
    secondary_roots: list[str] | None = None
    q: str = ""
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def _scope(req: FilesRequest, cfg, store) -> PathContext:
    """The path context whose roots bound this listing."""
    if req.session_id:
        sess = store.get(req.session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        return sess.agent.path_context()

    from easycode.web.routes_chat import _draft_roots

    roots = _draft_roots(req.root, req.secondary_roots, cfg, store)
    primary, *secondary = roots
    err = root_error(primary)
    if err is not None:
        raise HTTPException(422, err)
    return PathContext(primary=primary, secondary=secondary)


def register_files(app: FastAPI, cfg, store) -> None:
    """Connect the workspace file listing to this app's session store."""

    @app.post("/api/files")
    def list_files(req: FilesRequest) -> dict:
        ctx = _scope(req, cfg, store)
        needle = req.q.strip().lower()
        found: list[dict] = []
        for root in ctx.roots:
            for path in _iter_files(root):
                if ctx.is_protected(path) or ctx.is_protected_path(path):
                    continue
                rel = path.relative_to(root).as_posix()
                if needle and needle not in path.name.lower() and needle not in rel.lower():
                    continue
                parent = Path(rel).parent.as_posix()
                found.append(
                    {
                        "path": rel,
                        "name": path.name,
                        "dir": "" if parent == "." else parent,
                        "root": str(root),
                    }
                )
        # A basename hit is a better answer than a deep path hit; otherwise the
        # listing reads as a stable, predictable tree.
        found.sort(key=lambda f: (0 if needle and needle in f["name"].lower() else 1, f["path"]))
        return {"files": found[: req.limit], "total": len(found)}

    @app.get("/api/files/content")
    def file_content(
        session_id: str,
        path: str,
        offset: int = 1,
        limit: int = 0,
    ) -> dict:
        """One file's text, for the pane's preview.

        Read directly rather than through the ``read_file`` tool: that handler
        wraps its output in the model's envelope (offset footers, line
        truncation notices), while a preview shows the file itself. The byte
        budget is the tool's ``MAX_READ_BYTES`` so a preview cannot pull in more
        than a read would.
        """
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        ctx = sess.agent.path_context()
        target = ctx.resolve(path)
        if ctx.is_protected(target) or ctx.is_protected_path(target):
            raise HTTPException(403, "path is protected")
        # `resolve` passes absolute paths through: containment is what keeps the
        # preview inside the roots the session may actually work in.
        if not any(target.is_relative_to(root) for root in ctx.roots):
            raise HTTPException(403, "path is outside the workspace")
        if not target.is_file():
            raise HTTPException(404, "not a file")

        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(0, offset - 1)
        window = lines[start:] if limit <= 0 else lines[start : start + limit]
        text = "\n".join(window)
        cut = len(text.encode("utf-8")) > MAX_READ_BYTES
        if cut:
            text = text.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        return {
            "path": ctx.display(target),
            "text": text,
            "start_line": start + 1,
            "total_lines": total,
            "truncated": cut or start + len(window) < total,
        }
