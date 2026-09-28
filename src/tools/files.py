"""File tools: read_file, write_file, edit_file.

All tools resolve paths against the sandbox :class:`PathContext` (primary +
secondary roots). Reads are allowed anywhere; writes/edits outside the safe
directories are blocked and reported with ``in_allowed: false`` so the
approval layer can decide.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from easycode.permissions.boundary import PathContext, ToolGrant
from easycode.tools.reading import MAX_READ_BYTES, read_window
from easycode.tools.registry import json_out, tool_scope

MAX_LINE_LEN = 2000
DEFAULT_READ_LIMIT = 2000

def _write_denied(scope: PathContext, p: Path, raw: str, grant: ToolGrant | None) -> str | None:
    """Error JSON when a file write target is denied, else ``None``.

    Shared by write_file and edit_file: the credential hard-deny first, then the
    permanent write boundaries, then the allowed/granted authorization check.
    The protected boundaries are checked before ``in_allowed`` on purpose —
    under the sandboxed presets they are hard protection that no approval can
    lift, so a grant must never open them. ``danger-full-access`` is not an
    approval either: it is the user's decision to drop the boundaries
    altogether, and ``PathContext`` reports them as unprotected there.
    """
    if scope.is_protected(p):
        return json_out(
            "error",
            {
                "message": f"path is protected (credentials): {raw}",
                "in_allowed": False,
                "class": scope.classify(p),
            },
        )
    if scope.is_protected_path(p):
        return json_out(
            "error",
            {
                "message": (
                    f"path is permanently protected (.git/.easycode/project config): {raw}"
                ),
                "in_allowed": False,
                "protected": True,
                "class": scope.classify(p),
            },
        )
    if scope.in_allowed(p) or (grant is not None and scope.grant_granted(p, grant)):
        return None
    return json_out(
        "error",
        {
            "message": f"path outside allowed directories (needs approval): {raw}",
            "in_allowed": False,
            "class": scope.classify(p),
        },
    )


class ReadFileArgs(BaseModel):
    path: str = Field(description="path, relative to a workspace root")
    offset: int = Field(
        1,
        description="1-based line number to start reading from (for paging large files)",
        ge=1,
        le=10_000_000,
    )
    limit: int = Field(
        DEFAULT_READ_LIMIT, description="maximum number of lines to return", ge=1, le=1_000_000
    )


def read_file(
    args: ReadFileArgs,
    *,
    root: Path,
    ctx: PathContext | None = None,
    grant: ToolGrant | None = None,
) -> str:
    scope = tool_scope(root, ctx)
    p = scope.resolve(args.path)
    if scope.is_protected(p):
        return json_out(
            "error",
            {"message": f"path is protected (credentials): {args.path}", "in_allowed": False},
        )
    if not p.is_file():
        return json_out("error", {"message": f"not a file: {args.path}"})
    window = read_window(
        p, args.offset - 1, args.limit, MAX_READ_BYTES, line_chars=MAX_LINE_LEN + 1
    )
    total = window.total
    if total == 0:
        if args.offset > 1:
            return json_out(
                "error", {"message": f"offset {args.offset} out of range (file has 0 lines)"}
            )
        content = "(empty file)"
        return json_out(
            "ok",
            {
                "path": scope.display(p),
                # The unambiguous target: a display path may be relative to any
                # of the session's roots, which is not enough to open the file.
                "absolute_path": str(p),
                "in_allowed": scope.in_allowed(p),
                "start_line": 1,
                "end_line": 0,
                "total_lines": 0,
                "truncated": False,
                "lines": 0,
                "chars": len(content),
                "content": content,
            },
        )
    start = args.offset - 1
    if start >= total:
        return json_out(
            "error", {"message": f"offset {args.offset} out of range (file has {total} lines)"}
        )

    selected: list[str] = []
    bytes_used = 0
    truncated = False
    for line in window.lines:
        out_line = line
        if len(line) > MAX_LINE_LEN:
            out_line = line[:MAX_LINE_LEN] + "…[truncated line]"
            truncated = True
        size = len(out_line.encode("utf-8", errors="replace")) + 1
        if selected and bytes_used + size > MAX_READ_BYTES:
            truncated = True
            break
        selected.append(out_line)
        bytes_used += size
    if start + len(selected) < total:
        truncated = True

    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=args.offset)]
    content = "\n".join(numbered)
    if truncated:
        content += f"\n\n(Showing lines {args.offset}-{args.offset + len(selected) - 1} of {total}. Use offset={args.offset + len(selected)} to continue.)"
    else:
        content += f"\n\n(End of file - total {total} lines)"
    return json_out(
        "ok",
        {
            "path": scope.display(p),
            "absolute_path": str(p),
            "in_allowed": scope.in_allowed(p),
            "start_line": args.offset,
            "end_line": args.offset + len(selected) - 1,
            "total_lines": total,
            "truncated": truncated,
            "lines": len(selected),
            "chars": len(content),
            "content": content,
        },
    )


class WriteFileArgs(BaseModel):
    path: str = Field(description="path, relative to a workspace root; parent dirs auto-created")
    content: str = Field(description="full new file content")


def write_file(
    args: WriteFileArgs,
    *,
    root: Path,
    ctx: PathContext | None = None,
    grant: ToolGrant | None = None,
) -> str:
    scope = tool_scope(root, ctx)
    p = scope.resolve(args.path)
    if (err := _write_denied(scope, p, args.path, grant)) is not None:
        return err
    granted = bool(grant and scope.grant_granted(p, grant))
    allowed = scope.in_allowed(p)
    # an *external* write authorized only by a grant must target a parent
    # that already exists (relative / missing / file-as-directory requests are
    # rejected structurally rather than auto-creating anything outside the safe
    # roots).
    if granted and not allowed:
        parent = p.parent
        if not parent.exists():
            return json_out(
                "error",
                {
                    "message": f"external write target parent does not exist: {parent}",
                    "in_allowed": False,
                    "class": scope.classify(p),
                },
            )
        if not parent.is_dir():
            return json_out(
                "error",
                {
                    "message": f"external write target parent is not a directory: {parent}",
                    "in_allowed": False,
                    "class": scope.classify(p),
                },
            )
        if p.is_dir():
            return json_out(
                "error",
                {
                    "message": f"cannot overwrite a directory: {args.path}",
                    "in_allowed": False,
                    "class": scope.classify(p),
                },
            )
    before = ""
    if p.is_file():
        try:
            before = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            before = ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args.content, encoding="utf-8")
    return json_out(
        "ok",
        {
            "path": scope.display(p),
            # The unambiguous target, as ``read_file`` reports it: a display path
            # may be relative to any of the session's roots.
            "absolute_path": str(p),
            "in_allowed": True,
            "bytes": len(args.content.encode("utf-8")),
            "diff": make_diff(scope.display(p), before, args.content),
        },
    )


class EditFileArgs(BaseModel):
    path: str = Field(description="path of the file to edit, relative to a workspace root")
    old_string: str = Field(
        description="exact text to replace; must appear exactly once in the file"
    )
    new_string: str = Field(description="replacement text")
    dry_run: bool = Field(
        False, description="if true, return the unified diff without modifying the file"
    )


def edit_file(
    args: EditFileArgs,
    *,
    root: Path,
    ctx: PathContext | None = None,
    grant: ToolGrant | None = None,
) -> str:
    scope = tool_scope(root, ctx)
    p = scope.resolve(args.path)
    if not p.is_file():
        return json_out("error", {"message": f"not a file: {args.path}"})
    if (err := _write_denied(scope, p, args.path, grant)) is not None:
        return err
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return json_out("error", {"message": f"cannot read {args.path}: {exc}"})

    count = text.count(args.old_string)
    if count == 0:
        return json_out("error", {"message": f"old_string not found in {args.path}"})
    if count > 1:
        return json_out(
            "error",
            {
                "message": f"old_string found {count} times in {args.path}; include more context to make it unique",
                "occurrences": count,
            },
        )

    new_text = text.replace(args.old_string, args.new_string, 1)
    display = scope.display(p)
    diff = make_diff(display, text, new_text)
    if args.dry_run:
        return json_out(
            "ok",
            {
                "path": display,
                "absolute_path": str(p),
                "dry_run": True,
                "message": "preview only, file unchanged",
                "diff": diff,
            },
        )
    p.write_text(new_text, encoding="utf-8")
    return json_out(
        "ok",
        {
            "path": display,
            "absolute_path": str(p),
            "applied": True,
            "diff": diff,
            "message": "edit applied",
        },
    )


def make_diff(filename: str, before: str, after: str) -> str:
    """Unified diff of one file's before/after content (empty if unchanged)."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=filename,
        tofile=filename,
    )
    return "".join(diff)
