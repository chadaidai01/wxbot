# -*- coding: utf-8 -*-
"""Schedule Preplan 计划日程，对应上游 src/schedule-preplan.ts
（HDS-Interlude 1.0.1-beta6-rebuild，上游文件 432 行）。

导出与上游 export 一一对应：
    SchedulePreplanConfig / DEFAULT_SCHEDULE_PREPLAN_CONFIG / resolveSchedulePreplanConfig /
    normalizeSchedulePreplanRecord / schedulePreplanReviewDue / schedulePreplanNeedsModel /
    refreshSchedulePreplan / applySchedulePreplanProposal / materializeSchedulePreplan /
    schedulePreplanWindow / nextSchedulePreplanTransition

移植约定：
- 同步函数；上游 `Date` → `datetime`（UTC aware；无 tzinfo 的输入按 UTC 处理），
  `new Date(0)` → `_EPOCH`。`now` 只在返回值里原样携带，不做格式化。
- 持久化字段名 / JSON key / 中文文案与上游逐字一致；`regimes[].from` 在 dict 里保持
  字符串 key `'from'`（Python 保留字，绝不写成 `from_`），取用一律 `d['from']`。
- 对象展开 `{...a}` → `{**a}`；上游文件私有函数 normalizeProposal / normalizeRegime /
  normalizeBlocks / mergeBy / dateKey / ... 一律加 `_` 前缀。
- JS 与 Python 的隐式转换差异集中在本文件末尾的 `_js_*` 辅助函数里：Number()/ToString()、
  关系比较、Math.max/Math.floor、Math.imul、字符串 trim/正则（JS `\\d` 只匹配 ASCII）、
  `localeCompare`（这里只用于等长 ISO 日期键）、数组排序稳定性、对象真值判断。
- Python 的 None 同时表示上游 undefined（缺字段）与 null；本模块所有调用点都来自
  「缺字段」路径（配置、记录字段、LLM JSON），因此按 undefined 处理：配置缺失 → 上游
  `Number(undefined) === NaN` 的回退分支，绝不按 `Number(null) === 0` 误落进 0。
"""

from __future__ import annotations

import functools
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, Union

from .time_utils import (
    calendar_day_key,
    ensure_aware,
    local_clock_minutes,
    parse_time,
    story_local_time_context,
)
from .types import (
    SchedulePreplanBlock,
    SchedulePreplanBlockKind,
    SchedulePreplanDay,
    SchedulePreplanException,
    SchedulePreplanProposal,
    SchedulePreplanRecord,
    SchedulePreplanRegime,
    SchedulePreplanWeekday,
    SchedulePreplanWindow,
    ScriptEntry,
)

__all__ = [
    'SchedulePreplanConfig',
    'DEFAULT_SCHEDULE_PREPLAN_CONFIG',
    'resolve_schedule_preplan_config',
    'normalize_schedule_preplan_record',
    'schedule_preplan_review_due',
    'schedule_preplan_needs_model',
    'refresh_schedule_preplan',
    'apply_schedule_preplan_proposal',
    'materialize_schedule_preplan',
    'schedule_preplan_window',
    'next_schedule_preplan_transition',
]


class SchedulePreplanConfig(TypedDict, total=False):
    """上游 SchedulePreplanConfig 接口；运行时就是普通 dict。"""

    enabled: bool
    horizonDays: int
    reviewAfterLocalHour: int
    anchorAutoAdvance: bool
    variationLevel: str  # 'stable' | 'contextual' | 'granular'
    candidateActivationProbability: float
    candidateRevealMinutes: int


DEFAULT_SCHEDULE_PREPLAN_CONFIG: SchedulePreplanConfig = {
    'enabled': True,
    'horizonDays': 14,
    'reviewAfterLocalHour': 3,
    'anchorAutoAdvance': True,
    'variationLevel': 'stable',
    'candidateActivationProbability': 0.25,
    'candidateRevealMinutes': 120,
}

# 上游 WEEKDAYS / KINDS / PRIORITY：下标 0 = 星期日，与 JS getUTCDay() 一致。
_WEEKDAYS: List[SchedulePreplanWeekday] = [
    'sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
]
_KINDS: List[SchedulePreplanBlockKind] = ['fixed', 'routine', 'flexible', 'open']
_PRIORITY: Dict[str, int] = {'fixed': 4, 'routine': 3, 'flexible': 2, 'open': 1}
_OUTCOMES: List[str] = ['unchanged', 'extend', 'patch', 'replace']
_VARIATION_LEVELS: List[str] = ['stable', 'contextual', 'granular']

_MAX_SAFE_INTEGER = 9007199254740991  # Number.MAX_SAFE_INTEGER
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # new Date(0)


def resolve_schedule_preplan_config(value: Optional[Dict[str, Any]] = None) -> SchedulePreplanConfig:
    """上游 resolveSchedulePreplanConfig：把任意配置收敛到合法范围。

    布尔字段用 `!== false`（只有显式 False 才关闭）；variationLevel 只接受三个枚举串；
    数值字段走 clampInt/clampNumber（Number() → NaN 时回落默认值）。
    """
    source: Dict[str, Any] = value if _is_record(value) else {}
    variation = source.get('variationLevel')
    return {
        'enabled': source.get('enabled') is not False,
        'horizonDays': _clamp_int(source.get('horizonDays'), 3, 30, 14),
        'reviewAfterLocalHour': _clamp_int(source.get('reviewAfterLocalHour'), 0, 23, 3),
        'anchorAutoAdvance': source.get('anchorAutoAdvance') is not False,
        'variationLevel': variation if isinstance(variation, str) and variation in _VARIATION_LEVELS else 'stable',
        'candidateActivationProbability': _clamp_number(
            source.get('candidateActivationProbability'), 0.05, 0.5, 0.25,
        ),
        'candidateRevealMinutes': _clamp_int(source.get('candidateRevealMinutes'), 15, 360, 120),
    }


def normalize_schedule_preplan_record(value: Any) -> Optional[SchedulePreplanRecord]:
    """上游 normalizeSchedulePreplanRecord：数据库行 → 记录。

    注意上游 storyId 只校验 typeof === 'string'，不做 trim/clip（空串也合法）；
    createdAt / updatedAt 非法时回落 `new Date(0)`（1970-01-01T00:00:00Z）。
    """
    if not _is_record(value):
        return None
    story_id = value.get('storyId')
    if not isinstance(story_id, str):
        return None
    valid_from = _date_key(value.get('validFrom'))
    valid_through = _date_key(value.get('validThrough'))
    if not valid_from or not valid_through:
        return None
    return {
        'storyId': story_id,
        'revision': _clamp_int(value.get('revision'), 0, 1000000, 0),
        'timezone': _text(value.get('timezone'), 127) or 'UTC',
        'validFrom': valid_from,
        'validThrough': valid_through,
        'lastReviewedLocalDate': _date_key(value.get('lastReviewedLocalDate')) or '',
        'lastEvidenceEntryId': _clamp_int(value.get('lastEvidenceEntryId'), 0, _MAX_SAFE_INTEGER, 0),
        'reviewReason': _text(value.get('reviewReason'), 500),
        'regimes': _normalize_regimes(value.get('regimes')),
        'exceptions': _normalize_exceptions(value.get('exceptions')),
        'materializedDays': _normalize_days(value.get('materializedDays')),
        'createdAt': _valid_date(value.get('createdAt')) or _EPOCH,
        'updatedAt': _valid_date(value.get('updatedAt')) or _EPOCH,
    }


def schedule_preplan_review_due(
    record: Optional[SchedulePreplanRecord],
    now: datetime,
    timezone: str,
    config: SchedulePreplanConfig,
) -> bool:
    """上游 schedulePreplanReviewDue：今天是否还没审查过、且已过审查时刻。"""
    if not config.get('enabled'):
        return False
    today = calendar_day_key(now, timezone)
    if not _is_record(record):
        return True
    if record.get('timezone') != timezone:
        return True
    return (
        record.get('lastReviewedLocalDate') != today
        and local_clock_minutes(now, timezone) >= _js_number(config.get('reviewAfterLocalHour')) * 60
    )


def schedule_preplan_needs_model(
    record: Optional[SchedulePreplanRecord],
    evidence: List[ScriptEntry],
    today: str,
    timezone: str,
    config: SchedulePreplanConfig,
) -> bool:
    """上游 schedulePreplanNeedsModel：是否需要请求模型重审。

    - 没有记录 / 时区变了 → True；
    - 出现比 lastEvidenceEntryId 更新的证据 → True；
    - 记录里没有任何 regime（显式空记录）→ False：等新证据，不反复请求模型；
    - regime 没有覆盖 `today + max(1, horizonDays - 3)`，或 validThrough 不够远 → True。
    """
    if not _is_record(record) or record.get('timezone') != timezone:
        return True
    last_evidence_id = record.get('lastEvidenceEntryId')
    for entry in evidence if isinstance(evidence, (list, tuple)) else []:
        entry_id = entry.get('id') if _is_record(entry) else None
        if _js_gt(entry_id, last_evidence_id):
            return True
    regimes_value = record.get('regimes')
    regimes = regimes_value if isinstance(regimes_value, (list, tuple)) else []
    if not regimes:
        return False
    coverage_target = _add_date(today, _math_max(1, _js_number(config.get('horizonDays')) - 3))
    covered = False
    for regime in regimes:
        if not _is_record(regime):
            continue
        # 上游 regime.from / regime.to：key 保持字符串 'from'。
        start = regime.get('from')
        end = regime.get('to')
        if _js_lte(start, coverage_target) and (not end or _js_gte(end, coverage_target)):
            covered = True
            break
    if not covered:
        return True
    return _js_lt(record.get('validThrough'), coverage_target)


def refresh_schedule_preplan(
    record: SchedulePreplanRecord,
    today: str,
    timezone: str,
    config: SchedulePreplanConfig,
    now: datetime,
    reason: str = 'Daily review found no schedule-changing evidence.',
) -> SchedulePreplanRecord:
    """上游 refreshSchedulePreplan：保留 regimes/exceptions，重算覆盖区间与 materializedDays。"""
    valid_through = _add_date(today, _js_number(config.get('horizonDays')) - 1)
    regimes_value = record.get('regimes')
    exceptions_value = record.get('exceptions')
    return {
        **record,
        'timezone': timezone,
        'validFrom': today,
        'validThrough': valid_through,
        'lastReviewedLocalDate': today,
        'reviewReason': reason,
        'materializedDays': materialize_schedule_preplan(
            regimes_value if isinstance(regimes_value, (list, tuple)) else [],
            exceptions_value if isinstance(exceptions_value, (list, tuple)) else [],
            today,
            config.get('horizonDays'),
        ),
        'updatedAt': now,
    }


def apply_schedule_preplan_proposal(
    current: Optional[SchedulePreplanRecord],
    proposal_value: Any,
    evidence: List[ScriptEntry],
    today: str,
    timezone: str,
    config: SchedulePreplanConfig,
    now: datetime,
    variation_level: str = 'stable',
) -> Optional[SchedulePreplanRecord]:
    """上游 applySchedulePreplanProposal：校验模型提案并合并进现有记录。

    语义要点（逐字复刻）：
    - outcome=unchanged 且已有记录：只刷新审查时间与证据游标，计划本体不动；
    - outcome=replace 或本来没有记录：整体替换 regimes/exceptions；
    - 其余（extend/patch）：按 regime.id / exception.date 做「后者覆盖前者、位置不变」的 mergeBy；
    - 合并后 regimes 为空：新记录返回空计划（等待新证据），已有记录则原样保留并刷新；
    - 落盘前 regimes 只留最后 6 条，exceptions 先剔除 < today-1 再留最后 30 条；
      但 materializeSchedulePreplan 用的是「截断前」的完整数组（与上游一致，不要提前 slice）。
    """
    evidence_entries = evidence if isinstance(evidence, (list, tuple)) else []
    valid_evidence_ids = {
        entry.get('id') if _is_record(entry) else None for entry in evidence_entries
    }
    proposal = _normalize_proposal(proposal_value, valid_evidence_ids, variation_level)
    has_current = _is_record(current)
    if proposal is None:
        if has_current:
            return refresh_schedule_preplan(
                current, today, timezone, config, now,
                'Invalid proposal ignored; existing Schedule Preplan retained.',
            )
        return None
    if proposal['outcome'] == 'unchanged' and has_current:
        return {
            **refresh_schedule_preplan(current, today, timezone, config, now, proposal['reason']),
            'lastEvidenceEntryId': _math_max(
                current.get('lastEvidenceEntryId'),
                *[entry.get('id') if _is_record(entry) else None for entry in evidence_entries],
                0,
            ),
        }

    current_regimes = current.get('regimes') if has_current else None
    current_exceptions = current.get('exceptions') if has_current else None
    regimes = list(current_regimes) if isinstance(current_regimes, (list, tuple)) else []
    exceptions = list(current_exceptions) if isinstance(current_exceptions, (list, tuple)) else []
    if proposal['outcome'] == 'replace' or not has_current:
        regimes = list(proposal.get('regimes') or [])
        exceptions = list(proposal.get('exceptions') or [])
    else:
        regimes = _merge_by(
            regimes, proposal.get('regimes') or [],
            lambda item: item.get('id') if _is_record(item) else None,
        )
        exceptions = _merge_by(
            exceptions, proposal.get('exceptions') or [],
            lambda item: item.get('date') if _is_record(item) else None,
        )
    if not regimes:
        # 空结果是合法初始结论：故事还没形成可靠周律时，把这次审查落盘，
        # 之后等新证据，而不是反复为同一个空结论付模型调用。
        if not has_current:
            return {
                'storyId': '', 'revision': 1, 'timezone': timezone,
                'validFrom': today,
                'validThrough': _add_date(today, _js_number(config.get('horizonDays')) - 1),
                'lastReviewedLocalDate': today,
                'lastEvidenceEntryId': _math_max(
                    *[entry.get('id') if _is_record(entry) else None for entry in evidence_entries], 0,
                ),
                'reviewReason': proposal['reason'],
                'regimes': [], 'exceptions': [], 'materializedDays': [],
                'createdAt': now, 'updatedAt': now,
            }
        return refresh_schedule_preplan(
            current, today, timezone, config, now,
            'Empty proposal ignored; existing Schedule Preplan retained.',
        )
    valid_through = _add_date(today, _js_number(config.get('horizonDays')) - 1)
    # exceptions.filter(item => item.date >= addDate(today, -1))：字符串比较，缺失 date 一律 False。
    exception_cutoff = _add_date(today, -1)
    recent_exceptions = [
        item for item in exceptions
        if _js_gte(item.get('date') if _is_record(item) else None, exception_cutoff)
    ]
    return {
        'storyId': _nullish(current.get('storyId') if has_current else None, ''),
        'revision': _js_plus_one(_nullish(current.get('revision') if has_current else None, 0)),
        'timezone': timezone,
        'validFrom': today,
        'validThrough': valid_through,
        'lastReviewedLocalDate': today,
        'lastEvidenceEntryId': _math_max(
            _nullish(current.get('lastEvidenceEntryId') if has_current else None, 0),
            *[entry.get('id') if _is_record(entry) else None for entry in evidence_entries],
            0,
        ),
        'reviewReason': proposal['reason'],
        'regimes': regimes[-6:],
        'exceptions': recent_exceptions[-30:],
        'materializedDays': materialize_schedule_preplan(
            regimes, exceptions, today, config.get('horizonDays'),
        ),
        'createdAt': _nullish(current.get('createdAt') if has_current else None, now),
        'updatedAt': now,
    }


def materialize_schedule_preplan(
    regimes: List[SchedulePreplanRegime],
    exceptions: List[SchedulePreplanException],
    start_date: str,
    horizon_days: int,
) -> List[SchedulePreplanDay]:
    """上游 materializeSchedulePreplan：把周律 + 例外展开成逐日 blocks。

    - 一天命中多个 regime 时取 `from` 最大（localeCompare 降序、稳定排序）的那个；
    - weekday 用 `${date}T00:00:00.000Z` 的 getUTCDay() 取 WEEKDAYS 下标（0=星期日）；
    - exception.mode=replace 整体替换；patch 先按 removeBlockIds 删除再追加 blocks；
    - 最后 resolveOverlaps 去重叠并最多保留 12 个 block。
    """
    regime_list = regimes if isinstance(regimes, (list, tuple)) else []
    exception_list = exceptions if isinstance(exceptions, (list, tuple)) else []
    days: List[SchedulePreplanDay] = []
    # 上游 `offset < Math.max(1, horizonDays)`：NaN 时一次都不循环。
    limit = _math_max(1, horizon_days)
    count = int(math.ceil(float(limit))) if math.isfinite(float(limit)) else 0
    for offset in range(count):
        date = _add_date(start_date, offset)
        matched: List[SchedulePreplanRegime] = []
        for regime in regime_list:
            if not _is_record(regime):
                continue
            if _js_lte(regime.get('from'), date) and (not regime.get('to') or _js_gte(regime.get('to'), date)):
                matched.append(regime)
        # right.from.localeCompare(left.from)：ISO 日期键等长，等价于字符串降序；稳定排序保持原始先后。
        matched.sort(
            key=lambda item: item.get('from') if isinstance(item.get('from'), str) else '',
            reverse=True,
        )
        matching = matched[0] if matched else None
        weekday = _weekday_key(date)
        blocks: List[SchedulePreplanBlock] = []
        if matching is not None:
            weekly_value = matching.get('weekly')
            weekday_blocks = weekly_value.get(weekday) if _is_record(weekly_value) else None
            if isinstance(weekday_blocks, (list, tuple)):
                blocks = [dict(block) for block in weekday_blocks if _is_record(block)]
        exception = None
        for item in exception_list:
            if _is_record(item) and item.get('date') == date:
                exception = item
                break
        if exception is not None and exception.get('mode') == 'replace':
            replacement = exception.get('blocks')
            blocks = (
                [dict(block) for block in replacement if _is_record(block)]
                if isinstance(replacement, (list, tuple)) else []
            )
        elif exception is not None:
            removed_value = exception.get('removeBlockIds')
            removed = set(removed_value) if isinstance(removed_value, (list, tuple)) else set()
            appended_value = exception.get('blocks')
            appended = (
                [dict(block) for block in appended_value if _is_record(block)]
                if isinstance(appended_value, (list, tuple)) else []
            )
            blocks = [block for block in blocks if block.get('id') not in removed] + appended
        days.append({'date': date, 'blocks': _resolve_overlaps(blocks)[:12]})
    return days


def schedule_preplan_window(
    record: Optional[SchedulePreplanRecord],
    now: datetime,
    timezone: str,
    hours: int = 12,
    config: Optional[SchedulePreplanConfig] = None,
) -> Optional[SchedulePreplanWindow]:
    """上游 schedulePreplanWindow：只投影未来约十二小时，其余量程不进主提示词。

    - 只看 materializedDays 里 local date 为今天/明天的天（dayOffset ∈ [0, 1]）；
    - 跨夜 block（end <= start）按 +1440 分钟处理；
    - tentative block：先按 FNV-1a 概率决定是否激活；距开始 > candidateRevealMinutes 时
      只以「可能的个人安排」占位（location 被上游显式置为 undefined，JSON 里会消失）；
    - 最多输出 8 个 block；`from` 键保持字符串 'from'。
    """
    active_config = config if _is_record(config) else DEFAULT_SCHEDULE_PREPLAN_CONFIG
    if not _is_record(record) or record.get('timezone') != timezone:
        return None
    local = story_local_time_context(now, timezone)
    today = local['date']
    start_minute = local['hour'] * 60 + int(local['time'][3:5])  # local.time = 'HH:MM:SS'
    end_minute = start_minute + _math_max(1, hours) * 60
    blocks: List[Dict[str, Any]] = []
    materialized_value = record.get('materializedDays')
    materialized = materialized_value if isinstance(materialized_value, (list, tuple)) else []
    for day in materialized:
        if not _is_record(day):
            continue
        day_date = day.get('date')
        day_offset = _date_difference(today, day_date)
        # NaN 时两个比较都为 False（与 JS 一致），继续按偏移 0 处理后续字段。
        if day_offset < 0 or day_offset > 1:
            continue
        day_blocks = day.get('blocks')
        if not isinstance(day_blocks, (list, tuple)):
            continue
        for block in day_blocks:
            if not _is_record(block):
                continue
            start = day_offset * 1440 + _time_minutes(block.get('start'))
            end = day_offset * 1440 + _time_minutes(block.get('end'))
            if end <= start:
                end += 1440
            if end <= start_minute or start >= end_minute:
                continue
            if block.get('tentative'):
                if not _is_tentative_block_active(
                    _template_string(record.get('storyId')),
                    _template_string(day_date),
                    _template_string(block.get('id')),
                    active_config.get('candidateActivationProbability'),
                ):
                    continue
                minutes_until = start - start_minute
                if _js_gt(minutes_until, active_config.get('candidateRevealMinutes')):
                    # 上游对象字面量显式写 `location: undefined`：这里按包内约定保留 key 并置 None，
                    # 读取方用 .get()；若要逐字节复刻 JSON.stringify，序列化时需丢弃 None 值。
                    blocks.append({
                        **block,
                        'label': '可能的个人安排',
                        'location': None,
                        'date': day_date,
                        'tentative': True,
                    })
                    continue
            blocks.append({**block, 'date': day_date})
    to_total = end_minute
    to_offset = math.floor(to_total / 1440) if math.isfinite(float(to_total)) else math.nan
    to_date = _add_date(today, to_offset)
    to_clock = _clock(to_total % 1440)
    return {
        'name': 'Schedule Preplan',
        'timezone': timezone,
        'from': f'{today} {_clock(start_minute)}',
        'to': f'{to_date} {to_clock}',
        'plannedNotObserved': True,
        'revision': record.get('revision'),
        'blocks': blocks[:8],
    }


def next_schedule_preplan_transition(
    record: Optional[SchedulePreplanRecord],
    now: datetime,
    timezone: str,
    max_hours: int = 12,
) -> Optional[datetime]:
    """上游 nextSchedulePreplanTransition：窗口内下一个 fixed block 的开始/结束时刻。

    只取 kind === 'fixed' 的 block；相对 local date 计算偏移；两个候选（start/end）
    都要求严格晚于当前本地分钟数；无候选返回 None。
    """
    window = schedule_preplan_window(record, now, timezone, max_hours)
    if window is None:
        return None
    local = story_local_time_context(now, timezone)
    current = local['hour'] * 60 + int(local['time'][3:5])
    candidates: List[Union[int, float]] = []
    window_blocks = window.get('blocks')
    for block in window_blocks if isinstance(window_blocks, (list, tuple)) else []:
        if not _is_record(block) or block.get('kind') != 'fixed':
            continue
        offset = _date_difference(local['date'], block.get('date')) * 1440
        start = offset + _time_minutes(block.get('start'))
        end = offset + _time_minutes(block.get('end'))
        if end <= start:
            end += 1440
        if _js_gt(start, current):
            candidates.append(start)
        if _js_gt(end, current):
            candidates.append(end)
    if not candidates:
        return None
    candidates.sort()
    next_minute = candidates[0]
    return ensure_aware(now) + timedelta(minutes=next_minute - current)


# ---------------------------------------------------------------------------
# 私有归一化函数（上游同名私有函数，逐分支对应）
# ---------------------------------------------------------------------------


def _normalize_proposal(
    value: Any,
    valid_evidence_ids: set,
    variation_level: str,
) -> Optional[SchedulePreplanProposal]:
    """上游 normalizeProposal：outcome/reason 必须合法，patch/replace 必须带证据来源。"""
    if not _is_record(value):
        return None
    outcome = value.get('outcome')
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        return None
    reason = _text(value.get('reason'), 500)
    if not reason:
        return None
    source_entry_ids = [
        item for item in _ids(value.get('sourceEntryIds')) if item in valid_evidence_ids
    ]
    allow_tentative = variation_level == 'granular'
    regimes = _normalize_regimes(value.get('regimes'), valid_evidence_ids, allow_tentative)
    exceptions = _normalize_exceptions(value.get('exceptions'), valid_evidence_ids, allow_tentative)
    if (
        outcome in ('patch', 'replace')
        and len(valid_evidence_ids)
        and not source_entry_ids
        and not any(_has_source_ids(item) for item in regimes)
        and not any(_has_source_ids(item) for item in exceptions)
    ):
        return None
    return {
        'outcome': outcome,
        'reason': reason,
        # 上游对象字面量显式写 `confidence: finite(value.confidence)`，
        # 非法/缺失时属性存在、值为 undefined；这里按包内约定置 None。
        'confidence': _finite(value.get('confidence')),
        'sourceEntryIds': source_entry_ids,
        'regimes': regimes,
        'exceptions': exceptions,
    }


def _normalize_regimes(
    value: Any,
    valid_evidence_ids: Optional[set] = None,
    allow_tentative: bool = True,
) -> List[SchedulePreplanRegime]:
    if not isinstance(value, (list, tuple)):
        return []
    regimes: List[SchedulePreplanRegime] = []
    for item in value:
        regime = _normalize_regime(item, valid_evidence_ids, allow_tentative)
        if regime is None:
            continue
        regimes.append(regime)
        if len(regimes) >= 6:
            break
    return regimes


def _normalize_regime(
    value: Any,
    valid_evidence_ids: Optional[set] = None,
    allow_tentative: bool = True,
) -> Optional[SchedulePreplanRegime]:
    if not _is_record(value) or not _is_record(value.get('weekly')):
        return None
    identifier = _slug(value.get('id'), 80)
    label = _text(value.get('label'), 120)
    start = _date_key(value.get('from'))  # 上游 value.from
    end = _date_key(value.get('to'))
    # `!id || !label || !from || (to && to < from)`
    if not identifier or not label or not start or (end and end < start):
        return None
    weekly: Dict[str, List[SchedulePreplanBlock]] = {}
    weekly_value = value.get('weekly')
    for weekday in _WEEKDAYS:
        blocks = _normalize_blocks(weekly_value.get(weekday), valid_evidence_ids, allow_tentative)
        if blocks:
            weekly[weekday] = blocks
    regime: SchedulePreplanRegime = {'id': identifier, 'label': label, 'from': start}
    if end:
        regime['to'] = end
    regime['weekly'] = weekly
    regime['sourceEntryIds'] = _evidence_ids(value.get('sourceEntryIds'), valid_evidence_ids)
    return regime


def _normalize_exceptions(
    value: Any,
    valid_evidence_ids: Optional[set] = None,
    allow_tentative: bool = True,
) -> List[SchedulePreplanException]:
    if not isinstance(value, (list, tuple)):
        return []
    exceptions: List[SchedulePreplanException] = []
    for item in value:
        exception = _normalize_exception(item, valid_evidence_ids, allow_tentative)
        if exception is None:
            continue
        exceptions.append(exception)
        if len(exceptions) >= 30:
            break
    return exceptions


def _normalize_exception(
    value: Any,
    valid_evidence_ids: Optional[set] = None,
    allow_tentative: bool = True,
) -> Optional[SchedulePreplanException]:
    if not _is_record(value):
        return None
    date = _date_key(value.get('date'))
    mode_value = value.get('mode')
    if mode_value == 'replace':
        mode = 'replace'
    elif mode_value == 'patch':
        mode = 'patch'
    else:
        mode = None
    reason = _text(value.get('reason'), 300)
    if not date or not mode or not reason:
        return None
    remove_block_ids: List[str] = []
    if isinstance(value.get('removeBlockIds'), (list, tuple)):
        remove_block_ids = [
            slug for slug in (_slug(item, 80) for item in value['removeBlockIds']) if slug
        ][:20]
    return {
        'date': date,
        'mode': mode,
        'reason': reason,
        'removeBlockIds': remove_block_ids,
        'blocks': _normalize_blocks(value.get('blocks'), valid_evidence_ids, allow_tentative),
        'sourceEntryIds': _evidence_ids(value.get('sourceEntryIds'), valid_evidence_ids),
    }


def _normalize_days(value: Any) -> List[SchedulePreplanDay]:
    """上游 normalizeDays：flatMap 后取前 31 天；blocks 不再按证据过滤。"""
    if not isinstance(value, (list, tuple)):
        return []
    days: List[SchedulePreplanDay] = []
    for item in value:
        if not _is_record(item):
            continue
        date = _date_key(item.get('date'))
        if not date:
            continue
        days.append({'date': date, 'blocks': _normalize_blocks(item.get('blocks'))})
        if len(days) >= 31:
            break
    return days


def _normalize_blocks(
    value: Any,
    valid_evidence_ids: Optional[set] = None,
    allow_tentative: bool = True,
) -> List[SchedulePreplanBlock]:
    if not isinstance(value, (list, tuple)):
        return []
    blocks: List[SchedulePreplanBlock] = []
    for item in value:
        block = _normalize_block(item, valid_evidence_ids, allow_tentative)
        if block is None:
            continue
        blocks.append(block)
        if len(blocks) >= 20:
            break
    return blocks


def _normalize_block(
    value: Any,
    valid_evidence_ids: Optional[set] = None,
    allow_tentative: bool = True,
) -> Optional[SchedulePreplanBlock]:
    if not _is_record(value):
        return None
    identifier = _slug(value.get('id'), 80)
    start = _time_key(value.get('start'))
    end = _time_key(value.get('end'))
    label = _text(value.get('label'), 160)
    kind_value = value.get('kind')
    kind = kind_value if isinstance(kind_value, str) and kind_value in _KINDS else None
    if not identifier or not start or not end or start == end or not label or not kind:
        return None
    location = _text(value.get('location'), 120)
    tentative = bool(
        allow_tentative and value.get('tentative') is True and (kind == 'flexible' or kind == 'open')
    )
    block: SchedulePreplanBlock = {
        'id': identifier, 'start': start, 'end': end, 'label': label, 'kind': kind,
    }
    if location:
        block['location'] = location
    if tentative:
        block['tentative'] = True
    block['sourceEntryIds'] = _evidence_ids(value.get('sourceEntryIds'), valid_evidence_ids)
    return block


# ---------------------------------------------------------------------------
# 私有判定 / 展开函数
# ---------------------------------------------------------------------------


def _is_tentative_block_active(
    story_id: str,
    date: str,
    block_id: str,
    probability: Any,
) -> bool:
    """上游 isTentativeBlockActive：FNV-1a（按 UTF-16 码元）映射到 [0,1) 后与概率比较。

    概率缺失/NaN 时 JS 的 `<` 恒为 False（等价于「未激活」）。
    """
    payload = f'{story_id}|{date}|{block_id}'
    hash_value = 2166136261
    for unit in _utf16_code_units(payload):
        hash_value = _imul(hash_value ^ unit, 16777619)
    ratio = (hash_value & 0xFFFFFFFF) / 4294967296.0
    return _js_lt(ratio, probability)


def _resolve_overlaps(blocks: List[SchedulePreplanBlock]) -> List[SchedulePreplanBlock]:
    """上游 resolveOverlaps：按 kind 优先级降序、start 升序贪心取不重叠 block，再按 start 升序。

    - 排序比较器 `PRIORITY[right.kind] - PRIORITY[left.kind] || timeMinutes(left.start) - timeMinutes(right.start)`：
      优先级差为 0 或 NaN（未知 kind）时回落到 start 比较；
    - 同 id 已选过则跳过；跨夜 block 的 end <= start 时 +1440 后再判重叠。
    """
    chosen: List[SchedulePreplanBlock] = []
    ordered = sorted(blocks, key=functools.cmp_to_key(_overlap_compare))
    for candidate in ordered:
        start = _time_minutes(candidate.get('start'))
        end = _time_minutes(candidate.get('end'))
        if end <= start:
            end += 1440
        overlaps = False
        for block in chosen:
            other_start = _time_minutes(block.get('start'))
            other_end = _time_minutes(block.get('end'))
            if other_end <= other_start:
                other_end += 1440
            if start < other_end and end > other_start:
                overlaps = True
                break
        if not overlaps and not any(block.get('id') == candidate.get('id') for block in chosen):
            chosen.append(candidate)
    chosen.sort(key=lambda block: _time_minutes(block.get('start')))
    return chosen


def _overlap_compare(left: Any, right: Any) -> int:
    """resolveOverlaps 的比较器；返回值符号与 JS sort 比较器一致（NaN 归 0）。"""
    left_priority = _PRIORITY.get(left.get('kind')) if _is_record(left) else None
    right_priority = _PRIORITY.get(right.get('kind')) if _is_record(right) else None
    priority_diff: Optional[int] = (
        right_priority - left_priority
        if left_priority is not None and right_priority is not None else None
    )
    if priority_diff:  # JS `a || b`：0 / NaN 都回落到时间比较
        return -1 if priority_diff < 0 else 1
    left_start = _time_minutes(left.get('start')) if _is_record(left) else math.nan
    right_start = _time_minutes(right.get('start')) if _is_record(right) else math.nan
    diff = _js_number(left_start) - _js_number(right_start)
    if math.isnan(diff) or diff == 0:
        return 0
    return -1 if diff < 0 else 1


def _merge_by(current: List[Any], changes: List[Any], key: Callable[[Any], Any]) -> List[Any]:
    """上游 mergeBy：以 Map 语义「后者覆盖、键首次出现的位置不变」合并。"""
    merged: Dict[Any, Any] = {}
    for item in current:
        merged[key(item)] = item
    for item in changes:
        merged[key(item)] = item
    return list(merged.values())


def _evidence_ids(value: Any, valid: Optional[set] = None) -> List[int]:
    """上游 evidenceIds：valid 为空集合也要过滤（JS 空 Set 是 truthy，不能按 Python 真值判断）。"""
    normalized = _ids(value)
    if valid is None:
        return normalized
    return [item for item in normalized if item in valid]


def _ids(value: Any) -> List[int]:
    """上游 ids：Array.map(Number) → Number.isSafeInteger && > 0 → Set 去重 → 前 30。"""
    if not isinstance(value, (list, tuple)):
        return []
    result: List[int] = []
    seen: set = set()
    for item in value:
        number = _js_number(item)
        if not math.isfinite(number) or math.floor(number) != number or abs(number) > _MAX_SAFE_INTEGER:
            continue
        integer = int(number)
        if integer <= 0 or integer in seen:
            continue
        seen.add(integer)
        result.append(integer)
        if len(result) >= 30:
            break
    return result


def _has_source_ids(item: Any) -> bool:
    """上游 `item.sourceEntryIds?.length` 的真值判断。"""
    ids_value = item.get('sourceEntryIds') if _is_record(item) else None
    return isinstance(ids_value, (list, tuple)) and len(ids_value) > 0


# ---------------------------------------------------------------------------
# 日期 / 时间工具（对应上游文件私有函数）
# ---------------------------------------------------------------------------


def _date_key(value: Any) -> Optional[str]:
    """上游 dateKey：trim 后必须严格是 YYYY-MM-DD 且是真实存在的日历日。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    return raw if _parse_date_key(raw) is not None else None


def _parse_date_key(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not _DATE_KEY_RE.match(raw):
        return None
    year = int(raw[0:4])
    month = int(raw[5:7])
    day = int(raw[8:10])
    if not _valid_ymd(year, month, day):
        return None
    return (year, month, day)


def _add_date(value: Any, days: Any) -> str:
    """上游 addDate：UTC 午夜加天数后取 toISOString().slice(0, 10)。

    日期键非法 / days 为 NaN 时上游会抛 RangeError（Invalid Date），这里对应 ValueError。
    """
    parsed = _js_date_from_key(value)
    offset = _js_number(days)
    if parsed is None or not math.isfinite(offset):
        raise ValueError('Invalid time value')
    # Date.setUTCDate(getUTCDate() + days)：加小数天等价于对天数向下取整后再取日期。
    total = _days_from_civil(parsed[0], parsed[1], parsed[2]) + int(math.floor(offset))
    return _iso_date(*_civil_from_days(total))


def _date_difference(left: Any, right: Any) -> Union[int, float]:
    """上游 dateDifference：Math.round((Date(right) - Date(left)) / 86_400_000)，非法日期 → NaN。"""
    start = _js_date_from_key(left)
    end = _js_date_from_key(right)
    if start is None or end is None:
        return math.nan
    return round(_days_from_civil(*end) - _days_from_civil(*start))


def _weekday_key(date: str) -> str:
    """`new Date(`${date}T00:00:00.000Z`).getUTCDay()` → WEEKDAYS 下标（0=星期日）。

    非法日期会得到 Invalid Date → getUTCDay() 为 NaN → WEEKDAYS[NaN] 为 undefined，
    此时 JS 实际查的是 `weekly[undefined]`，即字符串 'undefined' 这个属性名。
    """
    parsed = _js_date_from_key(date)
    if parsed is None:
        return 'undefined'
    # 1970-01-01 是星期四，getUTCDay() === 4。
    return _WEEKDAYS[(_days_from_civil(parsed[0], parsed[1], parsed[2]) + 4) % 7]


def _js_date_from_key(value: Any) -> Optional[Tuple[int, int, int]]:
    """复刻 `new Date(`${value}T00:00:00.000Z`)` 对日期键的解析（V8 的 ISO 解析）。

    与 dateKey 不同，这里不做 toISOString() 往返校验、也不 trim：
    - 只接受 4-2-2 位数字（否则 Invalid Date）；
    - 月份必须 01-12、日必须 01-31，否则 Invalid Date；
    - 日超出当月长度时按 MakeDay 语义向后续月份溢出：2024-02-30 → 2024-03-01，
      2024-02-31 → 2024-03-02，2024-04-31 → 2024-05-01；
    - 0000 年是合法年份（proleptic Gregorian）。
    """
    if not isinstance(value, str) or not _DATE_KEY_RE.match(value):
        return None
    year = int(value[0:4])
    month = int(value[5:7])
    day = int(value[8:10])
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return _civil_from_days(_days_from_civil(year, month, 1) + (day - 1))


def _valid_ymd(year: int, month: int, day: int) -> bool:
    """公历日期合法性：Hinnant 算法往返校验，支持 0000 年（datetime 不支持）。"""
    if not 1 <= month <= 12 or day < 1:
        return False
    return _civil_from_days(_days_from_civil(year, month, day)) == (year, month, day)


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Howard Hinnant days_from_civil：proleptic Gregorian，day 0 = 1970-01-01。"""
    adjusted_year = year - (1 if month <= 2 else 0)
    # Hinnant 的 `(y >= 0 ? y : y - 399) / 400` 在 C++ 里是截断除法，等价于 Python 的 floor 除法。
    era = adjusted_year // 400
    year_of_era = adjusted_year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _civil_from_days(days: int) -> Tuple[int, int, int]:
    """Howard Hinnant civil_from_days：与 JS Date 相同的 proleptic Gregorian 日历。"""
    shifted = days + 719468
    # 同上：`(z >= 0 ? z : z - 146096) / 146097` 等价于 floor 除法（不要先减 146096 再 floor）。
    era = shifted // 146097
    day_of_era = shifted - era * 146097
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    month_prime = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * month_prime + 2) // 5 + 1
    month = month_prime + (3 if month_prime < 10 else -9)
    return (year + (1 if month <= 2 else 0), month, day)


def _iso_date(year: int, month: int, day: int) -> str:
    """`toISOString().slice(0, 10)`；0..9999 年输出 4 位年，其余按 JS 展开年份后截断。"""
    if 0 <= year <= 9999:
        text = f'{year:04d}-{month:02d}-{day:02d}'
    else:
        text = f'{"-" if year < 0 else "+"}{abs(year):06d}-{month:02d}-{day:02d}'
    return text[:10]


def _time_key(value: Any) -> Optional[str]:
    """上游 timeKey：trim 后严格 HH:mm（ASCII 数字），hour<=23、minute<=59，返回 trim 后的原串。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    match = _TIME_KEY_RE.match(raw)
    if not match:
        return None
    if int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return raw


def _time_minutes(value: Any) -> Union[int, float]:
    """上游 timeMinutes：`value.split(':').map(Number)` → hour * 60 + minute。"""
    if not isinstance(value, str):
        return math.nan
    parts = value.split(':')
    hour = _js_number(parts[0]) if parts else math.nan
    minute = _js_number(parts[1]) if len(parts) > 1 else math.nan
    total = hour * 60 + minute
    if math.isfinite(total) and float(total).is_integer():
        return int(total)
    return total


def _clock(minutes: Any) -> str:
    """上游 clock：`String(Math.floor(minutes / 60)).padStart(2, '0')` + `%` 取分。"""
    number = _js_number(minutes)
    if math.isfinite(number):
        hour_text = _number_to_string(math.floor(number / 60))
        minute_text = _number_to_string(math.fmod(number, 60))
    else:
        hour_text = 'NaN'
        minute_text = 'NaN'
    return f'{_pad_start(hour_text, 2)}:{_pad_start(minute_text, 2)}'


# ---------------------------------------------------------------------------
# JS 语义辅助函数
# ---------------------------------------------------------------------------


def _is_record(value: Any) -> bool:
    """上游 isRecord：非 null 的对象且不是数组（空对象也是 record）。

    注意：utils.is_record 用 `bool(value)` 实现 `!!value`，会把空 dict 判为 False，
    而上游 `!!{}` 是 True；本文件的归一化分支依赖这一点，因此本地实现。
    """
    return isinstance(value, dict)


def _js_number(value: Any) -> float:
    """复刻 JS Number(value)，用于 clamp/ids/算术。

    与 JS 的差异：Python None 代表上游 undefined（缺字段），按 NaN 处理
    （`Number(undefined) === NaN`），而不是 `Number(null) === 0`。
    """
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int):
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        if text in ('Infinity', '+Infinity'):
            return math.inf
        if text == '-Infinity':
            return -math.inf
        if _JS_DECIMAL_RE.match(text):
            try:
                return float(text)
            except ValueError:
                return math.nan
        if _JS_HEX_RE.match(text):
            try:
                return float(int(text[2:], 16))
            except (ValueError, OverflowError):
                return math.inf
        if _JS_OCTAL_RE.match(text):
            try:
                return float(int(text[2:], 8))
            except (ValueError, OverflowError):
                return math.inf
        if _JS_BINARY_RE.match(text):
            try:
                return float(int(text[2:], 2))
            except (ValueError, OverflowError):
                return math.inf
        return math.nan
    if isinstance(value, (list, tuple)):
        if not value:
            return 0.0
        if len(value) == 1:
            return _js_number(value[0])
        return math.nan
    return math.nan


def _js_compare(left: Any, right: Any) -> Optional[int]:
    """复刻 JS 关系比较 `<` / `<=` / `>` / `>=`。

    两侧同为字符串 → 字符串比较（上游只用于 ASCII 日期键，Python 码点序与 UTF-16 码元序一致）；
    否则 ToNumber 后比较；任一侧缺失（None，上游 undefined/null）或 NaN → None，
    调用方按 JS「结果为 false」处理。显式 null 被并入缺失语义，遵循本文件开头的约定。
    """
    if left is None or right is None:
        return None
    if isinstance(left, str) and isinstance(right, str):
        if left < right:
            return -1
        if left > right:
            return 1
        return 0
    left_number = _js_number(left)
    right_number = _js_number(right)
    if math.isnan(left_number) or math.isnan(right_number):
        return None
    if left_number < right_number:
        return -1
    if left_number > right_number:
        return 1
    return 0


def _js_lt(left: Any, right: Any) -> bool:
    result = _js_compare(left, right)
    return result is not None and result < 0


def _js_lte(left: Any, right: Any) -> bool:
    result = _js_compare(left, right)
    return result is not None and result <= 0


def _js_gt(left: Any, right: Any) -> bool:
    result = _js_compare(left, right)
    return result is not None and result > 0


def _js_gte(left: Any, right: Any) -> bool:
    result = _js_compare(left, right)
    return result is not None and result >= 0


def _math_max(*values: Any) -> Union[int, float]:
    """复刻 Math.max(...)：任一参数为 NaN（含缺失字段）→ NaN。"""
    best = -math.inf
    for value in values:
        number = _js_number(value)
        if math.isnan(number):
            return math.nan
        if number > best:
            best = number
    if math.isfinite(best) and float(best).is_integer() and abs(best) <= _MAX_SAFE_INTEGER:
        return int(best)
    return best


def _js_plus_one(value: Any) -> Any:
    """复刻 `(current?.revision ?? 0) + 1` 的 JS `+` 语义。

    字符串 / 数组 / 对象一侧会走 ToString 拼接（'4' + 1 === '41'），其余走数值相加。
    正常记录里 revision 经 clampInt 后必为数字，这里只防御手工构造/迁移数据。
    """
    if isinstance(value, (str, list, tuple, dict)):
        return _template_string(value) + '1'
    number = _js_number(value) + 1
    if math.isfinite(number) and float(number).is_integer() and abs(number) <= _MAX_SAFE_INTEGER:
        return int(number)
    return number


def _nullish(value: Any, fallback: Any) -> Any:
    """上游 `?? fallback`：只有 None（undefined/null）才回落。"""
    return fallback if value is None else value


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    """上游 clampInt：Number → Number.isFinite → Math.floor 后 clamp，否则 fallback。"""
    number = _js_number(value)
    if math.isfinite(number):
        return max(minimum, min(maximum, int(math.floor(number))))
    return fallback


def _clamp_number(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    """上游 clampNumber：只做 clamp，不取整。"""
    number = _js_number(value)
    if math.isfinite(number):
        return max(minimum, min(maximum, number))
    return fallback


def _finite(value: Any) -> Optional[float]:
    """上游 finite：只接受真正的 number，clamp 到 [0, 1]；否则 undefined（None）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def _text(value: Any, limit: int) -> str:
    """上游 text：trim → 把连续 CR/LF 压成单个空格 → slice。"""
    if not isinstance(value, str):
        return ''
    return re.sub(r'[\r\n]+', ' ', value.strip())[:limit]


def _slug(value: Any, limit: int) -> str:
    """上游 slug：trim 后只保留 Unicode 字母/数字/下划线/连字符（JS \\p{L}\\p{N}），
    其它字符换成 '-'，连续 '-' 合并，去掉首尾 '-'，最后 slice。
    """
    if not isinstance(value, str):
        return ''
    characters: List[str] = []
    for character in value.strip():
        category = unicodedata.category(character)
        if category[0] in ('L', 'N') or character in ('_', '-'):
            characters.append(character)
        else:
            characters.append('-')
    collapsed = re.sub(r'-+', '-', ''.join(characters))
    return collapsed.strip('-')[:limit]


def _valid_date(value: Any) -> Optional[datetime]:
    """上游 validDate：Date 实例 / 字符串 / 数字（epoch 毫秒）→ datetime；无效返回 None。"""
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, bool):
        return None  # JS: typeof true !== 'string'|'number' 且不是 Date
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        try:
            return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        return parse_time(value)
    return None


def _number_to_string(value: Any) -> str:
    """JS String(number) 的常用分支；时钟格式化只用到整数。"""
    number = float(value)
    if math.isnan(number):
        return 'NaN'
    if math.isinf(number):
        return 'Infinity' if number > 0 else '-Infinity'
    if number == 0:
        return '0'
    if number.is_integer() and abs(number) < 1e21:
        return str(int(number))
    return repr(number)


def _template_string(value: Any) -> str:
    """JS 模板字面量 `${value}` 的 ToString：None（缺字段）→ 'undefined'。"""
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return _number_to_string(value)
    if isinstance(value, float):
        return _number_to_string(value)
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None else _template_string(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    return str(value)


def _pad_start(text: str, target: int) -> str:
    return text.rjust(target, '0') if len(text) < target else text


def _utf16_code_units(value: str) -> List[int]:
    """JS String.charCodeAt 序列：非 BMP 字符拆成两个 UTF-16 码元（代理对）。"""
    encoded = value.encode('utf-16-le', 'surrogatepass')
    return [encoded[index] | (encoded[index + 1] << 8) for index in range(0, len(encoded), 2)]


def _imul(left: int, right: int) -> int:
    """Math.imul：32 位有符号整数乘法（按 2^32 回绕，结果为 int32）。"""
    result = (left * right) & 0xFFFFFFFF
    return result - 0x100000000 if result >= 0x80000000 else result


_DATE_KEY_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
_TIME_KEY_RE = re.compile(r'^([0-9]{2}):([0-9]{2})$')
_JS_DECIMAL_RE = re.compile(r'^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$')
_JS_HEX_RE = re.compile(r'^0[xX][0-9a-fA-F]+$')
_JS_OCTAL_RE = re.compile(r'^0[oO][0-7]+$')
_JS_BINARY_RE = re.compile(r'^0[bB][01]+$')
