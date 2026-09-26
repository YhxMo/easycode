"""Live Git state of the directories a session works in.

The pane shows two different things side by side: what this conversation's tools
did (recorded per session, see ``artifacts``) and what the working tree looks
like right now. The second one is the repositories' own state — it includes
changes that predate the conversation — so it is reported as its own fact and
never merged into the session's record.

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
        if staged in ("R", "C"):
            index += 1  # the path it came from is reported on its own
        target = repo / path
        out.append(
            {
                "path": path,
                "absolute_path": str(target),
                "untracked": staged == "?" and worktree == "?",
                "staged": "" if staged in (" ", "?") else staged,
                "unstaged": "" if worktree in (" ", "?") else worktree,
                # Only a file that is still there can be previewed.
                "exists": target.is_file(),
            }
        )
    return out


def session_changes(sess: Session) -> dict:
    """Uncommitted changes in the repositories this session's roots live in."""
    if shutil.which("git") is None:
        return {"repos": [], "files": [], "truncated": False, "error": "未找到 git 命令"}
    roots = sess.agent.path_context().roots
    repos: list[Path] = []
    for root in roots:
        top = repo_root(root)
        if top is not None and top not in repos:
            repos.append(top)

    files: list[dict] = []
    truncated = False
    error: str | None = None
    for repo in repos:
        try:
            entries = _status(repo)
        except (GitError, OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
            continue
        for entry in entries:
            if len(files) >= MAX_FILES:
                truncated = True
                break
            files.append({**entry, "repo": str(repo)})
    return {
        "repos": [{"root": str(r), "name": r.name} for r in repos],
        "files": files,
        "truncated": truncated,
        "error": error,
    }
