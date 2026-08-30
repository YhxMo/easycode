"""Platform integration: OS / subprocess orchestration used by the web layer.

B1 extraction: the Finder folder picker (``osascript``), the reveal call
(macOS ``open``), and the git-worktree creation pipeline (``git worktree add``
+ ``git apply`` + ``.worktreeinclude`` copy + sandboxed ``.easycode/setup.sh``)
live here so ``main.py`` routes stay thin HTTP shells.

Every public function either returns plain data or raises a
:class:`ValueError` subclass that the route layer maps to HTTP semantics
(422 for user-input/validation, :class:`WorktreeAddError` → 500 for a
platform-level failure).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from easycode.credentials import data_home
from easycode.sandbox import child_env
from easycode.workspace import PathContext

FINDER_APPLESCRIPT = 'POSIX path of (choose folder with prompt "{prompt}"{multiple})'
FINDER_MULTIPLE_SUFFIX = " with multiple selections allowed"


def finder_supported() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


#: Max length for the user-controlled ``prompt`` that is interpolated into the
#: Finder AppleScript literal. Kept short so a hostile/oversized value
#: can never stretch the script past a single sane statement.
FINDER_PROMPT_MAX_LEN = 200


def _sanitize_finder_prompt(prompt: str, max_len: int = FINDER_PROMPT_MAX_LEN) -> str:
    """Sanitize a user-controlled prompt before it is embedded in AppleScript.

    The value is spliced into an ``osascript -e`` command, so every control
    character (including newlines that could break the ``choose folder``
    statement) is stripped; only printable characters survive. The result is
    whitespace-trimmed and length-capped. Backslashes/double-quotes are escaped
    separately by the caller so the string stays a single AppleScript literal.
    """
    printable = "".join(ch for ch in prompt if ch.isprintable())
    return printable.strip()[:max_len]


def choose_folders_via_finder(multiple: bool = False, prompt: str = "选择目录") -> list[str]:
    """Open a macOS Finder folder picker via osascript; [] when cancelled/unavailable."""
    if not finder_supported():
        return []
    # Only printable, trimmed, capped text may reach the AppleScript
    # literal — never raw newlines / control characters from the request.
    safe = _sanitize_finder_prompt(prompt).replace("\\", "\\\\").replace('"', '\\"')
    script = FINDER_APPLESCRIPT.format(
        prompt=safe,
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


def reveal_in_finder(path: str) -> dict:
    """Reveal a directory in the system file browser (macOS ``open``).

    The caller resolves the project root and validates its presence before
    delegating here; this function returns the same ``{"ok", "supported", ...}``
    payload the route forwarded before extraction.
    """
    if not Path(path).is_dir():
        return {"ok": False, "supported": False, "error": f"not a directory: {path}"}
    if not (sys.platform == "darwin" and shutil.which("open")):
        return {"ok": False, "supported": False, "error": "open is only supported on macOS"}
    try:
        subprocess.run(["open", path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "supported": False, "error": "open failed"}
    return {"ok": True, "supported": True, "path": path}


class WorktreeAddError(ValueError):
    """The ``git worktree add`` step itself failed (maps to HTTP 500)."""


def create_worktree(src: Path, *, sandbox_command) -> dict:
    """Create a permanent Git worktree as its own project (平台编排段).

    Pure platform orchestration: resolves the commit sha, detaches HEAD, applies
    best-effort local changes + ``.worktreeinclude`` files, and runs
    ``.easycode/setup.sh`` through the same sandbox boundary as ``execute_shell``.

    Returns ``{"root", "slug", "notes", "warnings"}``; raises :class:`ValueError`
    for fail-closed validation errors (→ HTTP 422) or :class:`WorktreeAddError`
    (→ HTTP 500) when ``git worktree add`` itself fails. ``sandbox_command`` is
    injected so the caller (``main.py``) can keep it patchable in tests.
    """
    def run(cmd: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )

    if not shutil.which("git"):
        raise ValueError("git is not installed")
    is_repo = run(["git", "rev-parse", "--is-inside-work-tree"], src)
    if is_repo.returncode != 0 or is_repo.stdout.strip() != "true":
        raise ValueError("项目不是 Git 仓库，无法创建 worktree")

    try:
        head_sha = run(["git", "rev-parse", "HEAD"], src).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("cannot resolve HEAD commit")
    if not head_sha:
        raise ValueError("repository has no commits")

    root_home = data_home() / "worktrees"
    root_home.mkdir(parents=True, exist_ok=True)
    slug = f"{src.name}-{uuid.uuid4().hex[:5]}"
    wt = root_home / slug
    added = run(["git", "worktree", "add", "--detach", str(wt), head_sha], src)
    if added.returncode != 0 or not (wt.is_dir() and (wt / ".git").exists() or (wt / ".git").is_file()):
        raise WorktreeAddError(f"git worktree add failed: {added.stderr[:200]}")

    notes: list[str] = []
    warnings: list[str] = []

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

    # .worktreeinclude: copy gitignored files listed at the repo root. Every
    # entry is resolve-checked so it can never escape the source or
    # worktree roots: absolute paths and ``..`` from a symlink escape are
    # recorded as a warning and skipped; the batch keeps going, and a fully
    # invalid list still leaves the worktree usable.
    include_file = src / ".worktreeinclude"
    if include_file.is_file():
        src_resolved = src.resolve()
        wt_resolved = wt.resolve()
        for raw in include_file.read_text(encoding="utf-8").splitlines():
            rel = raw.strip()
            if not rel or rel.startswith("#") or rel.startswith("!"):
                continue
            raw_s = src / rel
            if not raw_s.exists() or raw_s.is_symlink():
                continue
            s = raw_s.resolve()
            d = (wt / rel).resolve()
            if not s.is_relative_to(src_resolved) or not d.is_relative_to(wt_resolved):
                warnings.append(
                    f"skipped .worktreeinclude entry outside worktree: {rel}"
                )
                continue
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

    # best-effort setup script (代码对齐 codex 的 .codex/setup.sh). Unlike the
    # git/apply work above (the server's own trusted operations), this is a
    # repository-controlled script, so it runs through the SAME seatbelt
    # boundary as ``execute_shell``. There is deliberately NO
    # unsandboxed escape hatch: if the platform cannot build a sandbox the
    # script simply is not run and the user is told why (fail-closed).
    setup = wt / ".easycode" / "setup.sh"
    if setup.is_file():
        try:
            sandboxed = sandbox_command(["bash", str(setup)], PathContext(primary=wt))
            proc = subprocess.run(
                sandboxed,
                cwd=wt,
                capture_output=True,
                text=True,
                timeout=300,
                env=child_env(),
            )
            if proc.returncode == 0:
                notes.append("ran .easycode/setup.sh (sandboxed)")
            else:
                notes.append(
                    f"setup.sh failed (sandboxed, exit {proc.returncode}): {proc.stderr[:200]}"
                )
        except RuntimeError as exc:
            warnings.append(
                f"did not run .easycode/setup.sh: {exc}（预设脚本需 macOS Seatbelt 支持）"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.append(f"setup.sh failed: {exc}")

    return {"root": str(wt.resolve()), "slug": slug, "notes": notes, "warnings": warnings}
