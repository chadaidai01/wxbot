# -*- coding: utf-8 -*-
"""hdsi 时间工具，对应上游 src/time.ts。

上游使用 Intl.DateTimeFormat 做时区渲染；这里用标准库 zoneinfo 实现同样的输出字段。
语义等价（year/month/day/weekday/hour/minute/second/period/periodZh/daylightExpectation/offset）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WEEKDAYS_EN = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_PERIOD_ZH = {
    'morning': '上午',
    'afternoon': '下午',
    'evening': '傍晚/晚上',
    'night': '夜间',
}
_RESOLVED_TIMEZONES: Dict[str, str] = {}

# 无 tzdata（如部分 Windows 环境）时的降级：只列无夏令时的固定偏移时区，
# 保证 Asia/Shanghai 等常用时区不会静默退回 UTC。时区名不在表中一律按上游语义回落 UTC。
_FALLBACK_OFFSETS: Dict[str, int] = {
    'UTC': 0,
    'Etc/UTC': 0,
    'Asia/Shanghai': 8 * 60,
    'Asia/Chongqing': 8 * 60,
    'Asia/Harbin': 8 * 60,
    'Asia/Hong_Kong': 8 * 60,
    'Asia/Macau': 8 * 60,
    'Asia/Taipei': 8 * 60,
    'Asia/Singapore': 8 * 60,
    'Asia/Kuala_Lumpur': 8 * 60,
    'Asia/Tokyo': 9 * 60,
    'Asia/Seoul': 9 * 60,
    'Asia/Bangkok': 7 * 60,
    'Asia/Jakarta': 7 * 60,
    'Asia/Kolkata': 5 * 60 + 30,
    'Asia/Dubai': 4 * 60,
    'Australia/Perth': 8 * 60,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def parse_time(value: Any) -> Optional[datetime]:
    """TS 的 toDate：接受 datetime / ISO 字符串 / epoch 毫秒，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, (int, float)):
        # 对应 JS `new Date(number)`：数字一律按 epoch 毫秒解释。
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            return ensure_aware(datetime.fromisoformat(text))
        except ValueError:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                try:
                    return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            return None
    return None


def iso(value: Optional[datetime]) -> Optional[str]:
    """对应 JS Date.prototype.toISOString()：始终毫秒 3 位 + Z。"""
    if value is None:
        return None
    utc = ensure_aware(value).astimezone(timezone.utc)
    return utc.strftime('%Y-%m-%dT%H:%M:%S.') + f'{utc.microsecond // 1000:03d}Z'


def iso_or(value: Any, fallback: datetime) -> str:
    parsed = parse_time(value)
    return iso(parsed or fallback) or ''


def minutes_from_now(minutes: float) -> datetime:
    return now_utc() + timedelta(minutes=minutes)


def resolve_timezone(timezone_name: str) -> str:
    candidate = (timezone_name or '').strip() or 'UTC'
    cached = _RESOLVED_TIMEZONES.get(candidate)
    if cached is not None:
        return cached
    try:
        ZoneInfo(candidate)
        _RESOLVED_TIMEZONES[candidate] = candidate
        return candidate
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if candidate in _FALLBACK_OFFSETS:
            _RESOLVED_TIMEZONES[candidate] = candidate
            return candidate
        _RESOLVED_TIMEZONES[candidate] = 'UTC'
        return 'UTC'


def _tzinfo(timezone_name: str) -> Any:
    resolved = resolve_timezone(timezone_name)
    try:
        return ZoneInfo(resolved)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone(timedelta(minutes=_FALLBACK_OFFSETS.get(resolved, 0)))


def _offset_label(value: datetime) -> str:
    offset = value.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    if total_minutes == 0:
        return 'GMT'
    sign = '+' if total_minutes > 0 else '-'
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if minutes:
        return f'GMT{sign}{hours}:{minutes:02d}'
    return f'GMT{sign}{hours}'


def story_local_time_context(value: datetime, timezone_name: str) -> Dict[str, Any]:
    resolved = resolve_timezone(timezone_name)
    local = ensure_aware(value).astimezone(_tzinfo(resolved))
    hour = local.hour
    period = 'morning' if 5 <= hour < 12 else 'afternoon' if 12 <= hour < 18 else 'evening' if 18 <= hour < 22 else 'night'
    daylight_expectation = (
        'normally daylight unless current weather, season, or setting explicitly says otherwise'
        if period in ('morning', 'afternoon')
        else 'transitioning toward darkness; use the established season and setting'
        if period == 'evening'
        else 'normally dark outside unless the setting explicitly says otherwise'
    )
    date = f'{local.year:04d}-{local.month:02d}-{local.day:02d}'
    time = f'{local.hour:02d}:{local.minute:02d}:{local.second:02d}'
    return {
        'timezone': resolved,
        'utc': iso(value),
        'local': f'{date} {time}',
        'date': date,
        'time': time,
        'hour': hour,
        'weekday': _WEEKDAYS_EN[local.weekday()],
        'offset': _offset_label(local),
        'period': period,
        'periodZh': _PERIOD_ZH[period],
        'daylightExpectation': daylight_expectation,
    }


def format_log_time(value: Optional[datetime], timezone_name: str) -> str:
    if value is None:
        return '-'
    local = ensure_aware(value).astimezone(_tzinfo(timezone_name))
    return f'{local.month:02d}-{local.day:02d} {local.hour:02d}:{local.minute:02d}:{local.second:02d}'


def format_story_display_time(value: Optional[datetime], timezone_name: str) -> str:
    if value is None:
        return '-'
    context = story_local_time_context(value, timezone_name)
    return f"{context['local']} {context['offset'] or 'GMT+0'}"


def local_clock_minutes(value: datetime, timezone_name: str) -> int:
    local = ensure_aware(value).astimezone(_tzinfo(timezone_name))
    return local.hour * 60 + local.minute


def calendar_day_key(value: datetime, timezone_name: str) -> str:
    local = ensure_aware(value).astimezone(_tzinfo(timezone_name))
    return f'{local.year:04d}-{local.month:02d}-{local.day:02d}'


def local_weekday_key(value: datetime, timezone_name: str) -> str:
    local = ensure_aware(value).astimezone(_tzinfo(timezone_name))
    return _WEEKDAYS_EN[local.weekday()].lower()


def local_date_to_utc_range(day_key: str, timezone_name: str):
    """把本地日期 YYYY-MM-DD 转成当天 [start, end) 的 UTC datetime。"""
    year, month, day = (int(part) for part in day_key.split('-'))
    tz = _tzinfo(timezone_name)
    start = datetime(year, month, day, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def local_hhmm_to_utc(day_key: str, hhmm: str, timezone_name: str) -> datetime:
    year, month, day = (int(part) for part in day_key.split('-'))
    hour, minute = (int(part) for part in (hhmm or '00:00').split(':')[:2])
    tz = _tzinfo(timezone_name)
    return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def time_formatter_cache_size() -> int:
    return len(_RESOLVED_TIMEZONES)


def same_timestamp(left: Any, right: Any, tolerance_ms: int = 2000) -> bool:
    a = parse_time(left)
    b = parse_time(right)
    if not a or not b:
        return False
    return abs((a - b).total_seconds() * 1000.0) < tolerance_ms
