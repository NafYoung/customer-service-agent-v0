from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a timezone-naive UTC datetime for consistent SQLite storage."""

    return datetime.now(timezone.utc).replace(tzinfo=None)
