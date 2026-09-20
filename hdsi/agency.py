# -*- coding: utf-8 -*-
"""Agency 行动窗口与主动联系容量判断，对应上游 src/agency.ts（HDS-Interlude 1.0.1-beta6-rebuild）。

上游把「主体能不能主动联系」拆成三层：窗口状态（日程负荷 / 隐私 / 设备访问）、
主动联系候选（来源 / 披露级别 / 结论）与容量矩阵。本文件按 agency.ts 逐分支、逐字段复刻，
不引入额外策略，也不重排判定顺序（顺序决定 reason 的优先级）。

约定：
- `Date` → `datetime`（UTC aware）；`now=None` 时取 `now_utc()`；
  需要落库的时间字段保持 ISO 字符串（`iso()`），运行期字段（如容量结果的 nextOpportunityAt）是 `datetime`。
- 上游对象字面量里显式写出的可选字段（值为 `undefined`）在返回 dict 里保留 key 并置 `None`，
  读取方统一用 `.get()`；`ProactiveContactDraft.willingness` 同理。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from functools import cmp_to_key
from typing import AbstractSet, Any, List, Optional, TypedDict

from .time_utils import iso, now_utc, parse_time
from .types import AgencyConfig, AgencyWindowState, ProactiveContactDraft, ProactiveContactOrigin
from .utils import clamp, clip, finite_number, is_array, is_record

# 上游 const MINUTE = 60_000 / const HOUR = 60 * MINUTE；这里用 timedelta 表达同样的步长。
_MINUTE = timedelta(minutes=1)
_HOUR = timedelta(hours=1)

# 上游内联在判定里的枚举，成员与含义逐字保留。
_ACTIVITY_LOADS = ('free', 'occupied', 'overloaded')
_PRIVACY_LEVELS = ('private', 'shared', 'public')
_DEVICE_ACCESS_LEVELS = ('available', 'limited', 'unavailable')
_PROACTIVE_ORIGINS = ('life-event', 'promise', 'practical-update', 'relationship-follow-up')
_PROACTIVE_DISCLOSURES = ('ordinary', 'personal')
_PROACTIVE_OUTCOMES = ('send-now', 'recheck-later', 'let-go')

DEFAULT_AGENCY_CONFIG: AgencyConfig = {
    'enabled': True,
    'maxWindowMinutes': 240,
    'minimumProactiveIntervalMinutes': 60,
    'maxCandidateHours': 24,
}


class AgencyCapacityResult(TypedDict, total=False):
    """上游 export interface AgencyCapacityResult；nextOpportunityAt 由 Date → datetime。"""

    allowed: bool
    reason: str
    nextOpportunityAt: Optional[datetime]


def resolve_agency_config(value: Optional[AgencyConfig] = None) -> AgencyConfig:
    """上游 resolveAgencyConfig：`{ ...DEFAULT_AGENCY_CONFIG, ...value }`。"""
    overlay = value if isinstance(value, dict) else {}
    return {**DEFAULT_AGENCY_CONFIG, **overlay}


def normalize_agency_window_state(value: Any) -> Optional[AgencyWindowState]:
    """上游 normalizeAgencyWindowState：校验并序列化已持久化的窗口状态。"""
    if not is_record(value):
        return None
    if _string(value.get('activityLoad')) not in _ACTIVITY_LOADS:
        return None
    if _string(value.get('privacy')) not in _PRIVACY_LEVELS:
        return None
    if _string(value.get('deviceAccess')) not in _DEVICE_ACCESS_LEVELS:
        return None
    valid_until = parse_time(value.get('validUntil'))
    updated_at = parse_time(value.get('updatedAt'))
    if valid_until is None or updated_at is None:
        return None
    next_opportunity_at = parse_time(value.get('nextOpportunityAt'))
    return {
        'activityLoad': value.get('activityLoad'),
        'privacy': value.get('privacy'),
        'deviceAccess': value.get('deviceAccess'),
        'nextOpportunityAt': iso(next_opportunity_at),
        'validUntil': iso(valid_until),
        'basis': clip(value.get('basis'), 500),
        # 注意：这里只 slice(-20)，不去重（上游如此）；去重在 _grounded_ids 里做。
        'sourceEntryIds': _positive_ids(value.get('sourceEntryIds'))[-20:],
        'updatedAt': iso(updated_at),
    }


def normalize_agency_window_draft(
    value: Any,
    now: datetime,
    config: AgencyConfig,
    valid_source_entry_ids: AbstractSet[int],
    fallback_source_entry_id: Optional[int] = None,
) -> Optional[AgencyWindowState]:
    """上游 normalizeAgencyWindowDraft：把模型给出的窗口草稿夹到允许区间内。"""
    if not is_record(value):
        return None
    if _string(value.get('activityLoad')) not in _ACTIVITY_LOADS:
        return None
    if _string(value.get('privacy')) not in _PRIVACY_LEVELS:
        return None
    if _string(value.get('deviceAccess')) not in _DEVICE_ACCESS_LEVELS:
        return None
    # 上游：new Date(now.getTime() + Math.max(5, config.maxWindowMinutes) * MINUTE)
    maximum = now + max(5, _config_value(config, 'maxWindowMinutes')) * _MINUTE
    requested_until = parse_time(value.get('validUntil'))
    valid_until = (
        min(requested_until, maximum)
        if requested_until is not None and requested_until > now
        else maximum
    )
    requested_opportunity = parse_time(value.get('nextOpportunityAt'))
    next_opportunity_at = (
        min(requested_opportunity, valid_until)
        if requested_opportunity is not None and requested_opportunity > now
        else None
    )
    source_entry_ids = _grounded_ids(
        value.get('sourceEntryIds'), valid_source_entry_ids, fallback_source_entry_id
    )
    basis = clip(value.get('basis'), 500)
    if not basis or not source_entry_ids:
        return None
    return {
        'activityLoad': value.get('activityLoad'),
        'privacy': value.get('privacy'),
        'deviceAccess': value.get('deviceAccess'),
        'nextOpportunityAt': iso(next_opportunity_at),
        'validUntil': iso(valid_until),
        'basis': basis,
        'sourceEntryIds': source_entry_ids,
        'updatedAt': iso(now),
    }


def active_agency_window(value: Any, now: Optional[datetime] = None) -> Optional[AgencyWindowState]:
    """上游 activeAgencyWindow：归一化后仅当 validUntil 严格晚于 now 时返回。"""
    state = normalize_agency_window_state(value)
    if state is None:
        return None
    current = now if now is not None else now_utc()
    valid_until = parse_time(state.get('validUntil'))
    return state if valid_until is not None and valid_until > current else None


def normalize_proactive_contact(
    value: Any,
    now: datetime,
    config: AgencyConfig,
    permitted_participant_ids: AbstractSet[str],
    valid_source_entry_ids: AbstractSet[int],
    fallback_source_entry_id: Optional[int] = None,
) -> Optional[ProactiveContactDraft]:
    """上游 normalizeProactiveContact：校验参与者/枚举/来源并夹紧时间字段。"""
    if not is_record(value):
        return None
    participant_id = _string(value.get('participantId'))
    if participant_id not in permitted_participant_ids:
        return None
    if _string(value.get('origin')) not in _PROACTIVE_ORIGINS:
        return None
    if _string(value.get('disclosure')) not in _PROACTIVE_DISCLOSURES:
        return None
    if _string(value.get('outcome')) not in _PROACTIVE_OUTCOMES:
        return None
    motive = clip(value.get('motive'), 600)
    source_entry_ids = _grounded_ids(
        value.get('sourceEntryIds'), valid_source_entry_ids, fallback_source_entry_id
    )
    if not motive or not source_entry_ids:
        return None
    # 上游：new Date(now.getTime() + Math.max(1, config.maxCandidateHours) * HOUR)
    maximum_expiry = now + max(1, _config_value(config, 'maxCandidateHours')) * _HOUR
    requested_expiry = parse_time(value.get('expiresAt'))
    expires_at = (
        min(requested_expiry, maximum_expiry)
        if requested_expiry is not None and requested_expiry > now
        else maximum_expiry
    )
    requested_not_before = parse_time(value.get('notBefore'))
    not_before = (
        iso(requested_not_before)
        if requested_not_before is not None
        and requested_not_before > now
        and requested_not_before < expires_at
        else None
    )
    willingness = finite_number(value.get('willingness'))
    return {
        'participantId': participant_id,
        'origin': value.get('origin'),
        'motive': motive,
        'disclosure': value.get('disclosure'),
        'sourceEntryIds': source_entry_ids,
        'willingness': None if willingness is None else clamp(willingness, 0, 1),
        'outcome': value.get('outcome'),
        'notBefore': not_before,
        'expiresAt': iso(expires_at),
    }


def evaluate_agency_capacity(
    window: Optional[AgencyWindowState],
    candidate: ProactiveContactDraft,
    now: datetime,
    config: AgencyConfig,
    last_character_message_at: Any = None,
) -> AgencyCapacityResult:
    """上游 evaluateAgencyCapacity：容量矩阵，分支顺序即 reason 优先级。"""
    # 上游 `!window || new Date(window.validUntil) <= now`。
    # validUntil 缺失/非法时 JS 得到 Invalid Date，比较恒为 false（不判过期），这里保持同一行为；
    # 另外，JS 里 `{}` 是 truthy，所以只有 None 才算“窗口缺失”。
    valid_until = parse_time(window.get('validUntil')) if window is not None else None
    if window is None or (valid_until is not None and valid_until <= now):
        return {'allowed': False, 'reason': 'agency-window-missing-or-expired'}
    next_opportunity_at = _future_date(window.get('nextOpportunityAt'), now)
    if window.get('deviceAccess') == 'unavailable':
        return {'allowed': False, 'reason': 'device-unavailable', 'nextOpportunityAt': next_opportunity_at}
    if window.get('deviceAccess') == 'limited':
        return {'allowed': False, 'reason': 'device-limited', 'nextOpportunityAt': next_opportunity_at}
    if window.get('activityLoad') == 'overloaded':
        return {'allowed': False, 'reason': 'schedule-overloaded', 'nextOpportunityAt': next_opportunity_at}
    if candidate.get('disclosure') == 'personal' and window.get('privacy') != 'private':
        return {'allowed': False, 'reason': 'privacy-insufficient', 'nextOpportunityAt': next_opportunity_at}
    last_contact = parse_time(last_character_message_at)
    minimum_interval = max(0, _config_value(config, 'minimumProactiveIntervalMinutes')) * _MINUTE
    if (
        candidate.get('origin') != 'promise'
        and last_contact is not None
        and now - last_contact < minimum_interval
    ):
        return {
            'allowed': False,
            'reason': 'minimum-proactive-interval',
            'nextOpportunityAt': last_contact + minimum_interval,
        }
    if (
        window.get('activityLoad') == 'occupied'
        and candidate.get('origin') != 'promise'
        and candidate.get('origin') != 'practical-update'
    ):
        return {'allowed': False, 'reason': 'schedule-occupied', 'nextOpportunityAt': next_opportunity_at}
    return {'allowed': True, 'reason': 'capacity-available'}


def proactive_candidate_fingerprint(candidate: ProactiveContactDraft) -> str:
    """上游 proactiveCandidateFingerprint：`participantId|origin|升序 sourceEntryIds`。"""
    source_entry_ids = candidate.get('sourceEntryIds')
    # TS 的 `[...ids].sort((a, b) => a - b)` 会先做数值强制转换；这里用同样的比较器。
    entries = [] if source_entry_ids is None else list(source_entry_ids)
    ordered = sorted(entries, key=cmp_to_key(_numeric_compare))
    grounded = ','.join(_join_part(item) for item in ordered)
    return '|'.join([
        _join_part(candidate.get('participantId')),
        _join_part(candidate.get('origin')),
        grounded,
    ])


def proactive_recheck_at(
    candidate: ProactiveContactDraft,
    capacity: AgencyCapacityResult,
    window: AgencyWindowState,
    now: datetime,
) -> datetime:
    """上游 proactiveRecheckAt：在 notBefore / 容量 / 窗口三个未来时间里取最早，并受 expiresAt 封顶。"""
    requested = parse_time(candidate.get('notBefore'))
    # 上游这里直接用 capacity.nextOpportunityAt（类型就是 Date，不再 toDate）；
    # 本移植的容量结果同样是 datetime，非 datetime 的值按 JS 的无效比较丢弃。
    capacity_time = capacity.get('nextOpportunityAt')
    if not isinstance(capacity_time, datetime):
        capacity_time = None
    window_time = parse_time(window.get('nextOpportunityAt'))
    fallback = now + 30 * _MINUTE
    selected = sorted(
        entry for entry in (requested, capacity_time, window_time)
        if entry is not None and entry > now
    )
    earliest = selected[0] if selected else fallback
    expiry = parse_time(candidate.get('expiresAt')) or (now + _HOUR)
    return min(earliest, expiry)


def proactive_origin_bypasses_ordinary_interval(origin: ProactiveContactOrigin) -> bool:
    """上游 proactiveOriginBypassesOrdinaryInterval：只有承诺型来源绕过普通间隔。"""
    return origin == 'promise'


def _grounded_ids(
    value: Any,
    valid: AbstractSet[int],
    fallback: Optional[int] = None,
) -> List[int]:
    """上游 groundedIds：先取有效来源，空则回落 fallback，去重后取最后 20 条。"""
    ids = [entry for entry in _positive_ids(value) if entry in valid]
    if not ids and isinstance(fallback, (int, float)) and fallback > 0:
        ids.append(fallback)
    return list(dict.fromkeys(ids))[-20:]


def _positive_ids(value: Any) -> List[int]:
    """上游 positiveIds：Array.map(Number).filter(Number.isInteger && > 0)。"""
    if not is_array(value):
        return []
    ids: List[int] = []
    for item in value:
        number = _js_number(item)
        if math.isfinite(number) and number > 0 and float(number).is_integer():
            ids.append(int(number))
    return ids


def _future_date(value: Any, now: datetime) -> Optional[datetime]:
    """上游 futureDate：toDate 后只保留严格晚于 now 的时间。"""
    date = parse_time(value)
    return date if date is not None and date > now else None


def _numeric_compare(left: Any, right: Any) -> int:
    """上游 sort 比较器 `(a, b) => a - b`；差额为 NaN 时按规范当作 +0。"""
    difference = _js_number(left) - _js_number(right)
    if math.isnan(difference):
        return 0
    return -1 if difference < 0 else 1 if difference > 0 else 0


def _config_value(config: AgencyConfig, key: str) -> float:
    """读取 AgencyConfig 的数值字段。

    上游类型与 index.ts 的 Schema.default 保证字段存在且为数字；这里对缺失 / null / 非数字
    回落到 DEFAULT_AGENCY_CONFIG 的同名字段，避免把 JS `Math.max(5, undefined) === NaN`
    这种异常语义带进 Python。
    """
    result = config.get(key, DEFAULT_AGENCY_CONFIG[key])
    if isinstance(result, bool) or not isinstance(result, (int, float)) or not math.isfinite(float(result)):
        return float(DEFAULT_AGENCY_CONFIG[key])
    return float(result)


def _string(value: Any) -> str:
    """JS String()：上游用它做 `String(value.activityLoad)` / `String(value.participantId)`。"""
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        if value == 0:
            return '0'
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return str(value)
    if isinstance(value, (list, tuple)):
        return ','.join(_join_part(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    return str(value)


def _join_part(value: Any) -> str:
    """Array.prototype.join 的单个元素：undefined/null → 空串，其余 ToString。"""
    return '' if value is None else _string(value)


def _js_number(value: Any) -> float:
    """JS Number()，对应上游 `value.map(Number)`。

    覆盖 JSON 里可能出现的标量：null → 0、布尔 → 0/1、空串/空白 → 0、
    0x/0b/0o 字符串 → 对应整数、其余字符串交给 float()（'Infinity'/'NaN' 与 JS 一致）、
    数组沿用 Number([x]) 的递归语义、其它 → NaN。
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        body = text
        if body[0] in '+-':
            # JS 的 StringNumericLiteral 只允许十进制数字带符号；'-0x10' 在 JS 里是 NaN。
            body = body[1:]
        else:
            prefix = body[:2].lower()
            try:
                if prefix == '0x':
                    return float(int(body[2:], 16))
                if prefix == '0b':
                    return float(int(body[2:], 2))
                if prefix == '0o':
                    return float(int(body[2:], 8))
            except ValueError:
                return float('nan')
        try:
            return float(text)
        except ValueError:
            return float('nan')
    if isinstance(value, (list, tuple)):
        if not value:
            return 0.0
        if len(value) == 1:
            return _js_number(value[0])
        return float('nan')
    return float('nan')
