"""Execution policy presets aligned with Codex sandbox and approval controls."""

from __future__ import annotations

from dataclasses import dataclass

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
