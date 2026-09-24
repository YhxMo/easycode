"""Model management tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.credentials import Credential, delete_credential, load_credentials, save_credential
from easycode.web.main import create_app


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

    def factory(alias: str, **_):
        from easycode.agent.loop import Agent
        from easycode.tools import build_registry
        from tests.conftest import FakeProvider

        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=tmp_path)

    from easycode.web.session import SessionStore

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def add_model_body(alias="gpt-local", model="gpt-4o", key="sk-lives-here"):
    return {
        "alias": alias,
        "model": model,
        "provider": "openai",
        "base_url": "http://127.0.0.1:9000/v1",
        "api_key": key,
    }


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

    from easycode.agentfactory import provider_kwargs

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
    cred = load_credentials(tmp_path / ".easycode" / "credentials.json")[
        model_key_id(client, "resp")
    ]
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
    from easycode.agentfactory import provider_kwargs

    cfg = Config.load(start=tmp_path)
    with pytest.raises(ValueError, match="no credential configured"):
        provider_kwargs(cfg, "plain")


def test_provider_kwargs_uses_config_api_format(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(key_id="k1", api_key="sk-cred"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias(
        "mixed", {"model": "gemini-2.0-flash", "key_id": "k1", "api_format": "anthropic"}
    )
    from easycode.agentfactory import provider_kwargs

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
    detail = client.get("/api/models/gpt-local").json()
    assert detail["provider"] == "openai"  # supplier lives on the model spec
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
    assert data["models"]["renamed"]["provider"] == "custom"
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
    assert creds[key_id].base_url == "http://router/v1"
    assert client.get("/api/models/gpt-local").json()["provider"] == "openrouter"


def test_update_model_model_only_keeps_credential_meta(tmp_path):
    client = make_app(tmp_path)
    client.post("/api/models/add", json=add_model_body())
    r = client.put("/api/models/gpt-local", json={"model": "gpt-4.1"})
    assert r.status_code == 200
    cred = load_credentials(tmp_path / ".easycode" / "credentials.json")[
        model_key_id(client, "gpt-local")
    ]
    assert cred.api_key == "sk-lives-here"
    assert cred.base_url == "http://127.0.0.1:9000/v1"
    assert client.get("/api/models/gpt-local").json()["provider"] == "openai"


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

    save_credential(
        Credential(key_id="web-keyed", api_key="sk-k"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    client = make_app(
        tmp_path,
        {"models": {"keyed": {"model": "gpt-4o", "key_id": "web-keyed"}}, "default_model": "keyed"},
    )
    r = client.post("/api/models", json={"alias": "keyed"})
    assert r.status_code == 200
    assert r.json()["default"] == "keyed"


def test_classify_path_categories(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    import tempfile

    from easycode.workspace import PathContext

    primary = tmp_path / "work"
    secondary = tmp_path / "other"
    (primary / "a").mkdir(parents=True)
    secondary.mkdir(parents=True)
    ctx = PathContext(primary=primary, secondary=[secondary])

    assert ctx.classify(primary / "a" / "f.py") == "workspace"
    assert ctx.classify(secondary / "g.py") == "workspace"
    assert ctx.classify(Path(tempfile.gettempdir()) / "x.tmp") == "temp"
    assert ctx.classify(tmp_path / ".easycode" / "sessions" / "s.json") == "system"
    assert ctx.classify(Path("/etc/hosts")) == "external"


def test_in_allowed_with_extra_safe_dirs(tmp_path):
    from easycode.workspace import PathContext

    extra = tmp_path / "notes"
    extra.mkdir()
    ctx = PathContext(primary=tmp_path, extra_safe_dirs=[extra])
    assert ctx.in_allowed(extra / "a.txt") is True
    assert ctx.in_allowed(extra.parent.parent / "elsewhere") is False


def test_multi_root_write_and_read(tmp_path):
    """With a context of primary + secondary, tools reach both roots."""
    import json as _json

    from easycode.tools import build_registry
    from easycode.workspace import PathContext

    reg = build_registry(8000)
    primary = tmp_path / "p"
    secondary = tmp_path / "s"
    primary.mkdir()
    secondary.mkdir()
    ctx = PathContext(primary=primary, secondary=[secondary])

    # relative path prefers the first root that contains it (primary first)
    out = _json.loads(
        reg.execute("write_file", {"path": "x.py", "content": "x=1"}, primary, ctx=ctx)
    )
    assert out["status"] == "ok"
    assert (primary / "x.py").read_text() == "x=1"

    # absolute path resolves to the secondary root and is editable there
    abs_path = str(secondary / "x.py")
    out2 = _json.loads(
        reg.execute("write_file", {"path": abs_path, "content": "y=2"}, primary, ctx=ctx)
    )
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
    primary = tmp_path / "p"
    secondary = tmp_path / "s"
    primary.mkdir()
    secondary.mkdir()
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
    primary = tmp_path / "p"
    primary.mkdir()
    ctx = PathContext(primary=primary)
    out = _json.loads(
        reg.execute("write_file", {"path": "../evil.txt", "content": "x"}, primary, ctx=ctx)
    )
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
    assert cfg.extra_safe_dirs == ["notes"]
    # `workspace.secondary` is not a supported field: CLI/Web pass secondary
    # roots explicitly, so the config must not widen the sandbox.
    assert cfg.path_context().secondary == []
    ctx = cfg.path_context(secondary=["sec_a", "~/sec_b"])
    assert len(ctx.secondary) == 2
    assert ctx.secondary[0] == (tmp_path / "sec_a").resolve()


def test_web_session_with_secondary_roots(tmp_path):
    """Chat creating a session with secondary_roots wires them into the agent."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    secondary = tmp_path / "s"
    primary.mkdir()
    secondary.mkdir()
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
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "hi", "secondary_roots": [str(secondary)]},
        )
        assert r.status_code == 200
    assert created
    assert created[0].secondary_roots == [str(secondary.resolve())]


def test_needs_approval_modes(tmp_path):

    from easycode.approval import needs_approval
    from easycode.models.base import ToolCall
    from easycode.policy import permission_parse
    from easycode.workspace import PathContext

    primary = tmp_path / "work"
    primary.mkdir()
    (primary / "a.py").write_text("x=1", encoding="utf-8")
    ctx = PathContext(primary=primary)

    edit_in = ToolCall(
        id="1",
        name="edit_file",
        arguments={"path": "a.py", "old_string": "x=1", "new_string": "x=2"},
    )
    edit_out = ToolCall(
        id="2",
        name="edit_file",
        arguments={"path": "../outside.py", "old_string": "a", "new_string": "b"},
    )
    shell_net = ToolCall(
        id="3", name="execute_shell", arguments={"command": "curl -s https://example.com"}
    )
    shell_local = ToolCall(id="4", name="execute_shell", arguments={"command": "ls -la"})
    shell_pip = ToolCall(
        id="5", name="execute_shell", arguments={"command": "pip install requests"}
    )

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
        {
            "tool_calls": [("c1", "write_file", {"path": str(outside), "content": "pwned"})],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
    )

    async def handler(tc: ToolCall, _reason: str) -> bool:
        return False

    agent.approval_handler = handler
    events = []
    async for ev in agent.respond("write it"):
        events.append(ev)
    kinds = [e.kind for e in events]
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
        {
            "tool_calls": [("c1", "write_file", {"path": str(outside), "content": "pwned"})],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
    )

    async def handler(tc: ToolCall, _reason: str) -> bool:
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
        {
            "tool_calls": [
                (
                    "c1",
                    "edit_file",
                    {"path": "p5-a.txt", "old_string": "hello", "new_string": "world"},
                )
            ],
            "text": "",
        },
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
        {
            "tool_calls": [
                ("c1", "write_file", {"path": str(tmp_path.parent / "p5-x.txt"), "content": "y"})
            ],
            "text": "",
        },
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

    def factory(alias: str, **_):
        return Agent(
            provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path
        )

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(
        create_app(
            cfg=cfg,
            session_store=store,
            static_dir=tmp_path / "no-dist",
            approval_broker=AutoBroker(),
        )
    )
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

    def factory(alias: str, **_):
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
        {
            "tool_calls": [
                ("c1", "write_file", {"path": str(tmp_path.parent / "p5-t.txt"), "content": "x"})
            ],
            "text": "",
        },
        {"text": "ok"},
    ]
    agent = Agent(
        provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path
    )
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


def test_build_provider_forwards_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(key_id="k1", api_key="sk-cred", base_url="http://x/v1"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias(
        "keyed", {"model": "gpt-4o", "key_id": "k1", "api_format": "openai_compatible"}
    )

    from easycode.agentfactory import build_provider

    prov = build_provider(cfg, "keyed")
    assert prov.model == "gpt-4o"
    assert prov.kwargs["api_key"] == "sk-cred"
    assert prov.kwargs["api_base"] == "http://x/v1"
    assert prov.kwargs["custom_llm_provider"] == "openai"

    cfg.set_model_alias(
        "prefixed", {"model": "openai/gpt-4o", "key_id": "k1", "api_format": "openai_compatible"}
    )
    prov2 = build_provider(cfg, "prefixed")
    assert prov2.model == "gpt-4o"
    assert prov2.kwargs["custom_llm_provider"] == "openai"


def test_build_provider_missing_credential_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config()
    cfg.set_model_alias("keyed", {"model": "gpt-4o", "key_id": "nope"})

    from easycode.agentfactory import build_provider

    with pytest.raises(ValueError, match="credential 'nope' not found"):
        build_provider(cfg, "keyed")


def test_apply_api_format_routing():
    from easycode.agentfactory import apply_api_format

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
    from easycode.agentfactory import apply_api_format

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
        Credential(key_id="k1", api_key="sk-cred", base_url="http://x/v1"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias(
        "keyed", {"model": "gpt-5", "key_id": "k1", "api_format": "openai_responses"}
    )

    from easycode.agentfactory import build_provider

    prov = build_provider(cfg, "keyed")
    assert prov.model == "responses/gpt-5"
    assert prov.kwargs["api_key"] == "sk-cred"
    assert prov.kwargs["api_base"] == "http://x/v1"
    assert prov.kwargs["custom_llm_provider"] == "openai"

    # prefixed model: the format wins over the stored prefix
    cfg.set_model_alias(
        "prefixed", {"model": "openai/gpt-5", "key_id": "k1", "api_format": "openai_responses"}
    )
    prov2 = build_provider(cfg, "prefixed")
    assert prov2.model == "responses/gpt-5"
    assert prov2.kwargs["custom_llm_provider"] == "openai"

    # api_format wins over the credential provider for unprefixed models
    save_credential(
        Credential(key_id="k2", api_key="sk-cred"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg.set_model_alias(
        "gemini", {"model": "gemini-2.0-flash", "key_id": "k2", "api_format": "gemini"}
    )
    prov3 = build_provider(cfg, "gemini")
    assert prov3.model == "gemini-2.0-flash"
    assert prov3.kwargs["custom_llm_provider"] == "gemini"


def test_build_provider_format_overrides_prefixed_model(tmp_path, monkeypatch):
    """deepseek-prefixed model + anthropic format must speak Anthropic, not DeepSeek."""
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(key_id="k1", api_key="sk-ds", base_url="https://api.deepseek.com"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    cfg = Config()
    cfg.set_model_alias(
        "ds", {"model": "deepseek/deepseek-v4-flash", "key_id": "k1", "api_format": "anthropic"}
    )

    from easycode.agentfactory import build_provider

    prov = build_provider(cfg, "ds")
    assert prov.model == "deepseek-v4-flash"
    assert prov.kwargs["custom_llm_provider"] == "anthropic"
    assert prov.kwargs["api_base"] == "https://api.deepseek.com"


def test_delete_builtin_alias_stays_deleted(tmp_path):
    """DEC-C3: built-in aliases seed a fresh config; deletions must persist."""
    client = make_app(tmp_path, {"default_model": "deepseek-v4flash"})
    assert "claude-opus5" in client.get("/api/models").json()["models"]

    assert client.delete("/api/models/claude-opus5").status_code == 200

    assert "claude-opus5" not in client.get("/api/models").json()["models"]


def test_get_models_uses_in_memory_config(tmp_path):
    """DEC-C4: the models endpoint never re-reads the config file mid-run."""
    client = make_app(tmp_path)
    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"models": {"external": "x/y"}}), encoding="utf-8"
    )

    body = client.get("/api/models").json()

    assert "external" not in body["models"]
    assert "fake-a" in body["models"]
