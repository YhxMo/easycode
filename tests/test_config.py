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


def test_path_context_relative_paths_use_load_time_base(tmp_path, monkeypatch):
    """10D: relative workspace paths anchor at the same base as base_dir()
    (config dir, else load-time root) — never a later CWD."""
    (tmp_path / "extra").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(start=tmp_path)  # no config file: root = load-time CWD
    cfg.extra_safe_dirs = ["extra"]

    monkeypatch.chdir(elsewhere)  # a later chdir must not move the anchor
    ctx = cfg.path_context()
    assert ctx.extra_safe_dirs == [(tmp_path / "extra").resolve()]
    assert cfg.base_dir() == cfg.root


def test_max_tool_iterations_is_optional_and_strict(tmp_path, monkeypatch):
    """An absent ceiling means "let the model finish"; a set one is validated
    rather than silently ignored."""
    cfg_file = tmp_path / "easycode.config.json"
    monkeypatch.chdir(tmp_path)

    cfg_file.write_text(json.dumps({"model": "x"}), encoding="utf-8")
    assert Config.load().max_tool_iterations is None

    cfg_file.write_text(json.dumps({"max_tool_iterations": None}), encoding="utf-8")
    assert Config.load().max_tool_iterations is None

    cfg_file.write_text(json.dumps({"max_tool_iterations": 25}), encoding="utf-8")
    assert Config.load().max_tool_iterations == 25

    for bad in (0, -1, True, False, 1.5, "12"):
        cfg_file.write_text(json.dumps({"max_tool_iterations": bad}), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid max_tool_iterations"):
            Config.load()


def test_max_tool_iterations_survives_other_saves_only_when_set(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = Config.load()
    cfg.save()
    # An unset ceiling stays absent: saving other settings must not turn
    # "unlimited" into a number.
    assert "max_tool_iterations" not in json.loads(cfg_file.read_text(encoding="utf-8"))

    cfg.max_tool_iterations = 40
    cfg.save()
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["max_tool_iterations"] == 40
    assert Config.load().max_tool_iterations == 40


def test_each_file_stops_at_its_own_nearest_ancestor(tmp_path, monkeypatch):
    """The two searches are independent: each finds its own nearest file."""
    from easycode.config import find_config_file, find_env_file

    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (tmp_path / "easycode.config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a" / "b" / ".env").write_text("X=1", encoding="utf-8")
    monkeypatch.chdir(deep)

    assert find_config_file() == tmp_path / "easycode.config.json"
    assert find_env_file() == tmp_path / "a" / "b" / ".env"


def test_upward_search_takes_the_nearest_of_two(tmp_path):
    from easycode.config import find_config_file

    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (tmp_path / "easycode.config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a" / "easycode.config.json").write_text("{}", encoding="utf-8")

    assert find_config_file(start=deep) == tmp_path / "a" / "easycode.config.json"


def test_upward_search_reports_nothing_found(tmp_path):
    from easycode.config import find_config_file, find_env_file

    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)

    assert find_config_file(start=deep) is None
    assert find_env_file(start=deep) is None
