from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        normalized = name.strip().lower()
        if normalized in {"asia/shanghai", "asia/chongqing", "asia/hong_kong", "prc"}:
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        if normalized in {"utc", "etc/utc", "z"}:
            return timezone.utc
        raise ValueError(
            f"Timezone {name!r} is unavailable on this Windows Python. "
            "Use Asia/Shanghai or install the tzdata package."
        )


def timezone_label(zone: tzinfo, configured_name: str) -> str:
    return getattr(zone, "key", configured_name)

