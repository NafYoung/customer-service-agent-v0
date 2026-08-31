from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.host_runtime import build_host_preparation_model_factory
from app.agent.openai_compatible import ChatModel
from app.api.host_routes import host_router
from app.api.routes import debug_router, router
from app.config import Settings
from app.config import settings as default_settings
from app.database import Database
from app.errors import ServiceError
from app.host.flow import HostPilotFlow
from app.seed import seed_demo_data
from app.tools.factory import build_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_app(
    *,
    settings: Settings | None = None,
    database_url: str | None = None,
    seed_demo: bool = True,
    preparation_model_factory: Callable[[str], ChatModel] | None = None,
) -> FastAPI:
    runtime_settings = settings or default_settings
    if database_url is not None:
        runtime_settings = replace(runtime_settings, database_url=database_url)
    if runtime_settings.enable_debug_routes and not runtime_settings.debug_admin_token:
        raise ValueError(
            "DEBUG_ADMIN_TOKEN must be configured when ENABLE_DEBUG_ROUTES is true"
        )
    model_factory = preparation_model_factory
    if model_factory is None and runtime_settings.enable_live_preparation_agent:
        model_factory = build_host_preparation_model_factory(runtime_settings)

    database = Database(runtime_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.create_all()
        if seed_demo:
            seed_demo_data(database, runtime_settings)
        try:
            yield
        finally:
            database.engine.dispose()

    app = FastAPI(
        title="RIVET Customer Service Agent Backend v0",
        version="0.1.0",
        description=(
            "A deterministic transaction layer for a future e-commerce customer "
            "service agent. All data and policies are synthetic."
        ),
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.settings = runtime_settings
    app.state.tools = build_tools(runtime_settings, policy_dir=PROJECT_ROOT / "policies")
    app.state.host_flow = HostPilotFlow(
        tools=app.state.tools,
        settings=runtime_settings,
        preparation_model_factory=model_factory,
    )
    app.include_router(router)
    app.include_router(host_router)
    if runtime_settings.enable_debug_routes:
        app.include_router(debug_router)

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app


app = create_app()
