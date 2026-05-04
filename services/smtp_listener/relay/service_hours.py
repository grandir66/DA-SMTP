"""Valutazione is_in_service basata sul service_hours JSON cached.

Schema atteso (da endpoint manager `/api/v1/relay/customers/active`):
  {
    "profile": "standard_8_18",
    "timezone": "Europe/Rome",
    "schedule": {
      "mon": [["08:00","13:00"], ["14:00","18:00"]],
      "tue": [["08:00","18:00"]],
      ...
    },
    "holidays": ["2026-04-25", "2026-05-01"]
  }
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":", 1)
    return time(hour=int(h), minute=int(m))


def is_in_service(schedule: dict[str, Any] | None, when: datetime | None = None) -> bool:
    if not schedule or not isinstance(schedule, dict):
        return False

    tz_name = schedule.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    now = (when or datetime.now()).astimezone(tz)
    iso_date = now.date().isoformat()

    holidays = schedule.get("holidays") or []
    if iso_date in holidays:
        return False

    day_key = _DAY_KEYS[now.weekday()]
    daily = (schedule.get("schedule") or {}).get(day_key) or []
    current = now.time().replace(microsecond=0)
    for window in daily:
        if not isinstance(window, (list, tuple)) or len(window) < 2:
            continue
        try:
            start = _parse_hhmm(str(window[0]))
            end = _parse_hhmm(str(window[1]))
        except (ValueError, TypeError):
            continue
        if start <= current < end:
            return True
    return False
