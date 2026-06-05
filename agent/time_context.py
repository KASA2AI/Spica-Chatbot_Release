from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def build_local_time_context(now: datetime | None = None) -> dict[str, str]:
    local_now = _normalize_local_datetime(now).replace(microsecond=0)
    utc_offset = _format_utc_offset(local_now.utcoffset())
    timezone_name = local_now.tzname() or "local"

    return {
        "local_datetime": local_now.isoformat(),
        "local_date": local_now.strftime("%Y-%m-%d"),
        "local_time": local_now.strftime("%H:%M:%S"),
        "weekday": _WEEKDAYS[local_now.weekday()],
        "period": _period_for_hour(local_now.hour),
        "timezone": timezone_name,
        "utc_offset": utc_offset,
        "display": f"{local_now.strftime('%Y-%m-%d %H:%M:%S')} {timezone_name} UTC{utc_offset}",
    }


def format_local_time_for_prompt(context: dict[str, Any] | None) -> str:
    if not context:
        return "未知"
    display = str(context.get("display") or "").strip()
    weekday = str(context.get("weekday") or "").strip()
    period = str(context.get("period") or "").strip()
    if not display:
        return "未知"
    if weekday and period:
        return f"{display}（{weekday}，{period}）"
    return display


def _normalize_local_datetime(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None or now.utcoffset() is None:
        return now.astimezone()
    return now


def _format_utc_offset(offset: timedelta | None) -> str:
    if offset is None:
        return "+00:00"
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _period_for_hour(hour: int) -> str:
    if 5 <= hour < 9:
        return "早上"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"
