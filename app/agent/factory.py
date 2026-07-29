from __future__ import annotations

import httpx

from app.agent.openai_compatible import (
    AttemptBudgetGuard,
    OpenAICompatibleChatClient,
)
from app.config import Settings


def build_deepseek_client(
    settings: Settings,
    *,
    budget_guard: AttemptBudgetGuard | None = None,
    transport: httpx.BaseTransport | None = None,
) -> OpenAICompatibleChatClient:
    """Build the v1 DeepSeek adapter without exposing credentials to the Agent."""

    if not settings.deepseek_api_key:
        raise ValueError(
            "DEEPSEEK_API_KEY is required to construct the DeepSeek model client"
        )
    if not settings.deepseek_base_url.startswith("https://"):
        raise ValueError(
            "DEEPSEEK_BASE_URL must use HTTPS when sending a bearer credential"
        )
    if settings.deepseek_base_url.rstrip("/").endswith("/beta"):
        raise ValueError(
            "The v1 adapter does not enable DeepSeek beta strict mode"
        )
    if budget_guard is None:
        raise ValueError(
            "A persistent budget guard is required for paid DeepSeek requests"
        )

    return OpenAICompatibleChatClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_timeout_seconds,
        max_tokens=settings.deepseek_max_tokens,
        max_retries=settings.deepseek_max_retries,
        # V4 thinking defaults to enabled. The v1 loop intentionally disables
        # it so tool-call turns do not require provider-specific reasoning
        # payloads to be retained and replayed.
        extra_body={"thinking": {"type": "disabled"}},
        budget_guard=budget_guard,
        transport=transport,
    )
