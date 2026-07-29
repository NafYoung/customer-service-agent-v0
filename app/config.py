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


settings = Settings()
