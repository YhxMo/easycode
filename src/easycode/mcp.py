"""MCP client: connect external MCP servers and expose their tools to agents.

Config shape (``mcp_servers`` in easycode.config.json)::

    {
      "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
      "remote":    {"url": "http://localhost:8000/mcp"}
    }

Registered tools are named ``mcp__<server>__<tool>``; the agent dispatches them
to the owning server's session.

Transport note: this module implements the MCP JSON-RPC framing directly
(stdio subprocess + newline-delimited JSON, or streamable-HTTP via httpx).
The official ``mcp`` SDK's stdio client deadlocks under Python 3.14 + anyio
(initialize never completes), while the wire protocol was verified working,
so we own ~100 lines of transport instead of pulling in that dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from easycode.sandbox import sandbox_command
from easycode.workspace import PathContext

log = logging.getLogger("easycode.mcp")

MCP_PREFIX = "mcp__"
PROTOCOL_VERSION = "2024-11-05"
REQUEST_TIMEOUT = 30.0


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}__{tool}"


class _BaseTransport:
    """Request/response over JSON-RPC 2.0; notifications carry no id."""

    def __init__(self) -> None:
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        raise NotImplementedError

    async def _send_line(self, line: str) -> None:
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        mid = self._next_id
        self._next_id += 1
        self._pending[mid] = fut
        payload = dict(params) if params else {}
        payload = {"method": method, "params": payload, "jsonrpc": "2.0", "id": mid}
        try:
            await self._send_line(json.dumps(payload, ensure_ascii=False))
            msg = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT)
            if "error" in msg:
                raise RuntimeError(f"MCP method {method} failed: {msg['error']}")
            return msg.get("result", {})
        finally:
            self._pending.pop(mid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"method": method, "params": params or {}, "jsonrpc": "2.0"}
        await self._send_line(json.dumps(payload, ensure_ascii=False))

    def _handle_message(self, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        if mid is None:
            return  # notification from server; ignore
        fut = self._pending.get(mid)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def close(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()


class StdioTransport(_BaseTransport):
    """Newline-delimited JSON over a spawned subprocess."""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None,
        cwd: str | None,
        ctx: PathContext,
    ) -> None:
        super().__init__()
        self.command = command
        self.args = args
        self.env = env
        self.cwd = cwd
        self.ctx = ctx
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        merged_env = {**os.environ, **(self.env or {})}
        command = sandbox_command([self.command, *self.args], self.ctx)
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=self.cwd,
        )
        asyncio.get_running_loop().create_task(self._read_loop())
        asyncio.get_running_loop().create_task(self._err_loop())

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            raw = line.strip()
            if not raw:
                continue
            try:
                self._handle_message(json.loads(raw))
            except json.JSONDecodeError:
                log.warning("MCP server sent non-JSON line: %.120s", raw)
        log.warning("MCP stdio server exited (code=%s); requests will fail", self.proc.returncode)

    async def _err_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.warning("[mcp:stderr] %s", text)

    async def _send_line(self, line: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("MCP stdio transport not started")
        async with self._write_lock:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def close(self) -> None:
        await super().close()
        if self.proc is not None:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
            self.proc = None


class HttpTransport(_BaseTransport):
    """MCP streamable HTTP: one JSON-RPC POST per request, SSE/JSON response."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        mid = self._next_id
        self._next_id += 1
        payload = {"method": method, "jsonrpc": "2.0", "id": mid}
        if params:
            payload["params"] = params
        assert self._client is not None
        resp = await self._client.post(
            self.url,
            json=payload,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        text = resp.text
        msg = self._parse_response(text)
        if "error" in msg:
            raise RuntimeError(f"MCP server error: {msg['error']}")
        return msg.get("result", {})

    @staticmethod
    def _parse_response(text: str) -> dict[str, Any]:
        data = text.strip()
        if data.startswith("data:"):
            data = data.split("\n")[0].split("data:", 1)[1].strip()
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}

    async def _send_line(self, line: str) -> None:
        raise NotImplementedError("HTTP transport uses request()/notify() directly")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"method": method, "params": params or {}, "jsonrpc": "2.0"}
        assert self._client is not None
        try:
            await self._client.post(
                self.url,
                json=payload,
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            pass  # one-way notification; fire and forget

    async def close(self) -> None:
        await super().close()
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class MCPSession:
    """One connected MCP server: initialize + cached tool schemas."""

    def __init__(self, name: str, transport: _BaseTransport) -> None:
        self.name = name
        self.transport = transport
        self.tools: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        await self.transport.start()
        result = await self.transport.request(
            "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "easycode", "version": "0.1.0"}}
        )
        await self.transport.notify("notifications/initialized")
        listing = await self.transport.request("tools/list", {})
        for tool in listing.get("tools", []):
            name = tool.get("name", "")
            fname = mcp_tool_name(self.name, name)
            try:
                schema = {
                    "type": "function",
                    "function": {
                        "name": fname,
                        "description": tool.get("description") or f"tool {name} from MCP server {self.name}",
                        "parameters": dict(tool.get("inputSchema") or {"type": "object"}),
                    },
                }
                json.dumps(schema)
            except (TypeError, ValueError):
                log.warning("MCP server %r tool %r has invalid schema; skipped", self.name, name)
                continue
            self.tools[fname] = {
                "name": name,
                "schema": schema,
                "annotations": dict(tool.get("annotations") or {}),
            }

    async def call(self, fname: str, arguments: dict[str, Any]) -> str:
        entry = self.tools.get(fname)
        if entry is None:
            return json.dumps({"status": "error", "message": f"unknown MCP tool: {fname}"}, ensure_ascii=False)
        try:
            result = await self.transport.request("tools/call", {"name": entry["name"], "arguments": arguments or {}})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        is_error = bool(result.get("isError", False))
        parts: list[str] = []
        for block in result.get("content", []) or []:
            btype = block.get("type", "unknown")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "image":
                parts.append(f"[image content omitted ({block.get('mimeType', '?')})]")
            else:
                parts.append(str(block.get("text", "")) or f"[{btype} content]")
        body = {"tool": fname, "content": "".join(parts)}
        if is_error:
            return json.dumps({"status": "error", **body}, ensure_ascii=False)
        return json.dumps({"status": "ok", **body}, ensure_ascii=False)

    async def close(self) -> None:
        await self.transport.close()


class MCPSessionManager:
    """Owns one :class:`MCPSession` per configured server; failures degrade."""

    def __init__(self, servers: dict[str, dict[str, Any]], ctx: PathContext | None = None) -> None:
        self._servers = servers
        self._ctx = ctx or PathContext(primary=Path.cwd())
        self._sessions: dict[str, MCPSession] = {}
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

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
        if conf.get("url"):
            transport = HttpTransport(str(conf["url"]))
        else:
            transport = StdioTransport(
                command=conf["command"],
                args=list(conf.get("args") or []),
                env=conf.get("env"),
                cwd=conf.get("cwd"),
                ctx=self._ctx,
            )
        session = MCPSession(name, transport)
        await session.start()
        return session

    def tool_schemas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for session in self._sessions.values():
            out.extend(entry["schema"] for entry in session.tools.values())
        return out

    def has_tool(self, name: str) -> bool:
        return any(name in session.tools for session in self._sessions.values())

    def _tool_entry(self, name: str) -> dict[str, Any] | None:
        for session in self._sessions.values():
            if name in session.tools:
                return session.tools[name]
        return None

    def requires_approval(self, name: str) -> bool:
        entry = self._tool_entry(name)
        if entry is None:
            return False
        annotations = entry.get("annotations") or {}
        return annotations.get("destructiveHint") is True or annotations.get("readOnlyHint") is False

    def approval_reason(self, name: str) -> str:
        return f"MCP 工具声明存在副作用: {name}"

    async def call(self, fname: str, arguments: dict[str, Any]) -> str:
        for session in self._sessions.values():
            if fname in session.tools:
                return await session.call(fname, arguments)
        return json.dumps({"status": "error", "message": f"unknown MCP tool: {fname}"}, ensure_ascii=False)

    async def close(self) -> None:
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
