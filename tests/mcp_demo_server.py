"""In-process MCP server used by tests (stdio transport).

Run: python mcp_demo_server.py

Optional environment, each exercising one client behaviour:

- ``MCP_DEMO_PIDFILE``: write the child pid there (tests assert it was reaped).
- ``MCP_DEMO_PAGE_SIZE``: answer ``tools/list`` one page at a time.
- ``MCP_DEMO_STARTUP_SLEEP``: stall before serving, so ``initialize`` never
  answers within a client's startup timeout.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("demo")

TOOLS = [
    types.Tool(
        name="add",
        description="add two integers",
        inputSchema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    ),
    types.Tool(
        name="greet",
        description="greet someone",
        inputSchema={"type": "object", "properties": {"name": {"type": "string"}}},
    ),
    types.Tool(
        name="boom",
        description="always fails",
        inputSchema={"type": "object"},
    ),
    types.Tool(
        name="slow",
        description="sleep for the requested number of seconds",
        inputSchema={"type": "object", "properties": {"seconds": {"type": "number"}}},
    ),
    types.Tool(
        name="readonly",
        description="a tool that declares no side effects",
        inputSchema={"type": "object"},
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
]


@server.list_tools()
async def list_tools(req: types.ListToolsRequest) -> types.ListToolsResult:
    size = os.environ.get("MCP_DEMO_PAGE_SIZE")
    if not size:
        return types.ListToolsResult(tools=TOOLS)
    start = int((req.params.cursor if req.params else None) or 0)
    page = TOOLS[start : start + int(size)]
    served = start + len(page)
    return types.ListToolsResult(
        tools=page, nextCursor=str(served) if served < len(TOOLS) else None
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent] | types.CallToolResult:
    if name == "add":
        total = int(arguments["a"]) + int(arguments["b"])
        return [types.TextContent(type="text", text=str(total))]
    if name == "greet":
        return [types.TextContent(type="text", text=f"hello, {arguments.get('name', 'world')}")]
    if name == "boom":
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text="exploded")],
        )
    if name == "slow":
        await asyncio.sleep(float(arguments.get("seconds", 0)))
        return [types.TextContent(type="text", text="slept")]
    if name == "readonly":
        return [types.TextContent(type="text", text="nothing changed")]
    raise ValueError(f"unknown tool: {name}")


async def main():
    # Optional handshake with the parent: tests read this file to assert the
    # child process was reaped, including from a sync TestClient test.
    pidfile = os.environ.get("MCP_DEMO_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
    if startup_sleep := os.environ.get("MCP_DEMO_STARTUP_SLEEP"):
        await asyncio.sleep(float(startup_sleep))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
