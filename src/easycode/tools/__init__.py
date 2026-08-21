"""Tool registration: build a default ToolRegistry with the 5 core tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from easycode.tools.files import (
    EditFileArgs,
    GlobArgs,
    GrepArgs,
    ReadFileArgs,
    WriteFileArgs,
    edit_file,
    glob,
    grep,
    read_file,
    write_file,
)
from easycode.tools.registry import ToolRegistry, tool
from easycode.tools.shell import ExecuteShellArgs, execute_shell
from easycode.workspace import PathContext

ALL_TOOL_NAMES = ("execute_shell", "read_file", "write_file", "edit_file", "grep", "glob")


@tool("execute_shell", "Run a shell command in the workspace root and capture output.", ExecuteShellArgs)
def _execute_shell(args: ExecuteShellArgs, *, root: Path, ctx: PathContext | None = None, force_allowed: bool = False) -> str:
    return execute_shell(args, root=root, ctx=ctx, force_allowed=force_allowed)


@tool("read_file", "Read a text file (relative to a workspace root).", ReadFileArgs)
def _read_file(args: ReadFileArgs, *, root: Path, ctx: PathContext | None = None, force_allowed: bool = False) -> str:
    return read_file(args, root=root, ctx=ctx, force_allowed=force_allowed)


@tool("write_file", "Create or overwrite a file (relative to a workspace root).", WriteFileArgs)
def _write_file(args: WriteFileArgs, *, root: Path, ctx: PathContext | None = None, force_allowed: bool = False) -> str:
    return write_file(args, root=root, ctx=ctx, force_allowed=force_allowed)


@tool(
    "edit_file",
    "Edit a file with an exact string replacement (relative to a workspace root). "
    "old_string must appear exactly once; use dry_run to preview the unified diff first.",
    EditFileArgs,
)
def _edit_file(args: EditFileArgs, *, root: Path, ctx: PathContext | None = None, force_allowed: bool = False) -> str:
    return edit_file(args, root=root, ctx=ctx, force_allowed=force_allowed)


@tool("grep", "Regex-search file contents under the workspace roots.", GrepArgs)
def _grep(args: GrepArgs, *, root: Path, ctx: PathContext | None = None, force_allowed: bool = False) -> str:
    return grep(args, root=root, ctx=ctx, force_allowed=force_allowed)


@tool("glob", "List files matching a glob pattern under the workspace roots.", GlobArgs)
def _glob(args: GlobArgs, *, root: Path, ctx: PathContext | None = None, force_allowed: bool = False) -> str:
    return glob(args, root=root, ctx=ctx, force_allowed=force_allowed)


def build_registry(max_result_chars: int) -> ToolRegistry:
    reg = ToolRegistry(max_result_chars=max_result_chars)
    for t in (_execute_shell, _read_file, _write_file, _edit_file, _grep, _glob):
        reg.register(t)
    return reg
