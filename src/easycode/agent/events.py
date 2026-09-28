"""What a running turn reports, and what a tool result says it changed."""

from __future__ import annotations

import json
from dataclasses import dataclass

from easycode.models.base import ToolCall


@dataclass
class AgentEvent:
    """Event yielded to the UI as a turn progresses."""

    kind: str  # "text" | "tool_start" | "tool_result" | "error" | "done" | "cancelled"
    content: str | None = None
    tool_call: ToolCall | None = None
    tool_result: str | None = None
    error: str | None = None
    #: Machine-readable reason for an ``error`` event, when there is one (e.g.
    #: ``tool_iteration_limit``); the text stays the human-facing statement.
    code: str | None = None


def file_change(name: str, result: str) -> dict | None:
    """Extract a real file change from a tool result, else ``None``.

    Only ``write_file``/``edit_file`` results that succeeded, were not a
    dry-run preview, and carry a path count as changes.
    """
    if name not in ("write_file", "edit_file"):
        return None
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("status") != "ok" or data.get("dry_run"):
        return None
    path = data.get("path")
    if not path:
        return None
    return {"tool": name, "path": path, "diff": data.get("diff") or ""}
