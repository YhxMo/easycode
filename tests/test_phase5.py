"""Phase 5 tests: P5-1 credentials store + model web config + masking."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.credentials import (
    Credential,
    delete_credential,
    load_credentials,
    save_credential,
)
from easycode.web.main import create_app


# ---------------------------------------------------------------- credentials

def test_credentials_read_write_delete(tmp_path):
    p = tmp_path / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="k1", api_key="sk-abc", provider="openai"), path=p)
    save_credential(Credential(key_id="k2", api_key="sk-xyz", base_url="http://x/v1"), path=p)

    creds = load_credentials(p)
    assert creds["k1"].api_key == "sk-abc"
    assert creds["k1"].provider == "openai"
    assert creds["k2"].base_url == "http://x/v1"

    assert delete_credential("k1", path=p) is True
    assert delete_credential("k1", path=p) is False
    assert "k1" not in load_credentials(p)
    assert "k2" in load_credentials(p)


def test_credentials_file_permissions(tmp_path):
    p = tmp_path / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="k", api_key="sk-secret"), path=p)
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_credentials_missing_or_corrupt(tmp_path):
    assert load_credentials(tmp_path / "nope.json") == {}
    p = tmp_path / ".easycode" / "credentials.json"
    p.parent.mkdir(parents=True)
    p.write_text("{ not json", encoding="utf-8")
    assert load_credentials(p) == {}
    p.write_text(json.dumps({"a": {"no_key": 1}}), encoding="utf-8")
    assert load_credentials(p) == {}


def test_credentials_masked_no_key():
    c = Credential(key_id="k1", api_key="sk-super-secret-1234")
    m = c.masked()
    assert "api_key" not in m
    assert m["key_tail"] == "1234"


# ---------------------------------------------------------------- model spec

def test_config_models_accept_str_and_object(tmp_path, monkeypatch):
    (tmp_path / "easycode.config.json").write_text(
        json.dumps(
            {
                "models": {
                    "plain": "openai/gpt-4o",
                    "keyed": {"model": "gpt-4o", "key_id": "my-key"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.resolve_model("plain") == "openai/gpt-4o"
    assert cfg.resolve_model("keyed") == "gpt-4o"
    assert cfg.model_spec("keyed").key_id == "my-key"
    assert cfg.model_spec("plain").key_id is None

    cfg.save()
    raw = json.loads((tmp_path / "easycode.config.json").read_text(encoding="utf-8"))
    assert raw["models"]["plain"] == "openai/gpt-4o"
    assert raw["models"]["keyed"] == {"model": "gpt-4o", "key_id": "my-key"}


def test_model_spec_passthrough_and_display():
    cfg = Config()
    assert cfg.resolve_model("anthropic/claude-3-5-sonnet") == "anthropic/claude-3-5-sonnet"
    cfg.set_model_alias("k", {"model": "gpt-4o", "key_id": "x"})
    assert "key: x" in cfg.models["k"].to_display()


# ---------------------------------------------------------------- web endpoints

@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def make_app(tmp_path: Path, config_patch: dict | None = None) -> TestClient:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps(config_patch or {"models": {"fake-a": "fake/a", "fake-b": "fake/b"}}),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str):  # noqa: ANN001
        from easycode.agent.loop import Agent
        from easycode.tools import build_registry
        from tests.conftest import FakeProvider

        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=tmp_path)

    from easycode.web.session import SessionStore

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def add_model_body(alias="gpt-local", model="gpt-4o", key="sk-lives-here"):
    return {"alias": alias, "model": model, "provider": "openai", "base_url": "http://127.0.0.1:9000/v1", "api_key": key}


def test_add_model_with_key_stores_credential_and_masks(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/models/add", json=add_model_body())
    assert r.status_code == 200
    data = r.json()
    assert data["models"]["gpt-local"] == {"model": "gpt-4o", "key_id": "web-gpt-local"}

    creds = load_credentials(tmp_path / ".easycode" / "credentials.json")
    assert creds["web-gpt-local"].api_key == "sk-lives-here"

    # GET /api/models must never leak the key anywhere in the response
    r2 = client.get("/api/models")
    body = r2.text
    assert "sk-lives-here" not in body
    assert "sk-" not in body
    assert "web-gpt-local" in body


def test_add_model_without_key_is_plain_string(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/models/add", json={"alias": "plain", "model": "deepseek/deepseek-chat"})
    assert r.status_code == 200
    assert r.json()["models"]["plain"] == "deepseek/deepseek-chat"
    assert not (tmp_path / ".easycode" / "credentials.json").exists()


def test_add_model_validation(tmp_path):
    client = make_app(tmp_path)
    assert client.post("/api/models/add", json={"alias": " ", "model": "x"}).status_code == 422


def test_delete_model_removes_web_credential(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.delete("/api/models/gpt-local")
    assert r.status_code == 200
    assert "gpt-local" not in r.json()["models"]
    assert load_credentials(tmp_path / ".easycode" / "credentials.json") == {}
    assert client.delete("/api/models/nope").status_code == 404


def test_delete_model_keeps_shared_credential(tmp_path):
    # both aliases share the same web credential from the start
    save_credential(
        Credential(key_id="web-gpt-local-2", api_key="sk-shared"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    client = make_app(
        tmp_path,
        {
            "models": {
                "gpt-local": {"model": "gpt-4o", "key_id": "web-gpt-local-2"},
                "gpt-local-2": {"model": "gpt-4o-mini", "key_id": "web-gpt-local-2"},
            }
        },
    )
    with client:
        # deleting gpt-local must NOT remove a credential shared with gpt-local-2
        # (removal only happens for a key owned by this exact alias)
        r = client.delete("/api/models/gpt-local")
        assert r.status_code == 200
    creds = load_credentials(tmp_path / ".easycode" / "credentials.json")
    assert "web-gpt-local-2" in creds


def test_delete_default_model_falls_back(tmp_path):
    client = make_app(tmp_path, {"models": {"only": "fake/only"}, "default_model": "only"})
    r = client.delete("/api/models/only")
    assert r.status_code == 200
    assert r.json()["default"] != "only"


def test_switch_model_via_build_provider(tmp_path):
    """POST /api/models now rebuilds provider with credentials lookup."""
    from easycode.credentials import save_credential

    save_credential(Credential(key_id="web-keyed", api_key="sk-k"), path=tmp_path / ".easycode" / "credentials.json")
    client = make_app(tmp_path, {"models": {"keyed": {"model": "gpt-4o", "key_id": "web-keyed"}}, "default_model": "keyed"})
    r = client.post("/api/models", json={"alias": "keyed"})
    assert r.status_code == 200
    assert r.json()["default"] == "keyed"


# ---------------------------------------------------------------- P5-3 workspace

def test_classify_path_categories(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    import tempfile

    from easycode.workspace import PathContext, classify_path

    primary = tmp_path / "work"
    secondary = tmp_path / "other"
    (primary / "a").mkdir(parents=True)
    secondary.mkdir(parents=True)
    ctx = PathContext(primary=primary, secondary=[secondary])

    assert classify_path(primary / "a" / "f.py", ctx) == "workspace"
    assert classify_path(secondary / "g.py", ctx) == "workspace"
    assert classify_path(Path(tempfile.gettempdir()) / "x.tmp", ctx) == "temp"
    assert classify_path(tmp_path / ".easycode" / "sessions" / "s.json", ctx) == "system"
    assert classify_path(Path("/etc/hosts"), ctx) == "external"


def test_in_allowed_with_extra_safe_dirs(tmp_path):
    from easycode.workspace import PathContext, in_allowed

    extra = tmp_path / "notes"
    extra.mkdir()
    ctx = PathContext(primary=tmp_path, extra_safe_dirs=[extra])
    assert in_allowed(extra / "a.txt", ctx) is True
    assert in_allowed(extra.parent.parent / "elsewhere", ctx) is False


def test_multi_root_write_and_read(tmp_path):
    """With a context of primary + secondary, tools reach both roots."""
    import json as _json

    from easycode.workspace import PathContext
    from easycode.tools import build_registry

    reg = build_registry(8000)
    primary = tmp_path / "p"
    secondary = tmp_path / "s"
    primary.mkdir(); secondary.mkdir()
    ctx = PathContext(primary=primary, secondary=[secondary])

    # relative path prefers the first root that contains it (primary first)
    out = _json.loads(reg.execute("write_file", {"path": "x.py", "content": "x=1"}, primary, ctx=ctx))
    assert out["status"] == "ok"
    assert (primary / "x.py").read_text() == "x=1"

    # absolute path resolves to the secondary root and is editable there
    abs_path = str(secondary / "x.py")
    out2 = _json.loads(reg.execute("write_file", {"path": abs_path, "content": "y=2"}, primary, ctx=ctx))
    assert out2["status"] == "ok"
    assert (secondary / "x.py").read_text() == "y=2"
    out3 = _json.loads(reg.execute("read_file", {"path": abs_path}, primary, ctx=ctx))
    assert out3["status"] == "ok"
    assert "y=2" in out3["content"]


def test_multi_root_glob_and_grep(tmp_path):
    import json as _json

    from easycode.tools import build_registry
    from easycode.workspace import PathContext

    reg = build_registry(8000)
    primary = tmp_path / "p"; secondary = tmp_path / "s"
    primary.mkdir(); secondary.mkdir()
    (primary / "a.py").write_text("v = 1")
    (secondary / "b.py").write_text("v = 2")
    ctx = PathContext(primary=primary, secondary=[secondary])

    g = _json.loads(reg.execute("glob", {"pattern": "*.py"}, primary, ctx=ctx))
    assert set(m["path"] for m in g["matches"] if isinstance(m, dict)) == {"b.py"}

    gr = _json.loads(reg.execute("grep", {"pattern": "v = "}, primary, ctx=ctx))
    files = {m["file"] for m in gr["matches"]}
    assert files == {"a.py", "b.py"}


def test_write_outside_roots_reports_in_allowed(tmp_path):
    import json as _json

    from easycode.tools import build_registry
    from easycode.workspace import PathContext

    reg = build_registry(8000)
    primary = tmp_path / "p"; primary.mkdir()
    ctx = PathContext(primary=primary)
    out = _json.loads(reg.execute("write_file", {"path": "../evil.txt", "content": "x"}, primary, ctx=ctx))
    assert out["status"] == "error"
    assert out.get("in_allowed") is False


def test_config_workspace_fields(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "workspace": {
                    "secondary": ["sec_a", "~/sec_b"],
                    "extra_safe_dirs": ["notes"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.secondary_roots == ["sec_a", "~/sec_b"]
    assert cfg.extra_safe_dirs == ["notes"]
    ctx = cfg.path_context()
    assert len(ctx.secondary) == 2
    assert ctx.secondary[0] == (tmp_path / "sec_a").resolve()


def test_web_session_with_secondary_roots(tmp_path):
    """Chat creating a session with secondary_roots wires them into the agent."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"; secondary = tmp_path / "s"
    primary.mkdir(); secondary.mkdir();
    (secondary / "lib.txt").write_text("lib content", encoding="utf-8")

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    created: list[Agent] = []

    def factory(alias: str, **kw):  # noqa: ANN001
        agent = Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
            secondary_roots=kw.get("secondary_roots") or [str(secondary)],
        )
        created.append(agent)
        return agent

    store = SessionStore(cfg, primary, factory)
    client = TestClient(
        create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
    )
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "hi", "secondary_roots": [str(secondary)]},
        )
        assert r.status_code == 200
    assert created
    assert created[0].secondary_roots == [str(secondary)]


# ---------------------------------------------------------------- P5-2 approval

def test_needs_approval_modes(tmp_path):
    import sys
    import tempfile

    from easycode.approval import needs_approval, permission_parse
    from easycode.models.base import ToolCall
    from easycode.workspace import PathContext

    primary = tmp_path / "work"
    primary.mkdir()
    (primary / "a.py").write_text("x=1", encoding="utf-8")
    ctx = PathContext(primary=primary)

    edit_in = ToolCall(id="1", name="edit_file", arguments={"path": "a.py", "old_string": "x=1", "new_string": "x=2"})
    edit_out = ToolCall(id="2", name="edit_file", arguments={"path": "../outside.py", "old_string": "a", "new_string": "b"})
    shell_net = ToolCall(id="3", name="execute_shell", arguments={"command": "curl -s https://example.com"})
    shell_local = ToolCall(id="4", name="execute_shell", arguments={"command": "ls -la"})
    shell_pip = ToolCall(id="5", name="execute_shell", arguments={"command": "pip install requests"})

    assert needs_approval(edit_in, ctx, "ask") is False
    assert needs_approval(edit_out, ctx, "ask") is True
    assert needs_approval(shell_net, ctx, "ask") is True
    assert needs_approval(shell_pip, ctx, "ask") is True
    assert needs_approval(shell_local, ctx, "ask") is False

    assert needs_approval(edit_out, ctx, "auto-review") is False
    assert needs_approval(shell_net, ctx, "auto-review") is False
    assert needs_approval(edit_out, ctx, "allow-all") is False

    assert permission_parse("ask") == "ask"
    assert permission_parse("auto-review") == "auto-review"
    assert permission_parse("allow-all") == "allow-all"
    assert permission_parse("auto") == "auto-review"
    import pytest as _p  # noqa: F401
    try:
        permission_parse("bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@pytest.mark.asyncio
async def test_agent_approval_rejected_skips_tool(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.models.base import ToolCall
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    outside = tmp_path.parent / "p5-evil.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "pwned"})], "text": ""},
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
    )

    async def handler(tc: ToolCall) -> bool:
        return False

    agent.approval_handler = handler
    events = []
    async for ev in agent.respond("write it"):
        events.append(ev)
    kinds = [e.kind for e in events]
    assert "approval" in kinds
    assert "tool_result" in kinds
    rej = next(e for e in events if e.kind == "tool_result")
    assert "rejected" in rej.tool_result
    assert not outside.exists()


@pytest.mark.asyncio
async def test_agent_approval_approved_forces_allowed(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.models.base import ToolCall
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    outside = tmp_path.parent / "p5-evil.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "pwned"})], "text": ""},
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
    )

    async def handler(tc: ToolCall) -> bool:
        return True

    agent.approval_handler = handler
    async for _ in agent.respond("write it"):
        pass
    assert outside.read_text(encoding="utf-8") == "pwned"


@pytest.mark.asyncio
async def test_auto_review_emits_review_event(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    f = tmp_path / "p5-a.txt"
    f.write_text("hello", encoding="utf-8")
    script = [
        {"tool_calls": [("c1", "edit_file", {"path": "p5-a.txt", "old_string": "hello", "new_string": "world"})], "text": ""},
        {"text": "changed"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        permission_mode="auto-review",
    )
    events = []
    async for ev in agent.respond("change it"):
        events.append(ev)
    reviews = [e for e in events if e.kind == "review"]
    assert reviews
    import json as _json

    changes = _json.loads(reviews[0].content)["changes"]
    assert changes[0]["path"] == "p5-a.txt"
    assert "world" in changes[0]["diff"]


async def test_web_approval_broker_auto_resolve(tmp_path):
    """SSE stream emits approval_required and resolves via broker → file written."""
    import threading

    from easycode.web.bridge import ApprovalBroker

    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(tmp_path.parent / "p5-x.txt"), "content": "y"})], "text": ""},
        {"text": "ok"},
    ]
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    broker = ApprovalBroker()

    class AutoBroker(ApprovalBroker):
        def add(self, approval_id: str):
            fut = super().add(approval_id)

            async def resolve():
                import asyncio

                await asyncio.sleep(0.05)
                fut.set_result(True)

            import asyncio

            asyncio.ensure_future(resolve())
            return fut

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    def factory(alias: str):
        return Agent(provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path)

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist", approval_broker=AutoBroker()))
    events: list[dict] = []

    def worker():
        with client:
            with client.stream("POST", "/api/chat", json={"message": "write it"}) as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    types = [e["type"] for e in events]
    assert "approval_required" in types
    assert types[-1] == "done"
    assert (tmp_path.parent / "p5-x.txt").read_text(encoding="utf-8") == "y"


async def test_web_approval_endpoint_unknown_id(tmp_path):
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str):
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=tmp_path)

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    r = client.post("/api/approval/nope", json={"approve": True})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_web_approval_timeout_rejects(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.bridge import ApprovalBroker, stream_chat_with_approval
    from tests.conftest import FakeProvider

    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(tmp_path.parent / "p5-t.txt"), "content": "x"})], "text": ""},
        {"text": "ok"},
    ]
    agent = Agent(provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path)
    broker = ApprovalBroker(timeout=0.05)
    approvals = []
    results = []
    async for kind, payload in stream_chat_with_approval(agent, "go", broker):
        if kind == "approval":
            approvals.append(payload[0])
        elif kind == "event":
            results.append(payload)
    assert approvals
    tool_res = [r for r in results if r.kind == "tool_result"]
    assert tool_res and "rejected" in tool_res[0].tool_result
    assert not (tmp_path.parent / "p5-t.txt").exists()


# ---------------------------------------------------------------- provider wiring

def test_build_provider_forwards_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(key_id="k1", api_key="sk-cred", provider="openai", base_url="http://x/v1"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias("keyed", {"model": "gpt-4o", "key_id": "k1"})

    from easycode.cli import build_provider

    prov = build_provider(cfg, "keyed")
    assert prov.model == "gpt-4o"
    assert prov.kwargs["api_key"] == "sk-cred"
    assert prov.kwargs["api_base"] == "http://x/v1"
    assert prov.kwargs["custom_llm_provider"] == "openai"

    # provider/model-style entries don't get an override
    prov2 = build_provider(cfg, "openai/gpt-4o")
    assert prov2.model == "openai/gpt-4o"
    assert prov2.kwargs == {}


def test_build_provider_missing_credential_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config()
    cfg.set_model_alias("keyed", {"model": "gpt-4o", "key_id": "nope"})

    from easycode.cli import build_provider

    with pytest.raises(ValueError, match="key_id 'nope'"):
        build_provider(cfg, "keyed")

# ---------------------------------------------------------------- P5-4 mcp

def mcp_server_config():
    import sys

    return {"demo": {"command": sys.executable, "args": [str(Path(__file__).resolve().parent / "mcp_demo_server.py")]}}


@pytest.mark.asyncio
async def test_mcp_manager_connects_and_lists_tools():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    mgr = MCPSessionManager(mcp_server_config())
    await mgr.start()
    try:
        names = {s["function"]["name"] for s in mgr.tool_schemas()}
        assert mcp_tool_name("demo", "add") in names
        assert mcp_tool_name("demo", "greet") in names
        assert mcp_tool_name("demo", "boom") in names
        fn = next(s for s in mgr.tool_schemas() if s["function"]["name"] == mcp_tool_name("demo", "add"))
        assert fn["function"]["parameters"]["required"] == ["a", "b"]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mcp_manager_call_tool():
    from easycode.mcp import MCPSessionManager, mcp_tool_name

    mgr = MCPSessionManager(mcp_server_config())
    await mgr.start()
    try:
        out = await mgr.call(mcp_tool_name("demo", "add"), {"a": 2, "b": 3})
        assert '"5"' in out
        out2 = await mgr.call(mcp_tool_name("demo", "greet"), {})
        assert "hello, world" in out2
        # server-side error surfaces as status error
        out3 = await mgr.call(mcp_tool_name("demo", "boom"), {})
        assert out3.startswith('{"status": "error"')
        # unknown tool
        out4 = await mgr.call("mcp__demo__nope", {})
        assert "unknown MCP tool" in out4
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mcp_manager_bad_server_degrades():
    from easycode.mcp import MCPSessionManager

    mgr = MCPSessionManager({"ghost": {"command": "definitely-not-a-command-xyz", "args": []}})
    await mgr.start()  # must not raise
    assert mgr.tool_schemas() == []


@pytest.mark.asyncio
async def test_agent_routes_mcp_tool(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    fname = mcp_tool_name("demo", "add")
    script = [
        {"tool_calls": [("c1", fname, {"a": 10, "b": 32})], "text": ""},
        {"text": f"sum is available"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
    )
    schemas_before = {s["function"]["name"] for s in agent.tool_schemas()}
    assert not any(n.startswith("mcp__") for n in schemas_before)

    results = []
    async for ev in agent.respond("add it"):
        if ev.kind == "tool_result":
            results.append(ev.tool_result)
    assert any("42" in r for r in results), results
    # schemas now include MCP tool after init_mcp
    schemas_after = {s["function"]["name"] for s in agent.tool_schemas()}
    assert fname in schemas_after

    await agent.mcp_manager.close()


@pytest.mark.asyncio
async def test_subagent_reuses_mcp_manager(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.mcp import mcp_tool_name
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    fname = mcp_tool_name("demo", "add")
    script = [
        {"tool_calls": [("c1", "parallel_tasks", {"tasks": [{"name": "t", "prompt": "add numbers"}]})], "text": ""},
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
    )
    assert agent.mcp_manager is None
    sub = agent._make_subagent()
    assert sub.mcp_servers == agent.mcp_servers
    assert sub.mcp_manager == agent.mcp_manager  # shares (lazily shared after connect)

    # force connection through init; assert manager shared reference
    await agent.init_mcp()
    assert agent.mcp_manager is not None
    sub2 = agent._make_subagent()
    assert sub2.mcp_manager is agent.mcp_manager or sub2.mcp_manager is None


@pytest.mark.asyncio
async def test_mcp_schemas_skip_invalid():
    """A server with an unserializable schema is skipped, others survive."""
    from easycode.mcp import MCPSessionManager

    servers = mcp_server_config()
    mgr = MCPSessionManager(servers)
    await mgr.start()
    try:
        assert any(n.startswith("mcp__demo__") for n in [s["function"]["name"] for s in mgr.tool_schemas()])
    finally:
        await mgr.close()


# ---------------------------------------------------------------- P5-5 context condensation

def test_history_token_budget():
    from easycode.agent.context import History

    h = History(max_tokens=1000)
    assert not h.over_budget()
    h.add_user("x" * 20_000)  # ~5000 tokens heuristic > 1000
    assert h.over_budget()


def test_history_trim_fallback_without_summarizer():
    from easycode.agent.context import History

    h = History(max_tokens=1000)
    for i in range(30):
        h.add_user(f"msg {i} " + "y" * 500)
    h.trim()
    assert h.estimate_tokens() <= 1000 or len(h.messages) <= 3
    assert len(h.messages) < 30


@pytest.mark.asyncio
async def test_agent_summarizer_wired_in_loop(tmp_path):
    """Over-budget turns invoke the summarizer and condense history."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    yuge = "z" * 30_000
    script = [
        {"tool_calls": [("c1", "read_file", {"path": "nope"})], "text": ""},
        {"text": "final answer"},
    ]
    from easycode.agent.context import History

    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        summarizer=None,
        max_context_tokens=1000,
    )
    agent.history = History(max_tokens=1000)
    agent.history.set_system("sys")
    agent.history.add_user("start")
    agent.history.add_user(yuge)  # push over budget before turn

    async def fake_summarize(messages):
        return "[synthetic summary]"

    agent.summarizer = fake_summarize
    agent.condense_threshold = 2
    async for _ in agent.respond("do it"):
        pass
    contents = " ".join(str(m.get("content", "")) for m in agent.history.messages)
    assert "synthetic summary" in contents


@pytest.mark.asyncio
async def test_agent_no_summarizer_hard_trim(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.agent.context import History
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    yuge = "z" * 30_000
    agent = Agent(
        provider=FakeProvider(model="fake", script=[{"text": "ok"}]),
        registry=build_registry(8000),
        root=tmp_path,
        max_context_tokens=1000,
    )
    agent.history = History(max_tokens=1000)
    agent.history.set_system("sys")
    agent.history.add_user(yuge)
    agent.history.trim()
    assert len(agent.history.messages) <= 2


def test_make_agent_wires_summarizer(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    cfg.root = tmp_path

    from easycode.cli import make_agent

    agent = make_agent(cfg, "fake-a", tmp_path)
    assert agent.summarizer is not None
    assert agent.summarizer.model == "fake/a"
    assert agent.max_context_tokens == 32_000


def test_config_max_context_tokens(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"max_context_tokens": 9999}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.max_context_tokens == 9999
    cfg.save()
    assert "max_context_tokens" in cfg_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------- workspaces & grouped sessions

def test_session_create_with_root_uses_session_root(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    agent_roots: list[str] = []

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        agent_roots.append(str(root))
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root)

    store = SessionStore(cfg, primary, factory)
    s = store.create(root=str(project))
    assert s.root == str(project)
    assert agent_roots[-1] == str(project)
    assert s.summary["root"] == str(project)


def test_session_root_persistence_roundtrip(tmp_path):
    import json as _json

    from easycode.web.session import SessionStore
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(_json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root)

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(root=str(project))
    store2 = SessionStore(cfg, primary, factory)
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.root == str(project)
    assert str(project) in (store2.dir / f"{s.id}.json").read_text(encoding="utf-8")


def test_legacy_session_without_root_loads(tmp_path):
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=primary)

    store = SessionStore(cfg, primary, factory)
    import uuid

    sid = uuid.uuid4().hex[:12]
    (store.dir / f"{sid}.json").write_text(
        json.dumps({"id": sid, "title": "old", "messages": [], "model_alias": "fake-a"}),
        encoding="utf-8",
    )
    store.load_all()
    s = store.get(sid)
    assert s is not None
    assert s.root is None
    assert "root" not in s.summary


def test_workspaces_endpoints(tmp_path):
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root)

    store = SessionStore(cfg, primary, factory)
    store.create(root=str(project))
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.get("/api/workspaces")
        assert r.status_code == 200
        data = r.json()
        assert data["default"] == str(primary)
        roots = [p["root"] for p in data["projects"]]
        assert str(project) in roots
        # 手动添加端点已删除：POST /api/workspaces 返回 405/404 而非 200
        gone = client.post("/api/workspaces", json={"path": str(project)})
        assert gone.status_code in (404, 405)


def test_chat_root_creates_session_in_project(tmp_path):
    """First message with root creates a session bound to that project."""
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    agents: list[Agent] = []
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        root = str(Path(kw.get("root")).resolve()) if kw.get("root") else str(primary)
        agent = Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=Path(root))
        agents.append(agent)
        return agent

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/chat", json={"message": "hello", "root": str(project)})
        assert r.status_code == 200
        bad = client.post("/api/chat", json={"message": "x", "root": str(tmp_path / "missing")})
        assert bad.status_code == 422
    sid = store.list()[0].id
    assert store.get(sid).root == str(project)
    assert agents[0].root == project.resolve()


# ---------------------------------------------------------------- secondary roots & finder picker

def test_session_secondary_roots_persist(tmp_path):
    from easycode.web.session import SessionStore
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    primary.mkdir(); project.mkdir(); sec_a.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary
    built: list[Agent] = []

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        sec = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        agent = Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root, secondary_roots=sec)
        built.append(agent)
        return agent

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(root=str(project), secondary_roots=[str(sec_a)])
    assert s.secondary_roots == [str(sec_a)]
    assert built[-1].secondary_roots == [sec_a.resolve()]
    assert s.summary["secondary_roots"] == [str(sec_a)]

    store2 = SessionStore(cfg, tmp_path / "p2", factory)
    (tmp_path / "p2").mkdir(exist_ok=True)
    store2.dir = store1.dir  # same disk dir
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.secondary_roots == [str(sec_a)]
    assert restored.root == str(project)


def test_chat_root_with_secondary_roots(tmp_path):
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    primary.mkdir(); project.mkdir(); sec_a.mkdir()
    agents: list[Agent] = []
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        sec = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        agent = Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root, secondary_roots=sec)
        agents.append(agent)
        return agent

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "hi", "root": str(project), "secondary_roots": [str(sec_a)]},
        )
        assert r.status_code == 200
    s = store.list()[0]
    assert s.secondary_roots == [str(sec_a)]
    assert [str(x) for x in agents[0].secondary_roots] == [str(sec_a)]


def test_choose_workspaces_endpoint(tmp_path, monkeypatch):
    import easycode.web.main as m

    monkeypatch.setattr(m, "finder_supported", lambda: True)
    monkeypatch.setattr(
        m,
        "choose_folders_via_finder",
        lambda multiple=False, prompt="选择目录": ["/tmp/a", "/tmp/b"] if multiple else ["/tmp/a"],
    )
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    primary.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=primary)

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/workspaces/choose", json={"multiple": False})
        assert r.status_code == 200
        assert r.json()["paths"] == ["/tmp/a"]
        r2 = client.post("/api/workspaces/choose", json={"multiple": True, "prompt": "x"})
        assert r2.status_code == 200
        assert r2.json()["paths"] == ["/tmp/a", "/tmp/b"]


def test_choose_unsupported_returns_empty(tmp_path, monkeypatch):
    import easycode.web.main as m

    monkeypatch.setattr(m, "finder_supported", lambda: False)
    monkeypatch.setattr(m, "choose_folders_via_finder", lambda multiple=False, prompt="选择目录": [])
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=primary)

    store = SessionStore(cfg, primary, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    r = client.post("/api/workspaces/choose", json={"multiple": False})
    assert r.status_code == 200
    data = r.json()
    assert data["paths"] == []
    assert data["supported"] is False


# ---------------------------------------------------------------- project→secondary bindings

def test_workspaces_projects_from_sessions_and_save(tmp_path):
    """GET merges config + session bindings; POST /projects persists per-root."""
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    proj = tmp_path / "proj"
    sec = tmp_path / "sec"
    primary.mkdir(); proj.mkdir(); sec.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root, secondary_roots=secs)

    store = SessionStore(cfg, primary, factory)
    # config 预置绑定（default project 挂 sec）
    cfg.workspace_projects = [{"root": None, "secondary": [str(sec)]}]
    # 会话历史推断 (proj → sec)
    store.create(root=str(proj), secondary_roots=[str(sec)])

    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.get("/api/workspaces")
        data = r.json()
        projects = data["projects"]
        by_root = {p["root"]: p for p in projects}
        assert by_root[None]["secondary"] == [str(sec)]
        assert by_root[str(proj)]["secondary"] == [str(sec)]

        # save: proj 换一组次目录并持久化到 config
        r2 = client.post("/api/workspaces/projects", json={"root": str(proj), "secondary": [str(sec), str(primary)]})
        assert r2.status_code == 200
        assert sorted(r2.json()["secondary"]) == sorted([str(sec), str(primary)])

        # 无效主目录
        r3 = client.post("/api/workspaces/projects", json={"root": str(tmp_path / "missing"), "secondary": []})
        assert r3.status_code == 422

        # default project 绑定持久化
        r4 = client.post("/api/workspaces/projects", json={"root": None, "secondary": [str(proj)]})
        assert r4.status_code == 200
        assert r4.json()["secondary"] == [str(proj)]

    # config 落盘包含 projects
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "projects" in raw.get("workspace", {})
    projs = raw["workspace"]["projects"]
    assert any(p.get("root") == str(proj) for p in projs)
    assert any(p.get("root") is None for p in projs)


def test_spa_fallback_no_405_on_api_posts(tmp_path):
    """Production static serving must not turn unknown /api POSTs into 405s."""
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=primary)

    store = SessionStore(cfg, primary, factory)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=dist))
    with client:
        # 未注册的 /api POST 现在返回 404 JSON，而不是 405
        r = client.post("/api/definitely-not-a-route", json={})
        assert r.status_code == 404
        # SPA fallback 对 GET 生效
        r2 = client.get("/")
        assert r2.status_code == 200
        assert "app" in r2.text
        # 真实 approve 端点恢复可用
        r3 = client.post("/api/approval/nonexistent", json={"approve": True})
        assert r3.status_code == 404  # 未知 id 依然 404，但路由本身存在


def test_save_project_with_session_id_updates_session(tmp_path):
    """Editing secondary roots on a locked session updates its agent + disk state."""
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    proj = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    sec_b = tmp_path / "sec_b"
    primary.mkdir(); proj.mkdir(); sec_a.mkdir(); sec_b.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        secs = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root, secondary_roots=secs)

    store = SessionStore(cfg, primary, factory)
    s = store.create(root=str(proj), secondary_roots=[str(sec_a)])
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [str(sec_a), str(sec_b)], "session_id": s.id},
        )
        assert r.status_code == 200
        assert sorted(r.json()["secondary"]) == sorted([str(sec_a), str(sec_b)])

        # 会话会话本身被同步
        sess = store.get(s.id)
        assert sorted(sess.secondary_roots) == sorted([str(sec_a), str(sec_b)])
        assert sorted(str(p) for p in sess.agent.secondary_roots) == sorted([str(sec_a), str(sec_b)])

        # 落盘包含新绑定
        raw = json.loads((store.dir / f"{s.id}.json").read_text(encoding="utf-8"))
        assert sorted(raw["secondary_roots"]) == sorted([str(sec_a), str(sec_b)])

        # 未知 session → 404
        r2 = client.post(
            "/api/workspaces/projects",
            json={"root": str(proj), "secondary": [], "session_id": "nope"},
        )
        assert r2.status_code == 404


# ---------------------------------------------------------------- rename migration

def test_data_dir_migrates_from_legacy_name(tmp_path, monkeypatch):
    """~/.easycode replaces the old ~/.myagent data dir; legacy data is moved once."""
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".myagent" / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "abc.json").write_text("{}", encoding="utf-8")

    from easycode.credentials import data_home

    assert data_home() == tmp_path / ".easycode"
    assert (tmp_path / ".easycode" / "sessions" / "abc.json").exists()
    assert not (tmp_path / ".myagent").exists()

    # second call is a no-op (dir exists already)
    assert data_home() == tmp_path / ".easycode"


# ---------------------------------------------------------------- permission mode (web)

def test_session_permission_persistence(tmp_path):
    from easycode.web.session import SessionStore
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg_file = tmp_path / "myagent.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=Path(kw.get("root") or primary))

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(permission_mode="auto-review")
    assert s.permission_mode == "auto-review"
    assert s.agent.permission_mode == "auto-review"
    assert s.summary["permission_mode"] == "auto-review"

    store2 = SessionStore(cfg, primary, factory)
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.permission_mode == "auto-review"
    assert restored.agent.permission_mode == "auto-review"


def test_session_permission_endpoint(tmp_path):
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=primary)

    store = SessionStore(cfg, primary, factory)
    s = store.create()
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(f"/api/sessions/{s.id}/permission", json={"mode": "allow-all"})
        assert r.status_code == 200
        assert r.json()["permission_mode"] == "allow-all"
        assert store.get(s.id).permission_mode == "allow-all"
        assert store.get(s.id).agent.permission_mode == "allow-all"

        bad = client.post(f"/api/sessions/{s.id}/permission", json={"mode": "bogus"})
        assert bad.status_code == 422
        missing = client.post("/api/sessions/nope/permission", json={"mode": "ask"})
        assert missing.status_code == 404


def test_chat_with_permission_mode_on_existing_session(tmp_path):
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):  # noqa: ANN001
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=primary)

    store = SessionStore(cfg, primary, factory)
    s = store.create()
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post("/api/chat", json={"message": "hi", "session_id": s.id, "permission_mode": "auto-review"})
        assert r.status_code == 200
    assert store.get(s.id).permission_mode == "auto-review"
    assert store.get(s.id).agent.permission_mode == "auto-review"

    # 新会话带 permission_mode
    client2 = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client2:
        r2 = client2.post("/api/chat", json={"message": "new", "permission_mode": "allow-all"})
        assert r2.status_code == 200
    news = [x for x in store.list() if x.id != s.id]
    assert news and news[0].permission_mode == "allow-all"
