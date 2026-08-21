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
from easycode.workspace import PathContext

PERM_ASK = "ask"
PERM_AUTO_REVIEW = "auto-review"
PERM_ALLOW_ALL = "allow-all"
PERMISSIONS = (PERM_ASK, PERM_AUTO_REVIEW, PERM_ALLOW_ALL)

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


def needs_approval(tc: ToolCall, ctx: PathContext, mode: str) -> bool:
    """Decision layer: True when the tool call must be confirmed first.

    Auto-review and allow-all never block; ask blocks external-file edits and
    network-looking shell commands.
    """
    if mode == PERM_ALLOW_ALL or mode == PERM_AUTO_REVIEW:
        return False
    if tc.name in FILE_EDIT_TOOLS:
        path = tc.arguments.get("path", "")
        if not path:
            return False
        return not ctx.in_allowed(ctx.resolve(str(path)))
    if tc.name == "execute_shell":
        return _looks_like_network(str(tc.arguments.get("command", "")))
    return False


def approval_reason(tc: ToolCall, ctx: PathContext) -> str:
    """Human-readable explanation for the approval prompt."""
    if tc.name in FILE_EDIT_TOOLS:
        path = tc.arguments.get("path", "")
        return f"编辑工作区外的文件: {path}（{ctx.classify(ctx.resolve(str(path)))}）"
    if tc.name == "execute_shell":
        return f"疑似联网命令: {str(tc.arguments.get('command', ''))[:120]}"
    return f"工具需要批准: {tc.name}"


def _looks_like_network(command: str) -> bool:
    return bool(NETWORK_HINT_RE.search(command))


def needs_review(tc: ToolCall) -> bool:
    """Auto-review mode: worth surfacing afterwards (file changes)."""
    if tc.name in FILE_EDIT_TOOLS:
        return True
    if tc.name == "execute_shell":
        return True
    return False


def permission_parse(value: str) -> str:
    v = (value or "").strip().lower()
    v = {"auto": PERM_AUTO_REVIEW, "allow_all": PERM_ALLOW_ALL, "allow": PERM_ALLOW_ALL}.get(v, v)
    if v not in PERMISSIONS:
        raise ValueError(f"invalid permission mode: {value} (use {', '.join(PERMISSIONS)})")
    return v