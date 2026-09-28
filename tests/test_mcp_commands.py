"""MCP commands: the ``/mcp:<service>`` menu entries, parsing and availability."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.web.chat_input import mcp_command_id
from easycode.web.main import create_app


def demo_server(**extra):
    server = {
        "command": sys.executable,
        "args": [str(Path(__file__).resolve().parent / "mcp_demo_server.py")],
    }
    server.update(extra)
    return server


def build(tmp_path, app_servers=None, project_servers=None):
    """An app with a default project A, a second project B, both registered."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.store import SessionStore
    from tests.conftest import FakeProvider

    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    cfg_raw = {"models": {"fake-a": "fake/a"}}
    if app_servers is not None:
        cfg_raw["mcp_servers"] = app_servers
    (tmp_path / "easycode.config.json").write_text(json.dumps(cfg_raw), encoding="utf-8")
    if project_servers is not None:
        (proj_a / "easycode.config.json").write_text(
            json.dumps({"mcp_servers": project_servers}), encoding="utf-8"
        )
    cfg = Config.load(start=tmp_path)
    cfg.root = proj_a
    cfg.workspace_projects = [{"root": str(proj_b), "secondary": [], "name": "Project B"}]

    def factory(alias: str = "fake-a", **kw):
        root = Path(kw["root"]).resolve() if kw.get("root") else proj_a
        return Agent(
            provider=FakeProvider(script=[{"text": "done"}]),
            registry=build_registry(8000),
            root=root,
            secondary_roots=[Path(p) for p in kw.get("secondary_roots") or []],
            mcp_servers=cfg.mcp_servers,
        )

    store = SessionStore(cfg, proj_a, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    return app, cfg, store, {"a": proj_a, "b": proj_b}


def commands(client, **params):
    r = client.get("/api/commands", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def mcp_rows(body) -> list[dict]:
    return [row for row in body["commands"] if row["kind"] == "mcp"]


# ------------------------------------------------------------------ the menu


def test_the_menu_lists_the_projects_own_effective_servers(tmp_path):
    app, _cfg, _store, roots = build(
        tmp_path, app_servers={"demo": {"command": "unused"}}
    )
    client = TestClient(app)

    body = commands(client)

    rows = mcp_rows(body)
    assert [row["name"] for row in rows] == ["mcp:demo"]
    row = rows[0]
    assert row["kind"] == "mcp"
    assert row["mcp_server"] == "demo"
    assert row["project_root"] == str(roots["a"])
    assert row["source"] == "app"
    assert row["argument_hint"] == "任务描述"
    assert row["id"] == mcp_command_id(str(roots["a"]), "demo")


def test_a_project_entry_replaces_the_application_one(tmp_path):
    """Three layers, whole-entry replacement: the project's command is the one."""
    app, _cfg, _store, _roots = build(
        tmp_path,
        app_servers={"demo": {"command": "app-command"}},
        project_servers={"demo": {"command": "project-command"}},
    )
    client = TestClient(app)

    rows = mcp_rows(commands(client))

    assert len(rows) == 1
    assert rows[0]["source"] == "project"
    assert rows[0]["source_label"].startswith("a")  # the folder name, until renamed
    # The menu never carries a credential, and never connects: it is a list of
    # what could be asked for.
    assert "app-command" not in json.dumps(rows)
    assert "project-command" not in json.dumps(rows)


def test_a_project_can_switch_off_an_application_server(tmp_path):
    app, _cfg, _store, _roots = build(
        tmp_path,
        app_servers={"demo": {"command": "app-command"}},
        project_servers={"demo": {"command": "unused", "enabled": False}},
    )
    client = TestClient(app)

    assert mcp_rows(commands(client)) == []


def test_the_servers_are_those_of_the_requested_project(tmp_path):
    app, _cfg, _store, roots = build(
        tmp_path,
        app_servers={"shared": {"command": "unused"}},
        project_servers={"local": {"command": "unused"}},
    )
    client = TestClient(app)

    in_a = mcp_rows(commands(client, root=str(roots["a"])))
    in_b = mcp_rows(commands(client, root=str(roots["b"])))

    assert [row["name"] for row in in_a] == ["mcp:local", "mcp:shared"]
    assert [row["name"] for row in in_b] == ["mcp:shared"]
    assert in_b[0]["project_root"] == str(roots["b"])
    assert in_b[0]["id"] != in_a[1]["id"]


def test_a_session_answers_with_its_own_projects_servers(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"shared": {"command": "unused"}})
    in_b = store.create(root=str(roots["b"]))
    client = TestClient(app)

    body = commands(client, session_id=in_b.id)

    assert mcp_rows(body)[0]["project_root"] == str(roots["b"])

    # Claiming another project for that conversation is refused, not ignored.
    conflict = client.get(
        "/api/commands", params={"session_id": in_b.id, "root": str(roots["a"])}
    )
    assert conflict.status_code == 409
    assert client.get("/api/commands", params={"session_id": "nope"}).status_code == 404


def test_an_unregistered_project_is_refused(tmp_path):
    app, _cfg, _store, _roots = build(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    client = TestClient(app)

    assert client.get("/api/commands", params={"root": str(outside)}).status_code == 422


def test_a_broken_config_is_reported_and_the_rest_still_listed(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    (roots["a"] / "easycode.config.json").write_text("{ not json", encoding="utf-8")
    (roots["a"] / ".easycode" / "commands").mkdir(parents=True)
    (roots["a"] / ".easycode" / "commands" / "hello.md").write_text(
        "---\ndescription: Say hello\n---\nHello $ARGUMENTS\n", encoding="utf-8"
    )
    client = TestClient(app)

    body = commands(client)

    assert body["errors"] and "无法读取" in body["errors"][0]
    assert "hello" in [row["name"] for row in body["commands"]]
    assert mcp_rows(body) == []


def test_a_command_under_the_reserved_prefix_is_reported_not_offered(tmp_path):
    app, _cfg, _store, roots = build(tmp_path)
    commands_dir = roots["a"] / ".easycode" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "mcp-demo.md").write_text(
        "---\nname: mcp:demo\ndescription: Looks like a service\n---\nbody\n", encoding="utf-8"
    )
    client = TestClient(app)

    body = commands(client)

    assert "mcp:demo" not in [row["name"] for row in body["commands"]]
    assert any("保留给 MCP 服务命令" in message for message in body["errors"])


def test_the_menu_carries_no_credential_value(tmp_path):
    app, _cfg, _store, _roots = build(
        tmp_path,
        app_servers={
            "demo": {
                "command": "unused",
                "secret_env": {"TOKEN": "token"},
                "env": {"PLAIN": "visible"},
            }
        },
    )
    from easycode.extensions.mcp.credentials import MCPCredential
    from easycode.extensions.mcp.credentials import store as credential_store

    credential_store().save(
        MCPCredential(id="c1", kind="env", scope="app", server="demo", values={"token": "s3cret"})
    )
    client = TestClient(app)

    body = client.get("/api/commands").text

    assert "s3cret" not in body


# ------------------------------------------------------------------- sending


def chat(client, session_id, message, **extra):
    return client.post("/api/chat", json={"session_id": session_id, "message": message, **extra})


def sent_prompt(store, session_id) -> str:
    """The last thing the model was given in this session."""
    sess = store.get(session_id)
    calls = sess.agent.provider.calls
    return calls[-1][-1]["content"]


@pytest.mark.skipif(sys.platform != "darwin", reason="MCP children run under the macOS sandbox")
def test_typing_the_command_sends_the_service_the_task_and_the_id(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    r = chat(client, sess.id, "/mcp:demo 把 2 和 3 相加")

    assert r.status_code == 200, r.text
    prompt = sent_prompt(store, sess.id)
    assert '"demo"' in prompt  # the service name, JSON-encoded
    assert "把 2 和 3 相加" in prompt
    # The transcript records what it was: the stable id, not a free-floating name.
    assert sess.turns[0]["command_id"] == mcp_command_id(str(roots["a"]), "demo")


def test_a_missing_task_is_refused_before_anything_is_created(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    client = TestClient(app)

    empty = client.post("/api/chat", json={"message": "/mcp:demo   ", "root": str(roots["a"])})

    assert empty.status_code == 422
    assert "任务" in empty.json()["detail"]
    # No session was created for a request that could never be answered.
    assert store.list() == []


def test_an_unknown_service_is_refused(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    r = chat(client, sess.id, "/mcp:nope do something")

    assert r.status_code == 400
    assert "nope" in r.json()["detail"]
    assert sess.turns == []


def test_a_disabled_service_is_not_offered_and_cannot_be_typed(tmp_path):
    app, _cfg, store, roots = build(
        tmp_path, app_servers={"demo": demo_server(enabled=False)}
    )
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    assert mcp_rows(commands(client, session_id=sess.id)) == []
    r = chat(client, sess.id, "/mcp:demo do something")

    assert r.status_code == 400
    assert sess.turns == []


def test_an_id_from_another_project_is_refused(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["b"]))
    client = TestClient(app)
    other_id = mcp_command_id(str(roots["a"]), "demo")

    r = chat(client, sess.id, "/mcp:demo do something", command_id=other_id)

    assert r.status_code == 409
    assert "另一个项目" in r.json()["detail"]
    assert sess.turns == []


def test_a_selection_that_became_stale_is_refused(tmp_path):
    """The menu was read before the service was switched off."""
    app, cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)
    stale = mcp_command_id(str(roots["a"]), "demo")
    assert mcp_rows(commands(client, session_id=sess.id))

    cfg.mcp_servers.clear()
    r = chat(client, sess.id, "/mcp:demo do something", command_id=stale)

    assert r.status_code == 400
    assert sess.turns == []


def test_a_broken_config_is_a_clear_error_not_an_empty_menu(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    (roots["a"] / "easycode.config.json").write_text("{ not json", encoding="utf-8")
    client = TestClient(app)

    r = chat(client, sess.id, "/mcp:demo do something")

    assert r.status_code == 422
    assert "无法读取" in r.json()["detail"]


def test_editing_keeps_the_command_while_the_command_is_the_same(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)
    chat(client, sess.id, "/mcp:demo 第一个任务")
    first = sess.turns[0]

    r = chat(
        client,
        sess.id,
        "/mcp:demo 第二个任务",
        edit_turn_id=first["id"],
        expected_revision=sess.revision,
    )

    assert r.status_code == 200, r.text
    assert len(sess.turns) == 1
    assert sess.turns[0]["command_id"] == mcp_command_id(str(roots["a"]), "demo")
    assert "第二个任务" in sess.turns[0]["model_input"]


def test_editing_into_plain_text_drops_the_command(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)
    chat(client, sess.id, "/mcp:demo 第一个任务")
    first = sess.turns[0]

    r = chat(
        client,
        sess.id,
        "改成普通消息",
        edit_turn_id=first["id"],
        expected_revision=sess.revision,
    )

    assert r.status_code == 200, r.text
    assert sess.turns[0]["command_id"] is None
    assert sent_prompt(store, sess.id) == "改成普通消息"


def test_switching_to_another_command_re_resolves_it(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    commands_dir = roots["a"] / ".easycode" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "note.md").write_text(
        "---\ndescription: Note\n---\nNote: $ARGUMENTS\n", encoding="utf-8"
    )
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)
    chat(client, sess.id, "/mcp:demo 第一个任务")
    first = sess.turns[0]

    r = chat(
        client,
        sess.id,
        "/note 换个命令",
        edit_turn_id=first["id"],
        expected_revision=sess.revision,
    )

    assert r.status_code == 200, r.text
    assert sess.turns[0]["command_id"] is None
    assert sent_prompt(store, sess.id) == "Note: 换个命令"


def test_the_recorded_command_survives_a_reload(tmp_path):
    app, _cfg, store, roots = build(tmp_path, app_servers={"demo": demo_server()})
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)
    chat(client, sess.id, "/mcp:demo 做一件事")

    detail = client.get(f"/api/sessions/{sess.id}").json()

    assert detail["turns"][0]["command_id"] == mcp_command_id(str(roots["a"]), "demo")


# --------------------------------------------------------------- availability


def test_asking_for_a_service_the_project_does_not_have_is_refused(tmp_path):
    app, _cfg, store, roots = build(tmp_path)
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    r = chat(client, sess.id, "/mcp:demo do something")

    # A service that is not configured here is a bad request, not a turn that
    # runs without it.
    assert r.status_code == 400
    assert sess.turns == []
    assert sess.agent.provider.calls == []


@pytest.mark.skipif(sys.platform != "darwin", reason="MCP children run under the macOS sandbox")
def test_a_service_that_cannot_connect_fails_the_turn_without_calling_the_model(tmp_path):
    app, _cfg, store, roots = build(
        tmp_path, app_servers={"demo": {"command": "/nonexistent/binary"}}
    )
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)
    provider = sess.agent.provider

    r = chat(client, sess.id, "/mcp:demo do something")

    assert r.status_code == 200, r.text
    assert '"code": "mcp_unavailable"' in r.text
    assert "连接失败" in r.text
    assert "turn_accepted" in r.text  # the turn exists, so the failure has a place
    assert provider.calls == []
    assert sess.turns[0]["status"] == "failed"
    # The error is kept with the turn, so a reload still shows why.
    assert sess.turns[0]["failures"][0]["code"] == "mcp_unavailable"


@pytest.mark.skipif(sys.platform != "darwin", reason="MCP children run under the macOS sandbox")
def test_a_failed_turn_that_asked_for_a_service_leaves_the_session_usable(tmp_path):
    app, _cfg, store, roots = build(
        tmp_path, app_servers={"demo": {"command": "/nonexistent/binary"}}
    )
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    asked = chat(client, sess.id, "/mcp:demo do something")
    assert '"code": "mcp_unavailable"' in asked.text
    assert sess.agent.provider.calls == []

    # A plain message in the same session still works: the failed turn did not
    # leave the session stuck or half-prepared.
    plain = chat(client, sess.id, "hello")
    assert plain.status_code == 200
    assert sess.agent.provider.calls
    assert sess.turns[-1]["status"] == "completed"


@pytest.mark.skipif(sys.platform != "darwin", reason="MCP children run under the macOS sandbox")
def test_an_unreachable_other_server_does_not_block_the_required_one(tmp_path):
    """A degraded neighbour is normal; only the asked-for service must work."""
    app, _cfg, store, roots = build(
        tmp_path,
        app_servers={"demo": demo_server(), "broken": {"command": "/nonexistent/binary"}},
    )
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    r = chat(client, sess.id, "/mcp:demo 统计一下")

    assert r.status_code == 200, r.text
    assert "mcp_unavailable" not in r.text
    assert sess.turns[0]["status"] == "completed"


@pytest.mark.skipif(sys.platform != "darwin", reason="MCP children run under the macOS sandbox")
def test_a_selected_service_with_no_tools_is_unavailable(tmp_path):
    app, _cfg, store, roots = build(
        tmp_path, app_servers={"demo": demo_server(enabled_tools=["nothing-matches"])}
    )
    sess = store.create(root=str(roots["a"]))
    client = TestClient(app)

    r = chat(client, sess.id, "/mcp:demo do something")

    assert '"code": "mcp_unavailable"' in r.text
    assert "没有提供任何工具" in r.text
    assert sess.agent.provider.calls == []


# --------------------------------------------------------- real subprocess


@pytest.mark.skipif(sys.platform != "darwin", reason="MCP children run under the macOS sandbox")
def test_a_selected_service_really_runs_its_tool(tmp_path):
    """End to end: the menu entry, the turn, the child process, its result."""
    from easycode.agent.loop import Agent
    from easycode.extensions.mcp.manager import mcp_tool_name
    from easycode.tools import build_registry
    from easycode.web.store import SessionStore
    from tests.conftest import FakeProvider

    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"models": {"fake-a": "fake/a"}, "mcp_servers": {"demo": demo_server()}}),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = proj
    tool = mcp_tool_name("demo", "add")

    def factory(alias: str = "fake-a", **kw):
        return Agent(
            provider=FakeProvider(
                script=[
                    {"text": "", "tool_calls": [("t1", tool, {"a": 2, "b": 3})]},
                    {"text": "结果是 5"},
                ]
            ),
            registry=build_registry(8000),
            root=proj,
            mcp_servers=cfg.mcp_servers,
            permission_mode="allow-all",
        )

    store = SessionStore(cfg, proj, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    # Created after the app: building it loads the sessions already on disk and
    # replaces the in-memory objects with the restored ones.
    # Full access, stated on the session: the store stamps the config's default
    # mode over the agent's, and an MCP tool without a read-only annotation
    # would otherwise wait for an approval nobody is here to give.
    sess = store.create(root=str(proj), permission_mode="allow-all")
    # The context manager runs the lifespan shutdown, which is what reaps the
    # child process this test really starts.
    with TestClient(app) as client:
        assert mcp_rows(commands(client, session_id=sess.id))[0]["mcp_server"] == "demo"

        r = chat(client, sess.id, "/mcp:demo 把 2 和 3 相加")

        assert r.status_code == 200, r.text
        # The child really ran: its tool call is streamed under the name the
        # model used, and its answer is in the recorded reply.
        assert tool in r.text
        answer = json.dumps(sess.turns[0]["messages"], ensure_ascii=False)
        assert "结果是 5" in answer
        assert sess.turns[0]["status"] == "completed"
        # The child's own answer is in the record as the tool's result, not just
        # a call the model claimed to have made.
        results = [
            row for row in sess.turns[0]["messages"] if row.get("role") == "tool"
        ]
        assert any('"content": "5"' in row.get("content", "") for row in results)


def test_the_id_is_percent_encoded_so_a_path_cannot_be_forged(tmp_path):
    """The id is one opaque token; nothing in it can be re-split by a caller."""
    weird_root = str(tmp_path / "a:b" / "c d")
    sid = mcp_command_id(weird_root, "demo:two")

    assert sid == f"mcp:{quote(weird_root, safe='')}:{quote('demo:two', safe='')}"
    assert sid.count(":") == 2
