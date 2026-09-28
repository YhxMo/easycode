"""FastAPI app: sessions + SSE chat + model management."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from easycode.config import Config
from easycode.web.bridge import ApprovalBroker
from easycode.web.middleware import _LocalOriginMiddleware
from easycode.web.routes_chat import register_chat
from easycode.web.routes_files import register_files
from easycode.web.routes_mcp import register_mcp
from easycode.web.routes_models import register_models
from easycode.web.routes_sessions import register_sessions
from easycode.web.routes_skills import register_skills
from easycode.web.routes_workspaces import register_workspaces
from easycode.web.session import SessionBusyError, SessionStore

FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(
    cfg: Config | None = None,
    session_store: SessionStore | None = None,
    static_dir: Path | None = None,
    approval_broker: ApprovalBroker | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8000,
) -> FastAPI:
    """App factory. ``session_store``/``approval_broker`` injectable for tests.

    ``bind_host``/``bind_port`` are the values ``cli.web`` passes to uvicorn; the
    origin guard uses them to decide whether a state-change request is same-origin
    and whether a bearer token is required (non-loopback bind -> token required).
    They default to the loopback CLI values. To be safe this should match the
    actual listener; the CLI passes the resolved host/port through to uvicorn.
    """
    from easycode.agent.factory import make_agent

    cfg = cfg or Config.load()
    broker = approval_broker or ApprovalBroker()

    def _factory(alias: str, *, defer_credential: bool, **agent_kwargs: object) -> object:
        root_kw = agent_kwargs.get("root")
        session_root = Path(root_kw).resolve() if isinstance(root_kw, str) and root_kw else cfg.root
        roots = agent_kwargs.get("secondary_roots") if agent_kwargs.get("secondary_roots") else None
        secondary = [Path(r).resolve() for r in (roots or [])] or None
        return make_agent(
            cfg,
            alias,
            session_root,
            secondary_roots=secondary,
            defer_credential=defer_credential,
        )

    def factory(alias: str, **agent_kwargs: object) -> object:
        return _factory(alias, defer_credential=False, **agent_kwargs)

    def restore_factory(alias: str, **agent_kwargs: object) -> object:
        return _factory(alias, defer_credential=True, **agent_kwargs)

    store = session_store or SessionStore(cfg, cfg.root, factory, restore_factory=restore_factory)
    store.load_all()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Release every session-owned MCP process when the server stops.
        for sess in store.list():
            await sess.agent.close_mcp()

    app = FastAPI(title="Easy code", version="0.1.0", lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(SessionBusyError)
    async def _session_busy(_request, exc: SessionBusyError) -> JSONResponse:
        """One 409 mapping for every route that mutates a busy session."""
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost: reject cross-origin state-change requests before CORS sees them.
    app.add_middleware(_LocalOriginMiddleware, bind_host=bind_host, bind_port=bind_port)

    register_models(app, cfg, store)
    register_workspaces(app, cfg, store)
    register_sessions(app, store, broker)
    register_chat(app, cfg, store, broker)
    register_files(app, cfg, store)
    register_mcp(app, cfg, store)
    register_skills(app, cfg, store)

    dist = static_dir or FRONTEND_DIST
    if dist.is_dir():
        from fastapi.responses import FileResponse

        # mounting StaticFiles at "/" turns unmatched POST /api/* into an
        # unclear 405; serve static files via a GET-only SPA fallback instead.
        @app.api_route(
            "/api/{path:path}",
            methods=["POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        def _api_unmatched(path: str) -> None:
            raise HTTPException(404, f"unknown api route: /api/{path}")

        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    return app
