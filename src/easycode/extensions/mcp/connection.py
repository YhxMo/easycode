"""The transport layer: one official-SDK client session per server.

Connections use the official ``mcp`` SDK: a sandboxed stdio child for
``command`` servers, or streamable HTTP for ``url`` servers. The SDK's client
transports are anyio context managers that must be entered and exited in one
task, so each connection runs in its own task (see :class:`MCPConnection`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from easycode.extensions.mcp.config import (
    PACKAGE_LAUNCHERS,
    MCPServerConfig,
    mcp_cache_dir,
    resolve_cwd,
)
from easycode.permissions.boundary import PathContext, ToolGrant
from easycode.permissions.sandbox import child_env, sandbox_command

log = logging.getLogger("easycode.extensions.mcp.connection")


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
