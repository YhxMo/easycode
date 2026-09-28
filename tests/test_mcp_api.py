"""MCP settings API: scopes, credentials, invalidation and status."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.extensions.mcp.config import effective_servers, personal_config_path
from easycode.extensions.mcp.credentials import CredentialStore, credentials_path
from easycode.extensions.mcp.manager import MCPSessionManager
from easycode.web.main import create_app


def build(tmp_path):
    """An app with a default project and one registered second project."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    default = tmp_path / "default"
    other = tmp_path / "other"
    default.mkdir()
    other.mkdir()
    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8"
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = default
    cfg.workspace_projects = [{"root": str(other), "secondary": []}]

    def factory(alias: str = "fake-a", **kw):
        root = Path(kw["root"]).resolve() if kw.get("root") else default
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=root,
        )

    store = SessionStore(cfg, default, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    return app, cfg, store, {"default": default, "other": other}


def attach_running_manager(agent) -> MCPSessionManager:
    """A stand-in for a live connection, so invalidation is observable."""
    manager = MCPSessionManager([], agent.path_context(), fingerprint="running")
    agent.mcp_manager = manager
    agent.mcp_owned = True
    return manager


def test_the_read_endpoint_lists_the_three_scopes(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    client = TestClient(app)

    body = client.get("/api/mcp", params={"root": str(roots["other"])}).json()

    assert [s["scope"] for s in body["scopes"]] == ["personal", "app", "project"]
    assert body["scopes"][2]["root"] == str(roots["other"])
    assert body["servers"] == []
    assert body["errors"] == []
    assert {p["root"] for p in body["projects"]} == {
        str(roots["default"]),
        str(roots["other"]),
    }


def test_saving_a_server_writes_the_projects_own_file(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    client = TestClient(app)

    r = client.post(
        "/api/mcp/servers",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "name": "demo",
            "config": {"command": "npx", "args": ["-y", "pkg"]},
        },
    )

    assert r.status_code == 200, r.text
    assert [(s["name"], s["scope"]) for s in r.json()["servers"]] == [("demo", "project")]
    written = json.loads((roots["other"] / "easycode.config.json").read_text(encoding="utf-8"))
    assert written["mcp_servers"]["demo"]["command"] == "npx"
    assert not (roots["default"] / "easycode.config.json").exists()


def test_a_project_can_switch_a_personal_server_off_for_itself(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    client.post(
        "/api/mcp/servers",
        json={"scope": "personal", "name": "shared", "config": {"command": "personal-cmd"}},
    )

    mine = client.get("/api/mcp", params={"root": str(roots["other"])}).json()["servers"]
    assert [(s["name"], s["scope"], s["enabled"], s["overrides"]) for s in mine] == [
        ("shared", "personal", True, [])
    ]

    client.post(
        "/api/mcp/servers",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "name": "shared",
            "config": {"command": "unused", "enabled": False},
        },
    )

    mine = client.get("/api/mcp", params={"root": str(roots["other"])}).json()["servers"]
    assert mine[0]["scope"] == "project"
    assert mine[0]["enabled"] is False
    assert mine[0]["overrides"] == ["personal"]
    # The default project keeps running the personal server.
    theirs = client.get("/api/mcp", params={"root": str(roots["default"])}).json()["servers"]
    assert [(s["scope"], s["enabled"]) for s in theirs] == [("personal", True)]


def test_a_secret_is_stored_but_never_returned(tmp_path):
    app, cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    client.post(
        "/api/mcp/servers",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "name": "gh",
            "config": {"command": "npx", "secret_env": {"GITHUB_TOKEN": "token"}},
        },
    )

    r = client.post(
        "/api/mcp/credentials",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "server": "gh",
            "kind": "env",
            "values": {"token": "ghp_supersecret"},
        },
    )

    assert r.status_code == 200, r.text
    assert "ghp_supersecret" not in r.text
    cred = r.json()["servers"][0]["credential"]
    assert (cred["kind"], cred["names"], cred["has_value"]) == ("env", ["token"], True)
    assert cred["updated_at"]
    # What a connect would actually use is only visible to the connection layer.
    servers = effective_servers(cfg.mcp_servers, str(roots["other"]))
    assert servers[0].config.env == {"GITHUB_TOKEN": "ghp_supersecret"}
    # And the stored configuration still holds no secret.
    written = json.loads((roots["other"] / "easycode.config.json").read_text(encoding="utf-8"))
    assert "ghp_supersecret" not in json.dumps(written)


def test_credentials_can_be_replaced_name_by_name(tmp_path):
    app, cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    client.post(
        "/api/mcp/servers",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "name": "gh",
            "config": {
                "command": "npx",
                "secret_env": {"A": "a", "B": "b"},
            },
        },
    )
    client.post(
        "/api/mcp/credentials",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "server": "gh",
            "values": {"a": "1", "b": "2"},
        },
    )

    # The panel never sees the old values, so it can only ever send the ones it
    # is changing: the record is merged, not replaced.
    r = client.post(
        "/api/mcp/credentials",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "server": "gh",
            "values": {"a": "9", "b": None},
        },
    )

    assert r.json()["servers"][0]["credential"]["names"] == ["a"]
    env = effective_servers(cfg.mcp_servers, str(roots["other"]))[0].config.env
    assert env == {"A": "9"}


def test_removing_a_server_forgets_what_was_stored_for_it(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    client.post(
        "/api/mcp/servers",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "name": "gh",
            "config": {"command": "npx", "secret_env": {"TOKEN": "token"}},
        },
    )
    client.post(
        "/api/mcp/credentials",
        json={
            "scope": "project",
            "root": str(roots["other"]),
            "server": "gh",
            "values": {"token": "ghp_x"},
        },
    )
    assert CredentialStore(credentials_path()).all()

    r = client.post(
        "/api/mcp/servers/remove",
        json={"scope": "project", "root": str(roots["other"]), "name": "gh"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["servers"] == []
    # A token left behind would be handed to whatever takes that name next.
    assert CredentialStore(credentials_path()).all() == []


def test_an_unregistered_project_root_is_rejected(tmp_path):
    app, _cfg, _store, _roots = build(tmp_path)
    client = TestClient(app)
    escape = tmp_path / "escape"

    r = client.post(
        "/api/mcp/servers",
        json={"scope": "project", "root": str(escape), "name": "s", "config": {"command": "c"}},
    )

    assert r.status_code == 422
    assert not (escape / "easycode.config.json").exists()
    assert client.get("/api/mcp", params={"root": str(escape)}).status_code == 422


def test_a_file_nobody_can_parse_is_reported(tmp_path):
    app, _cfg, _store, _roots = build(tmp_path)
    personal = personal_config_path()
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text("{ not json", encoding="utf-8")

    body = TestClient(app).get("/api/mcp").json()

    assert body["servers"] == []
    assert any("无法读取" in e for e in body["errors"])


def test_editing_a_scope_drops_only_the_sessions_it_affects(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    other_session = store.create(root=str(roots["other"]))
    default_session = store.create(root=str(roots["default"]))
    attach_running_manager(other_session.agent)
    attach_running_manager(default_session.agent)
    client = TestClient(app)

    r = client.post(
        "/api/mcp/servers",
        json={"scope": "project", "root": str(roots["other"]), "name": "s",
              "config": {"command": "c"}},
    )

    assert r.status_code == 200, r.text
    assert other_session.agent.mcp_manager is None, "the edited project must reconnect"
    assert default_session.agent.mcp_manager is not None, "another project is unaffected"


def test_a_project_edit_without_a_root_targets_the_default_project(tmp_path):
    """An omitted root means the default project, whose sessions carry its path."""
    app, _cfg, store, _roots = build(tmp_path)
    sess = store.create()
    attach_running_manager(sess.agent)

    r = TestClient(app).post(
        "/api/mcp/servers",
        json={"scope": "project", "name": "s", "config": {"command": "c"}},
    )

    assert r.status_code == 200, r.text
    assert sess.agent.mcp_manager is None, "the default project's session must reconnect"


def test_a_credential_for_the_default_project_is_found_again(tmp_path):
    """The stored record's root must be the one a session resolves for itself."""
    app, cfg, _store, roots = build(tmp_path)
    client = TestClient(app)
    client.post(
        "/api/mcp/servers",
        json={
            "scope": "project",
            "name": "gh",
            "config": {"command": "npx", "secret_env": {"TOKEN": "token"}},
        },
    )

    r = client.post(
        "/api/mcp/credentials",
        json={"scope": "project", "server": "gh", "values": {"token": "s3cret"}},
    )

    assert r.status_code == 200, r.text
    cred = r.json()["servers"][0]["credential"]
    assert cred is not None and cred["has_value"] is True
    # And a session of that project would really be handed the value.
    env = effective_servers(cfg.mcp_servers, str(roots["default"]))[0].config.env
    assert env == {"TOKEN": "s3cret"}


def test_a_global_scope_edit_drops_every_session(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    sessions = [store.create(root=str(roots["other"])), store.create(root=str(roots["default"]))]
    for sess in sessions:
        attach_running_manager(sess.agent)

    r = TestClient(app).post(
        "/api/mcp/servers", json={"scope": "personal", "name": "s", "config": {"command": "c"}}
    )

    assert r.status_code == 200, r.text
    assert all(s.agent.mcp_manager is None for s in sessions)


def test_the_app_scope_is_not_listed_when_it_is_the_projects_own_file(tmp_path):
    """Started inside a project, easycode's own config *is* that project's file.

    Listing it as two scopes would show one entry as two layers of a merge that
    does not exist.
    """
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    store = SessionStore(
        cfg,
        tmp_path,
        lambda alias="fake-a", **_: Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=tmp_path,
        ),
    )
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))

    client.post(
        "/api/mcp/servers",
        json={"scope": "project", "root": str(tmp_path), "name": "s",
              "config": {"command": "c"}},
    )
    body = client.get("/api/mcp", params={"root": str(tmp_path)}).json()

    assert [row["scope"] for row in body["scopes"]] == ["personal", "project"]
    # One entry, one layer: the project's own.
    assert [(s["name"], s["scope"], s["overrides"]) for s in body["servers"]] == [
        ("s", "project", [])
    ]


def test_status_reports_a_session_that_has_not_connected(tmp_path):
    app, _cfg, store, _roots = build(tmp_path)
    sess = store.create()
    client = TestClient(app)

    assert client.get("/api/mcp/status", params={"session_id": sess.id}).json() == {
        "started": False,
        "servers": [],
    }
    assert client.get("/api/mcp/status", params={"session_id": "nope"}).status_code == 404


@pytest.mark.asyncio
async def test_editing_is_refused_while_a_turn_is_running(tmp_path):
    """A settings write must not change the servers under a running turn."""
    app, _cfg, store, _roots = build(tmp_path)
    sess = store.create()
    payload = {"scope": "personal", "name": "s", "config": {"command": "c"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        async with sess._lock:
            busy = await c.post("/api/mcp/servers", json=payload)
        after = await c.post("/api/mcp/servers", json=payload)

    assert busy.status_code == 409, busy.text
    assert after.status_code == 200, after.text


def test_panel_reads_the_credential_file_once_per_request(tmp_path):
    """Five credentialed servers, one read: the panel is one snapshot."""
    from easycode.extensions.mcp.credentials import MCPCredential
    from easycode.extensions.mcp.credentials import store as credential_store

    app, _cfg, _store, roots = build(tmp_path)
    store = credential_store()
    names = [f"srv{i}" for i in range(5)]
    for name in names:
        store.save(
            MCPCredential(
                id=f"model-{name}",
                kind="bearer",
                scope="project",
                server=name,
                root=str(roots["default"]),
                values={"token": f"tok-{name}"},
            )
        )
    project_path = roots["default"] / "easycode.config.json"
    project_path.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    name: {"transport": "http", "url": f"http://127.0.0.1:9/{name}"}
                    for name in names
                }
            }
        ),
        encoding="utf-8",
    )

    real_load = CredentialStore._load
    reads = {"n": 0}

    def counted(self):
        reads["n"] += 1
        return real_load(self)

    CredentialStore._load = counted
    try:
        body = TestClient(app).get("/api/mcp", params={"root": str(roots["default"])}).json()
    finally:
        CredentialStore._load = real_load

    assert {row["name"] for row in body["servers"]} == set(names)
    assert all(row["credential"] is not None for row in body["servers"])
    # The token itself never reaches the browser, only its identity.
    assert "tok-srv0" not in json.dumps(body)
    assert reads["n"] == 1, reads
