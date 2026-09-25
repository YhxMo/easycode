"""Tool registration: build a default ToolRegistry with the six core tools."""

from __future__ import annotations

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
from easycode.tools.registry import Tool, ToolRegistry
from easycode.tools.shell import ExecuteShellArgs, execute_shell


def build_registry(max_result_chars: int) -> ToolRegistry:
    registry = ToolRegistry(max_result_chars=max_result_chars)
    for name, description, params, handler in (
        (
            "execute_shell",
            (
                "Run a shell command in the workspace and capture output. "
                "To write outside the workspace, pass existing absolute directories in "
                "writable_roots and provide a justification."
            ),
            ExecuteShellArgs,
            execute_shell,
        ),
        ("read_file", "Read a text file relative to a workspace root.", ReadFileArgs, read_file),
        ("write_file", "Create or overwrite a file.", WriteFileArgs, write_file),
        (
            "edit_file",
            (
                "Replace an exact string in a file. old_string must appear exactly once; "
                "use dry_run to preview the diff."
            ),
            EditFileArgs,
            edit_file,
        ),
        ("grep", "Regex-search file contents under the workspace roots.", GrepArgs, grep),
        ("glob", "List files matching a glob pattern under the workspace roots.", GlobArgs, glob),
    ):
        registry.register(Tool(name, description, params, handler))
    return registry
