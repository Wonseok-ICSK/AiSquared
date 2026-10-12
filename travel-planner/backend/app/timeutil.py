"""Time helpers. DB datetimes are naive UTC; the API renders them as ISO-8601 with ``Z``."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from app.config import get_settings

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def utcnow() -> datetime:
    """Current time as a *naive* UTC datetime (the DB storage convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc_iso(dt: datetime | None) -> str | None:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SSZ``; naive values are assumed UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_tz() -> ZoneInfo:
    """The configured local timezone (``TIMEZONE``, default Asia/Seoul)."""
    return ZoneInfo(get_settings().timezone)


def now_local() -> datetime:
    """Current time as an aware datetime in the local timezone."""
    return datetime.now(local_tz())


def utc_to_local(dt: datetime) -> datetime:
    """Convert a naive-UTC (or aware) datetime to an aware local-timezone datetime."""
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return aware.astimezone(local_tz())


def validate_hhmm(value: str) -> str:
    """Validate a ``HH:MM`` 24h time and return it normalised (``"8:05"`` -> ``"08:05"``)."""
    match = _HHMM_RE.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"time must be in HH:MM format, got {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"time out of range: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def parse_hhmm(value: str) -> time:
    """Parse a ``HH:MM`` string into :class:`datetime.time` (raises ``ValueError``)."""
    hour, minute = validate_hhmm(value).split(":")
    return time(int(hour), int(minute))
