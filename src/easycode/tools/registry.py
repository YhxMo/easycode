"""Tool registry: pydantic-model-driven tools with automatic JSON schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from easycode.workspace import PathContext, ToolGrant

ParamsT = TypeVar("ParamsT", bound=BaseModel)

MAX_DEFAULT_CHARS = 8000


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        params_model: type[BaseModel],
        handler: Callable[[Any], Any],
    ) -> None:
        self.name = name
        self.description = description
        self.params_model = params_model
        self.handler = handler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_model.model_json_schema(),
            },
        }

    def run(
        self,
        arguments: dict[str, Any],
        root: Path,
        ctx: "PathContext | None" = None,
        force_allowed: bool = False,
        grant: "ToolGrant | None" = None,
    ) -> Any:
        params = self.params_model.model_validate(arguments)
        return self.handler(params, root=root, ctx=ctx, force_allowed=force_allowed, grant=grant)


class ToolRegistry:
    def __init__(self, max_result_chars: int = MAX_DEFAULT_CHARS) -> None:
        self._tools: dict[str, Tool] = {}
        self.max_result_chars = max_result_chars

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def schemas(self, enabled: set[str] | None = None) -> list[dict[str, Any]]:
        names = enabled or set(self._tools)
        return [t.schema() for name, t in self._tools.items() if name in names]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        root: Path,
        ctx: "PathContext | None" = None,
        force_allowed: bool = False,
        grant: "ToolGrant | None" = None,
    ) -> str:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        try:
            result = self._tools[name].run(
                arguments, root=root, ctx=ctx, force_allowed=force_allowed, grant=grant
            )
            return self._serialize(name, result)
        except Exception as exc:  # noqa: BLE001 - report tool failures as JSON
            return json.dumps(
                {"status": "error", "message": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

    def _serialize(self, name: str, result: Any) -> str:
        if isinstance(result, str):
            text = result
        else:
            try:
                text = json.dumps(result, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(result)
        if len(text) <= self.max_result_chars:
            return text
        # JSON-aware truncation so the model always receives valid JSON: shrink
        # the largest string payload (deep search, covers fields like ``diff``
        # and nested ``grep`` matches) instead of slicing the serialized string
        # mid-JSON. Only non-JSON output falls back to a char slice.
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text[: self.max_result_chars] + f"\n...[truncated {name}]"
        if isinstance(data, dict):
            data = self._truncate_dumpable(data, name)
            out = json.dumps(data, ensure_ascii=False, default=str)
            if len(out) > self.max_result_chars:
                return self._minimal_json(data, name)
            return out
        return text[: self.max_result_chars] + f"\n...[truncated {name}]"

    @staticmethod
    def _set_at(node: Any, path: tuple, value: str) -> None:
        cur = node
        for part in path[:-1]:
            cur = cur[part]
        cur[path[-1]] = value

    def _truncate_dumpable(self, data: dict[str, Any], name: str) -> dict[str, Any]:
        """Shrink the largest string payload(s) until the result fits the budget.

        Iteratively re-serializes (JSON escaping inflates multi-line content,
        e.g. ``diff``) and converges on the budget instead of guessing a char
        target. A field that is itself bigger than the budget collapses to just
        the truncation marker so the JSON stays valid.
        """
        data.setdefault("truncated", True)
        marker = f"\n...[truncated {name}]"
        for _ in range(8):
            if len(json.dumps(data, ensure_ascii=False, default=str)) <= self.max_result_chars:
                return data
            self._shrink_all(data, marker)
            if len(json.dumps(data, ensure_ascii=False, default=str)) <= self.max_result_chars:
                return data
            self._shrink_longest_list(data)
        return data

    def _shrink_longest_list(self, data: dict[str, Any]) -> None:
        """Trim the heaviest list to the longest head that fits the budget.

        Keeps the first items (e.g. the first ``grep`` matches) instead of
        dropping the whole payload when structural overhead dominates.
        """
        best: tuple[int, tuple, list[Any]] | None = None

        def walk(node: Any, path: tuple) -> None:
            nonlocal best
            if isinstance(node, list) and node:
                size = len(json.dumps(node, ensure_ascii=False))
                if best is None or size > best[0]:
                    best = (size, path, node)
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, path + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, path + (i,))

        walk(data, ())
        if best is None:
            return
        _, path, lst = best
        lo, hi = 1, len(lst)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            tail = lst[mid:]
            del lst[mid:]
            fits = len(json.dumps(data, ensure_ascii=False, default=str)) <= self.max_result_chars
            lst.extend(tail)
            if fits:
                lo = mid
            else:
                hi = mid - 1
        if lo < len(lst):
            del lst[lo:]
            data.setdefault("truncated", True)

    def _shrink_all(self, data: dict[str, Any], marker: str) -> None:
        """Trim the overshoot off the large string leaves in one proportional pass.

        Small structural fields (``status``, ``path``, …) are left untouched;
        only payload blobs (escaped size > 96) are reduced, each by a share of
        the overshoot proportional to its escaped size, so many-matching
        payloads (e.g. ``grep``) converge in a constant number of passes.
        """
        leaves: list[tuple[tuple, str]] = []

        def walk(node: Any, path: tuple) -> None:
            if isinstance(node, str):
                if node:
                    leaves.append((path, node))
            elif isinstance(node, dict):
                for k, v in node.items():
                    walk(v, path + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, path + (i,))

        walk(data, ())
        if not leaves:
            return
        values = [v for _, v in leaves]
        bases = [v[: -len(marker)] if v.endswith(marker) else v for v in values]
        escs = [len(json.dumps(b, ensure_ascii=False)) for b in bases]
        idx = [i for i, e in enumerate(escs) if e > 96]
        if not idx:
            return
        big_total = sum(escs[i] for i in idx)
        out = json.dumps(data, ensure_ascii=False, default=str)
        overshoot = len(out) - self.max_result_chars
        reduce_total = overshoot + len(marker) * len(idx) + 4
        if reduce_total <= 0:
            return
        ratio = min(1.0 - 1e-3, reduce_total / big_total)
        for i in idx:
            path, _ = leaves[i]
            base = bases[i]
            target_esc = max(32, int(escs[i] * (1 - ratio)))
            keep = self._scan_len(base, target_esc)
            new_value = base[:keep] + marker
            if new_value != values[i]:
                self._set_at(data, path, new_value)

    @staticmethod
    def _scan_len(raw: str, target_esc: int) -> int:
        """Longest raw prefix of ``raw`` whose escaped form fits ``target_esc``."""
        hi = len(raw)
        if len(json.dumps(raw, ensure_ascii=False)) <= target_esc:
            return hi
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(raw[:mid], ensure_ascii=False)) <= target_esc:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _minimal_json(self, data: dict[str, Any], name: str) -> str:
        """Last-resort valid JSON when nothing above can fit the budget."""
        out: dict[str, Any] = {"status": data.get("status", "ok"), "truncated": True}
        if data.get("path"):
            out["path"] = data["path"]
        out["message"] = f"result too large to include (tool: {name})"
        result = json.dumps(out, ensure_ascii=False, default=str)
        if len(result) > self.max_result_chars:
            return json.dumps(
                {"status": "error", "message": f"result too large (tool: {name})"},
                ensure_ascii=False,
            )
        return result


def tool(name: str, description: str, params_model: type[BaseModel]):
    """Decorator: wrap a handler into a :class:`Tool`.

    The handler receives the validated params model as first argument and a
    keyword-only ``root: Path`` (workspace root).

    Example:
        class ReadArgs(BaseModel):
            path: str = Field(description="path relative to workspace root")

        @tool("read_file", "Read a file", ReadArgs)
        def _read_file(args: ReadArgs, *, root: Path) -> str:
            ...
    """

    def decorator(fn: Callable[[Any], Any]) -> Tool:
        return Tool(name, description, params_model, fn)

    return decorator