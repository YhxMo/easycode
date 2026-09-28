"""macOS Finder integration: the folder picker and reveal-in-Finder."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

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
            check=False,
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
        subprocess.run(["open", path], capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "supported": False, "error": "open failed"}
    return {"ok": True, "supported": True, "path": path}
