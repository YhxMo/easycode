"""Tool registry: pydantic-model-driven tools with automatic JSON schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from easycode.workspace import PathContext

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
    ) -> Any:
        params = self.params_model.model_validate(arguments)
        return self.handler(params, root=root, ctx=ctx, force_allowed=force_allowed)


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
    ) -> str:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        try:
            result = self._tools[name].run(arguments, root=root, ctx=ctx, force_allowed=force_allowed)
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
        if len(text) > self.max_result_chars:
            text = text[: self.max_result_chars] + f"\n...[truncated {name}]"
        return text


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