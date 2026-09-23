"""Model configuration, credential storage, and live-session model switching."""

from __future__ import annotations

from easycode.agentfactory import provider_kwargs, rebind_agent
from easycode.config import DEFAULT_MODEL_ALIAS, Config, ModelSpec
from easycode.credentials import (
    Credential,
    delete_credential,
    load_credentials,
    new_credential_id,
    save_credential,
)

FORMAT_PROVIDERS = {
    "openai_responses": "openai",
    "openai_compatible": "openai",
    "anthropic": "anthropic",
    "bedrock": "bedrock",
    "gemini": "gemini",
}


def infer_provider(model: str, api_format: str) -> str:
    """Infer the supplier independently from the wire API format."""
    prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
    if prefix and prefix != "responses":
        return prefix
    return FORMAT_PROVIDERS.get(api_format, "custom")


def _display_provider(spec: ModelSpec, credential: Credential | None) -> str:
    """Supplier label for display: spec → credential → model/format inference."""
    return (
        spec.provider
        or (credential.provider if credential else None)
        or infer_provider(spec.model, spec.api_format)
    )


def models_response(cfg: Config) -> dict:
    """Snapshot of model records (no API keys leaked)."""
    providers: dict[str, str] = {}
    limits: dict[str, dict[str, int] | None] = {}
    credentials = load_credentials()
    for alias, spec in cfg.models.items():
        credential = credentials.get(spec.key_id) if spec.key_id else None
        providers[alias] = _display_provider(spec, credential)
        limits[alias] = cfg.get_model_limits(alias)
    return {
        "default": cfg.default_model,
        "models": {alias: spec.to_value() for alias, spec in cfg.models.items()},
        "providers": providers,
        "limits": limits,
    }


def get_model_detail(cfg: Config, alias: str) -> dict:
    """One model's editable configuration (never the raw API key)."""
    spec = cfg.models.get(alias)
    if spec is None:
        raise LookupError(alias)
    cred = load_credentials().get(spec.key_id) if spec.key_id else None
    api_format = spec.api_format
    provider = _display_provider(spec, cred)
    api_key = cred.api_key if cred else ""
    base_url = cred.base_url if cred else None
    return {
        "alias": alias,
        "model": spec.model,
        "key_id": spec.key_id,
        "provider": provider,
        "base_url": base_url,
        "api_format": api_format,
        "has_api_key": bool(api_key),
        "key_tail": (cred.masked().get("key_tail") or "") if cred else "",
    }


def add_model(
    cfg: Config,
    *,
    alias: str,
    model: str,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    api_format: str,
) -> dict:
    """Create a model alias (with optional credential) and persist."""
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip() or None
    key_id = None
    if api_key or base_url:
        key_id = new_credential_id()
        save_credential(
            Credential(
                key_id=key_id,
                api_key=api_key,
                provider=provider,
                base_url=base_url,
            )
        )
    entry: dict[str, str] = {"model": model, "api_format": api_format}
    if provider and provider.strip():
        entry["provider"] = provider.strip()
    if key_id:
        entry["key_id"] = key_id
    cfg.set_model_alias(alias, entry)
    if cfg.default_model not in cfg.models:
        cfg.set_default_model(alias)
    cfg.save()
    return models_response(cfg)


def update_model(
    cfg: Config,
    store,
    *,
    alias: str,
    target_alias: str,
    model: str,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    clear_key: bool,
    api_format: str,
) -> dict:
    """Update a model's config/credential, then rebind sessions on the alias."""
    spec = cfg.models.get(alias)
    if spec is None:
        raise LookupError(alias)

    current_cred = load_credentials().get(spec.key_id) if spec.key_id else None
    if clear_key:
        if spec.key_id:
            delete_credential(spec.key_id)
        key_id = None
    else:
        next_api_key = (
            api_key if api_key is not None else (current_cred.api_key if current_cred else "")
        ).strip()
        next_base_url = (
            base_url if base_url is not None else (current_cred.base_url if current_cred else None)
        )
        if spec.key_id or next_api_key or next_base_url:
            key_id = spec.key_id or new_credential_id()
            save_credential(
                Credential(
                    key_id=key_id,
                    api_key=next_api_key,
                    provider=provider
                    if provider is not None
                    else (current_cred.provider if current_cred else None),
                    base_url=next_base_url,
                )
            )
        else:
            key_id = None

    if target_alias != alias:
        cfg.rename_model_alias(alias, target_alias)
    resolved_provider = provider.strip() if provider is not None else spec.provider
    entry: dict[str, str] = {"model": model, "api_format": api_format}
    if resolved_provider:
        entry["provider"] = resolved_provider
    if key_id:
        entry["key_id"] = key_id
    cfg.set_model_alias(target_alias, entry)
    cfg.save()

    for sess in store.list():
        if sess.model_alias != alias:
            continue
        # Editing a model may leave it without a usable credential; keep
        # such sessions on the old binding instead of failing the save.
        try:
            rebind_agent(sess.agent, cfg, target_alias)
        except ValueError:
            continue
        sess.model_alias = target_alias
        store.record_exchange(sess)

    return models_response(cfg)


def delete_model(cfg: Config, *, alias: str) -> dict:
    """Delete a model alias (and its credential), fixing default if needed."""
    spec = cfg.models[alias]
    del cfg.models[alias]
    if spec.key_id:
        delete_credential(spec.key_id)
    if cfg.default_model == alias:
        cfg.set_default_model(next(iter(cfg.models), DEFAULT_MODEL_ALIAS))
    cfg.save()
    return models_response(cfg)


def switch_default(cfg: Config, store, *, alias: str) -> dict:
    """Switch the default model and rebind every live session (atomic)."""
    # Validate the target model BEFORE mutating anything: switching to a model
    # without a usable credential must fail atomically — no default flip on
    # disk, no partially rebound sessions.
    provider_kwargs(cfg, alias)
    for sess in store.list():
        rebind_agent(sess.agent, cfg, alias)
        sess.model_alias = alias
        # Flush per-session alias so a restart cannot silently revert it.
        store.record_exchange(sess)
    cfg.set_default_model(alias)
    cfg.save()
    return models_response(cfg)
