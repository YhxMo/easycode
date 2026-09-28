"""Read-only workspace file access for the composer's @-mention menu.

The scope is the same one a turn would execute in — a session's own roots, or
the roots a new session would be created with — so the menu can only ever offer
files the agent could actually read. Nothing here needs approval: it is a
read-only listing inside the workspace, and credential / write-boundary paths
are excluded outright (a full-access session has neither boundary, and its
previews may reach any host path its tools could).

POST rather than GET because the body has to distinguish "no secondary roots
given, inherit the project binding" from "explicitly no secondary roots" — an
absent query parameter and an empty one read the same, and the difference
decides what this session's scope actually is.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from easycode.permissions.boundary import PathContext, root_error
from easycode.tools.reading import MAX_READ_BYTES, read_window
from easycode.tools.search import iter_files

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

    from easycode.web.routes.chat import _draft_roots

    # A draft root that cannot be resolved or validated is a bad request, the
    # same as it is for /api/commands and /api/chat: 422, never a 500 from a
    # validation that happens to raise.
    try:
        roots = _draft_roots(req.root, req.secondary_roots, cfg, store)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    primary, *secondary = roots
    err = root_error(primary)
    if err is not None:
        raise HTTPException(422, err)
    return PathContext(primary=primary, secondary=secondary)


def register_files(app: FastAPI, cfg, store) -> None:
    """Connect the workspace file listing to this app's session store."""

    @app.post("/api/files")
    def list_files(req: FilesRequest) -> dict:
        """Workspace files, each with the display path *and* its real target.

        A display path is relative to the root it came from, so the same
        relative name can exist under several roots. ``absolute_path`` is the
        canonical file the caller must use to reference or preview it.
        """
        ctx = _scope(req, cfg, store)
        needle = req.q.strip().lower()
        roots = ctx.roots
        found: list[dict] = []
        seen: set[Path] = set()
        for root in roots:
            for path in iter_files(root):
                target = path.resolve()
                # Overlapping roots offer one file twice; the first root that
                # reaches it — the primary — is what names it.
                if target in seen:
                    continue
                seen.add(target)
                # Only files the preview can actually open belong here: a
                # symlink out of the workspace, or into a protected path, would
                # be listed and then refused.
                if ctx.is_protected(target) or ctx.is_protected_path(target):
                    continue
                if not any(target.is_relative_to(r) for r in roots):
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
                        "absolute_path": str(target),
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

        The preview follows the session's own boundary: under full access any
        host path a tool could read is previewable, while the sandboxed presets
        stay inside the roots that session works in.

        ``offset``/``limit`` keep their historical tolerance — any integer is
        accepted, a non-positive ``limit`` means "to the end", and an offset
        past the end returns an empty window rather than an error.
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
        if not ctx.full_access and not any(target.is_relative_to(root) for root in ctx.roots):
            raise HTTPException(403, "path is outside the workspace")
        if not target.is_file():
            raise HTTPException(404, "not a file")

        start = max(0, offset - 1)
        try:
            reader = read_window(target, start, limit)
        except FileNotFoundError as exc:
            # Removed between the check above and the read.
            raise HTTPException(404, "not a file") from exc
        except OSError as exc:
            raise HTTPException(500, f"读取文件失败: {exc}") from exc
        total = reader.total
        text = "\n".join(reader.lines)
        cut = len(text.encode("utf-8")) > MAX_READ_BYTES
        if cut:
            text = text.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        return {
            "path": ctx.display(target),
            "text": text,
            "start_line": start + 1,
            "total_lines": total,
            "truncated": cut or start + len(reader.lines) < total,
        }
