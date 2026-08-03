from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def resolve_demo_allowed_origin(
    *,
    explicit: str | None = None,
    render_external_url: str | None = None,
    railway_public_domain: str | None = None,
) -> str:
    """Resolve the browser Origin allowed for public_demo CSRF/CORS checks.

    Prefer an explicit DEMO_ALLOWED_ORIGIN. On managed hosts, fall back to the
    platform-provided public URL so a first deploy works without a circular
    "set Origin after you know the URL" step.
    """

    candidates = (
        explicit if explicit is not None else os.getenv("DEMO_ALLOWED_ORIGIN"),
        (
            render_external_url
            if render_external_url is not None
            else os.getenv("RENDER_EXTERNAL_URL")
        ),
    )
    for raw in candidates:
        value = (raw or "").strip().rstrip("/")
        if value:
            return value

    railway = (
        railway_public_domain
        if railway_public_domain is not None
        else os.getenv("RAILWAY_PUBLIC_DOMAIN")
    )
    railway_value = (railway or "").strip().rstrip("/")
    if railway_value:
        if railway_value.startswith("http://") or railway_value.startswith(
            "https://"
        ):
            return railway_value
        return f"https://{railway_value}"

    return "http://127.0.0.1:8000"


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the v0 service.

    The defaults are intentionally local-only. Production deployments should
    provide secrets and a managed database through environment variables.
    """

    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./customer_service.db")
    auth_session_minutes: int = int(os.getenv("AUTH_SESSION_MINUTES", "60"))
    approval_ttl_minutes: int = int(os.getenv("APPROVAL_TTL_MINUTES", "10"))
    demo_verification_code: str = os.getenv("DEMO_VERIFICATION_CODE", "246810")
    host_confirmation_token: str | None = os.getenv("HOST_CONFIRMATION_TOKEN") or None
    enable_debug_routes: bool = _env_bool("ENABLE_DEBUG_ROUTES", False)
    debug_admin_token: str | None = os.getenv("DEBUG_ADMIN_TOKEN") or None
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY") or None
    deepseek_base_url: str = os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com",
    )
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    deepseek_timeout_seconds: float = float(
        os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "30")
    )
    deepseek_max_tokens: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "1024"))
    deepseek_temperature: float = float(
        os.getenv("DEEPSEEK_TEMPERATURE", "0")
    )
    deepseek_max_retries: int = int(os.getenv("DEEPSEEK_MAX_RETRIES", "2"))
    agent_max_tool_rounds: int = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "4"))
    agent_max_tool_calls: int = int(os.getenv("AGENT_MAX_TOOL_CALLS", "12"))
    app_mode: str = os.getenv("APP_MODE", "local").strip() or "local"
    demo_agent_mode: str = (
        os.getenv("DEMO_AGENT_MODE", "offline_replay").strip() or "offline_replay"
    )
    demo_allowed_origin: str = resolve_demo_allowed_origin()
    demo_cookie_secure: bool = _env_bool("DEMO_COOKIE_SECURE", True)
    demo_session_ttl_minutes: int = int(os.getenv("DEMO_SESSION_TTL_MINUTES", "30"))
    demo_max_active_sessions: int = int(os.getenv("DEMO_MAX_ACTIVE_SESSIONS", "50"))
    demo_max_messages_per_session: int = int(
        os.getenv("DEMO_MAX_MESSAGES_PER_SESSION", "30")
    )
    demo_max_prepare_per_session: int = int(
        os.getenv("DEMO_MAX_PREPARE_PER_SESSION", "3")
    )
    demo_max_confirm_per_session: int = int(
        os.getenv("DEMO_MAX_CONFIRM_PER_SESSION", "3")
    )


settings = Settings()
