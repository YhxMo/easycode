"""Config tests: loading, merging, alias resolution."""

from __future__ import annotations

import json

import pytest

from easycode.config import Config


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.default_model == "deepseek-v4flash"
    assert cfg.resolve_model("deepseek-v4flash") == "deepseek/deepseek-v4-flash"
    assert cfg.resolve_model("openai/gpt-4o") == "openai/gpt-4o"
    assert cfg.max_tool_result_chars == 8000
    # No config file anywhere → the workspace anchors at CWD itself,
    # never at CWD's parent (which would widen the sandbox and save
    # easycode.config.json outside the project).
    assert cfg.root == tmp_path
    cfg.save()
    assert (tmp_path / "easycode.config.json").is_file()


def test_merges_file_over_defaults(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "default_model": "my-gpt",
                "models": {"my-gpt": "openai/x"},
                "max_tool_result_chars": 100,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.default_model == "my-gpt"
    assert cfg.resolve_model("my-gpt") == "openai/x"
    # DEC-C3: an explicit `models` key replaces the built-in defaults
    assert "gpt5.6-terra" not in cfg.models
    assert cfg.max_tool_result_chars == 100


def test_finds_config_in_parent(tmp_path, monkeypatch):
    (tmp_path / "easycode.config.json").write_text(json.dumps({"default_model": "x"}), encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    cfg = Config.load()
    assert cfg.default_model == "x"


def test_runtime_alias_and_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    cfg.set_model_alias("local", "ollama/llama3")
    assert cfg.resolve_model("local") == "ollama/llama3"
    cfg.set_default_model("local")
    cfg.save()
    reloaded = Config.load()
    assert reloaded.resolve_model("local") == "ollama/llama3"
    assert reloaded.default_model == "local"


def test_tools_enabled_map(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps({"tools": {"execute_shell": False}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.tools["execute_shell"] is False
    assert cfg.tools["read_file"] is True


def test_permission_rules_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps({"permissions": {"execute_shell": {"*": "ask", "git status*": "allow"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.permission_rules["execute_shell"]["git status*"] == "allow"

    cfg.save()
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["permissions"] == cfg.permission_rules


def test_config_permission_mode_valid_and_invalid(tmp_path, monkeypatch):
    """DEC-C2: only the `permission` key is read; an invalid value is an error."""
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"permission": "auto-review"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Config.load().permission_mode == "auto-review"

    cfg_file.write_text(json.dumps({"permission": "bogus"}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid permission mode"):
        Config.load()

    # the legacy `permission_mode` key is no longer read
    cfg_file.write_text(json.dumps({"permission_mode": "allow-all"}), encoding="utf-8")
    assert Config.load().permission_mode == "ask"
