"""Configuration loading and model alias resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from easycode.agent.compaction import COMPACTION_DEFAULTS
from easycode.policy import PERM_ASK, permission_parse
from easycode.workspace import CONFIG_FILENAME, PathContext, resolve_workspace_path

DEFAULT_MAX_TOOL_RESULT_CHARS = 8000
DEFAULT_MAX_CONTEXT_TOKENS = 32_000

API_FORMATS = (
    "openai_responses",
    "openai_compatible",
    "anthropic",
    "bedrock",
    "gemini",
)
DEFAULT_API_FORMAT = "openai_compatible"

DEFAULT_MODEL_ALIAS = "deepseek-v4flash"

DEFAULT_MODELS: dict[str, str] = {
    DEFAULT_MODEL_ALIAS: "deepseek/deepseek-v4-flash",
    "gpt5.6-terra": "openai/gpt-5.6-terra",
    "gpt5.6-sol": "openai/gpt-5.6-sol",
    "claude-sonnet5": "anthropic/claude-sonnet-5",
    "claude-opus5": "anthropic/claude-opus-5",
}

DEFAULT_TOOLS = dict.fromkeys(
    ("execute_shell", "read_file", "write_file", "edit_file", "grep", "glob", "parallel_tasks"),
    True,
)


def _skills_enabled(raw: dict[str, Any]) -> bool:
    skills = raw.get("skills")
    if isinstance(skills, dict):
        return bool(skills.get("enabled", True))
    return True


@dataclass(frozen=True)
class ModelSpec:
    """Canonical model entry: model name, supplier, protocol, and credential reference."""

    model: str
    key_id: str | None = None
    api_format: str = DEFAULT_API_FORMAT
    provider: str | None = None

    @classmethod
    def parse(cls, value: str | dict[str, Any]) -> ModelSpec:
        if isinstance(value, str):
            return cls(model=value, api_format=infer_api_format(value))
        if isinstance(value, dict):
            model = str(value.get("model") or "")
            api_format = str(value.get("api_format") or infer_api_format(model))
            if api_format not in API_FORMATS:
                raise ValueError(f"invalid api_format: {api_format!r}")
            return cls(
                model=model,
                key_id=value.get("key_id") or None,
                api_format=api_format,
                provider=str(value.get("provider") or "").strip() or None,
            )
        raise ValueError(f"invalid model entry: {value!r}")

    def to_value(self) -> str | dict[str, str]:
        out: dict[str, str] = {"model": self.model}
        if self.key_id is not None:
            out["key_id"] = self.key_id
        out["api_format"] = self.api_format
        if self.provider is not None:
            out["provider"] = self.provider
        return out

    def to_display(self) -> str:
        if self.key_id is None:
            return self.model
        return f"{self.model} (key: {self.key_id})"


def infer_api_format(model: str) -> str:
    """Infer a protocol for built-in model defaults before explicit setup."""
    if model.startswith("responses/") or "/responses/" in model:
        return "openai_responses"
    prefix = model.split("/", 1)[0] if "/" in model else ""
    if prefix in ("anthropic", "bedrock", "gemini"):
        return prefix
    return DEFAULT_API_FORMAT


def find_config_file(start: Path | None = None) -> Path | None:
    """Search from cwd (or ``start``) upward for the nearest config file."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def find_env_file(start: Path | None = None) -> Path | None:
    """Search from cwd (or ``start``) upward for the nearest ``.env`` file."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / ".env"
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Config:
    """merged configuration; overrides are kept in-memory only."""

    config_path: Path | None = None
    root: Path = field(default_factory=Path.cwd)
    default_model: str = DEFAULT_MODEL_ALIAS
    models: dict[str, ModelSpec] = field(
        default_factory=lambda: {k: ModelSpec(v) for k, v in DEFAULT_MODELS.items()}
    )
    tools: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_TOOLS))
    permission_rules: dict[str, Any] = field(default_factory=dict)
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS
    extra_safe_dirs: list[str] = field(default_factory=list)
    permission_mode: str = PERM_ASK
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    compaction: dict[str, Any] = field(default_factory=lambda: dict(COMPACTION_DEFAULTS))
    model_limits_cache: dict[str, dict[str, int] | None] = field(default_factory=dict, repr=False)
    workspace_projects: list[dict[str, Any]] = field(default_factory=list)  # [{root, secondary}]
    skills_enabled: bool = True

    @classmethod
    def load(cls, start: Path | None = None) -> Config:
        load_dotenv(find_env_file(start), override=False)
        cfg_path = find_config_file(start)
        raw: dict[str, Any] = {}
        if cfg_path:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        # With a config file the workspace anchors at its directory; without
        # one it anchors at CWD itself (never CWD's parent — that would widen
        # the sandbox and write easycode.config.json outside the project).
        root = cfg_path.parent.resolve() if cfg_path else Path.cwd().resolve()
        # Built-in aliases seed a fresh config only; once the file carries a
        # `models` key (cfg.save always writes it) deletions must stick.
        raw_models = raw.get("models", DEFAULT_MODELS)
        models = {alias: ModelSpec.parse(value) for alias, value in (raw_models or {}).items()}
        tools = {**DEFAULT_TOOLS, **(raw.get("tools") or {})}
        workspace = raw.get("workspace") or {}
        # Only the documented key is read, and an invalid value is an error
        # instead of a silent fallback to the most permissive parse.
        permission_mode = permission_parse(str(raw.get("permission", PERM_ASK)))
        return cls(
            config_path=cfg_path,
            root=root,
            default_model=raw.get("default_model", DEFAULT_MODEL_ALIAS),
            models=models,
            tools=tools,
            permission_rules=dict(raw.get("permissions") or {}),
            max_tool_result_chars=int(
                raw.get("max_tool_result_chars", DEFAULT_MAX_TOOL_RESULT_CHARS)
            ),
            extra_safe_dirs=list(workspace.get("extra_safe_dirs") or []),
            workspace_projects=list(workspace.get("projects") or []),
            permission_mode=permission_mode,
            mcp_servers=dict(raw.get("mcp_servers") or {}),
            max_context_tokens=int(raw.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            compaction={**COMPACTION_DEFAULTS, **(raw.get("compaction") or {})},
            skills_enabled=_skills_enabled(raw),
        )

    def resolve_model(self, alias_or_model: str) -> str:
        """Resolve an alias to a litellm model string.

        Aliases map through ``self.models``; anything containing a provider
        prefix (``"provider/model"``) is passed through unchanged.
        """
        return self.model_spec(alias_or_model).model

    def model_spec(self, alias_or_model: str) -> ModelSpec:
        """Alias → :class:`ModelSpec`; unknown aliases pass through as literal model."""
        spec = self.models.get(alias_or_model)
        if spec is not None:
            return spec
        return ModelSpec.parse(alias_or_model)

    def set_model_alias(self, alias: str, model: str | dict[str, Any]) -> None:
        """Runtime alias override (in-memory); accepts str or {model, key_id}."""
        self.models[alias] = ModelSpec.parse(model)

    def rename_model_alias(self, old: str, new: str) -> None:
        """Rename a model alias in-place; no-op if names match."""
        if old == new:
            return
        if old not in self.models:
            raise KeyError(f"unknown alias: {old}")
        if new in self.models:
            raise ValueError(f"alias already exists: {new}")
        self.models[new] = self.models.pop(old)
        if self.default_model == old:
            self.default_model = new

    def set_default_model(self, alias: str) -> None:
        self.default_model = alias

    def get_model_limits(self, alias_or_model: str) -> dict[str, int] | None:
        """Model context/output token limits from litellm; None when unknown.

        Returns ``{"context": int, "output": int}`` or None. Results are cached
        per resolved model string; callers fall back to ``max_context_tokens``.
        """
        model = self.resolve_model(alias_or_model)
        if model in self.model_limits_cache:
            return self.model_limits_cache[model]
        limits: dict[str, int] | None = None
        try:
            import litellm

            info = litellm.get_model_info(model)
            max_in = info.get("max_input_tokens")
            if max_in:
                limits = {"context": int(max_in), "output": int(info.get("max_output_tokens") or 0)}
        except Exception:  # noqa: BLE001 - unknown model/custom provider → fallback
            limits = None
        self.model_limits_cache[model] = limits
        return limits

    def base_dir(self) -> Path:
        """Anchor for resolving relative workspace paths (config dir, not CWD)."""
        return self.config_path.parent if self.config_path else self.root

    def path_context(
        self, root: Path | None = None, secondary: list[Path] | None = None
    ) -> PathContext:
        """Sandbox context for an agent: config workspace + overrides.

        ``root``/``secondary`` override the config values when given (CLI/Web).
        Config paths are resolved relative to the config file location;
        ``~`` is expanded.
        """
        primary = (root or self.root).resolve()
        base = self.config_path.parent if self.config_path else Path.cwd()
        secondary_resolved = [resolve_workspace_path(raw, base) for raw in (secondary or [])]
        extra = [resolve_workspace_path(raw, base) for raw in self.extra_safe_dirs]
        return PathContext(primary=primary, secondary=secondary_resolved, extra_safe_dirs=extra)

    def save(self) -> None:
        """Persist current config to disk."""
        if not self.config_path:
            self.config_path = self.root / CONFIG_FILENAME
        payload = {
            "default_model": self.default_model,
            "models": {alias: spec.to_value() for alias, spec in self.models.items()},
            "tools": self.tools,
            "max_tool_result_chars": self.max_tool_result_chars,
            "max_context_tokens": self.max_context_tokens,
        }
        if self.permission_rules:
            payload["permissions"] = self.permission_rules
        workspace_payload: dict[str, Any] = {}
        if self.extra_safe_dirs:
            workspace_payload["extra_safe_dirs"] = self.extra_safe_dirs
        if self.workspace_projects:
            workspace_payload["projects"] = self.workspace_projects
        if workspace_payload:
            payload["workspace"] = workspace_payload
        if self.permission_mode != PERM_ASK:
            payload["permission"] = self.permission_mode
        if self.mcp_servers:
            payload["mcp_servers"] = self.mcp_servers
        if self.compaction != COMPACTION_DEFAULTS:
            payload["compaction"] = self.compaction
        if not self.skills_enabled:
            payload["skills"] = {"enabled": False}
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
