"""MCP client: connect external MCP servers and expose their tools to agents.

Which servers exist is decided by :mod:`easycode.mcp_config` (three scopes
merged for one project) and what their secrets are by :mod:`easycode.mcp_auth`;
this module only connects and calls them.

Registered tools are named ``mcp__<server>__<tool>``; the agent dispatches them
to the owning server's session.

Connections use the official ``mcp`` SDK: a sandboxed stdio child for
``command`` servers, or streamable HTTP for ``url`` servers. The SDK's client
transports are anyio context managers that must be entered and exited in one
task, so each connection runs in its own task (see :class:`MCPConnection`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from easycode.mcp_config import (
    PACKAGE_LAUNCHERS,
    MCPServerConfig,
    ResolvedServer,
    mcp_cache_dir,
    resolve_cwd,
)
from easycode.permissions.boundary import PathContext, ToolGrant
from easycode.permissions.sandbox import child_env, sandbox_command

log = logging.getLogger("easycode.mcp")

MCP_PREFIX = "mcp__"

#: A server's connection state, as the settings panel reports it.
STATE_PENDING = "pending"
STATE_CONNECTED = "connected"
STATE_FAILED = "failed"
STATE_DISABLED = "disabled"


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}__{tool}"


@dataclass
class ServerStatus:
    """What one configured server is currently doing."""

    name: str
    scope: str
    state: str = STATE_PENDING
    error: str | None = None
    tools: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "state": self.state,
            "error": self.error,
            "tools": list(self.tools),
        }


class MCPConnection:
    """One official-SDK client session, driven from a task this class owns.

    The SDK's transports are async context managers built on anyio task groups,
    which must be entered and exited in the same task. easycode connects during
    a turn but may release the connection from an unrelated later task (session
    delete, permission change, app shutdown), so the context managers run in a
    dedicated task and callers only ever touch the session it publishes.
    """

    def __init__(self, config: MCPServerConfig, ctx: PathContext | None = None) -> None:
        self.config = config
        self.ctx = ctx or PathContext(primary=Path.cwd())
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stopping = asyncio.Event()
        self._error: BaseException | None = None

    @property
    def name(self) -> str:
        return self.config.name

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
                _open(self.config, self.ctx) as (read, write),
                ClientSession(
                    read,
                    write,
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

    async def _abort(self) -> None:
        """Cancel a connect that never became ready.

        ``_stopping`` only ends an *established* session, so a connect still in
        flight has to be cancelled — otherwise its child process would outlive
        the startup timeout that gave up waiting for it.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        """Unwind the connection; the child process is reaped before this returns."""
        if self._task is None:
            return
        if self._session is None:
            # It never became ready, so there is no session to end politely —
            # and waiting would block behind the very connect that is stuck.
            await self._abort()
            return
        self._stopping.set()
        error = await self._finish()
        if error is not None and not isinstance(error, (asyncio.CancelledError, GeneratorExit)):
            log.debug("MCP server %r connection ended with %r", self.name, error)


def _stdio_grant(config: MCPServerConfig) -> ToolGrant | None:
    """What a stdio server is allowed to reach beyond the workspace.

    Network is granted only when the configuration asks for it: a local server
    that needs no download should not be able to talk to the internet just
    because it is an MCP server. The package cache of a launcher is granted
    unconditionally, because a launcher that cannot write its cache cannot
    start a server that was already fetched.
    """
    launcher = config.launcher()
    roots = (mcp_cache_dir(launcher),) if launcher is not None else ()
    network = config.network_enabled is True
    if not roots and not network:
        return None
    return ToolGrant(network_allowed=network, writable_roots=roots)


def _launcher_env(config: MCPServerConfig) -> dict[str, str]:
    """Point a package launcher at easycode's own cache instead of the user's."""
    launcher = config.launcher()
    if launcher is None:
        return {}
    return {var: str(mcp_cache_dir(launcher)) for var in PACKAGE_LAUNCHERS[launcher]}


@asynccontextmanager
async def _open(config: MCPServerConfig, ctx: PathContext) -> AsyncIterator[tuple[Any, Any]]:
    """Open the configured server: a sandboxed stdio child, or a remote URL."""
    if config.transport == "http":
        headers = config.resolved_headers()
        async with streamablehttp_client(config.url, headers=headers or None) as streams:
            yield streams[0], streams[1]
        return
    argv = sandbox_command([config.command, *config.args], ctx, grant=_stdio_grant(config))
    params = StdioServerParameters(
        command=argv[0],
        args=argv[1:],
        # The SDK merges this over its own inherited-variable allowlist, so the
        # conservative env (no secret-bearing parent variables, explicit MCP
        # ``env`` and the launcher's cache on top) is exactly what the child sees.
        env=child_env({**config.resolved_env(), **_launcher_env(config)}),
        cwd=resolve_cwd(config, ctx),
    )
    async with stdio_client(params) as (read, write):
        yield read, write


def _annotations(tool: Any) -> dict[str, Any]:
    """A tool's hints as a plain dict; ``requires_approval`` reads it fail-closed."""
    annotations = getattr(tool, "annotations", None)
    return dict(annotations.model_dump(exclude_none=True)) if annotations is not None else {}


class MCPSession:
    """One connected MCP server: initialize + cached tool schemas."""

    def __init__(self, config: MCPServerConfig, scope: str, connection: MCPConnection) -> None:
        self.config = config
        self.scope = scope
        self.connection = connection
        self.tools: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return self.config.name

    async def start(self) -> None:
        """Connect, initialize and list tools, all within the startup timeout.

        The bound has to cover the whole handshake: a child that starts but
        never answers ``initialize`` reaches the sandbox and the process table
        just the same as one that never starts at all.
        """
        timeout = self.config.startup_timeout_sec
        try:
            async with asyncio.timeout(timeout):
                await self.connection.start()
                session = self.connection.session
                await session.initialize()
                tools = await self._list_tools(session)
        except TimeoutError:
            raise RuntimeError(f"MCP server {self.name!r} 启动超时（{timeout:g}s 内未就绪）") from None
        for tool in tools:
            name = tool.name
            if not self.config.allows_tool(name):
                continue
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

    async def _list_tools(self, session: ClientSession) -> list[Any]:
        """Every tool the server advertises, across all of its pages.

        ``list_tools`` answers with one page and a cursor; a server with more
        tools than fit in a page would otherwise be silently half-registered.
        The whole start runs under the startup timeout, so a cursor that never
        ends is bounded there rather than here.
        """
        tools: list[Any] = []
        cursor: str | None = None
        while True:
            page = await (session.list_tools(cursor=cursor) if cursor else session.list_tools())
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools

    async def call(self, fname: str, arguments: dict[str, Any]) -> str:
        entry = self.tools[fname]
        timeout = self.config.tool_timeout_sec
        try:
            # The bound covers the whole call, not just the wait for a reply:
            # a server that stops draining its stdin blocks on the write, and
            # the SDK's own read timeout would never see that.
            async with asyncio.timeout(timeout):
                result = await self.connection.session.call_tool(entry["name"], arguments or {})
        except TimeoutError:
            return json.dumps(
                {"status": "error", "message": f"工具调用超过 {timeout:g}s 未返回"},
                ensure_ascii=False,
            )
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
    """Owns one :class:`MCPSession` per configured server; failures degrade.

    A server that cannot start is recorded and skipped, not retried on every
    call: the model simply does not see its tools, and the status says why.
    """

    def __init__(
        self,
        servers: list[ResolvedServer],
        ctx: PathContext | None = None,
        *,
        fingerprint: str = "",
    ) -> None:
        self._servers = servers
        self._ctx = ctx or PathContext(primary=Path.cwd())
        self._fingerprint = fingerprint
        self._sessions: dict[str, MCPSession] = {}
        self._status: dict[str, ServerStatus] = {}
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def ctx(self) -> PathContext:
        """The sandbox context this manager's processes were started under."""
        return self._ctx

    @property
    def fingerprint(self) -> str:
        """The configuration digest these processes were started under."""
        return self._fingerprint

    async def start(self) -> None:
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            enabled = [s for s in self._servers if s.config.enabled]
            for server in self._servers:
                if not server.config.enabled:
                    self._status[server.name] = ServerStatus(
                        server.name, server.scope, state=STATE_DISABLED
                    )
            # Servers are independent processes: connecting them one at a time
            # would make the turn wait out every slow server in sequence.
            results = await asyncio.gather(
                *(self._connect_one(server) for server in enabled), return_exceptions=True
            )
            for server, result in zip(enabled, results, strict=True):
                self._record(server, result)
            self._started = True

    def _record(self, server: ResolvedServer, result: MCPSession | BaseException) -> None:
        status = ServerStatus(server.name, server.scope)
        if isinstance(result, BaseException):
            status.state = STATE_FAILED
            status.error = f"{type(result).__name__}: {result}"
            log.warning("MCP server %r failed to connect: %s", server.name, result)
        else:
            self._sessions[server.name] = result
            status.state = STATE_CONNECTED
            status.tools = sorted(t["name"] for t in result.tools.values())
        self._status[server.name] = status

    async def _connect_one(self, server: ResolvedServer) -> MCPSession:
        connection = MCPConnection(server.config, self._ctx)
        session = MCPSession(server.config, server.scope, connection)
        try:
            await session.start()
        except BaseException:
            # A connection that was created but never initialized must not leak
            # its subprocess/socket (including when startup is cancelled).
            await connection.close()
            raise
        return session

    def status(self) -> list[ServerStatus]:
        """Every configured server and what it is doing, in configuration order."""
        return [
            self._status.get(s.name, ServerStatus(s.name, s.scope)) for s in self._servers
        ]

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
