"""Approval policy: which tool calls need a human nod before running.

Three permission modes (config global / CLI flag / Web session):

- ``ask`` (default): workspace/temp/~/.easycode actions run automatically;
  editing external files and network-ish shell commands ask the user first.
- ``auto-review``: everything runs; changes are summarized afterwards.
- ``allow-all``: everything runs; no tracking at all.
"""

from __future__ import annotations

import re
from typing import Any

from easycode.models.base import ToolCall
from easycode.policy import (
    PERMISSIONS,
    PERM_ALLOW_ALL,
    PERM_ASK,
    PERM_AUTO_REVIEW,
    permission_parse,
)
from easycode.workspace import PathContext

NETWORK_HINTS = (
    "curl",
    "wget",
    "git clone",
    "git pull",
    "git push",
    "pip install",
    "npm install",
    "npx ",
    "cargo install",
    "docker pull",
    "rsync",
    "ssh ",
    "scp ",
    "http://",
    "https://",
    "ftp://",
)

NETWORK_HINT_RE = re.compile("|".join(re.escape(h) for h in NETWORK_HINTS), re.IGNORECASE)

FILE_EDIT_TOOLS = {"write_file", "edit_file"}

TRUNC_LIMIT = 80


def approval_key(tc: ToolCall) -> str:
    """Per-session 'always allow' key: tool + first/relevant argument.

    Shared by CLI and Web so 'always allow' semantics are identical. File
    tools match by their parent-directory scope so the Web UI can display a
    ``/dir/*`` pattern that mirrors opencode's permission dialog.
    """
    if tc.name in FILE_EDIT_TOOLS:
        return f"{tc.name}:{approval_scope(tc)}"
    if tc.name == "execute_shell":
        return f"{tc.name}:{str(tc.arguments.get('command', ''))[:TRUNC_LIMIT]}"
    return tc.name


def approval_scope(tc: ToolCall) -> str:
    """Human-facing boundary that an approval grants when 'always allowed'.

    - File tools: the parent directory with a ``/*`` glob (this is the scope
      the user is granting, matching the ``/dir/*`` pattern in the UI).
    - Shell: the command text (truncated).
    - Everything else: the tool name.
    """
    if tc.name in FILE_EDIT_TOOLS:
        path = str(tc.arguments.get("path", "")).rstrip("/")
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        return f"{parent}/*" if parent else "/*"
    if tc.name == "execute_shell":
        return str(tc.arguments.get("command", ""))[:TRUNC_LIMIT]
    return tc.name


def needs_approval(tc: ToolCall, ctx: PathContext, mode: str) -> bool:
    """Decision layer: True when the tool call must be confirmed first.

    Auto-review and allow-all never block; ask blocks external-file edits and
    network-looking shell commands.
    """
    if mode == PERM_ALLOW_ALL:
        return False
    if tc.name in FILE_EDIT_TOOLS:
        path = tc.arguments.get("path", "")
        if not path:
            return False
        return not ctx.in_allowed(ctx.resolve(str(path)))
    if tc.name == "execute_shell":
        return tc.arguments.get("sandbox_permissions") == "require_escalated" or _looks_like_network(
            str(tc.arguments.get("command", ""))
        )
    return False


def approval_reason(tc: ToolCall, ctx: PathContext) -> str:
    """Categorical, human-readable explanation for the approval prompt.

    Kept free of the concrete command/path (the UI shows that separately as
    the ``scope`` line), so the prompt reads like opencode's permission
    dialog category line.
    """
    if tc.name in FILE_EDIT_TOOLS:
        path = tc.arguments.get("path", "")
        resolved = ctx.resolve(str(path))
        for protected in ctx.protected_paths():
            if resolved.is_relative_to(protected):
                return "修改受保护目录 (.git/.easycode)"
        return "访问项目目录之外的文件"
    if tc.name == "execute_shell":
        return "执行疑似联网命令"
    return f"工具需要批准: {tc.name}"


def _looks_like_network(command: str) -> bool:
    return bool(NETWORK_HINT_RE.search(command))


def looks_like_network(command: str) -> bool:
    """Compatibility preflight; Seatbelt is the actual network boundary."""
    return _looks_like_network(command)


def needs_review(tc: ToolCall) -> bool:
    """Auto-review mode: worth surfacing afterwards (file changes)."""
    if tc.name in FILE_EDIT_TOOLS:
        return True
    if tc.name == "execute_shell":
        return True
    return False
