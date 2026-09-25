"""MCP client: connect external MCP servers and expose their tools to agents.

Config shape (``mcp_servers`` in easycode.config.json)::

    {
      "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
      "remote":    {"url": "http://localhost:8000/mcp"}
    }

Registered tools are named ``mcp__<server>__<tool>``; the agent dispatches them
to the owning server's session.

Connections use the official ``mcp`` SDK: a sandboxed stdio child for
``command`` servers, or streamable-HTTP for ``url`` servers. The SDK's client
transports are anyio context managers that must be entered and exited in one
task, so each connection runs in its own task (see :class:`MCPConnection`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from easycode.sandbox import child_env, sandbox_command
from easycode.workspace import PathContext

log = logging.getLogger("easycode.mcp")

MCP_PREFIX = "mcp__"
#: How long one MCP request may wait for its response before it fails.
REQUEST_TIMEOUT = 30.0


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}__{tool}"


class MCPConnection:
    """One official-SDK client session, driven from a task this class owns.

    The SDK's transports are async context managers built on anyio task groups,
    which must be entered and exited in the same task. easycode connects during
    a turn but may release the connection from an unrelated later task (session
    delete, permission change, app shutdown), so the context managers run in a
    dedicated task and callers only ever touch the session it publishes.
    """

    def __init__(self, name: str, conf: dict[str, Any], ctx: PathContext | None = None) -> None:
        self.name = name
        self.conf = conf
        self.ctx = ctx or PathContext(primary=Path.cwd())
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stopping = asyncio.Event()
        self._error: BaseException | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._session is None:
            error = await self._finish()
            raise RuntimeError(f"MCP server {self.name!r} did not start: {error!r}")

    async def _run(self) -> None:
        try:
            async with (
                _open(self.conf, self.ctx) as (read, write),
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=REQUEST_TIMEOUT),
                    client_info=Implementation(name="easycode", version="0.1.0"),
                ) as session,
            ):
                self._session = session
                self._ready.set()
                await self._stopping.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reported by start()/call()
            self._error = exc
        finally:
            self._session = None
            self._ready.set()

    @property
    def session(self) -> ClientSession:
        """The connected session; raises when the server is not (or no longer) up."""
        if self._session is None:
            raise RuntimeError(f"MCP server {self.name!r} is not connected")
        return self._session

    async def _finish(self) -> BaseException | None:
        task, self._task = self._task, None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self._error

    async def close(self) -> None:
        """Unwind the connection; the child process is reaped before this returns."""
        if self._task is None:
            return
        self._stopping.set()
        error = await self._finish()
        if error is not None and not isinstance(error, (asyncio.CancelledError, GeneratorExit)):
            log.debug("MCP server %r connection ended with %r", self.name, error)


@asynccontextmanager
async def _open(conf: dict[str, Any], ctx: PathContext) -> AsyncIterator[tuple[Any, Any]]:
    """Open the configured server: a sandboxed stdio child, or a remote URL."""
    if conf.get("url"):
        async with streamablehttp_client(str(conf["url"])) as streams:
            yield streams[0], streams[1]
        return
    argv = sandbox_command([str(conf["command"]), *(conf.get("args") or [])], ctx)
    params = StdioServerParameters(
        command=argv[0],
        args=argv[1:],
        # The SDK merges this over its own inherited-variable allowlist, so the
        # conservative env (no secret-bearing parent variables, explicit MCP
        # ``env`` on top) is exactly what the child sees.
        env=child_env(conf.get("env")),
        cwd=conf.get("cwd"),
    )
    async with stdio_client(params) as (read, write):
        yield read, write


def _annotations(tool: Any) -> dict[str, Any]:
    """A tool's hints as a plain dict; ``requires_approval`` reads it fail-closed."""
    annotations = getattr(tool, "annotations", None)
    return dict(annotations.model_dump(exclude_none=True)) if annotations is not None else {}


class MCPSession:
    """One connected MCP server: initialize + cached tool schemas."""

    def __init__(self, name: str, connection: MCPConnection) -> None:
        self.name = name
        self.connection = connection
        self.tools: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        await self.connection.start()
        session = self.connection.session
        await session.initialize()
        for tool in (await session.list_tools()).tools:
            name = tool.name
            fname = mcp_tool_name(self.name, name)
            try:
                schema = {
                    "type": "function",
                    "function": {
                        "name": fname,
                        "description": tool.description or f"tool {name} from MCP server {self.name}",
                        "parameters": dict(tool.inputSchema or {"type": "object"}),
                    },
                }
                json.dumps(schema)
            except (TypeError, ValueError):
                log.warning("MCP server %r tool %r has invalid schema; skipped", self.name, name)
                continue
            self.tools[fname] = {
                "name": name,
                "schema": schema,
                "annotations": _annotations(tool),
            }

    async def call(self, fname: str, arguments: dict[str, Any]) -> str:
        entry = self.tools[fname]
        try:
            result = await self.connection.session.call_tool(entry["name"], arguments or {})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        is_error = bool(result.isError)
        parts: list[str] = []
        for block in result.content or []:
            btype = getattr(block, "type", "unknown")
            if btype == "text":
                parts.append(str(getattr(block, "text", "")))
            elif btype == "image":
                parts.append(f"[image content omitted ({getattr(block, 'mimeType', '?')})]")
            else:
                parts.append(str(getattr(block, "text", "")) or f"[{btype} content]")
        body = {"tool": fname, "content": "".join(parts)}
        if is_error:
            return json.dumps({"status": "error", **body}, ensure_ascii=False)
        return json.dumps({"status": "ok", **body}, ensure_ascii=False)

    async def close(self) -> None:
        await self.connection.close()


class MCPSessionManager:
    """Owns one :class:`MCPSession` per configured server; failures degrade."""

    def __init__(self, servers: dict[str, dict[str, Any]], ctx: PathContext | None = None) -> None:
        self._servers = servers
        self._ctx = ctx or PathContext(primary=Path.cwd())
        self._sessions: dict[str, MCPSession] = {}
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def ctx(self) -> PathContext:
        """The sandbox context this manager's processes were started under."""
        return self._ctx

    async def start(self) -> None:
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            for name, conf in self._servers.items():
                try:
                    session = await self._connect_one(name, conf)
                    self._sessions[name] = session
                except Exception as exc:  # noqa: BLE001 - degraded server
                    log.warning("MCP server %r failed to connect: %s", name, exc)
            self._started = True

    async def _connect_one(self, name: str, conf: dict[str, Any]) -> MCPSession:
        connection = MCPConnection(name, conf, self._ctx)
        session = MCPSession(name, connection)
        try:
            await session.start()
        except BaseException:
            # A connection that was created but never initialized must not leak
            # its subprocess/socket (including when startup is cancelled).
            await connection.close()
            raise
        return session

    def tool_schemas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for session in self._sessions.values():
            out.extend(entry["schema"] for entry in session.tools.values())
        return out

    def tool_names(self) -> set[str]:
        return {name for session in self._sessions.values() for name in session.tools}

    def has_tool(self, name: str) -> bool:
        return any(name in session.tools for session in self._sessions.values())

    def _tool_entry(self, name: str) -> dict[str, Any] | None:
        for session in self._sessions.values():
            if name in session.tools:
                return session.tools[name]
        return None

    def requires_approval(self, name: str) -> bool:
        """Fail-closed MCP approval decision (权限最小化).

        Only a tool that is *explicitly* read-only (``readOnlyHint`` is True
        and ``destructiveHint`` is not True) is auto-allowed. Missing
        annotations, ``readOnlyHint`` false, or any ``destructiveHint`` all
        require approval. An unknown name (not a registered MCP tool scope)
        is left to the caller's non-MCP policy and returns False here.
        """
        entry = self._tool_entry(name)
        if entry is None:
            return False
        annotations = entry.get("annotations") or {}
        read_only = annotations.get("readOnlyHint") is True
        destructive = annotations.get("destructiveHint") is True
        return not (read_only and not destructive)

    def approval_reason(self, name: str) -> str:
        return f"MCP 工具声明存在副作用: {name}"

    async def call(self, fname: str, arguments: dict[str, Any]) -> str:
        for session in self._sessions.values():
            if fname in session.tools:
                return await session.call(fname, arguments)
        return json.dumps({"status": "error", "message": f"unknown MCP tool: {fname}"}, ensure_ascii=False)

    async def close(self) -> None:
        """Close every server; safe to call repeatedly.

        Errors from one server are logged and do not prevent the others from
        being released.
        """
        sessions, self._sessions = self._sessions, {}
        self._started = False
        for name, session in sessions.items():
            try:
                await session.close()
            except Exception:
                log.warning("MCP server %r failed to close cleanly", name, exc_info=True)
