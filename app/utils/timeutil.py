"""Time helpers used across workers and retention jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def retention_cutoff(days: int) -> datetime:
    return utc_now_naive() - timedelta(days=days)
