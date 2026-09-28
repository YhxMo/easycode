"""HTTP endpoints for external MCP servers: configuration, credentials, status.

Reads never carry a secret. The stored configuration names where a secret comes
from (``secret_env``, ``secret_headers``, ``bearer_credential``) and the
credential file holds the value; :func:`easycode.extensions.mcp.config.effective_servers`
joins the two at connect time, so nothing here has to handle a token at all.

Every request is scoped to one project, because that is what decides the
effective list: a project's own file can switch a personal server off, so there
is no such thing as a project-independent set of servers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.config import Config
from easycode.extensions.mcp.auth import KINDS, MCPCredential
from easycode.extensions.mcp.auth import store as credential_store
from easycode.extensions.mcp.config import (
    SCOPES,
    MCPConfigError,
    MCPServerConfig,
    known_projects,
    read_scope,
    resolve,
    scope_config_path,
    write_scope,
)
from easycode.web.session import Session, SessionStore, idle_sessions, project_key, run_mutation


class SaveServerRequest(BaseModel):
    scope: str
    root: str | None = None
    name: str
    config: dict[str, Any] = {}


class RemoveServerRequest(BaseModel):
    scope: str
    root: str | None = None
    name: str


class SaveCredentialRequest(BaseModel):
    scope: str
    root: str | None = None
    server: str
    kind: str = "env"
    #: ``None`` removes that name; the rest of the record is left alone, so the
    #: panel never has to echo back a value it was never shown.
    values: dict[str, str | None] = {}


class RemoveCredentialRequest(BaseModel):
    scope: str
    root: str | None = None
    server: str


def _resolve_root(root: str | None) -> str | None:
    return str(Path(root).expanduser().resolve()) if root else None


def _credential_root(scope: str, root: str | None) -> str:
    """Where a scope's credential lives; only the project scope is keyed by root."""
    return (root or "") if scope == "project" else ""


def register_mcp(app: FastAPI, cfg: Config, store: SessionStore) -> None:
    def _projects() -> list[dict[str, Any]]:
        return known_projects(cfg, store)

    def _project_root(requested: str | None) -> str:
        """The project a request may name; never an arbitrary directory.

        An omitted root means the default project, which every session without
        an explicit directory of its own runs in. The file path is never taken
        from the request, so no request can make the server write to a file of
        its choosing by spelling it as a project root.
        """
        root = _resolve_root(requested) or str(Path(cfg.root).expanduser().resolve())
        if root not in {p["root"] for p in _projects()}:
            raise HTTPException(422, "项目作用域需要一个已登记的项目目录")
        return root

    def _scope_path(scope: str, root: str | None) -> Path:
        if scope not in SCOPES:
            raise HTTPException(422, f"未知作用域: {scope!r}")
        target = _project_root(root) if scope == "project" else None
        try:
            return scope_config_path(scope, target, cfg)
        except MCPConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    def _session_root(sess: Session) -> str:
        return str(Path(sess.root or cfg.root).expanduser().resolve())

    def _targets(scope: str, root: str | None) -> list[Session]:
        """Sessions whose MCP setup this edit invalidates.

        The project key is always the resolved directory, never the request's
        spelling: an omitted root means the default project, and the sessions in
        it carry that project's path, not ``None``.
        """
        if scope != "project":
            return store.list()
        key = project_key(_project_root(root))
        return [s for s in store.list() if project_key(_session_root(s)) == key]

    def _snapshot(root: str | None) -> dict[str, Any]:
        """Everything the panel reads, for one project, with no secret in it."""
        errors: list[str] = []
        project_root = _project_root(root)
        rows: list[dict[str, Any]] = []
        stored: dict[str, dict[str, dict[str, Any]]] = {}

        def load(scope: str, scope_root: str | None) -> dict[str, dict[str, Any]]:
            path = _scope_path(scope, scope_root)
            try:
                data = read_scope(path)
            except MCPConfigError as exc:
                # A file nobody can parse is reported rather than shown as a
                # scope that simply has no servers.
                errors.append(str(exc))
                data = {}
            rows.append(
                {
                    "scope": scope,
                    "root": scope_root,
                    "path": str(path),
                    "exists": path.is_file(),
                }
            )
            return data

        stored["personal"] = load("personal", None)
        # The application's own startup config is normally the project's own
        # file (easycode is started inside the project it serves). Listing it
        # twice would show one entry as two layers of a merge that does not
        # exist, so the overlap is listed once, as the project scope.
        app_path = _scope_path("app", None)
        project_path = _scope_path("project", project_root)
        stored["app"] = (
            load("app", None) if app_path.resolve() != project_path.resolve() else {}
        )
        stored["project"] = load("project", project_root)

        # ``resolve`` rather than ``effective_servers``: the panel sees where a
        # secret comes from, never the value a connect would fill in.
        servers = []
        # Every server is looked up in one snapshot, so a write that lands while
        # the panel is being built cannot make the list describe two file states.
        credentials = credential_store().snapshot()
        for entry in resolve(
            personal=stored["personal"], app=stored["app"], project=stored["project"]
        ):
            cred = credentials.find(
                scope=entry.scope,
                server=entry.name,
                root=_credential_root(entry.scope, project_root),
            )
            servers.append(
                {
                    "name": entry.name,
                    "scope": entry.scope,
                    "overrides": entry.overrides,
                    "enabled": entry.config.enabled,
                    "config": entry.config.to_dict(),
                    "credential": cred.public() if cred else None,
                }
            )
        return {
            "root": project_root,
            "projects": _projects(),
            "scopes": rows,
            "servers": servers,
            "errors": errors,
        }

    async def _snapshot_async(root: str | None) -> dict[str, Any]:
        return await asyncio.to_thread(_snapshot, root)

    def _server_url(scope: str, root: str | None, server: str) -> str:
        """The URL a credential is issued for, read from the stored config."""
        try:
            raw = read_scope(_scope_path(scope, root)).get(server) or {}
        except MCPConfigError:
            return ""
        return str(raw.get("url") or "")

    @app.get("/api/mcp")
    async def read_mcp(root: str | None = None) -> dict:
        """Effective servers for one project, the files behind them, and read errors."""
        return await _snapshot_async(root)

    @app.get("/api/mcp/status")
    def mcp_status(session_id: str) -> dict:
        """How one session's servers are doing right now."""
        sess = store.get(session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        manager = sess.agent.mcp_manager
        if manager is None:
            return {"started": False, "servers": []}
        return {"started": True, "servers": [s.public() for s in manager.status()]}

    @app.post("/api/mcp/servers")
    async def save_server(req: SaveServerRequest) -> dict:
        path = _scope_path(req.scope, req.root)
        root = _resolve_root(req.root)
        try:
            config = MCPServerConfig.parse(req.name, req.config)
        except MCPConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        targets = _targets(req.scope, root)

        def mutate() -> None:
            servers = dict(read_scope(path))
            servers[config.name] = config.to_dict()
            write_scope(path, servers)
            if req.scope == "app":
                # Agents hold this very dict; replacing it would leave every
                # open session reading the list it was built with.
                cfg.mcp_servers.clear()
                cfg.mcp_servers.update(servers)

        async with store.config_change(), idle_sessions(targets):
            await run_mutation(mutate)
            for sess in targets:
                await sess.agent.invalidate_mcp_if_context_changed()
        return await _snapshot_async(root)

    @app.post("/api/mcp/servers/remove")
    async def remove_server(req: RemoveServerRequest) -> dict:
        path = _scope_path(req.scope, req.root)
        root = _resolve_root(req.root)
        targets = _targets(req.scope, root)

        def mutate() -> None:
            servers = dict(read_scope(path))
            if servers.pop(req.name, None) is None:
                return
            write_scope(path, servers)
            if req.scope == "app":
                cfg.mcp_servers.clear()
                cfg.mcp_servers.update(servers)

        async with store.config_change(), idle_sessions(targets):
            await run_mutation(mutate)
            # Removing the registration removes what was stored for it: a token
            # left behind would be handed to whatever takes that name next.
            credential_store().delete_for_server(
                scope=req.scope,
                server=req.name,
                root=_credential_root(req.scope, _project_root(req.root)),
            )
            for sess in targets:
                await sess.agent.invalidate_mcp_if_context_changed()
        return await _snapshot_async(root)

    @app.post("/api/mcp/credentials")
    async def save_credential(req: SaveCredentialRequest) -> dict:
        """Store secret values for one server; they are never returned again."""
        _scope_path(req.scope, req.root)
        if req.kind not in KINDS:
            raise HTTPException(422, f"未知的凭据类型: {req.kind!r}")
        root = _resolve_root(req.root)
        cred_root = _credential_root(req.scope, _project_root(req.root))
        targets = _targets(req.scope, root)

        def mutate() -> None:
            creds = credential_store()
            existing = creds.find(scope=req.scope, server=req.server, root=cred_root)
            values = dict(existing.values) if existing else {}
            for name, value in req.values.items():
                if value is None:
                    values.pop(name, None)
                else:
                    values[name] = value
            creds.save(
                MCPCredential(
                    id=existing.id if existing else uuid4().hex[:12],
                    kind=req.kind,
                    scope=req.scope,
                    server=req.server,
                    root=cred_root,
                    # The host it is issued for is part of the record, so
                    # pointing the server elsewhere cannot reuse this token.
                    url=existing.url if existing else _server_url(req.scope, root, req.server),
                    values=values,
                )
            )

        async with store.config_change(), idle_sessions(targets):
            await run_mutation(mutate)
            for sess in targets:
                await sess.agent.invalidate_mcp_if_context_changed()
        return await _snapshot_async(root)

    @app.post("/api/mcp/credentials/remove")
    async def remove_credential(req: RemoveCredentialRequest) -> dict:
        """Forget every secret stored for one server registration."""
        _scope_path(req.scope, req.root)
        root = _resolve_root(req.root)
        targets = _targets(req.scope, root)

        def mutate() -> int:
            return credential_store().delete_for_server(
                scope=req.scope, server=req.server, root=_credential_root(req.scope, _project_root(req.root))
            )

        async with store.config_change(), idle_sessions(targets):
            await run_mutation(mutate)
            for sess in targets:
                await sess.agent.invalidate_mcp_if_context_changed()
        return await _snapshot_async(root)
