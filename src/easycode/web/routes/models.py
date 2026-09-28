"""HTTP endpoints for models."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.config import API_FORMATS, DEFAULT_API_FORMAT, Config
from easycode.web import model_admin
from easycode.web.session import SessionStore, idle_sessions, run_mutation


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
        """Return model records without exposing API keys (in-memory config)."""
        return model_admin.models_response(cfg)

    @app.post("/api/models/add")
    async def add_model(req: AddModelRequest) -> dict:
        alias = req.alias.strip()
        model = req.model.strip()
        if not alias or not model:
            raise HTTPException(422, "alias and model are required")
        api_format = req.api_format
        if api_format not in API_FORMATS:
            raise HTTPException(422, f"unsupported api_format: {api_format}")
        async with store.config_change():
            if alias in cfg.models:
                raise HTTPException(409, f"model alias already exists: {alias}")
            return model_admin.add_model(
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
        return model_admin.get_model_detail(cfg, alias)

    @app.put("/api/models/{alias}")
    async def update_model(alias: str, req: UpdateModelRequest) -> dict:
        target_alias = (req.new_alias or alias).strip()
        model = req.model.strip()
        if not target_alias or not model:
            raise HTTPException(422, "alias and model are required")
        async with store.config_change():
            spec = cfg.models.get(alias)
            if spec is None:
                raise HTTPException(404, f"unknown alias: {alias}")
            if target_alias != alias and target_alias in cfg.models:
                raise HTTPException(409, f"alias already exists: {target_alias}")
            api_format = req.api_format or spec.api_format
            if api_format not in API_FORMATS:
                raise HTTPException(422, f"unsupported api_format: {api_format}")
            affected = [s for s in store.list() if s.model_alias == alias]
            async with idle_sessions(affected):
                return await run_mutation(
                    model_admin.update_model,
                    cfg,
                    store,
                    affected,
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
    async def delete_model(alias: str) -> dict:
        async with store.config_change():
            if alias not in cfg.models:
                raise HTTPException(404, f"unknown alias: {alias}")
            affected = [s for s in store.list() if s.model_alias == alias]
            async with idle_sessions(affected):
                return await run_mutation(
                    model_admin.delete_model, cfg, store, affected, alias=alias
                )

    @app.post("/api/models")
    async def set_model(req: ModelRequest) -> dict:
        async with store.config_change():
            if req.alias not in cfg.models:
                raise HTTPException(404, f"unknown alias: {req.alias}")
            # Validate the target model BEFORE mutating anything: switching to a
            # model without a usable credential must fail atomically — no
            # default flip on disk, no partially rebound sessions
            # (ValueError -> 422). Switching rebinds every live session, so all
            # of them must be idle.
            sessions = store.list()
            try:
                async with idle_sessions(sessions):
                    return await run_mutation(
                        model_admin.switch_default, cfg, store, sessions, alias=req.alias
                    )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
