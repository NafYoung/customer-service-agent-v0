"""Same-origin demo BFF: scripted/live Preparation Agent, host confirm."""

from __future__ import annotations

APP_MODE_PUBLIC_DEMO = "public_demo"
DEMO_AGENT_MODE_OFFLINE_REPLAY = "offline_replay"
DEMO_AGENT_MODE_PREPARATION_SCRIPTED = "preparation_scripted"
DEMO_AGENT_MODE_PREPARATION_LIVE = "preparation_live"

PUBLIC_DEMO_AGENT_MODES = frozenset(
    {
        DEMO_AGENT_MODE_OFFLINE_REPLAY,
        DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
    }
)

LOCAL_DEMO_AGENT_MODES = frozenset(
    {
        *PUBLIC_DEMO_AGENT_MODES,
        DEMO_AGENT_MODE_PREPARATION_LIVE,
    }
)

__all__ = [
    "APP_MODE_PUBLIC_DEMO",
    "DEMO_AGENT_MODE_OFFLINE_REPLAY",
    "DEMO_AGENT_MODE_PREPARATION_SCRIPTED",
    "DEMO_AGENT_MODE_PREPARATION_LIVE",
    "PUBLIC_DEMO_AGENT_MODES",
    "LOCAL_DEMO_AGENT_MODES",
]
