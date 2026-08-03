"""Same-origin public demo BFF: offline replay, host confirm, ephemeral DB."""

from __future__ import annotations

APP_MODE_PUBLIC_DEMO = "public_demo"
DEMO_AGENT_MODE_OFFLINE_REPLAY = "offline_replay"
DEMO_AGENT_MODE_PREPARATION_SCRIPTED = "preparation_scripted"

PUBLIC_DEMO_AGENT_MODES = frozenset(
    {
        DEMO_AGENT_MODE_OFFLINE_REPLAY,
        DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
    }
)

__all__ = [
    "APP_MODE_PUBLIC_DEMO",
    "DEMO_AGENT_MODE_OFFLINE_REPLAY",
    "DEMO_AGENT_MODE_PREPARATION_SCRIPTED",
    "PUBLIC_DEMO_AGENT_MODES",
]
