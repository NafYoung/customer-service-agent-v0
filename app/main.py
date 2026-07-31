from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import debug_router, router
from app.config import Settings
from app.config import settings as default_settings
from app.database import Database
from app.demo import APP_MODE_PUBLIC_DEMO, DEMO_AGENT_MODE_OFFLINE_REPLAY
from app.demo.routes import demo_index, handle_demo_service_error
from app.demo.routes import router as demo_router
from app.demo.session import DemoSessionManager
from app.errors import ServiceError
from app.seed import seed_demo_data
from app.tools.factory import build_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_STATIC_DIR = Path(__file__).resolve().parent / "static" / "demo"


def _normalize_public_demo_settings(settings: Settings) -> Settings:
    """Fail closed for public demo: offline replay only, no live model key."""

    if settings.app_mode != APP_MODE_PUBLIC_DEMO:
        return settings
    if settings.demo_agent_mode != DEMO_AGENT_MODE_OFFLINE_REPLAY:
        raise ValueError(
            "APP_MODE=public_demo requires DEMO_AGENT_MODE=offline_replay "
            "(refuse live model network)"
        )
    if settings.enable_debug_routes:
        raise ValueError("APP_MODE=public_demo refuses ENABLE_DEBUG_ROUTES")
    host_token = settings.host_confirmation_token
    if not host_token:
        host_token = "public-demo-host-token-not-for-browser"
    return replace(
        settings,
        deepseek_api_key=None,
        host_confirmation_token=host_token,
        enable_debug_routes=False,
        debug_admin_token=None,
        demo_agent_mode=DEMO_AGENT_MODE_OFFLINE_REPLAY,
    )


def create_app(
    *,
    settings: Settings | None = None,
    database_url: str | None = None,
    seed_demo: bool = True,
) -> FastAPI:
    runtime_settings = settings or default_settings
    if database_url is not None:
        runtime_settings = replace(runtime_settings, database_url=database_url)
    runtime_settings = _normalize_public_demo_settings(runtime_settings)

    if runtime_settings.enable_debug_routes and not runtime_settings.debug_admin_token:
        raise ValueError(
            "DEBUG_ADMIN_TOKEN must be configured when ENABLE_DEBUG_ROUTES is true"
        )

    public_demo = runtime_settings.app_mode == APP_MODE_PUBLIC_DEMO
    database = Database(
        "sqlite:///:memory:" if public_demo else runtime_settings.database_url
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.create_all()
        if seed_demo and not public_demo:
            seed_demo_data(database, runtime_settings)
        try:
            yield
        finally:
            if public_demo and hasattr(app.state, "demo_sessions"):
                app.state.demo_sessions.dispose_all()
            database.engine.dispose()

    app = FastAPI(
        title=(
            "RIVET Public Demo"
            if public_demo
            else "RIVET Customer Service Agent Backend v0"
        ),
        version="0.1.0",
        description=(
            "Same-origin offline public demo: prepare → host card → confirm → execute."
            if public_demo
            else (
                "A deterministic transaction layer for a future e-commerce customer "
                "service agent. All data and policies are synthetic."
            )
        ),
        lifespan=lifespan,
        docs_url=None if public_demo else "/docs",
        redoc_url=None if public_demo else "/redoc",
        openapi_url=None if public_demo else "/openapi.json",
    )
    app.state.database = database
    app.state.settings = runtime_settings
    app.state.tools = build_tools(runtime_settings, policy_dir=PROJECT_ROOT / "policies")
    app.state.provider_http_calls = 0

    if public_demo:
        app.state.demo_sessions = DemoSessionManager(runtime_settings)
        app.include_router(demo_router)
        app.add_api_route("/", demo_index, methods=["GET"], include_in_schema=False)
        if DEMO_STATIC_DIR.is_dir():
            app.mount(
                "/demo-static",
                StaticFiles(directory=str(DEMO_STATIC_DIR)),
                name="demo-static",
            )
    else:
        app.include_router(router)
        if runtime_settings.enable_debug_routes:
            app.include_router(debug_router)

    @app.exception_handler(ServiceError)
    async def handle_service_error(request: Request, exc: ServiceError) -> JSONResponse:
        if public_demo:
            return handle_demo_service_error(request, exc)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        payload: dict[str, object] = {"status": "ok", "version": "0.1.0"}
        if public_demo:
            payload["app_mode"] = APP_MODE_PUBLIC_DEMO
            payload["demo_agent_mode"] = DEMO_AGENT_MODE_OFFLINE_REPLAY
            payload["provider_http_calls"] = 0
        return payload

    return app


app = create_app()
