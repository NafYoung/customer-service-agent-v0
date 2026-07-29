from __future__ import annotations

from dataclasses import dataclass

from app.enums import EligibilityReason


@dataclass(frozen=True)
class EligibilityDecision:
    allowed: bool
    reason_code: EligibilityReason
    user_message: str
    available_alternative: str | None = None
