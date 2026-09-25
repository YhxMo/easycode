"""In-process MCP server used by tests (stdio transport).

Run: python mcp_demo_server.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("demo")


@server.list_tools()
async def list_tools():
    return [
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
    ]


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
    raise ValueError(f"unknown tool: {name}")


async def main():
    # Optional handshake with the parent: tests read this file to assert the
    # child process was reaped, including from a sync TestClient test.
    pidfile = os.environ.get("MCP_DEMO_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())