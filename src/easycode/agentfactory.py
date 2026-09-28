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


def bind_agent(agent: Agent, cfg: Config, alias: str) -> Provider:
    """Bind provider/summarizer/reviewer/limits for ``alias`` onto ``agent``.

    ``provider_kwargs`` raises ``ValueError`` when the alias has no usable
    credential; the whole binding is built before the first assignment, so a
    failure never leaves a mix of old and new components. The provider is
    returned for ``DeferredProvider``'s first resolve.
    """
    from easycode.agent.summarizer import LLMSummarizer
    from easycode.permissions.reviewer import AutoReviewer

    model, kwargs = provider_kwargs(cfg, alias)
    provider = LiteLLMProvider(model, **kwargs)
    # The reviewer gets its own kwargs copy because subagents may mutate
    # ``provider.kwargs``.
    reviewer = AutoReviewer(LiteLLMProvider(model, **dict(kwargs))).review
    summarizer = LLMSummarizer(
        model, max_chars=int(cfg.compaction["summary_max_chars"]), **kwargs
    )
    limits = cfg.get_model_limits(alias)
    max_tokens = agent.compactor.usable_tokens(agent.max_context_tokens, limits)

    agent.provider = provider
    agent.summarizer = summarizer
    agent.review_handler = reviewer
    agent.model_limits = limits
    agent.model_alias = alias
    agent.history.max_tokens = max_tokens
    return provider


def defer_binding(agent: Agent, cfg: Config, alias: str) -> None:
    """Point ``agent`` at ``alias`` but resolve credentials on first use.

    Used when a model edit/delete leaves a session without a usable provider:
    the alias rename still takes effect and persists, while the deferred
    provider makes the first send fail with the existing 422 path (and recover
    in place once the credential is restored).
    """
    agent.provider = DeferredProvider(lambda: bind_agent(agent, cfg, alias))
    agent.summarizer = None
    agent.review_handler = None
    agent.model_limits = None
    agent.model_alias = alias
    agent.history.max_tokens = agent.compactor.usable_tokens(agent.max_context_tokens, None)


def make_agent(
    cfg: Config,
    model_alias: str,
    root: Path,
    secondary_roots: list[Path] | None = None,
    *,
    defer_credential: bool = False,
) -> Agent:
    registry = build_registry(cfg.max_tool_result_chars)
    disabled = {name for name, on in cfg.tools.items() if not on}
    discovery_roots = [root, *(Path(p) for p in (secondary_roots or []))]
    from easycode.agents import AgentRegistry
    from easycode.skills import SkillRegistry

    agents = AgentRegistry.discover(discovery_roots)
    skills = SkillRegistry.discover(discovery_roots) if cfg.skills_enabled else None
    agent = Agent(
        provider=LiteLLMProvider(model_alias),  # rebound below from one credential read
        registry=registry,
        root=root,
        disabled_tools=disabled,
        secondary_roots=list(secondary_roots or []),
        extra_safe_dirs=cfg.path_context(root=root, secondary=[]).extra_safe_dirs,
        permission_mode=cfg.permission_mode,
        permission_rules=dict(cfg.permission_rules),
        mcp_servers=cfg.mcp_servers,
        max_context_tokens=cfg.max_context_tokens,
        compaction=dict(cfg.compaction),
        agents=agents,
        skills=skills,
        max_tool_iterations=cfg.max_tool_iterations,
    )
    try:
        bind_agent(agent, cfg, model_alias)
    except ValueError:
        if not defer_credential:
            raise
        # Restoring a session must not fail because the model currently has no
        # credential; the binding is retried on the first turn (routes 422).
        defer_binding(agent, cfg, model_alias)
    # Delegated work runs where its parent runs: the roots are read when a
    # subtask is built, so a session that changed its workspace (or its
    # secondary directories) does not hand the subtask the old ones.
    agent.subagent_factory = lambda alias: make_agent(
        cfg, alias, agent.root, list(agent.secondary_roots)
    )
    return agent
