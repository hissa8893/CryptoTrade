"""UTC time helpers. All stored timestamps are UTC ISO-8601.

`now_utc()` is the single source of "now" for the whole app so tests (and the
catch-up simulation) can control the clock. `TRADER_FAKE_NOW` is a test-only
override; when set, it is logged loudly at startup.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd

UTC = timezone.utc
DAY = timedelta(days=1)
MS_PER_DAY = 86_400_000

_override: datetime | None = None


def set_now(dt: datetime | None) -> None:
    """Override the clock (tests only). Pass None to restore real time."""
    global _override
    if dt is not None and dt.tzinfo is None:
        raise ValueError("clock override must be timezone-aware")
    _override = dt.astimezone(UTC) if dt is not None else None


def fake_now_env() -> datetime | None:
    raw = os.environ.get("TRADER_FAKE_NOW")
    if not raw:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def now_utc() -> datetime:
    if _override is not None:
        return _override
    env = fake_now_env()
    if env is not None:
        return env
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """ISO-8601 UTC string with 'Z'-free offset form (+00:00), second precision."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to iso(); all times must be UTC-aware")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def now_iso() -> str:
    return iso(now_utc())


def to_day(value) -> date:
    """Coerce a Timestamp/datetime/date/ISO string to a UTC calendar date."""
    if isinstance(value, pd.Timestamp):
        ts = value.tz_convert(UTC) if value.tzinfo is not None else value
        return ts.date()
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def last_closed_day(now: datetime | None = None) -> date:
    """The most recent UTC day whose daily candle has fully closed.

    A daily candle for day D covers [D 00:00, D+1 00:00). It is closed once
    now >= D+1 00:00 UTC, so the last closed day is always "yesterday" in UTC.
    """
    now = now or now_utc()
    return (now.astimezone(UTC) - DAY).date()


def is_day_closed(d: date, now: datetime | None = None) -> bool:
    now = now or now_utc()
    return now.astimezone(UTC) >= day_start(d) + DAY


def ms(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1000)


def from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)
