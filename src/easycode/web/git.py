"""Live Git state of the directories a session works in.

The pane shows two different things side by side: what this conversation's tools
did (recorded per session, see ``artifacts``) and what the working tree looks
like right now. The second one is the repositories' own state — it includes
changes that predate the conversation — so it is reported as its own fact and
never merged into the session's record.

Everything is measured against each repository's current ``HEAD``: staged,
unstaged and untracked files all count as changes on top of it.

Read-only and server-side, like the workspace file listing: the same roots bound
it, no approval is involved, and nothing here writes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from easycode.web.session import Session

#: A hung or enormous status must not hold the request open.
GIT_TIMEOUT = 10.0
#: Changed files reported per session (a fresh clone can have thousands).
MAX_FILES = 300
#: Text handed to the browser for one file's diff; a longer one keeps its counts
#: and is reported without a preview.
MAX_DIFF_CHARS = 20_000
#: Files a single request generates a diff for. Each one is its own git call, so
#: a large working tree reports counts first and previews only its first files.
MAX_DIFF_FILES = 60
#: The tree an empty repository (no commit yet) is compared against.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    """``git`` refused to report the state of a repository."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def repo_root(path: Path) -> Path | None:
    """The repository ``path`` belongs to, or None when it is not in one."""
    if not path.is_dir():
        return None
    try:
        top = _git(path, "rev-parse", "--show-toplevel").strip()
    except (GitError, OSError, subprocess.SubprocessError):
        return None
    return Path(top) if top else None


def _baseline(repo: Path) -> str:
    """The commit the working tree is measured against.

    A repository without a commit yet has no ``HEAD``; the empty tree gives the
    same answer there — every file is new — instead of failing the whole report.
    """
    try:
        return _git(repo, "rev-parse", "--verify", "HEAD").strip() or EMPTY_TREE
    except (GitError, OSError, subprocess.SubprocessError):
        return EMPTY_TREE


def _status(repo: Path) -> list[dict]:
    """Every uncommitted file, staged and unstaged, as porcelain entries.

    ``-z`` keeps paths verbatim (no quoting or escaping to undo); a rename or
    copy is followed by its original path in the next NUL-separated field.
    """
    fields = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    out: list[dict] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        staged, worktree, path = entry[0], entry[1], entry[3:]
        origin = ""
        if staged in ("R", "C"):
            # The path it came from is reported on its own, and it is what makes
            # the file's diff describe a rename instead of a whole new file.
            origin = fields[index] if index < len(fields) else ""
            index += 1
        target = repo / path
        out.append(
            {
                "path": path,
                "origin": origin,
                "absolute_path": str(target),
                "untracked": staged == "?" and worktree == "?",
                "staged": "" if staged in (" ", "?") else staged,
                "unstaged": "" if worktree in (" ", "?") else worktree,
                # Only a file that is still there can be previewed.
                "exists": target.is_file(),
            }
        )
    return out


def _line_counts(repo: Path, baseline: str) -> dict[str, tuple[int, int, bool]]:
    """Per-path ``(added, removed, binary)`` for everything that differs from
    ``baseline``, in one call for the whole repository.

    ``--numstat`` reports index and worktree together, which is what the pane
    describes, and answers ``-`` for a binary file instead of counts.
    """
    fields = _git(repo, "diff", baseline, "--numstat", "-z", "--no-color").split("\0")
    counts: dict[str, tuple[int, int, bool]] = {}
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        added, _, path = entry.partition("\t")
        removed, _, path = path.partition("\t")
        if not path:  # a rename reports the old path first, then the new one
            index += 1
            path = fields[index] if index < len(fields) else ""
            index += 1
        if not path:
            continue
        if not added.isdigit() or not removed.isdigit():
            # ``-`` for a binary file; anything else is not a count to report.
            counts[path] = (0, 0, added == "-")
            continue
        counts[path] = (int(added), int(removed), False)
    return counts


def _untracked_text(path: Path, limit: int) -> tuple[int, bool, str | None]:
    """``(added, binary, preview)`` for a file git does not track yet.

    The whole file is new, so it is read in chunks: the line count stays exact
    while an enormous file is never held in memory. Past ``limit`` characters
    the preview is dropped — it would be the whole file — and the counts remain.
    """
    added = 0
    parts: list[str] = []
    kept = 0
    overflow = False
    last = b""
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 16):
            if b"\0" in chunk:
                return 0, True, None
            added += chunk.count(b"\n")
            last = chunk[-1:]
            if overflow:
                continue
            piece = chunk.decode("utf-8", "replace")
            kept += len(piece)
            if kept > limit:
                overflow = True
                parts.clear()
            else:
                parts.append(piece)
    if last and last != b"\n":
        added += 1  # a final line without a newline is still a line
    if overflow:
        return added, False, None
    text = "".join(f"+{line}\n" for line in "".join(parts).splitlines())
    return added, False, text


def _describe(
    repo: Path, entry: dict, baseline: str, counts: dict, diffs: bool, allow_diff: bool = True
) -> dict:
    """Line counts against ``HEAD`` plus the longest usable preview of a file.

    The counts always describe the file; the preview is dropped when it would be
    unusable — the file is binary, it is gone, or its diff is past the size cap —
    and the reason travels with the entry, so the reader is told why instead of
    simply seeing nothing.

    ``diffs=False`` reports the same counts without producing any preview: a
    caller that only shows the summary must not pay for one ``git diff`` per
    file. What it does keep is ``diff_note`` for the reasons that need no diff
    to know (binary, deleted) — the row that has nothing to open must still be
    able to say why. Untracked files are still read: that is how they are
    counted.
    """
    if entry["untracked"]:
        added, binary, text = _untracked_text(Path(entry["absolute_path"]), MAX_DIFF_CHARS)
        if not diffs:
            return {
                "added": added,
                "removed": 0,
                "binary": binary,
                "diff": None,
                "diff_note": "二进制文件" if binary else None,
            }
        return {
            "added": added,
            "removed": 0,
            "binary": binary,
            "diff": text,
            "diff_note": "二进制文件" if binary else None,
        }
    added, removed, binary = counts.get(entry["path"], (0, 0, False))
    out = {"added": added, "removed": removed, "binary": binary, "diff": None, "diff_note": None}
    if binary:
        out["diff_note"] = "二进制文件"
    elif not entry["exists"]:
        out["diff_note"] = "文件已删除"
    if not diffs:
        return out
    if binary or not entry["exists"]:
        return out  # nothing to generate: the note above already says why
    if not allow_diff:
        out["diff_note"] = "改动较多，未生成逐文件预览"
    else:
        # A rename is diffed against both of its paths: restricted to the new
        # one alone, git would report the whole file as added.
        paths = [entry["path"], *([entry["origin"]] if entry.get("origin") else [])]
        text = _git(repo, "diff", baseline, "--no-color", "--unified=3", "--", *paths)
        if len(text) > MAX_DIFF_CHARS:
            out["diff_note"] = "改动过大，未生成预览"
        else:
            out["diff"] = text
    return out


def session_repos(sess: Session) -> list[Path]:
    """The repositories this session's roots sit in, in root order."""
    repos: list[Path] = []
    for root in sess.agent.path_context().roots:
        top = repo_root(root)
        if top is not None and top not in repos:
            repos.append(top)
    return repos


def file_diff(sess: Session, repo: str, path: str) -> dict:
    """The diff of one changed file in one of this session's repositories.

    Both arguments are checked against what the session can actually see — the
    repository has to be one of its roots' own, and the path has to be in the
    repository's current status — so a request can describe a file the pane is
    already showing, never an arbitrary file on the host.
    """
    wanted = Path(repo).resolve() if repo else None
    match = next((r for r in session_repos(sess) if r.resolve() == wanted), None)
    if match is None:
        raise GitError("仓库不属于该会话")
    if not path:
        raise GitError("需要一个文件路径")
    entry = next((e for e in _status(match) if e["path"] == path), None)
    if entry is None:
        raise GitError("该文件没有未提交改动")
    baseline = _baseline(match)
    try:
        counts = _line_counts(match, baseline)
    except (GitError, OSError, subprocess.SubprocessError):
        counts = {}
    described = _describe(match, entry, baseline, counts, diffs=True)
    return {"diff": described["diff"], "diff_note": described["diff_note"]}


def session_changes(sess: Session, *, diffs: bool = True) -> dict:
    """Uncommitted changes in the repositories this session's roots live in.

    ``diffs=False`` reports the same files and counts without producing any
    per-file diff: a caller that only shows the section's summary must not pay
    for one ``git diff`` per changed file.
    """
    empty = {"repos": [], "files": [], "added": 0, "removed": 0, "truncated": False}
    if shutil.which("git") is None:
        return {**empty, "error": "未找到 git 命令"}
    repos = session_repos(sess)

    files: list[dict] = []
    truncated = False
    error: str | None = None
    diff_budget = MAX_DIFF_FILES
    for repo in repos:
        try:
            entries = _status(repo)
        except (GitError, OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
            continue
        baseline = _baseline(repo)
        try:
            counts = _line_counts(repo, baseline)
        except (GitError, OSError, subprocess.SubprocessError):
            counts = {}  # nothing to compare against: report the files uncounted
        try:
            for entry in entries:
                if len(files) >= MAX_FILES:
                    truncated = True
                    break
                allow = diffs and diff_budget > 0
                if allow:
                    diff_budget -= 1
                described = _describe(repo, entry, baseline, counts, diffs, allow)
                files.append({**entry, "repo": str(repo), **described})
        except (GitError, OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
    return {
        "repos": [{"root": str(r), "name": r.name} for r in repos],
        "files": files,
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
        "truncated": truncated,
        "error": error,
    }
