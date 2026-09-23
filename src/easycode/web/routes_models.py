"""HTTP endpoints for models."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.config import API_FORMATS, DEFAULT_API_FORMAT, Config
from easycode.web import services
from easycode.web.session import SessionStore


class ModelRequest(BaseModel):
    alias: str


class AddModelRequest(BaseModel):
    alias: str
    model: str
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_format: str = DEFAULT_API_FORMAT


class UpdateModelRequest(BaseModel):
    model: str
    new_alias: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    clear_key: bool = False
    api_format: str | None = None


def register_models(app: FastAPI, cfg: Config, store: SessionStore) -> None:
    @app.get("/api/models")
    def get_models() -> dict:
        """Return model records without exposing API keys."""
        reloaded = Config.load(start=cfg.root)
        cfg.models = reloaded.models
        cfg.default_model = reloaded.default_model
        return services.models_response(cfg)

    @app.post("/api/models/add")
    def add_model(req: AddModelRequest) -> dict:
        alias = req.alias.strip()
        model = req.model.strip()
        if not alias or not model:
            raise HTTPException(422, "alias and model are required")
        api_format = req.api_format
        if api_format not in API_FORMATS:
            raise HTTPException(422, f"unsupported api_format: {api_format}")
        if alias in cfg.models:
            raise HTTPException(409, f"model alias already exists: {alias}")
        return services.add_model(
            cfg,
            alias=alias,
            model=model,
            provider=req.provider,
            base_url=req.base_url,
            api_key=req.api_key,
            api_format=api_format,
        )

    @app.get("/api/models/{alias}")
    def get_model_detail(alias: str) -> dict:
        """Return one model's editable configuration.

        The raw API key is never returned to the browser — only a ``key_tail``
        hint (readability) and whether a key is configured (``has_api_key``).
        This keeps the localhost edit dialog usable without ever exposing the
        secret, matching what the collection endpoint already promises.
        """
        if cfg.models.get(alias) is None:
            raise HTTPException(404, f"unknown alias: {alias}")
        return services.get_model_detail(cfg, alias)

    @app.put("/api/models/{alias}")
    def update_model(alias: str, req: UpdateModelRequest) -> dict:
        spec = cfg.models.get(alias)
        target_alias = (req.new_alias or alias).strip()
        model = req.model.strip()
        if spec is None:
            raise HTTPException(404, f"unknown alias: {alias}")
        if not target_alias or not model:
            raise HTTPException(422, "alias and model are required")
        if target_alias != alias and target_alias in cfg.models:
            raise HTTPException(409, f"alias already exists: {target_alias}")

        api_format = req.api_format or spec.api_format
        if api_format not in API_FORMATS:
            raise HTTPException(422, f"unsupported api_format: {api_format}")
        return services.update_model(
            cfg,
            store,
            alias=alias,
            target_alias=target_alias,
            model=model,
            provider=req.provider,
            base_url=req.base_url,
            api_key=req.api_key,
            clear_key=req.clear_key,
            api_format=api_format,
        )

    @app.delete("/api/models/{alias}")
    def delete_model(alias: str) -> dict:
        if alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {alias}")
        return services.delete_model(cfg, alias=alias)

    @app.post("/api/models")
    def set_model(req: ModelRequest) -> dict:
        if req.alias not in cfg.models:
            raise HTTPException(404, f"unknown alias: {req.alias}")
        # Validate the target model BEFORE mutating anything: switching to a
        # model without a usable credential must fail atomically — no default
        # flip on disk, no partially rebound sessions (ValueError -> 422).
        try:
            return services.switch_default(cfg, store, alias=req.alias)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
