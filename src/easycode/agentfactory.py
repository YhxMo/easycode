"""Build and configure agents for CLI and Web entry points."""

from __future__ import annotations

from pathlib import Path

from easycode.agent.loop import Agent
from easycode.config import API_FORMATS, Config
from easycode.credentials import load_credentials
from easycode.models.base import DeferredProvider, Provider
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
            f"model '{alias}' has no credential configured（请在该模型的编辑对话框中填写 API Key）"
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


def _bind_model(agent: Agent, cfg: Config, alias: str) -> None:
    """Bind provider/summarizer/reviewer/limits for ``alias`` onto ``agent``.

    ``provider_kwargs`` is read once per binding. The reviewer gets its own
    kwargs copy because subagents may mutate ``provider.kwargs``.
    """
    from easycode.agent.summarizer import LLMSummarizer
    from easycode.reviewer import AutoReviewer

    model, kwargs = provider_kwargs(cfg, alias)
    agent.provider = LiteLLMProvider(model, **kwargs)
    agent.review_handler = AutoReviewer(LiteLLMProvider(model, **dict(kwargs))).review
    agent.summarizer = LLMSummarizer(
        model, max_chars=int(cfg.compaction["summary_max_chars"]), **kwargs
    )
    agent.model_limits = cfg.get_model_limits(alias)
    agent.model_alias = alias
    agent.history.max_tokens = agent.compactor.usable_tokens(
        agent.max_context_tokens, agent.model_limits
    )


rebind_agent = _bind_model


def _bind_now(agent: Agent, cfg: Config, alias: str) -> Provider:
    """Bind the model now; used by a deferred restore's first resolve."""
    rebind_agent(agent, cfg, alias)
    return agent.provider


def make_agent(
    cfg: Config,
    model_alias: str,
    root: Path,
    secondary_roots: list[Path] | None = None,
    *,
    defer_credential: bool = False,
) -> Agent:
    registry = build_registry(cfg.max_tool_result_chars)
    enabled = {name for name, on in cfg.tools.items() if on}
    discovery_roots = [root, *(Path(p) for p in (secondary_roots or []))]
    from easycode.agents import AgentRegistry
    from easycode.skills import SkillRegistry

    agents = AgentRegistry.discover(discovery_roots)
    skills = SkillRegistry.discover(discovery_roots) if cfg.skills_enabled else None
    agent = Agent(
        provider=LiteLLMProvider(model_alias),  # rebound below from one credential read
        registry=registry,
        root=root,
        enabled_tools=enabled,
        secondary_roots=list(secondary_roots or []),
        extra_safe_dirs=cfg.path_context(root=root, secondary=[]).extra_safe_dirs,
        permission_mode=cfg.permission_mode,
        permission_rules=dict(cfg.permission_rules),
        mcp_servers=cfg.mcp_servers,
        max_context_tokens=cfg.max_context_tokens,
        compaction=dict(cfg.compaction),
        agents=agents,
        skills=skills,
    )
    try:
        rebind_agent(agent, cfg, model_alias)
    except ValueError:
        if not defer_credential:
            raise
        # Restoring a session must not fail because the model currently has no
        # credential; the binding is retried on the first turn (routes 422).
        agent.provider = DeferredProvider(lambda: _bind_now(agent, cfg, model_alias))
    agent.subagent_factory = lambda alias: make_agent(cfg, alias, root, secondary_roots)
    return agent
