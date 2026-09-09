"""Neutral agent factory shared by the CLI and the web control plane.

This module owns the provider-building chain (``provider_kwargs`` /
``build_provider`` / ``build_summarizer``) and the agent assembly
(``make_agent`` / ``rebind_agent``). Both ``easycode.cli`` and ``easycode.web``
consume it as peers; ``easycode.cli`` re-exports these names for backwards
compatibility, so this module deliberately depends on no CLI or web module.
"""

from __future__ import annotations

from pathlib import Path

from easycode.agent.loop import Agent
from easycode.config import API_FORMATS, Config
from easycode.credentials import load_credentials
from easycode.models.litellm_provider import LiteLLMProvider
from easycode.tools import build_registry

KNOWN_PROVIDER_PREFIXES = {
    "openai",
    "anthropic",
    "deepseek",
    "openrouter",
    "bedrock",
    "gemini",
    "azure",
    "vertex_ai",
    "mistral",
    "groq",
    "xai",
    "ollama",
    "ollama_chat",
}


def _strip_provider_prefix(model: str) -> str:
    prefix, sep, rest = model.partition("/")
    return rest if sep and prefix in KNOWN_PROVIDER_PREFIXES else model


def apply_api_format(model: str, api_format: str) -> tuple[str, str | None]:
    """Map an interface format to a ``(litellm model, custom_llm_provider)`` pair.

    The format is authoritative: any known provider prefix on the stored model
    string is stripped so the chosen wire protocol wins.

    - ``openai_compatible`` → OpenAI-compatible chat completions (provider ``openai``)
    - ``openai_responses`` → OpenAI Responses API via the ``responses/`` route
    - ``anthropic`` / ``bedrock`` / ``gemini`` → the matching litellm provider

    Unknown formats are rejected by ``provider_kwargs`` before this helper is
    called.
    """
    stripped = _strip_provider_prefix(model)
    if api_format == "openai_compatible":
        return stripped, "openai"
    if api_format == "openai_responses":
        if stripped.startswith("responses/"):
            return stripped, "openai"
        return f"responses/{stripped}", "openai"
    if api_format in ("anthropic", "bedrock", "gemini"):
        return stripped, api_format
    return model, None


def provider_kwargs(cfg: Config, alias: str) -> tuple[str, dict]:
    """(litellm model, provider kwargs) for an alias, wired to credentials.

    The interface format is the only routing selector. API credentials are
    always read from the model's own credential record.
    """
    spec = cfg.model_spec(alias)
    model = spec.model
    if spec.api_format not in API_FORMATS:
        raise ValueError(f"invalid api_format '{spec.api_format}' for model '{alias}'")
    if not spec.key_id:
        raise ValueError(
            f"model '{alias}' has no credential configured"
            "（请在该模型的编辑对话框中填写 API Key）"
        )
    cred = load_credentials().get(spec.key_id)
    if cred is None:
        raise ValueError(
            f"credential '{spec.key_id}' not found for model '{alias}' "
            f"(configure it in the Web model editor)"
        )

    kwargs: dict = {}
    if cred.api_key:
        kwargs["api_key"] = cred.api_key
    if cred.base_url:
        kwargs["api_base"] = cred.base_url
    if not cred.api_key and spec.api_format != "bedrock":
        raise ValueError(f"model '{alias}' has no API key configured")

    model, fmt_provider = apply_api_format(model, spec.api_format)
    if fmt_provider:
        kwargs["custom_llm_provider"] = fmt_provider
    return model, kwargs


def build_provider(cfg: Config, alias: str) -> LiteLLMProvider:
    """Build a provider using the model's explicit credential profile."""
    model, kwargs = provider_kwargs(cfg, alias)
    return LiteLLMProvider(model, **kwargs)


def build_summarizer(cfg: Config, alias: str, max_chars: int = 8_000):
    """LLM summarizer for an alias (same credentials as the stream provider)."""
    from easycode.agent.summarizer import LLMSummarizer

    model, kwargs = provider_kwargs(cfg, alias)
    return LLMSummarizer(model, max_chars=max_chars, **kwargs)


def make_agent(
    cfg: Config,
    model_alias: str,
    root: Path,
    secondary_roots: list[Path] | None = None,
    extra_safe_dirs: list[Path] | None = None,
) -> Agent:
    provider = build_provider(cfg, model_alias)
    registry = build_registry(cfg.max_tool_result_chars)
    enabled = {name for name, on in cfg.tools.items() if on}
    discovery_roots = [root, *(Path(p) for p in (secondary_roots or []))]
    from easycode.agents import AgentRegistry
    from easycode.skills import SkillRegistry

    agents = AgentRegistry.discover(discovery_roots)
    skills = SkillRegistry.discover(discovery_roots) if cfg.skills_enabled else None
    agent = Agent(
        provider=provider,
        registry=registry,
        root=root,
        enabled_tools=enabled,
        secondary_roots=list(secondary_roots or []),
        extra_safe_dirs=list(extra_safe_dirs or []),
        permission_mode=cfg.permission_mode,
        permission_rules=dict(cfg.permission_rules),
        mcp_servers=cfg.mcp_servers,
        max_context_tokens=cfg.max_context_tokens,
        compaction=dict(cfg.compaction),
        model_limits=cfg.get_model_limits(model_alias),
        summarizer=build_summarizer(
            cfg, model_alias, max_chars=int(cfg.compaction.get("summary_max_chars", 8_000))
        ),
        agents=agents,
        skills=skills,
    )
    from easycode.reviewer import AutoReviewer

    reviewer = AutoReviewer(build_provider(cfg, model_alias))
    agent.review_handler = reviewer.review
    return agent


def rebind_agent(agent: Agent, cfg: Config, alias: str) -> None:
    """Swap the provider on the existing agent, keeping history.

    Public name; ``easycode.cli`` re-exports it as ``_rebind_agent`` for
    backwards compatibility with any callers that referenced the old name.
    """
    agent.provider = build_provider(cfg, alias)
    from easycode.reviewer import AutoReviewer

    agent.review_handler = AutoReviewer(build_provider(cfg, alias)).review
    agent.summarizer = build_summarizer(
        cfg, alias, max_chars=int(cfg.compaction.get("summary_max_chars", 8_000))
    )
    agent.model_limits = cfg.get_model_limits(alias)
    agent.history.max_tokens = agent._usable_tokens()
