"""Bounded per-session records of the file tools a turn used.

The right-hand pane reads a conversation's context cards and diffs from these
records rather than from the message history, so they survive both a page
refresh and the context compaction that eventually drops the tool results
themselves. Each record keeps the tool-result shape the pane already reads —
``{"id", "name", "args", "result"}`` with ``result`` a JSON string — trimmed to
what a card shows: an excerpt, the search rows, the diff. A session that read
hundreds of files must not turn its own file into a copy of every result.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

#: Tools whose results the pane can show. Anything else (shell, subtask,
#: skills, MCP) leaves nothing a file card could be built from.
FILE_TOOLS = {"read_file", "write_file", "edit_file", "grep", "glob"}

#: Kept per record: a read's excerpt, one diff, and how many search rows. The
#: row caps match the tools' own defaults, so a card's counts stay truthful for
#: everything except an explicitly larger request.
MAX_EXCERPT = 1200
MAX_DIFF = 6000
MAX_HIT_TEXT = 160
MAX_ROWS = {"grep": 200, "glob": 500}

_TRUNCATED = "\n\n（会话记录只保留前 {n} 个字符）"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _TRUNCATED.format(n=limit)


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def build_record(
    call_id: str,
    name: str,
    args: object,
    result: str,
    resolve: Callable[[str], Path] | None = None,
) -> dict | None:
    """One trimmed record, or ``None`` when this call has nothing to keep.

    ``resolve`` turns a display path into the canonical file, for the tools
    whose own result names it relatively (``write_file``/``edit_file``): the
    pane needs one target per record, and the same relative name can exist under
    several roots.
    """
    if name not in FILE_TOOLS:
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    trimmed = _trim(name, data, resolve)
    if trimmed is None:
        return None
    return {
        "id": str(call_id),
        "name": name,
        "args": args if isinstance(args, dict) else {},
        "result": json.dumps(trimmed, ensure_ascii=False),
    }


def _trim(
    name: str, data: dict, resolve: Callable[[str], Path] | None
) -> dict | None:
    if str(data.get("status") or "") == "error":
        # A failed call is worth keeping as an error card, never as a change.
        out = {"status": "error", "message": str(data.get("message") or "工具执行失败")}
        if "in_allowed" in data:
            out["in_allowed"] = data["in_allowed"]
        return out

    if name == "read_file":
        path = str(data.get("path") or "")
        if not path:
            return None
        out = {
            "status": "ok",
            "path": path,
            "total_lines": _int(data.get("total_lines")),
            "lines": _int(data.get("lines")),
            "content": _cut(str(data.get("content") or ""), MAX_EXCERPT),
        }
        if data.get("absolute_path"):
            out["absolute_path"] = str(data["absolute_path"])
        return _with_target(out, resolve)

    if name in ("write_file", "edit_file"):
        diff = str(data.get("diff") or "")
        if not diff:
            # Nothing changed (or the call never got that far): no card.
            return None
        out = {
            "status": "ok",
            "path": str(data.get("path") or ""),
            "diff": _cut(diff, MAX_DIFF),
        }
        if data.get("dry_run") is True:
            out["dry_run"] = True
        if isinstance(data.get("bytes"), int):
            out["bytes"] = data["bytes"]
        return _with_target(out, resolve)

    rows = data.get("matches")
    if not isinstance(rows, list):
        return None
    limit = MAX_ROWS.get(name, 0)
    kept = [_row(name, row) for row in rows[:limit]]
    out = {"status": "ok", "matches": [row for row in kept if row is not None]}
    if data.get("truncated") or len(rows) > limit:
        out["truncated"] = True
    if name == "glob" and isinstance(data.get("count"), int):
        out["count"] = data["count"]
    return out


def _row(name: str, row: object) -> dict | None:
    """One search/glob row, keeping only the fields a card or a link needs."""
    if name == "glob":
        if isinstance(row, str):
            return {"path": row} if row else None
        if isinstance(row, dict) and row.get("path"):
            out = {"path": str(row["path"])}
            if row.get("root"):
                out["root"] = str(row["root"])
            return out
        return None
    if not isinstance(row, dict) or not row.get("file"):
        return None
    out = {
        "file": str(row["file"]),
        "line": _int(row.get("line")),
        "text": _cut(str(row.get("text") or ""), MAX_HIT_TEXT),
    }
    if row.get("root"):
        out["root"] = str(row["root"])
    return out


def _with_target(out: dict, resolve: Callable[[str], Path] | None) -> dict:
    """Name the canonical file when the result did not already carry it."""
    if out.get("absolute_path") or resolve is None or not out.get("path"):
        return out
    try:
        target = resolve(str(out["path"]))
    except (OSError, ValueError):
        return out
    out["absolute_path"] = str(target)
    return out


def _args(raw: object) -> dict:
    """Tool arguments as stored in the history are a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def records_from_messages(
    messages: list[dict], resolve: Callable[[str], Path] | None = None
) -> list[dict]:
    """Rebuild records from a conversation that predates them (best effort).

    Only what the surviving history still carries can come back: tool results
    the context compaction already dropped are gone for good.
    """
    results = {
        str(message["tool_call_id"]): message.get("content")
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    out: list[dict] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            result = results.get(str(call.get("id") or ""))
            if not isinstance(result, str):
                continue
            record = build_record(
                str(call.get("id") or ""),
                str(function.get("name") or ""),
                _args(function.get("arguments")),
                result,
                resolve,
            )
            if record is not None:
                out.append(record)
    return out
