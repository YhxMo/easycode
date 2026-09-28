"""MCP server configuration: scopes, validation, and atomic persistence.

Three layers decide which MCP servers a session runs, lowest priority first:

1. the personal file ``~/.easycode/mcp.json``;
2. the application's own startup config (``mcp_servers`` in
   ``easycode.config.json``), kept so existing setups keep working;
3. the current project's ``easycode.config.json``.

A name defined in a higher layer replaces the whole entry from a lower one
rather than merging field by field: half of one server's command and half of
another's environment would be a third configuration nobody wrote. Setting
``enabled: false`` in a higher layer is therefore how a project switches off a
personal server, and removing the override brings the lower one back.

This module holds configuration only. Credentials live in ``mcp_auth`` and are
referenced from here; the connection itself stays in ``mcp``.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from easycode.paths import data_home
from easycode.permissions.boundary import CONFIG_FILENAME, PathContext, resolve_workspace_path

PERSONAL_FILENAME = "mcp.json"

#: The two transports. ``http`` covers Streamable HTTP; the SDK's SSE-only
#: transport is deprecated and is not offered.
TRANSPORTS = ("stdio", "http")

DEFAULT_STARTUP_TIMEOUT = 10.0
DEFAULT_TOOL_TIMEOUT = 60.0

#: Packages a stdio server may be fetched from on first use. Only these get the
#: redirect that points a package manager at easycode's own cache.
PACKAGE_LAUNCHERS = {
    "npx": ("npm_config_cache",),
    "npm": ("npm_config_cache",),
    "pnpm": ("npm_config_cache",),
    "yarn": ("npm_config_cache",),
    "bunx": ("BUN_INSTALL_CACHE_DIR",),
    "uvx": ("UV_CACHE_DIR",),
    "uv": ("UV_CACHE_DIR",),
    "pipx": ("PIPX_HOME",),
}

#: Scope names, low priority to high.
SCOPES = ("personal", "app", "project")


class MCPConfigError(ValueError):
    """A configuration the user asked to save is not usable."""


def personal_config_path() -> Path:
    return data_home() / PERSONAL_FILENAME


def project_config_path(root: Path) -> Path:
    return Path(root) / CONFIG_FILENAME


def cache_home() -> Path:
    """Where launcher-fetched packages are cached, outside the data home.

    A package manager told to use the data home could never write there: the
    sandbox denies writes under it, because that is where sessions and
    credentials live. The cache therefore sits in the user's cache directory,
    and the sandbox grants exactly this one subtree for the launch.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "easycode"


def mcp_cache_dir(launcher: str) -> Path:
    return cache_home() / "mcp" / launcher


def _positive(value: Any, label: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MCPConfigError(f"{label} 必须是正数，收到 {value!r}")
    return float(value)


def _string_map(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MCPConfigError(f"{label} 必须是字符串映射")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise MCPConfigError(f"{label} 的键和值都必须是字符串")
        out[key] = item
    return out


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MCPConfigError(f"{label} 必须是字符串数组")
    return [item for item in value if item]


def _valid_name(name: str) -> bool:
    """A name that can become a stable tool prefix.

    The registered tool name is ``mcp__<server>__<tool>``, so a separator or
    whitespace in the server name would make that name ambiguous.
    """
    return bool(name) and all(ch.isalnum() or ch in "-_." for ch in name)


@dataclass
class MCPServerConfig:
    """One server as configured in a single scope."""

    name: str
    transport: str = "stdio"
    enabled: bool = True
    startup_timeout_sec: float = DEFAULT_STARTUP_TIMEOUT
    tool_timeout_sec: float = DEFAULT_TOOL_TIMEOUT
    enabled_tools: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    #: stdio
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_vars: list[str] = field(default_factory=list)
    #: ``None`` keeps the sandbox's default (no network); True grants the child
    #: network access so a package launcher can fetch its package.
    network_enabled: bool | None = None
    #: http
    url: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    env_http_headers: dict[str, str] = field(default_factory=dict)
    bearer_token_env_var: str | None = None
    #: Credential references (see ``mcp_auth``): config stores where a secret
    #: lives, never the secret itself.
    secret_env: dict[str, str] = field(default_factory=dict)
    secret_headers: dict[str, str] = field(default_factory=dict)
    bearer_credential: str | None = None
    oauth: dict[str, Any] | None = None

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> MCPServerConfig:
        """Validate one stored entry.

        A legacy entry (``command`` or ``url`` and nothing else) parses as the
        transport its fields imply, with no grant, exactly as it behaved before
        authorization existed.
        """
        if not isinstance(raw, dict):
            raise MCPConfigError(f"{name}: 配置必须是对象")
        if not _valid_name(name):
            raise MCPConfigError(f"服务名不合法: {name!r}（只允许字母、数字、-、_、.）")
        has_command = bool(str(raw.get("command") or "").strip())
        has_url = bool(str(raw.get("url") or "").strip())
        if has_command and has_url:
            raise MCPConfigError(f"{name}: command 和 url 不能同时配置")
        if not has_command and not has_url:
            raise MCPConfigError(f"{name}: 需要 command（本地）或 url（远程）")
        transport = str(raw.get("transport") or ("http" if has_url else "stdio"))
        if transport not in TRANSPORTS:
            raise MCPConfigError(f"{name}: 未知的传输类型 {transport!r}")
        if transport == "http" and not has_url:
            raise MCPConfigError(f"{name}: http 传输需要 url")
        if transport == "stdio" and not has_command:
            raise MCPConfigError(f"{name}: stdio 传输需要 command")

        url = str(raw.get("url") or "").strip()
        if transport == "http":
            if not url.startswith(("http://", "https://")):
                raise MCPConfigError(f"{name}: URL 只支持 http(s)")
            # One rule for the transport, so credentials can never outrun it:
            # plaintext is for a server on this machine, anything remote is
            # https. A token therefore never travels in the clear either.
            if not url.startswith("https://") and not _is_loopback(url):
                raise MCPConfigError(
                    f"{name}: 明文地址只允许本机回环（127.0.0.1/localhost），远程服务请使用 https"
                )

        cwd = raw.get("cwd")
        if cwd is not None:
            cwd = str(cwd).strip() or None

        return cls(
            name=name,
            transport=transport,
            enabled=bool(raw.get("enabled", True)),
            startup_timeout_sec=_positive(
                raw.get("startup_timeout_sec"), f"{name}: startup_timeout_sec", DEFAULT_STARTUP_TIMEOUT
            ),
            tool_timeout_sec=_positive(
                raw.get("tool_timeout_sec"), f"{name}: tool_timeout_sec", DEFAULT_TOOL_TIMEOUT
            ),
            enabled_tools=_string_list(raw.get("enabled_tools"), f"{name}: enabled_tools"),
            disabled_tools=_string_list(raw.get("disabled_tools"), f"{name}: disabled_tools"),
            command=str(raw.get("command") or "").strip(),
            args=_string_list(raw.get("args"), f"{name}: args"),
            cwd=cwd,
            env=_string_map(raw.get("env"), f"{name}: env"),
            env_vars=_string_list(raw.get("env_vars"), f"{name}: env_vars"),
            network_enabled=(
                None if raw.get("network_enabled") is None else bool(raw.get("network_enabled"))
            ),
            url=url,
            http_headers=_string_map(raw.get("http_headers"), f"{name}: http_headers"),
            env_http_headers=_string_map(raw.get("env_http_headers"), f"{name}: env_http_headers"),
            bearer_token_env_var=(str(raw.get("bearer_token_env_var") or "").strip() or None),
            secret_env=_string_map(raw.get("secret_env"), f"{name}: secret_env"),
            secret_headers=_string_map(raw.get("secret_headers"), f"{name}: secret_headers"),
            bearer_credential=(str(raw.get("bearer_credential") or "").strip() or None),
            oauth=dict(raw["oauth"]) if isinstance(raw.get("oauth"), dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """The stored form; absent values are omitted so files stay readable."""
        out: dict[str, Any] = {"transport": self.transport, "enabled": self.enabled}
        out["startup_timeout_sec"] = self.startup_timeout_sec
        out["tool_timeout_sec"] = self.tool_timeout_sec
        if self.enabled_tools:
            out["enabled_tools"] = list(self.enabled_tools)
        if self.disabled_tools:
            out["disabled_tools"] = list(self.disabled_tools)
        if self.transport == "stdio":
            out["command"] = self.command
            if self.args:
                out["args"] = list(self.args)
            if self.cwd:
                out["cwd"] = self.cwd
            if self.env:
                out["env"] = dict(self.env)
            if self.env_vars:
                out["env_vars"] = list(self.env_vars)
            if self.network_enabled is not None:
                out["network_enabled"] = self.network_enabled
        else:
            out["url"] = self.url
            if self.http_headers:
                out["http_headers"] = dict(self.http_headers)
            if self.env_http_headers:
                out["env_http_headers"] = dict(self.env_http_headers)
            if self.bearer_token_env_var:
                out["bearer_token_env_var"] = self.bearer_token_env_var
        if self.secret_env:
            out["secret_env"] = dict(self.secret_env)
        if self.secret_headers:
            out["secret_headers"] = dict(self.secret_headers)
        if self.bearer_credential:
            out["bearer_credential"] = self.bearer_credential
        if self.oauth:
            out["oauth"] = dict(self.oauth)
        return out

    def allows_tool(self, tool: str) -> bool:
        """Whether the tool survives this server's own filter.

        ``enabled_tools`` narrows first and ``disabled_tools`` then removes from
        what is left, so a name in both lists is off — the deny list has the
        last word.
        """
        if self.enabled_tools and not any(_matches(pattern, tool) for pattern in self.enabled_tools):
            return False
        return not any(_matches(pattern, tool) for pattern in self.disabled_tools)

    def resolved_headers(self) -> dict[str, str]:
        """Literal headers plus the environment-sourced ones."""
        out = dict(self.http_headers)
        for header, var in self.env_http_headers.items():
            value = os.environ.get(var)
            if value:
                out[header] = value
        return out

    def resolved_env(self) -> dict[str, str]:
        """Literal ``env`` plus the parent variables named in ``env_vars``."""
        out = dict(self.env)
        for var in self.env_vars:
            value = os.environ.get(var)
            if value is not None:
                out[var] = value
        return out

    def launcher(self) -> str | None:
        """The package launcher this server runs under, matched on basename."""
        if self.transport != "stdio":
            return None
        base = Path(self.command).name
        return base if base in PACKAGE_LAUNCHERS else None


def _matches(pattern: str, tool: str) -> bool:
    """A filter entry names a tool, or ``*`` for all of them."""
    return pattern == "*" or pattern == tool


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


# ------------------------------------------------------------------ scopes


def read_scope_configs(path: Path) -> dict[str, MCPServerConfig]:
    """Every server stored in one config file, parsed and validated.

    A missing file is an empty scope; a malformed one is an error the caller
    reports rather than a scope that silently loses servers.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConfigError(f"无法读取 {path}: {exc}") from exc
    servers = raw.get("mcp_servers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise MCPConfigError(f"{path} 的 mcp_servers 必须是对象")
    out: dict[str, MCPServerConfig] = {}
    for name, entry in servers.items():
        config = MCPServerConfig.parse(str(name), entry)
        out[config.name] = config
    return out


def read_scope(path: Path) -> dict[str, dict[str, Any]]:
    """The same servers as plain dicts, for callers that hand them around."""
    return {name: config.to_dict() for name, config in read_scope_configs(path).items()}


def write_scope(path: Path, servers: dict[str, dict[str, Any]]) -> None:
    """Save one scope's servers atomically, keeping the file's other settings."""
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            # An unreadable file is replaced rather than left half-edited; the
            # caller has already surfaced whatever it could not parse.
            payload = {}
    if servers:
        payload["mcp_servers"] = servers
    else:
        payload.pop("mcp_servers", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class ResolvedServer:
    """One effective server: its config, and which scope it came from."""

    config: MCPServerConfig
    scope: str
    #: Lower scopes this entry shadows, so the UI can explain the override.
    overrides: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.config.name


def resolve(
    *,
    personal: dict[str, dict[str, Any]] | None = None,
    app: dict[str, dict[str, Any]] | None = None,
    project: dict[str, dict[str, Any]] | None = None,
) -> list[ResolvedServer]:
    """Merge the three scopes into the list a session would run.

    A higher scope replaces the whole entry; the scopes it shadowed are kept so
    the settings panel can show what would come back if the override went away.
    """
    layers = (("personal", personal or {}), ("app", app or {}), ("project", project or {}))
    out: dict[str, ResolvedServer] = {}
    for scope, servers in layers:
        for name, raw in servers.items():
            # A caller that already validated its file passes the parsed
            # configs; one that read plain JSON passes the dicts.
            config = raw if isinstance(raw, MCPServerConfig) else MCPServerConfig.parse(name, raw)
            previous = out.get(name)
            overrides = [*(previous.overrides if previous else []), previous.scope] if previous else []
            out[name] = ResolvedServer(config=config, scope=scope, overrides=overrides)
    return sorted(out.values(), key=lambda entry: entry.name)


def configured_servers(
    app: dict[str, dict[str, Any]] | None, root: str | None
) -> list[ResolvedServer]:
    """The servers one project would run, exactly as configured.

    Only the project's own primary directory is a project scope: a secondary
    directory is somewhere the session reads, not the project it belongs to, and
    letting it contribute servers would make the same conversation behave
    differently depending on which directory happened to be listed first.

    No secret is filled in. Read paths that must not carry a credential — the
    ``/`` menu, the settings panel — use this; connecting uses
    :func:`effective_servers`, which is this list plus the stored values.
    """
    project_root = str(root or "")
    personal = read_scope_configs(personal_config_path())
    project = read_scope_configs(project_config_path(Path(project_root))) if project_root else {}
    return resolve(personal=personal, app=app or {}, project=project)


def effective_servers(
    app: dict[str, dict[str, Any]] | None, root: str | None
) -> list[ResolvedServer]:
    """The servers one project actually runs, with their secrets filled in.

    Secrets belong to the scope that won the name — a personal credential must
    not authenticate a project's replacement entry for that server.
    """
    from easycode.extensions.mcp.auth import store as credential_store

    project_root = str(root or "")
    # One read for the whole list: every server is looked up in the same view of
    # the credential file, and the file is read once instead of once per server.
    credentials = credential_store().snapshot()
    out: list[ResolvedServer] = []
    for server in configured_servers(app, root):
        cred_root = project_root if server.scope == "project" else ""
        config = apply_credentials(
            server.config, scope=server.scope, root=cred_root, store=credentials
        )
        out.append(replace(server, config=config))
    return out


def matching_credential(
    config: MCPServerConfig,
    *,
    scope: str,
    root: str = "",
    store: Any,
):
    """The stored grant that authenticates ``config``, or None.

    The one place this rule exists: the panel, the connecting path and the
    connection fingerprint all ask the same question, so a token is never
    filled in from one record and invalidated by another.
    """
    if not (config.secret_env or config.secret_headers or config.bearer_credential):
        return None
    cred = store.find(scope=scope, server=config.name, root=root)
    if cred is None:
        return None
    # A record is only used for the URL it was issued for. Pointing the server
    # at a different host must not send it the old host's token, because nobody
    # agreed to that host.
    if cred.url and config.url and cred.url != config.url:
        return None
    return cred


def related_credential_versions(
    servers: list[ResolvedServer], project_root: str, store: Any
) -> dict[str, str]:
    """Per-credential digests, for the records these servers actually use.

    Only the related ones: an unrelated token rotation is not a reason to drop a
    running process. Matching goes through :func:`matching_credential`, so a
    server whose credential was replaced by one for another URL is covered too
    — otherwise that change would leave the old token in a live session.
    """
    from easycode.extensions.mcp.auth import credential_versions

    credentials = store.snapshot()
    all_versions = credential_versions(store)
    out: dict[str, str] = {}
    for server in servers:
        cred = matching_credential(
            server.config,
            scope=server.scope,
            root=project_root if server.scope == "project" else "",
            store=credentials,
        )
        if cred is not None and cred.id in all_versions:
            out[cred.id] = all_versions[cred.id]
    return out


def apply_credentials(
    config: MCPServerConfig,
    *,
    scope: str,
    root: str = "",
    store: Any = None,
) -> MCPServerConfig:
    """``config`` with the secrets it references filled in from the store.

    The names in ``secret_env``/``secret_headers`` are the *targets* (an
    environment variable, an HTTP header) and their values are the key to read
    inside the stored record, so one server can take several secrets and each
    can be replaced on its own.

    A record is only used for the URL it was issued for. Pointing the server at
    a different host must not send it the old host's token, because nobody
    agreed to that host.
    """
    from easycode.extensions.mcp.auth import store as credential_store

    cred = matching_credential(config, scope=scope, root=root, store=store or credential_store())
    if cred is None:
        return config

    def value(key: str) -> str:
        return cred.values.get(key, "")

    out = replace(config, env=dict(config.env), http_headers=dict(config.http_headers))
    for target, key in config.secret_env.items():
        if secret := value(key):
            out.env[target] = secret
    for target, key in config.secret_headers.items():
        if secret := value(key):
            out.http_headers[target] = secret
    if config.bearer_credential:
        token = value(config.bearer_credential)
        if token:
            out.http_headers["Authorization"] = f"Bearer {token}"
    return out


def scope_config_path(scope: str, root: str | None, cfg) -> Path:
    """The file a scope writes to, with the project scope's root decided here.

    The path is never taken from the request: a caller names a scope and the
    server decides which file that is, so no request can write to an arbitrary
    file by spelling it as a project root.
    """
    if scope == "personal":
        return personal_config_path()
    if scope == "project":
        if not root:
            raise MCPConfigError("项目作用域需要明确的项目目录")
        return project_config_path(Path(root))
    if scope == "app":
        # Same location ``Config.save`` would use, so the panel and the app can
        # never disagree about which file holds the application's own servers.
        return Path(cfg.config_path or Path(cfg.root) / CONFIG_FILENAME)
    raise MCPConfigError(f"未知作用域: {scope!r}")


def known_projects(cfg, store) -> list[dict[str, Any]]:
    """Projects a request may name as the MCP scope root.

    Registered projects and the configured default workspace, never the process
    CWD: the server's own directory is not the user's project.
    """
    from easycode.web.routes.workspaces import build_projects

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in [*build_projects(cfg, store), {"root": None, "secondary": []}]:
        resolved = str(Path(project.get("root") or cfg.root).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(
            {
                "root": resolved,
                "name": str(project.get("name") or Path(resolved).name),
            }
        )
    return out


def resolve_cwd(config: MCPServerConfig, ctx: PathContext) -> str:
    """The working directory for a stdio server, checked against the sandbox.

    A missing ``cwd`` is the session's own primary directory. An explicit one
    must be a real directory inside the allowed roots: a server pointed outside
    the workspace would run with a working directory its sandbox does not cover,
    which is how a "local" server ends up reading another project.
    """
    if not config.cwd:
        return str(ctx.primary)
    target = resolve_workspace_path(config.cwd, ctx.primary)
    if not target.is_dir():
        raise MCPConfigError(f"{config.name}: cwd 不是目录: {target}")
    if not ctx.in_allowed(target):
        raise MCPConfigError(f"{config.name}: cwd 不在工作区内: {target}")
    return str(target)


def fingerprint(
    servers: list[ResolvedServer], ctx: PathContext | None, *, project_root: str
) -> str:
    """A value that changes whenever a session's effective MCP setup does.

    The manager compares this against the one it started under: same value means
    the running processes still describe the configuration, a different one
    means they are stale and the next turn must reconnect. The credentials of
    these servers are part of it, so rotating one of their tokens reconnects
    instead of leaving the old one in a live session — while a token belonging
    to some other project's server is not this session's business.
    """
    import hashlib

    from easycode.extensions.mcp.auth import store as credential_store

    payload = json.dumps(
        [{"name": s.name, "scope": s.scope, "config": s.config.to_dict()} for s in servers],
        ensure_ascii=False,
        sort_keys=True,
    )
    raw = json.dumps(
        {
            "servers": payload,
            "sandbox": getattr(ctx, "sandbox_mode", ""),
            "primary": str(getattr(ctx, "primary", "")),
            "secondary": sorted(str(p) for p in getattr(ctx, "secondary", []) or []),
            "extra": sorted(str(p) for p in getattr(ctx, "extra_safe_dirs", []) or []),
            "credentials": related_credential_versions(
                servers, project_root, credential_store()
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
