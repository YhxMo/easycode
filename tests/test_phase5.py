"""Phase 5 tests: P5-1 credentials store + model web config + masking."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.credentials import Credential, delete_credential, load_credentials, save_credential
from easycode.web.main import create_app

# ---------------------------------------------------------------- credentials

def test_credentials_read_write_delete_and_metadata(tmp_path):
    p = tmp_path / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="model-a", api_key="sk-a", base_url="http://a/v1"), path=p)
    save_credential(Credential(key_id="model-b", api_key="sk-b", base_url="http://b/v1"), path=p)

    creds = load_credentials(p)
    assert creds["model-a"].api_key == "sk-a"
    assert creds["model-a"].base_url == "http://a/v1"
    assert creds["model-b"].api_key == "sk-b"
    assert delete_credential("model-a", path=p) is True
    assert delete_credential("model-a", path=p) is False
    assert "model-a" not in load_credentials(p)
    assert "model-b" in load_credentials(p)


def test_credentials_file_permissions(tmp_path):
    p = tmp_path / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="model", api_key="sk-secret"), path=p)
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
    c = Credential(key_id="model", api_key="sk-super-secret-1234")
    m = c.masked()
    assert "api_key" not in m
    assert m["key_tail"] == "1234"


def test_credentials_support_metadata_without_api_key(tmp_path):
    p = tmp_path / ".easycode" / "credentials.json"
    save_credential(Credential(key_id="model", api_key="", base_url="http://local/v1"), path=p)
    cred = load_credentials(p)["model"]
    assert cred.api_key == ""
    assert cred.base_url == "http://local/v1"


# ---------------------------------------------------------------- model spec

def test_config_models_accept_str_and_object(tmp_path, monkeypatch):
    (tmp_path / "easycode.config.json").write_text(
        json.dumps(
            {
                "models": {
                    "plain": "openai/gpt-4o",
                    "keyed": {"model": "gpt-4o", "key_id": "my-key", "provider": "rightcode"},
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
    assert cfg.model_spec("keyed").provider == "rightcode"
    assert cfg.model_spec("plain").key_id is None

    cfg.save()
    raw = json.loads((tmp_path / "easycode.config.json").read_text(encoding="utf-8"))
    assert raw["models"]["plain"] == {"model": "openai/gpt-4o", "api_format": "openai_compatible"}
    assert raw["models"]["keyed"] == {
        "model": "gpt-4o",
        "key_id": "my-key",
        "api_format": "openai_compatible",
        "provider": "rightcode",
    }


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

    def factory(alias: str):
        from easycode.agent.loop import Agent
        from easycode.tools import build_registry
        from tests.conftest import FakeProvider

        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=tmp_path)

    from easycode.web.session import SessionStore

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def add_model_body(alias="gpt-local", model="gpt-4o", key="sk-lives-here"):
    return {"alias": alias, "model": model, "provider": "openai", "base_url": "http://127.0.0.1:9000/v1", "api_key": key}


def model_key_id(client: TestClient, alias: str) -> str:
    key_id = client.get(f"/api/models/{alias}").json()["key_id"]
    assert key_id
    return key_id


def test_add_model_with_key_stores_credential_and_masks(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/models/add", json=add_model_body())
    assert r.status_code == 200
    data = r.json()
    assert data["models"]["gpt-local"]["model"] == "gpt-4o"
    assert data["models"]["gpt-local"]["api_format"] == "openai_compatible"
    assert data["models"]["gpt-local"]["provider"] == "openai"
    assert data["models"]["gpt-local"]["key_id"].startswith("model-")
    assert data["providers"]["gpt-local"] == "openai"

    creds = load_credentials(tmp_path / ".easycode" / "credentials.json")
    assert creds[data["models"]["gpt-local"]["key_id"]].api_key == "sk-lives-here"

    # GET /api/models must never leak the key anywhere in the response
    r2 = client.get("/api/models")
    body = r2.text
    assert "sk-lives-here" not in body
    assert "sk-" not in body
    assert data["models"]["gpt-local"]["key_id"] in body


def test_models_group_by_supplier_not_api_format(tmp_path):
    client = make_app(tmp_path)
    body = add_model_body(alias="hosted-gpt", model="gpt-4o")
    body["provider"] = "rightcode"
    body["api_format"] = "anthropic"

    response = client.post("/api/models/add", json=body)

    assert response.status_code == 200
    assert response.json()["providers"]["hosted-gpt"] == "rightcode"
    assert response.json()["models"]["hosted-gpt"]["api_format"] == "anthropic"


def test_each_added_model_gets_its_own_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = make_app(tmp_path)
    first = add_model_body(alias="first", model="model-a", key="sk-first")
    second = add_model_body(alias="second", model="model-b", key="sk-second")
    first["api_format"] = "openai_compatible"
    second["api_format"] = "anthropic"
    assert client.post("/api/models/add", json=first).status_code == 200
    assert client.post("/api/models/add", json=second).status_code == 200

    first_id = model_key_id(client, "first")
    second_id = model_key_id(client, "second")
    assert first_id != second_id
    creds = load_credentials(tmp_path / ".easycode" / "credentials.json")
    assert creds[first_id].api_key == "sk-first"
    assert creds[second_id].api_key == "sk-second"

    from easycode.cli import provider_kwargs

    cfg = Config.load(start=tmp_path)
    first_model, first_kwargs = provider_kwargs(cfg, "first")
    second_model, second_kwargs = provider_kwargs(cfg, "second")
    assert first_model == "model-a"
    assert first_kwargs["api_key"] == "sk-first"
    assert first_kwargs["custom_llm_provider"] == "openai"
    assert second_model == "model-b"
    assert second_kwargs["api_key"] == "sk-second"
    assert second_kwargs["custom_llm_provider"] == "anthropic"


def test_add_model_without_key_still_gets_independent_profile(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/models/add", json={"alias": "plain", "model": "deepseek/deepseek-chat"})
    assert r.status_code == 200
    entry = r.json()["models"]["plain"]
    assert entry["model"] == "deepseek/deepseek-chat"
    assert entry["api_format"] == "openai_compatible"
    assert "key_id" not in entry
    assert not (tmp_path / ".easycode" / "credentials.json").exists()


def test_add_model_validation(tmp_path):
    client = make_app(tmp_path)
    assert client.post("/api/models/add", json={"alias": " ", "model": "x"}).status_code == 422


def test_get_model_detail_returns_editable_credential(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.get("/api/models/gpt-local")
    assert r.status_code == 200
    assert r.json() == {
        "alias": "gpt-local",
        "model": "gpt-4o",
        "key_id": model_key_id(client, "gpt-local"),
        "provider": "openai",
        "base_url": "http://127.0.0.1:9000/v1",
        "api_format": "openai_compatible",
        "has_api_key": True,
        "key_tail": "here",
    }
    assert client.get("/api/models/nope").status_code == 404


def test_get_model_detail_does_not_use_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://api.deepseek.test")
    client = make_app(
        tmp_path,
        {"models": {"ds": "deepseek/deepseek-chat", "oa": "openai/gpt-4o", "odd": "weird-model"}},
    )
    r = client.get("/api/models/ds")
    assert r.status_code == 200
    assert r.json() == {
        "alias": "ds",
        "model": "deepseek/deepseek-chat",
        "key_id": None,
        "provider": "deepseek",
        "base_url": None,
        "api_format": "openai_compatible",
        "has_api_key": False,
        "key_tail": "",
    }
    assert client.get("/api/models/oa").json()["base_url"] is None
    odd = client.get("/api/models/odd").json()
    assert odd["provider"] == "openai"
    assert odd["key_tail"] == ""
    assert odd["base_url"] is None


def test_get_model_detail_derives_api_format(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "BEDROCK_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    client = make_app(
        tmp_path,
        {
            "models": {
                "cl": "anthropic/claude-3-5-sonnet",
                "rs": "openai/responses/gpt-5",
                "ds": "deepseek/deepseek-chat",
                "odd": "weird-model",
            }
        },
    )
    assert client.get("/api/models/cl").json()["api_format"] == "anthropic"
    assert client.get("/api/models/rs").json()["api_format"] == "openai_responses"
    assert client.get("/api/models/ds").json()["api_format"] == "openai_compatible"
    assert client.get("/api/models/odd").json()["api_format"] == "openai_compatible"


def test_add_model_stores_and_returns_api_format(tmp_path):
    client = make_app(tmp_path)
    body = add_model_body(alias="resp", model="gpt-5")
    body["api_format"] = "openai_responses"
    r = client.post("/api/models/add", json=body)
    assert r.status_code == 200
    assert client.get("/api/models/resp").json()["api_format"] == "openai_responses"
    cred = load_credentials(tmp_path / ".easycode" / "credentials.json")[model_key_id(client, "resp")]
    assert cred.api_key == body["api_key"]


def test_add_model_without_key_persists_api_format(tmp_path):
    client = make_app(tmp_path)
    r = client.post(
        "/api/models/add",
        json={"alias": "plain", "model": "gpt-5", "api_format": "openai_responses"},
    )
    assert r.status_code == 200
    assert client.get("/api/models/plain").json()["api_format"] == "openai_responses"
    raw = json.loads((tmp_path / "easycode.config.json").read_text(encoding="utf-8"))
    assert raw["models"]["plain"]["model"] == "gpt-5"
    assert raw["models"]["plain"]["api_format"] == "openai_responses"
    assert "key_id" not in raw["models"]["plain"]
    assert not (tmp_path / ".easycode" / "credentials.json").exists()


def test_update_model_without_credential_persists_api_format(tmp_path):
    client = make_app(tmp_path, {"models": {"plain": "gpt-5"}})
    r = client.put("/api/models/plain", json={"model": "gpt-5", "api_format": "openai_responses"})
    assert r.status_code == 200
    assert client.get("/api/models/plain").json()["api_format"] == "openai_responses"
    raw = json.loads((tmp_path / "easycode.config.json").read_text(encoding="utf-8"))
    assert raw["models"]["plain"]["model"] == "gpt-5"
    assert raw["models"]["plain"]["api_format"] == "openai_responses"
    assert "key_id" not in raw["models"]["plain"]

    # a model without an independent credential cannot connect
    from easycode.cli import provider_kwargs

    cfg = Config.load(start=tmp_path)
    with pytest.raises(ValueError, match="no credential configured"):
        provider_kwargs(cfg, "plain")


def test_provider_kwargs_prefers_config_format_over_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(key_id="k1", api_key="sk-cred", provider="custom"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias("mixed", {"model": "gemini-2.0-flash", "key_id": "k1", "api_format": "anthropic"})
    from easycode.cli import provider_kwargs

    model, kwargs = provider_kwargs(cfg, "mixed")
    assert model == "gemini-2.0-flash"
    assert kwargs["custom_llm_provider"] == "anthropic"
    assert kwargs["api_key"] == "sk-cred"


def test_update_model_updates_api_format_keeps_key(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.put("/api/models/gpt-local", json={"model": "claude-4", "api_format": "anthropic"})
    assert r.status_code == 200
    key_id = model_key_id(client, "gpt-local")
    cred = load_credentials(tmp_path / ".easycode" / "credentials.json")[key_id]
    assert cred.api_key == "sk-lives-here"
    assert cred.provider == "openai"
    detail = client.get("/api/models/gpt-local").json()
    assert detail["api_format"] == "anthropic"

    # model-only update keeps the stored format
    client.put("/api/models/gpt-local", json={"model": "claude-4.1"})
    assert client.get("/api/models/gpt-local").json()["api_format"] == "anthropic"


def test_update_model_renames_alias_migrates_key_and_sessions(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    with client:
        session = client.app.state.store.create("gpt-local")
        r = client.put(
            "/api/models/gpt-local",
            json={
                "model": "gpt-4.1",
                "new_alias": "renamed",
                "provider": "custom",
                "base_url": "http://new.example/v1",
                "api_key": "sk-replaced",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert "gpt-local" not in data["models"]
        assert data["models"]["renamed"]["model"] == "gpt-4.1"
        assert data["models"]["renamed"]["api_format"] == "openai_compatible"
        assert session.model_alias == "renamed"

    creds = load_credentials(tmp_path / ".easycode" / "credentials.json")
    renamed_key = data["models"]["renamed"]["key_id"]
    assert creds[renamed_key].api_key == "sk-replaced"
    assert creds[renamed_key].provider == "custom"
    assert creds[renamed_key].base_url == "http://new.example/v1"


def test_update_model_keeps_key_and_updates_meta(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.put(
        "/api/models/gpt-local",
        json={"model": "gpt-4.1-mini", "provider": "openrouter", "base_url": "http://router/v1"},
    )
    assert r.status_code == 200
    key_id = model_key_id(client, "gpt-local")
    creds = load_credentials(tmp_path / ".easycode" / "credentials.json")
    assert creds[key_id].api_key == "sk-lives-here"
    assert creds[key_id].provider == "openrouter"
    assert creds[key_id].base_url == "http://router/v1"


def test_update_model_model_only_keeps_credential_meta(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.put("/api/models/gpt-local", json={"model": "gpt-4.1"})
    assert r.status_code == 200
    cred = load_credentials(tmp_path / ".easycode" / "credentials.json")[model_key_id(client, "gpt-local")]
    assert cred.api_key == "sk-lives-here"
    assert cred.provider == "openai"
    assert cred.base_url == "http://127.0.0.1:9000/v1"


def test_update_model_clears_owned_key(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.put(
        "/api/models/gpt-local",
        json={"model": "gpt-4o", "clear_key": True},
    )
    assert r.status_code == 200
    assert r.json()["models"]["gpt-local"]["model"] == "gpt-4o"
    assert "key_id" not in r.json()["models"]["gpt-local"]
    assert load_credentials(tmp_path / ".easycode" / "credentials.json") == {}


def test_update_model_rejects_alias_conflict(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.put("/api/models/gpt-local", json={"model": "gpt-4o", "new_alias": "fake-a"})
    assert r.status_code == 409


def test_delete_model_removes_web_credential(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.delete("/api/models/gpt-local")
    assert r.status_code == 200
    assert "gpt-local" not in r.json()["models"]
    assert load_credentials(tmp_path / ".easycode" / "credentials.json") == {}
    assert client.delete("/api/models/nope").status_code == 404


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

    from easycode.tools import build_registry
    from easycode.workspace import PathContext

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
    assert {m["path"] for m in g["matches"] if isinstance(m, dict)} == {"b.py"}

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

    def factory(alias: str, **kw):
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
    assert created[0].secondary_roots == [str(secondary.resolve())]


# ---------------------------------------------------------------- P5-2 approval

def test_needs_approval_modes(tmp_path):

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

    assert needs_approval(edit_out, ctx, "auto-review") is True
    assert needs_approval(shell_net, ctx, "auto-review") is True
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

    class AutoBroker(ApprovalBroker):
        def add(self, approval_id: str):
            fut = super().add(approval_id)

            async def resolve():
                import asyncio

                await asyncio.sleep(0.05)
                fut.set_result((True, False))

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
        with client, client.stream("POST", "/api/chat", json={"message": "write it"}) as r:
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

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
    cfg.set_model_alias("keyed", {"model": "gpt-4o", "key_id": "k1", "api_format": "openai_compatible"})

    from easycode.cli import build_provider

    prov = build_provider(cfg, "keyed")
    assert prov.model == "gpt-4o"
    assert prov.kwargs["api_key"] == "sk-cred"
    assert prov.kwargs["api_base"] == "http://x/v1"
    assert prov.kwargs["custom_llm_provider"] == "openai"

    cfg.set_model_alias("prefixed", {"model": "openai/gpt-4o", "key_id": "k1", "api_format": "openai_compatible"})
    prov2 = build_provider(cfg, "prefixed")
    assert prov2.model == "gpt-4o"
    assert prov2.kwargs["custom_llm_provider"] == "openai"


def test_build_provider_missing_credential_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config()
    cfg.set_model_alias("keyed", {"model": "gpt-4o", "key_id": "nope"})

    from easycode.cli import build_provider

    with pytest.raises(ValueError, match="credential 'nope' not found"):
        build_provider(cfg, "keyed")


def test_apply_api_format_routing():
    from easycode.cli import apply_api_format

    assert apply_api_format("gpt-4o", "openai_compatible") == ("gpt-4o", "openai")
    assert apply_api_format("gpt-5", "openai_responses") == ("responses/gpt-5", "openai")
    assert apply_api_format("openai/gpt-5", "openai_responses") == ("responses/gpt-5", "openai")
    assert apply_api_format("responses/gpt-5", "openai_responses") == ("responses/gpt-5", "openai")
    assert apply_api_format("openai/gpt-4o", "openai_compatible") == ("gpt-4o", "openai")
    assert apply_api_format("claude-sonnet", "anthropic") == ("claude-sonnet", "anthropic")
    assert apply_api_format("claude-3", "bedrock") == ("claude-3", "bedrock")
    assert apply_api_format("gemini-2.0", "gemini") == ("gemini-2.0", "gemini")
    assert apply_api_format("gpt-4o", "mystery-format") == ("gpt-4o", None)


def test_apply_api_format_overrides_model_prefix():
    from easycode.cli import apply_api_format

    # the chosen format wins over a stored provider prefix
    assert apply_api_format("deepseek/deepseek-v4-flash", "anthropic") == (
        "deepseek-v4-flash",
        "anthropic",
    )
    assert apply_api_format("anthropic/claude-sonnet-5", "openai_compatible") == (
        "claude-sonnet-5",
        "openai",
    )


def test_build_provider_forwards_api_format(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(
            key_id="k1",
            api_key="sk-cred",
            provider="openai",
            base_url="http://x/v1",
        ),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias("keyed", {"model": "gpt-5", "key_id": "k1", "api_format": "openai_responses"})

    from easycode.cli import build_provider

    prov = build_provider(cfg, "keyed")
    assert prov.model == "responses/gpt-5"
    assert prov.kwargs["api_key"] == "sk-cred"
    assert prov.kwargs["api_base"] == "http://x/v1"
    assert prov.kwargs["custom_llm_provider"] == "openai"

    # prefixed model: the format wins over the stored prefix
    cfg.set_model_alias("prefixed", {"model": "openai/gpt-5", "key_id": "k1", "api_format": "openai_responses"})
    prov2 = build_provider(cfg, "prefixed")
    assert prov2.model == "responses/gpt-5"
    assert prov2.kwargs["custom_llm_provider"] == "openai"

    # api_format wins over the credential provider for unprefixed models
    save_credential(
        Credential(key_id="k2", api_key="sk-cred", provider="custom"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg.set_model_alias("gemini", {"model": "gemini-2.0-flash", "key_id": "k2", "api_format": "gemini"})
    prov3 = build_provider(cfg, "gemini")
    assert prov3.model == "gemini-2.0-flash"
    assert prov3.kwargs["custom_llm_provider"] == "gemini"


def test_build_provider_format_overrides_prefixed_model(tmp_path, monkeypatch):
    """deepseek-prefixed model + anthropic format must speak Anthropic, not DeepSeek."""
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(
            key_id="k1",
            api_key="sk-ds",
            provider="deepseek",
            base_url="https://api.deepseek.com",
        ),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias("ds", {"model": "deepseek/deepseek-v4-flash", "key_id": "k1", "api_format": "anthropic"})

    from easycode.cli import build_provider

    prov = build_provider(cfg, "ds")
    assert prov.model == "deepseek-v4-flash"
    assert prov.kwargs["custom_llm_provider"] == "anthropic"
    assert prov.kwargs["api_base"] == "https://api.deepseek.com"

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
        {"text": "sum is available"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_servers=mcp_server_config(),
        # The demo server declares no annotations, so `mcp__demo__add` is not
        # auto-allowed under the default (ask) mode. This test is about MCP
        # *routing*, not the fail-closed approval policy, so run under allow-all.
        permission_mode="allow-all",
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
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

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


def test_mcp_requires_approval_is_fail_closed():
    """Only an explicit read-only tool (readOnlyHint True and
    destructiveHint not True) is auto-allowed. Missing annotations, readOnly
    false, and destructive all require approval; unknown names are not an MCP
    approval scope."""
    from easycode.mcp import MCPSession, MCPSessionManager

    mgr = MCPSessionManager({})
    sess = MCPSession("demo", None)
    sess.tools = {
        "mcp__demo__readonly": {
            "name": "readonly",
            "schema": {"type": "function", "function": {}},
            "annotations": {"readOnlyHint": True},
        },
        "mcp__demo__destructive": {
            "name": "destructive",
            "schema": {"type": "function", "function": {}},
            "annotations": {"destructiveHint": True},
        },
        "mcp__demo__plain": {
            "name": "plain",
            "schema": {"type": "function", "function": {}},
            "annotations": {},
        },
        "mcp__demo__rw": {
            "name": "rw",
            "schema": {"type": "function", "function": {}},
            "annotations": {"readOnlyHint": False},
        },
        "mcp__demo__readonly_destructive": {
            "name": "readonly_destructive",
            "schema": {"type": "function", "function": {}},
            "annotations": {"readOnlyHint": True, "destructiveHint": True},
        },
    }
    mgr._sessions = {"demo": sess}

    assert mgr.requires_approval("mcp__demo__readonly") is False
    assert mgr.requires_approval("mcp__demo__destructive") is True
    assert mgr.requires_approval("mcp__demo__plain") is True  # annotation missing → fail-closed
    assert mgr.requires_approval("mcp__demo__rw") is True  # readOnly false → approval
    # destructive dominates readOnly: a supposedly read-only tool that is also
    # destructive must still require approval
    assert mgr.requires_approval("mcp__demo__readonly_destructive") is True
    # unknown / non-MCP names are not an MCP approval scope
    assert mgr.requires_approval("write_file") is False
    assert mgr.requires_approval("mcp__demo__nope") is False


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
    """Over-budget turns invoke the summarizer and keep a token-selected tail."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

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
        max_context_tokens=100_000,
    )
    agent.history = History(max_tokens=100_000)
    agent.history.set_system("sys")
    agent.history.add_user("old " + "z" * 20_000)  # old turn to summarize
    agent.history.add_assistant("old answer")
    agent.history.add_user("recent question")  # recent turn fits the tail budget
    agent.history.add_assistant("recent answer")
    agent.history.max_chars = 1_000  # force over-budget before the turn
    agent.compaction["preserve_recent_tokens"] = 2_000

    async def fake_summarize(messages):
        return "[synthetic summary]"

    agent.summarizer = fake_summarize
    async for _ in agent.respond("do it"):
        pass
    contents = " ".join(str(m.get("content", "")) for m in agent.history.messages)
    assert "synthetic summary" in contents
    assert "recent answer" in contents


@pytest.mark.asyncio
async def test_agent_no_summarizer_hard_trim(tmp_path):
    from easycode.agent.context import History
    from easycode.agent.loop import Agent
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


def test_usable_tokens_reserves_output_buffer(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    a = Agent(
        provider=FakeProvider(),
        registry=build_registry(8000),
        root=tmp_path,
        max_context_tokens=32_000,
        model_limits={"context": 100_000, "output": 8_000},
    )
    # default buffer 20_000 → reserved = min(20_000, 8_000) = 8_000
    assert a._usable_tokens() == 100_000 - 8_000

    b = Agent(provider=FakeProvider(), registry=build_registry(8000), root=tmp_path, max_context_tokens=32_000)
    assert b.model_limits is None
    assert b._usable_tokens() == 32_000  # unknown model → no reserve


def test_prune_clears_old_tool_outputs(tmp_path, monkeypatch):
    import easycode.agent.loop as loop

    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    from easycode.agent.loop import PRUNED_OUTPUT, Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    agent = Agent(provider=FakeProvider(), registry=build_registry(8000), root=tmp_path)
    h = agent.history
    h.add_user("q1")
    h.add_tool("1", "read_file", "x" * 400)
    h.add_user("q2")
    h.add_tool("2", "read_file", "y" * 400)
    h.add_user("q3")
    h.add_tool("3", "read_file", "z" * 400)

    agent._prune_tool_outputs()

    assert h.messages[1]["content"] == PRUNED_OUTPUT  # oldest cleared
    assert h.messages[3]["content"] == "y" * 400  # last 2 turns protected
    assert h.messages[5]["content"] == "z" * 400


def test_prune_protects_skill_output(tmp_path, monkeypatch):
    import easycode.agent.loop as loop

    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    agent = Agent(provider=FakeProvider(), registry=build_registry(8000), root=tmp_path)
    h = agent.history
    h.add_user("q1")
    h.add_tool("1", "use_skill", "x" * 400)
    h.add_user("q2")
    h.add_tool("2", "read_file", "y" * 400)
    h.add_user("q3")
    h.add_tool("3", "read_file", "z" * 400)

    agent._prune_tool_outputs()

    assert h.messages[1]["content"] == "x" * 400  # skill output never cleared
    assert h.messages[3]["content"] == "y" * 400
    assert h.messages[5]["content"] == "z" * 400


@pytest.mark.asyncio
async def test_summarizer_merges_previous_summary(tmp_path, monkeypatch):
    """The loop passes the prior summary to LLMSummarizer for rolling merge."""
    from easycode.agent.loop import Agent
    from easycode.agent.summarizer import LLMSummarizer
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    captured = {}

    async def fake_summarize(self, messages, previous_summary=None):
        captured["previous"] = previous_summary
        return "[merged]"

    monkeypatch.setattr(LLMSummarizer, "summarize", fake_summarize)

    agent = Agent(
        provider=FakeProvider(script=[{"text": "ok"}]),
        registry=build_registry(8000),
        root=tmp_path,
        summarizer=LLMSummarizer("fake/a"),
        max_context_tokens=100_000,
    )
    from easycode.agent.context import History

    agent.history = History(max_tokens=100_000)
    agent.history.set_system("sys")
    agent.history.add_user("old " + "z" * 20_000)
    agent.history.add_assistant("old answer")
    agent.history.add_user("recent")
    agent.history.add_assistant("recent answer")
    agent.history.summary = "[first summary]"
    agent.history.max_chars = 1_000
    agent.compaction["preserve_recent_tokens"] = 2_000

    async for _ in agent.respond("do it"):
        pass
    assert captured.get("previous") == "[first summary]"
    assert "[merged]" in " ".join(str(m.get("content", "")) for m in agent.history.messages)


def test_make_agent_wires_summarizer(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps({"models": {"fake-a": {"model": "fake/a", "key_id": "fake-key"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(Credential(key_id="fake-key", api_key="sk-fake"), path=tmp_path / ".easycode" / "credentials.json")
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

    def factory(alias: str, **kw):
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

    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(_json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    primary.mkdir(); project.mkdir()
    agents: list[Agent] = []
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
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

    def factory(alias: str, **kw):
        root = Path(kw.get("root")).resolve() if kw.get("root") else primary
        sec = [Path(p).resolve() for p in (kw.get("secondary_roots") or [])]
        agent = Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=root, secondary_roots=sec)
        built.append(agent)
        return agent

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(root=str(project), secondary_roots=[str(sec_a)])
    assert s.secondary_roots == [str(sec_a.resolve())]
    assert built[-1].secondary_roots == [sec_a.resolve()]
    assert s.summary["secondary_roots"] == [str(sec_a.resolve())]

    store2 = SessionStore(cfg, tmp_path / "p2", factory)
    (tmp_path / "p2").mkdir(exist_ok=True)
    store2.dir = store1.dir  # same disk dir
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.secondary_roots == [str(sec_a.resolve())]
    assert restored.root == str(project)


def test_chat_root_with_secondary_roots(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    project = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    primary.mkdir(); project.mkdir(); sec_a.mkdir()
    agents: list[Agent] = []
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    proj = tmp_path / "proj"
    sec = tmp_path / "sec"
    primary.mkdir(); proj.mkdir(); sec.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    proj = tmp_path / "proj"
    sec_a = tmp_path / "sec_a"
    sec_b = tmp_path / "sec_b"
    primary.mkdir(); proj.mkdir(); sec_a.mkdir(); sec_b.mkdir()
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg_file = tmp_path / "myagent.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(provider=FakeProvider(script=[{"text": "ok"}]), registry=build_registry(8000), root=Path(kw.get("root") or primary))

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(permission_mode="auto-review")
    assert s.permission_mode == "auto-review"
    assert s.agent.permission_mode == "auto-review"
    assert s.summary["permission_mode"] == "auto-review"
    assert s.summary["sandbox_mode"] == "workspace-write"
    assert s.summary["approval_policy"] == "on-request"
    assert s.summary["approvals_reviewer"] == "auto-review"

    store2 = SessionStore(cfg, primary, factory)
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.permission_mode == "auto-review"
    assert restored.agent.permission_mode == "auto-review"


def test_session_permission_endpoint(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.main import create_app
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
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


@pytest.mark.asyncio
async def test_web_always_allow_matches_session_scope_no_prompt(tmp_path):
    """A pre-registered session always-allow scope skips the approval prompt."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.bridge import ApprovalBroker, stream_chat_with_approval
    from easycode.web.session import Session
    from tests.conftest import FakeProvider

    outside = tmp_path.parent / "always-x.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "x"})], "text": ""},
        {"text": "ok"},
    ]
    agent = Agent(provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path)
    broker = ApprovalBroker()
    sess = Session(id="s1", title="t", created_at="now", model_alias="fake-a", agent=agent)
    parent = str(outside.parent).rstrip("/")
    sess.always_allow.append(f"write_file:{parent}/*")

    approvals = []

    async for kind, payload in stream_chat_with_approval(agent, "go", broker, session=sess):
        if kind == "approval":
            approvals.append(payload)

    assert approvals == []
    assert outside.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_web_approval_log_records_decision(tmp_path):
    """Resolved approvals are recorded on the session (approved / denied / expired)."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.bridge import ApprovalBroker, stream_chat_with_approval
    from easycode.web.session import Session
    from tests.conftest import FakeProvider

    outside = tmp_path.parent / "log-y.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "y"})], "text": ""},
        {"text": "ok"},
    ]
    agent = Agent(provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path)
    broker = ApprovalBroker()

    class AutoBroker(ApprovalBroker):
        def add(self, approval_id: str):
            fut = super().add(approval_id)

            async def resolve():
                import asyncio

                await asyncio.sleep(0.05)
                fut.set_result((True, True))

            import asyncio

            asyncio.ensure_future(resolve())
            return fut

    broker = AutoBroker()
    sess = Session(id="s1", title="t", created_at="now", model_alias="fake-a", agent=agent)
    async for _kind, _payload in stream_chat_with_approval(agent, "go", broker, session=sess):
        pass

    assert sess.approval_log
    log = sess.approval_log[0]
    assert log["name"] == "write_file"
    assert log["decision"] == "approved"
    assert log["always"] is True
    assert log["tool_call_id"] == "c1"
    assert log["scope"].endswith("/*")
