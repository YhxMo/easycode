"""Execution policy presets aligned with Codex sandbox and approval controls."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from typing import Any

SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"
SANDBOX_DANGER_FULL_ACCESS = "danger-full-access"

APPROVAL_ON_REQUEST = "on-request"
APPROVAL_NEVER = "never"

REVIEWER_USER = "user"
REVIEWER_AUTO = "auto-review"

PERM_ASK = "ask"
PERM_AUTO_REVIEW = "auto-review"
PERM_ALLOW_ALL = "allow-all"
PERMISSIONS = (PERM_ASK, PERM_AUTO_REVIEW, PERM_ALLOW_ALL)
RULE_ALLOW = "allow"
RULE_ASK = "ask"
RULE_DENY = "deny"
RULE_ACTIONS = (RULE_ALLOW, RULE_ASK, RULE_DENY)


def permission_rule_action(
    rules: dict[str, Any] | None,
    tool_name: str,
    argument: str = "",
) -> str | None:
    """Resolve an optional per-tool rule using last-match-wins semantics.

    ``rules`` accepts either a flat action (``{"mcp__*": "ask"}``) or a
    pattern map (``{"execute_shell": {"*": "ask", "git status*": "allow"}}``).
    The rule layer is intentionally independent from the three legacy presets:
    it can force a prompt or denial, while the existing sandbox still decides
    the actual filesystem and network boundary.
    """
    if not rules:
        return None
    action: str | None = None
    for tool_pattern, raw in rules.items():
        if not fnmatch.fnmatchcase(tool_name, str(tool_pattern)):
            continue
        if isinstance(raw, str):
            if raw in RULE_ACTIONS:
                action = raw
            continue
        if isinstance(raw, dict):
            for pattern, value in raw.items():
                if (
                    isinstance(value, str)
                    and value in RULE_ACTIONS
                    and fnmatch.fnmatchcase(argument, str(pattern))
                ):
                    action = value
    return action


@dataclass(frozen=True)
class ExecutionPolicy:
    sandbox_mode: str
    approval_policy: str
    approvals_reviewer: str

    @classmethod
    def from_preset(cls, value: str) -> "ExecutionPolicy":
        preset = permission_parse(value)
        if preset == PERM_ALLOW_ALL:
            return cls(SANDBOX_DANGER_FULL_ACCESS, APPROVAL_NEVER, REVIEWER_USER)
        reviewer = REVIEWER_AUTO if preset == PERM_AUTO_REVIEW else REVIEWER_USER
        return cls(SANDBOX_WORKSPACE_WRITE, APPROVAL_ON_REQUEST, reviewer)


def permission_parse(value: str) -> str:
    v = (value or "").strip().lower()
    v = {"auto": PERM_AUTO_REVIEW, "allow_all": PERM_ALLOW_ALL, "allow": PERM_ALLOW_ALL}.get(v, v)
    if v not in PERMISSIONS:
        raise ValueError(f"invalid permission mode: {value} (use {', '.join(PERMISSIONS)})")
    return v


def cap_permission(parent: str, requested: str | None) -> str:
    """A delegated agent may narrow, but never widen, its parent's preset."""
    if not requested:
        return permission_parse(parent)
    ranks = {PERM_ASK: 0, PERM_AUTO_REVIEW: 1, PERM_ALLOW_ALL: 2}
    parent_mode = permission_parse(parent)
    child_mode = permission_parse(requested)
    return child_mode if ranks[child_mode] <= ranks[parent_mode] else parent_mode
