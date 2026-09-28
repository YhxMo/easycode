"""MCP configuration scopes, credential resolution and connection fingerprint."""

from __future__ import annotations

import json

import pytest

from easycode.extensions.mcp.auth import CredentialStore, MCPCredential
from easycode.extensions.mcp.config import (
    MCPConfigError,
    MCPServerConfig,
    apply_credentials,
    cache_home,
    effective_servers,
    fingerprint,
    mcp_cache_dir,
    personal_config_path,
    project_config_path,
    read_scope,
    resolve,
    resolve_cwd,
    scope_config_path,
    write_scope,
)
from easycode.permissions.boundary import PathContext


def store_from(tmp_path) -> CredentialStore:
    return CredentialStore(tmp_path / "store" / "mcp-credentials.json")


# ------------------------------------------------------------------ scopes


def test_scopes_merge_lowest_to_highest(tmp_path):
    write_scope(personal_config_path(), {"shared": {"command": "personal-cmd"}})
    write_scope(project_config_path(tmp_path), {"local": {"command": "project-cmd"}})

    servers = effective_servers({"app": {"command": "app-cmd"}}, str(tmp_path))

    assert [(s.name, s.scope) for s in servers] == [
        ("app", "app"),
        ("local", "project"),
        ("shared", "personal"),
    ]


def test_a_higher_scope_replaces_the_whole_entry(tmp_path):
    personal = {"srv": {"command": "personal", "args": ["--a"], "env": {"A": "1"}}}
    project = {"srv": {"command": "project"}}

    servers = resolve(personal=personal, app={}, project=project)

    assert len(servers) == 1
    entry = servers[0]
    assert entry.scope == "project"
    assert entry.config.command == "project"
    # Half of one server's configuration next to half of another's would be a
    # third configuration nobody wrote, so nothing is merged field by field.
    assert entry.config.args == []
    assert entry.config.env == {}
    assert entry.overrides == ["personal"]


def test_disabled_in_a_higher_scope_switches_a_personal_server_off(tmp_path):
    write_scope(personal_config_path(), {"srv": {"command": "personal-cmd"}})
    write_scope(project_config_path(tmp_path), {"srv": {"command": "unused", "enabled": False}})

    servers = effective_servers({}, str(tmp_path))

    assert [s.scope for s in servers] == ["project"]
    assert servers[0].config.enabled is False


def test_without_a_project_only_personal_and_app_scopes_apply(tmp_path):
    write_scope(personal_config_path(), {"personal": {"command": "c"}})

    assert [s.name for s in effective_servers({"app": {"command": "c"}}, None)] == [
        "app",
        "personal",
    ]


def test_reading_a_malformed_scope_reports_instead_of_dropping_servers(tmp_path):
    path = personal_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(MCPConfigError, match="无法读取"):
        read_scope(path)


def test_write_scope_keeps_the_files_other_settings(tmp_path):
    path = project_config_path(tmp_path)
    path.write_text(json.dumps({"models": {"a": "b"}}), encoding="utf-8")

    # The writer stores what it is given; normalising (defaults, dropped empty
    # fields) is the reader's job, so a hand-edited file survives a round trip.
    write_scope(path, {"srv": {"command": "c"}})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "models": {"a": "b"},
        "mcp_servers": {"srv": {"command": "c"}},
    }
    assert read_scope(path)["srv"]["startup_timeout_sec"] == 10.0

    write_scope(path, {})
    assert json.loads(path.read_text(encoding="utf-8")) == {"models": {"a": "b"}}


def test_scope_config_path_never_takes_a_path_from_the_caller(tmp_path):
    class Cfg:
        config_path = str(tmp_path / "app.json")

    assert scope_config_path("personal", None, Cfg()) == personal_config_path()
    assert scope_config_path("project", str(tmp_path), Cfg()) == project_config_path(tmp_path)
    assert scope_config_path("app", None, Cfg()).name == "app.json"
    with pytest.raises(MCPConfigError, match="项目作用域"):
        scope_config_path("project", None, Cfg())
    with pytest.raises(MCPConfigError, match="未知作用域"):
        scope_config_path("elsewhere", None, Cfg())


# --------------------------------------------------------------- validation


def test_an_entry_needs_exactly_one_transport():
    with pytest.raises(MCPConfigError, match="不能同时配置"):
        MCPServerConfig.parse("s", {"command": "c", "url": "https://h/mcp"})
    with pytest.raises(MCPConfigError, match="需要 command"):
        MCPServerConfig.parse("s", {})
    with pytest.raises(MCPConfigError, match="只允许字母"):
        MCPServerConfig.parse("bad name", {"command": "c"})


def test_a_plaintext_url_is_only_allowed_on_loopback():
    with pytest.raises(MCPConfigError, match="明文地址只允许本机回环"):
        MCPServerConfig.parse("s", {"url": "http://remote.example/mcp"})
    # The rule is the transport's, not the credential's: a plaintext remote
    # endpoint is refused whether or not a token would travel over it.
    with pytest.raises(MCPConfigError, match="明文地址只允许本机回环"):
        MCPServerConfig.parse(
            "s", {"url": "http://remote.example/mcp", "bearer_credential": "token"}
        )
    for host in ("http://127.0.0.1:8000/mcp", "http://localhost/mcp", "https://remote/mcp"):
        config = MCPServerConfig.parse("s", {"url": host, "bearer_credential": "token"})
        assert config.transport == "http"


def test_timeouts_must_be_positive():
    with pytest.raises(MCPConfigError, match="startup_timeout_sec 必须是正数"):
        MCPServerConfig.parse("s", {"command": "c", "startup_timeout_sec": 0})
    config = MCPServerConfig.parse("s", {"command": "c", "tool_timeout_sec": 2.5})
    assert config.tool_timeout_sec == 2.5


def test_tool_filter_narrows_then_removes():
    config = MCPServerConfig.parse(
        "s", {"command": "c", "enabled_tools": ["a", "b"], "disabled_tools": ["b"]}
    )
    assert config.allows_tool("a") is True
    assert config.allows_tool("b") is False
    assert config.allows_tool("c") is False

    assert MCPServerConfig.parse("s", {"command": "c", "disabled_tools": ["*"]}).allows_tool("a") is False
    assert MCPServerConfig.parse("s", {"command": "c"}).allows_tool("anything") is True


def test_launcher_matches_the_command_basename_only():
    assert MCPServerConfig.parse("s", {"command": "/usr/local/bin/npx"}).launcher() == "npx"
    assert MCPServerConfig.parse("s", {"command": "uvx"}).launcher() == "uvx"
    assert MCPServerConfig.parse("s", {"command": "node"}).launcher() is None
    assert MCPServerConfig.parse("s", {"url": "https://h/mcp"}).launcher() is None


def test_resolve_cwd_stays_inside_the_workspace(tmp_path):
    ctx = PathContext(primary=tmp_path)
    assert resolve_cwd(MCPServerConfig.parse("s", {"command": "c"}), ctx) == str(tmp_path)

    outside = MCPServerConfig.parse("s", {"command": "c", "cwd": str(tmp_path.parent)})
    with pytest.raises(MCPConfigError, match="不在工作区内"):
        resolve_cwd(outside, ctx)

    missing = MCPServerConfig.parse("s", {"command": "c", "cwd": str(tmp_path / "nope")})
    with pytest.raises(MCPConfigError, match="不是目录"):
        resolve_cwd(missing, ctx)


def test_the_launcher_cache_is_outside_the_data_home():
    from easycode.paths import data_home

    cache = mcp_cache_dir("npx")
    assert cache.is_relative_to(cache_home())
    # The sandbox denies writes under the data home, so a cache placed there
    # could never be written by the launcher it exists for.
    assert not cache.is_relative_to(data_home())


# -------------------------------------------------------------- credentials


def test_secrets_are_filled_from_the_store(tmp_path):
    store = store_from(tmp_path)
    store.save(
        MCPCredential(
            id="c1",
            kind="env",
            scope="app",
            server="s",
            values={"GITHUB_TOKEN": "ghp_x", "AUTH": "Bearer stored"},
        )
    )
    config = MCPServerConfig.parse(
        "s",
        {
            "command": "c",
            "secret_env": {"GITHUB_TOKEN": "GITHUB_TOKEN"},
            "secret_headers": {"X-Key": "AUTH"},
        },
    )

    out = apply_credentials(config, scope="app", store=store)

    assert out.env == {"GITHUB_TOKEN": "ghp_x"}
    assert out.http_headers == {"X-Key": "Bearer stored"}
    # The configuration itself never holds a secret.
    assert config.env == {} and config.http_headers == {}


def test_a_bearer_credential_becomes_an_authorization_header(tmp_path):
    store = store_from(tmp_path)
    store.save(
        MCPCredential(id="c1", kind="bearer", scope="app", server="s", values={"token": "t"})
    )
    config = MCPServerConfig.parse("s", {"url": "https://h/mcp", "bearer_credential": "token"})

    out = apply_credentials(config, scope="app", store=store)

    assert out.http_headers == {"Authorization": "Bearer t"}


def test_a_token_is_not_sent_to_a_host_it_was_not_issued_for(tmp_path):
    store = store_from(tmp_path)
    store.save(
        MCPCredential(
            id="c1",
            kind="bearer",
            scope="app",
            server="s",
            url="https://old.example/mcp",
            values={"token": "t"},
        )
    )
    moved = MCPServerConfig.parse(
        "s", {"url": "https://new.example/mcp", "bearer_credential": "token"}
    )
    assert apply_credentials(moved, scope="app", store=store).http_headers == {}

    unchanged = MCPServerConfig.parse(
        "s", {"url": "https://old.example/mcp", "bearer_credential": "token"}
    )
    assert apply_credentials(unchanged, scope="app", store=store).http_headers == {
        "Authorization": "Bearer t"
    }


def test_a_credential_belongs_to_the_scope_that_won_the_name(tmp_path):
    """A personal token must not authenticate a project's replacement entry."""
    from easycode.extensions.mcp.auth import store as credential_store

    write_scope(
        project_config_path(tmp_path),
        {"srv": {"command": "project-cmd", "secret_env": {"TOKEN": "token"}}},
    )
    credential_store().save(
        MCPCredential(id="p1", kind="env", scope="project", server="srv", root=str(tmp_path),
                      values={"token": "project-secret"})
    )

    servers = effective_servers({}, str(tmp_path))

    assert servers[0].config.env == {"TOKEN": "project-secret"}


def test_effective_servers_uses_the_project_scopes_own_credential_root(tmp_path):
    from easycode.extensions.mcp.auth import store as credential_store

    write_scope(project_config_path(tmp_path), {"srv": {"command": "c"}})
    credential_store().save(
        MCPCredential(id="p1", kind="env", scope="personal", server="srv", root=str(tmp_path),
                      values={"token": "wrong"})
    )

    servers = effective_servers({}, str(tmp_path))

    # The personal record names a root, but the winning scope is the project, so
    # it is looked up under the project root — where only a project record lives.
    assert servers[0].scope == "project"
    assert servers[0].config.env == {}


# -------------------------------------------------------------- fingerprint


def test_fingerprint_tracks_configuration_and_context(tmp_path):
    ctx = PathContext(primary=tmp_path)
    servers = resolve(app={"s": {"command": "c"}})
    base = fingerprint(servers, ctx, project_root="")

    assert base == fingerprint(resolve(app={"s": {"command": "c"}}), ctx, project_root="")
    assert base != fingerprint(resolve(app={"s": {"command": "d"}}), ctx, project_root="")
    assert base != fingerprint(
        servers,
        PathContext(primary=tmp_path, sandbox_mode="danger-full-access"),
        project_root="",
    )
    assert base != fingerprint(servers, PathContext(primary=tmp_path.parent), project_root="")


def test_fingerprint_tracks_credential_rotation(tmp_path):
    from easycode.extensions.mcp.auth import store as credential_store

    ctx = PathContext(primary=tmp_path)
    # The server has to reference the secret, otherwise no credential of its
    # name could ever reach it and rotating one changes nothing about the run.
    servers = resolve(app={"s": {"command": "c", "secret_env": {"TOKEN": "token"}}})
    before = fingerprint(servers, ctx, project_root="")

    credential_store().save(
        MCPCredential(id="c1", kind="env", scope="app", server="s", values={"token": "a"})
    )
    issued = fingerprint(servers, ctx, project_root="")
    assert issued != before

    credential_store().replace_values("c1", {"token": "b"})
    assert fingerprint(servers, ctx, project_root="") != issued


def test_fingerprint_ignores_credentials_this_session_cannot_use(tmp_path):
    """A token for another project's server is not this session's business."""
    from easycode.extensions.mcp.auth import store as credential_store

    ctx = PathContext(primary=tmp_path)
    here = tmp_path / "here"
    elsewhere = tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    servers = resolve(project={"s": {"command": "c", "secret_env": {"TOKEN": "token"}}})

    # Two projects, same server name, one token each: only this project's is
    # the one this session would actually send.
    credential_store().save(
        MCPCredential(
            id="mine",
            kind="env",
            scope="project",
            server="s",
            root=str(here),
            values={"token": "a"},
        )
    )
    other = credential_store().save(
        MCPCredential(
            id="their",
            kind="env",
            scope="project",
            server="s",
            root=str(elsewhere),
            values={"token": "b"},
        )
    )
    before = fingerprint(servers, ctx, project_root=str(here))

    # Rotating the other project's token leaves this session's process alone...
    credential_store().replace_values(other.id, {"token": "rotated"})
    assert fingerprint(servers, ctx, project_root=str(here)) == before

    # ...while rotating the one it uses must reconnect.
    credential_store().replace_values("mine", {"token": "rotated"})
    assert fingerprint(servers, ctx, project_root=str(here)) != before


def test_configured_servers_parse_each_entry_once(tmp_path):
    """One read, one validation per entry: the scope dicts are not re-parsed."""
    from easycode.extensions.mcp.config import MCPServerConfig, configured_servers

    path = personal_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "alpha": {"transport": "http", "url": "http://127.0.0.1:9/a"},
                    "beta": {"transport": "http", "url": "http://127.0.0.1:9/b"},
                }
            }
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}
    real = MCPServerConfig.parse

    def counted(name, entry):
        calls["n"] += 1
        return real(name, entry)

    MCPServerConfig.parse = staticmethod(counted)
    try:
        servers = configured_servers(None, str(tmp_path))
    finally:
        MCPServerConfig.parse = real

    assert [s.name for s in servers] == ["alpha", "beta"]
    # Two entries, two parses: resolving must not re-parse what was just read.
    assert calls["n"] == 2, calls


def test_fingerprint_follows_a_changed_server_url(tmp_path):
    """Pointing the server elsewhere retires its token, so the setup changed."""
    from easycode.extensions.mcp.auth import store as credential_store

    ctx = PathContext(primary=tmp_path)
    servers = resolve(
        project={
            "s": {
                "transport": "http",
                "url": "http://127.0.0.1:9/a",
                "bearer_credential": "token",
            }
        }
    )
    plain = fingerprint(servers, ctx, project_root=str(tmp_path))

    credential_store().save(
        MCPCredential(
            id="c1",
            kind="bearer",
            scope="project",
            server="s",
            root=str(tmp_path),
            url="http://127.0.0.1:9/a",
            values={"token": "a"},
        )
    )
    attached = fingerprint(servers, ctx, project_root=str(tmp_path))
    assert attached != plain

    # The same record no longer authenticates a server pointed at another URL:
    # the session keeps no token, which is a different setup again.
    credential_store().save(
        MCPCredential(
            id="c1",
            kind="bearer",
            scope="project",
            server="s",
            root=str(tmp_path),
            url="http://127.0.0.1:9/b",
            values={"token": "a"},
        )
    )
    assert fingerprint(servers, ctx, project_root=str(tmp_path)) == plain


def test_fingerprint_separates_projects_that_share_a_server_name(tmp_path):
    """Two projects, one server name: each root fingerprints its own token."""
    from easycode.extensions.mcp.auth import store as credential_store

    here = tmp_path / "here"
    elsewhere = tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    ctx = PathContext(primary=here)
    servers = resolve(project={"s": {"command": "c", "secret_env": {"TOKEN": "token"}}})

    credential_store().save(
        MCPCredential(
            id="here",
            kind="env",
            scope="project",
            server="s",
            root=str(here),
            values={"token": "a"},
        )
    )
    credential_store().save(
        MCPCredential(
            id="there",
            kind="env",
            scope="project",
            server="s",
            root=str(elsewhere),
            values={"token": "b"},
        )
    )

    # Same server list and context, but each project's own root resolves to its
    # own record: a subagent may borrow the parent's manager, and a session in
    # another project must not look like the same setup.
    assert fingerprint(servers, ctx, project_root=str(here)) != fingerprint(
        servers, ctx, project_root=str(elsewhere)
    )
    # The same view twice is stable.
    assert fingerprint(servers, ctx, project_root=str(here)) == fingerprint(
        servers, ctx, project_root=str(here)
    )
