# -*- coding: utf-8 -*-
"""时间线导演路由，对应上游 `.hdsi_reference/src/script/timeline-routing.ts`（1.0.1-beta6-rebuild，17 行）。

路由只改变模型工作量，绝不改写散文：短对话通常不需要时间线导演，
但跨天、超过 20 分钟、advance 阶段，或窗口跨过已知日程边界时仍需要它。

约定：`calendarDayKey` / `localClockMinutes` 来自 `hdsi/time_utils.py`（与上游 `../time` 对应）；
上游形参名 `from` 是 Python 保留字，这里写作 `from_`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..time_utils import calendar_day_key, local_clock_minutes
from ..types import NarrativePhase, SchedulePreplanWindow

__all__ = ['needs_timeline_director']


def _js_number(value: str) -> float:
    """JS Number(str)：空串 / 空白 → 0，其余交给 float()，解析失败 → NaN。"""
    text = value.strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float('nan')


def _clock_minutes(clock: Any) -> float:
    """上游 `const [hour, minute] = clock.split(':').map(Number); return hour * 60 + minute`。

    缺失 / 非字符串的时钟在 JS 里会抛错，这里返回 NaN，让两个比较都恒为 false（保守不触发）。
    """
    if not isinstance(clock, str):
        return float('nan')
    parts = clock.split(':')
    hour = _js_number(parts[0]) if parts else float('nan')
    minute = _js_number(parts[1]) if len(parts) > 1 else float('nan')
    return hour * 60 + minute


def needs_timeline_director(
    phase: NarrativePhase,
    from_: datetime,
    now: datetime,
    timezone: str,
    schedule: Optional[SchedulePreplanWindow] = None,
) -> bool:
    """上游 needsTimelineDirector：判定本回合是否需要时间线导演（形参 from → from_）。"""
    if phase == 'user-message':
        return False
    # 上游 `now.getTime() - from.getTime() > 20 * 60_000`。
    if phase == 'advance' or (now - from_).total_seconds() * 1000 > 20 * 60_000:
        return True
    day_key = calendar_day_key(now, timezone)
    if calendar_day_key(from_, timezone) != day_key:
        return True
    start = local_clock_minutes(from_, timezone)
    end = local_clock_minutes(now, timezone)
    blocks = schedule.get('blocks') if isinstance(schedule, dict) else None
    if not isinstance(blocks, list):
        return False
    # 上游 `!!schedule?.blocks.some(block => block.date === calendarDayKey(now, timezone) && ...)`：
    # 命中条件为 start < clock <= end（左开右闭），block.start / block.end 任一命中即可。
    return any(
        isinstance(block, dict)
        and block.get('date') == day_key
        and any(
            _clock_minutes(clock) > start and _clock_minutes(clock) <= end
            for clock in (block.get('start'), block.get('end'))
        )
        for block in blocks
    )
