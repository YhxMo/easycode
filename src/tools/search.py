"""grep and glob with early pruning of ignored directories.

Dependency and cache directories are skipped as a cost question in every mode;
the repository database is skipped only while the sandbox is on, because there
it is protected metadata rather than an ordinary readable path.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from easycode.permissions.boundary import PathContext, ToolGrant
from easycode.tools.registry import json_out, tool_scope

#: Dependency, cache and build directories: skipped in every mode, because
#: descending into them is cost without content.
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".pytest_cache", "venv"}
#: The repository database is skipped as well while the sandbox is on — it is
#: protected metadata there. Under full access it is simply a readable path.
GIT_DIR = ".git"

#: Files larger than this are left out of listings and searches entirely.
MAX_FILE_BYTES = 2 * 1024 * 1024


class GrepArgs(BaseModel):
    pattern: str = Field(description="regular expression (Python re) to search file contents")
    include: str | None = Field(None, description="only files matching this glob, e.g. '*.py'")
    max_matches: int = Field(200, description="max matching lines to return", ge=1, le=5000)


def grep(
    args: GrepArgs, *, root: Path, ctx: PathContext | None = None, grant: ToolGrant | None = None
) -> str:
    try:
        rx = re.compile(args.pattern)
    except re.error as exc:
        return json_out("error", {"message": f"invalid regex: {exc}"})
    scope = tool_scope(root, ctx)
    matches: list[dict] = []
    for r in scope.roots:
        for p in iter_files(r, include=args.include, skip_dirs=_search_skip_dirs(scope)):
            if scope.is_protected(p):
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, 1):
                if rx.search(line):
                    match: dict = {"file": scope.display(p), "line": lineno, "text": line[:300]}
                    if not p.is_relative_to(scope.primary.resolve()):
                        match["root"] = str(r)
                    matches.append(match)
                    if len(matches) >= args.max_matches:
                        return json_out("ok", {"matches": matches, "truncated": True})
    return json_out("ok", {"matches": matches, "truncated": False})


class GlobArgs(BaseModel):
    pattern: str = Field(description="glob pattern relative to a workspace root; '**' recurses")
    max_results: int = Field(500, description="max file paths to return", ge=1, le=5000)


def glob(
    args: GlobArgs, *, root: Path, ctx: PathContext | None = None, grant: ToolGrant | None = None
) -> str:
    scope = tool_scope(root, ctx)
    results: set[tuple[str, str | None]] = set()

    def record(p: Path, r: Path) -> None:
        rel = scope.display(p)
        tag: str | None = None
        if not p.is_relative_to(scope.primary.resolve()):
            tag = str(r)
        results.add((rel, tag))

    skip_dirs = _search_skip_dirs(scope)
    for r in scope.roots:
        for pat in (args.pattern, f"**/{args.pattern}"):
            for p in _glob_paths(r, pat, skip_dirs):
                if (
                    p.is_file()
                    and not _is_skipped(p, r, skip_dirs)
                    and not scope.is_protected(p)
                ):
                    record(p, r)
    ordered = sorted(results, key=lambda x: x[0])[: args.max_results]
    out: list[Any] = []
    for rel, tag in ordered:
        out.append(rel if tag is None else {"path": rel, "root": tag})
    return json_out(
        "ok", {"matches": out, "count": len(out), "truncated": len(results) > args.max_results}
    )


def _glob_paths(root: Path, pattern: str, skip_dirs: set[str]) -> Iterator[Path]:
    """Prune ignored directories while expanding ordinary relative patterns.

    Special expressions retain Path.glob's runtime-specific behavior. Literal
    segments retain Path.glob's case handling, normal wildcards follow directory
    links, and ** only descends into real directories.
    """
    parsed = Path(pattern)
    parts = parsed.parts
    separators = tuple(s for s in (os.sep, os.altsep) if s)
    if (
        not parts
        or parsed.drive
        or parsed.is_absolute()
        or pattern.endswith(separators)
        or parts[-1] == "**"
        or ".." in parts
        or any("**" in part and part != "**" for part in parts)
    ):
        yield from root.glob(pattern)
        return

    pending = [(root, 0)]
    seen: set[tuple[Path, int]] = set()
    while pending:
        directory, index = pending.pop()
        state = (directory, index)
        if state in seen:
            continue
        seen.add(state)
        part = parts[index]
        if part in skip_dirs:
            continue
        last = index == len(parts) - 1
        if not any(char in part for char in "*?["):
            for target in directory.glob(part):
                if last:
                    yield target
                elif target.is_dir():
                    pending.append((target, index + 1))
            continue
        if part == "**":
            pending.append((directory, index + 1))  # zero directory levels
        try:
            # Close before descending, even on deeply nested trees.
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            continue
        for entry in entries:
            if entry.name in skip_dirs:
                continue
            target = directory / entry.name
            try:
                if part == "**":
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((target, index))
                elif fnmatch.fnmatch(entry.name, part):
                    if last:
                        yield target
                    elif entry.is_dir():
                        pending.append((target, index + 1))
            except OSError:
                continue


#: What a listing skips unless the caller says otherwise: the performance
#: ignores plus the repository database.
DEFAULT_SKIP_DIRS = SKIP_DIRS | {GIT_DIR}


def _search_skip_dirs(scope: PathContext) -> set[str]:
    """Directories a search under ``scope`` never descends into.

    Dependency and cache directories are a cost question, not a boundary, so
    they stay skipped in every mode; ``.git`` is skipped only while the sandbox
    is on — under full access it is an ordinary readable path.
    """
    return SKIP_DIRS if scope.full_access else DEFAULT_SKIP_DIRS


def iter_files(
    root: Path, include: str | None = None, skip_dirs: set[str] = DEFAULT_SKIP_DIRS
) -> list[Path]:
    """Every file under ``root`` the tools may read, in a stable path order.

    Walks directory by directory so ignored directories are pruned before they
    are entered: ``rglob`` visited every file of ``node_modules`` only to throw
    it away, which is what made a large tree expensive. The result keeps the
    previous order (full path, lexicographic), so callers that stop at the
    first N matches still see the same set.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in skip_dirs)
        for name in filenames:
            if name.startswith("._") or name == ".DS_Store":
                continue
            p = Path(dirpath) / name
            if include and not fnmatch.fnmatch(name, include) and not fnmatch.fnmatch(
                p.relative_to(root).as_posix(), include
            ):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(p)
    files.sort()
    return files


def _is_skipped(p: Path, root: Path, skip_dirs: set[str]) -> bool:
    return any(part in skip_dirs for part in p.relative_to(root).parts) or _is_meta(p)


def _is_meta(p: Path) -> bool:
    return p.name.startswith("._") or p.name == ".DS_Store"
