# -*- coding: utf-8 -*-
"""Urge 调度元数据，对应上游 .hdsi_reference/src/urge.ts（1.0.1-beta6-rebuild）。

上游注释：Scheduling metadata only: never a contact decision or a memory source.
本文件按 PORTING_GUIDE.md 同步化移植：函数/变量改 snake_case，
持久化字段与 JSON key（mode/buckets/value/pace/suggested/sourceEntryId/armed/burst/spent/reason 等）
保持 camelCase 不变。

- 上游 `random = Math.random` 默认参数 → Python `random_fn=None`，函数内部回退到 `random.random`。
- 上游 `undefined` → Python `None`（可选字段可能缺 key 或显式为 None，读取一律用 `.get()`）。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, TypedDict

__all__ = [
    'UrgeConfig',
    'UrgeState',
    'ResolvedUrgeConfig',
    'resolve_urge_config',
    'normalize_urge_state',
    'urge_user_event',
    'urge_density',
    'commit_urge',
    'acknowledge_urge',
    'urge_burst_active',
    'plan_urge',
    'urge_instruction',
]

_MINUTE = 60_000
_FREQUENCIES = ('low', 'medium', 'high', 'custom')
_URGE_PHASES = ('advance', 'conversation-follow-up', 'intent-due')

_URGE_INSTRUCTION = (
    '\nAfter writing the full script and its actions, optionally return '
    'urge:{value:0..1, pace:"normal"|"slow", suggestedDelayMinutes:number, '
    'basisQuote:"exact sentence from this script"}. Reflect the protagonist\'s '
    'present impulse and natural next opportunity to continue life; slow suits '
    'sleep or sustained focus. This is only a scheduling handoff, not speech, a '
    'future event, or a second contact decision. Continue using the existing '
    'authored actions and proactiveContact for any contact.'
)


class _UrgeAdvancedConfig(TypedDict, total=False):
    hotMin: float
    hotMax: float
    idleMin: float
    idleMax: float
    burstMin: float
    burstMax: float
    slowMin: float
    slowMax: float
    halfLifeMinutes: float
    burstThreshold: float
    jitter: float
    extremeChance: float
    burstTtlMinutes: float
    burstBudget: float
    burstContactMinMinutes: float


class _UrgeArmedState(TypedDict, total=False):
    participantId: str
    entryId: int
    at: float


class _UrgeBurstState(TypedDict, total=False):
    participantId: str
    started: float
    used: int


class UrgeConfig(TypedDict, total=False):
    """上游 interface UrgeConfig。"""

    enabled: bool
    frequency: Literal['low', 'medium', 'high', 'custom']
    proactiveWillingnessThreshold: float
    advanced: _UrgeAdvancedConfig


class UrgeState(TypedDict, total=False):
    """上游 interface UrgeState。"""

    version: int
    mode: str
    buckets: List[float]
    value: float
    pace: Literal['normal', 'slow']
    suggested: float
    sourceEntryId: int
    armed: _UrgeArmedState
    burst: _UrgeBurstState
    # 一旦消耗，只有真实 incoming event 才会开启新的联系阶段。
    spent: bool
    reason: str


class ResolvedUrgeConfig(TypedDict):
    """上游 type ResolvedUrgeConfig = ReturnType<typeof resolveUrgeConfig>。"""

    enabled: bool
    frequency: str
    willingness: float
    hot: List[float]
    idle: List[float]
    burst: List[float]
    slow: List[float]
    halfLife: float
    threshold: float
    jitter: float
    extremeChance: float
    ttl: float
    budget: int
    contactMin: float


def _record(value: Any) -> Dict[str, Any]:
    """TS 私有函数 record(v)：仅接受对象，数组/原始值一律返回 {}。"""
    return value if isinstance(value, dict) else {}


def _is_number(value: Any) -> bool:
    """TS: typeof value === 'number'（bool 不是 number，NaN/Infinity 仍是 number）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    """TS: typeof value === 'number' && Number.isFinite(value)。"""
    if not _is_number(value):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


def _finite(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    """TS 私有函数 finite(v, fallback, min, max)：非有限数字回退，否则按 [min, max] 夹取。"""
    if not _is_finite_number(value):
        return fallback
    return max(minimum, min(maximum, value))


def _js_iso_from_epoch_ms(epoch_ms: float) -> str:
    """等价于 JS ``new Date(ms).toISOString()``：UTC、3 位毫秒、Z 结尾。"""
    total_ms = math.trunc(epoch_ms)
    seconds, millis = divmod(total_ms, 1000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return (
        f'{moment.year:04d}-{moment.month:02d}-{moment.day:02d}'
        f'T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{millis:03d}Z'
    )


def resolve_urge_config(raw: Optional[UrgeConfig] = None) -> ResolvedUrgeConfig:
    """上游 resolveUrgeConfig：解析档位与高级区间，默认值逐字一致。"""
    c = _record(raw)
    a = _record(c.get('advanced'))
    frequency_value = c.get('frequency')
    frequency = frequency_value if isinstance(frequency_value, str) and frequency_value in _FREQUENCIES else 'medium'
    if frequency == 'low':
        defaults = [20, 30, 60, 90, 5, 10, 120, 180]
    elif frequency == 'high':
        defaults = [6, 12, 20, 35, 2, 5, 90, 120]
    else:
        defaults = [10, 20, 35, 55, 3, 7, 110, 130]

    def range_(name: str, index: int) -> List[float]:
        low = _finite(a.get(name + 'Min'), defaults[index], 1, 1440)
        return [low, max(low, _finite(a.get(name + 'Max'), defaults[index + 1], 1, 1440))]

    return {
        'enabled': c.get('enabled') is True,
        'frequency': frequency,
        'willingness': _finite(c.get('proactiveWillingnessThreshold'), .4, 0, 1),
        'hot': range_('hot', 0),
        'idle': range_('idle', 2),
        'burst': range_('burst', 4),
        'slow': range_('slow', 6),
        'halfLife': _finite(a.get('halfLifeMinutes'), 45, 5, 240),
        'threshold': _finite(a.get('burstThreshold'), .75, 0, 1),
        'jitter': _finite(a.get('jitter'), .15, 0, 1),
        'extremeChance': _finite(a.get('extremeChance'), .03, 0, 1),
        'ttl': _finite(a.get('burstTtlMinutes'), 35, 5, 120),
        'budget': math.floor(_finite(a.get('burstBudget'), 3, 0, 10)),
        'contactMin': _finite(a.get('burstContactMinMinutes'), 5, 1, 60),
    }


def normalize_urge_state(raw: Any, now: float) -> UrgeState:
    """上游 normalizeUrgeState：旧 JSON/残缺状态一律防御式读取。"""
    r = _record(raw)
    version = r.get('version')
    if not (_is_number(version) and version == 1):
        return {'version': 1, 'buckets': []}

    def timestamp(value: Any) -> bool:
        """TS 私有 timestamp(v)：有限数字、>0 且不晚于 now。"""
        return _is_finite_number(value) and value > 0 and value <= now

    a = _record(r.get('armed'))
    b = _record(r.get('burst'))

    buckets: List[float] = []
    raw_buckets = r.get('buckets')
    if isinstance(raw_buckets, (list, tuple)):
        seen = set()
        candidates: List[float] = []
        for value in raw_buckets:
            if timestamp(value) and now - value < 240 * _MINUTE:
                if value not in seen:
                    seen.add(value)
                    candidates.append(value)
        candidates.sort()
        buckets = candidates[-120:]

    armed: Optional[_UrgeArmedState] = None
    armed_participant = a.get('participantId')
    armed_entry_id = a.get('entryId')
    armed_at = a.get('at')
    if (
        isinstance(armed_participant, str)
        and _is_number(armed_entry_id)
        and timestamp(armed_at)
        and now - armed_at < 10 * _MINUTE
    ):
        armed = {'participantId': armed_participant, 'entryId': armed_entry_id, 'at': armed_at}

    burst: Optional[_UrgeBurstState] = None
    burst_participant = b.get('participantId')
    burst_started = b.get('started')
    if isinstance(burst_participant, str) and timestamp(burst_started):
        burst = {
            'participantId': burst_participant,
            'started': burst_started,
            'used': math.floor(_finite(b.get('used'), 0, 0, 10)),
        }

    mode = r.get('mode')
    value = r.get('value')
    suggested = r.get('suggested')
    source_entry_id = r.get('sourceEntryId')
    spent = r.get('spent')
    reason = r.get('reason')

    return {
        'version': 1,
        'mode': mode if isinstance(mode, str) else None,
        'buckets': buckets,
        'value': _finite(value, 0, 0, 1) if _is_number(value) else None,
        'pace': 'slow' if r.get('pace') == 'slow' else 'normal',
        'suggested': _finite(suggested, 120, 1, 1440) if _is_number(suggested) else None,
        'sourceEntryId': source_entry_id if _is_number(source_entry_id) else None,
        'spent': spent is True,
        'reason': reason[:160] if isinstance(reason, str) else None,
        'armed': armed,
        'burst': burst,
    }


def urge_user_event(state: UrgeState, now: float) -> UrgeState:
    """上游 urgeUserEvent：两分钟桶合并输入碎片；只有真实 incoming event 调用。"""
    bucket = math.floor(now / (2 * _MINUTE)) * 2 * _MINUTE
    raw_buckets = state.get('buckets') or []
    seen = set()
    buckets: List[float] = []
    for value in list(raw_buckets) + [bucket]:
        if value in seen:
            continue
        seen.add(value)
        buckets.append(value)
    return {
        **state,
        'buckets': buckets[-120:],
        'pace': 'normal',
        'suggested': None,
        'armed': None,
        'burst': None,
        'spent': False,
    }


def urge_density(state: UrgeState, now: float, c: ResolvedUrgeConfig) -> float:
    """上游 urgeDensity：最近 15 分钟密度 × 半衰期衰减。"""
    buckets = state.get('buckets') or []
    if not buckets:
        return 0
    last = buckets[-1]
    recent = sum(1 for value in buckets if value >= last - 15 * _MINUTE)
    return min(1, recent / 6) * math.pow(.5, max(0, now - last) / (c['halfLife'] * _MINUTE))


def commit_urge(
    state: UrgeState,
    raw: Any,
    script: str,
    entry_id: int,
    target: Optional[str],
    now: float,
    c: ResolvedUrgeConfig,
    random_fn=None,
) -> UrgeState:
    """上游 commitUrge：只有剧本里真实出现的 basisQuote 才能交接 Urge。"""
    if random_fn is None:
        random_fn = random.random
    r = _record(raw)
    basis_quote = r.get('basisQuote')
    quote = basis_quote.strip() if isinstance(basis_quote, str) else ''
    # 缺失/无效的交接不得延续旧的 slow 阶段或 burst。
    if not quote or quote not in script or not _is_finite_number(r.get('value')):
        return {**state, 'pace': 'normal', 'suggested': None, 'armed': None, 'burst': None}

    if random_fn() < c['extremeChance']:
        value = random_fn()
    else:
        value = _finite(r.get('value') + (random_fn() * 2 - 1) * c['jitter'], 0, 0, 1)
    pace = 'slow' if r.get('pace') == 'slow' else 'normal'
    high = value >= c['threshold'] and pace != 'slow'
    suggested_value = r.get('suggestedDelayMinutes')
    suggested = _finite(suggested_value, 120, 1, 1440) if _is_number(suggested_value) else None
    armed = None
    if high and target and not state.get('spent') and state.get('burst') is None and c['budget'] > 0:
        armed = {'participantId': target, 'entryId': entry_id, 'at': now}
    return {
        **state,
        'value': value,
        'pace': pace,
        'sourceEntryId': entry_id,
        'suggested': suggested,
        'burst': state.get('burst') if high else None,
        'armed': armed,
    }


def acknowledge_urge(state: UrgeState, participant_id: str, entry_id: int, now: float) -> UrgeState:
    """上游 acknowledgeUrge：只有匹配的实际投递回执才开启 burst。"""
    armed = state.get('armed')
    if (
        not isinstance(armed, dict)
        or armed.get('participantId') != participant_id
        or armed.get('entryId') != entry_id
        or state.get('spent')
    ):
        return state
    return {
        **state,
        'armed': None,
        'spent': True,
        'burst': {'participantId': participant_id, 'started': now, 'used': 0},
    }


def urge_burst_active(
    state: UrgeState,
    now: float,
    c: ResolvedUrgeConfig,
    participant_id: Optional[str] = None,
) -> bool:
    """上游 urgeBurstActive：TTL 内、预算未耗尽且非 slow 的 confirmed-contact burst。"""
    burst = state.get('burst')
    if not isinstance(burst, dict):
        return False
    if participant_id and participant_id != burst.get('participantId'):
        return False
    started = burst.get('started')
    used = burst.get('used')
    if not _is_number(started) or not _is_number(used):
        return False
    return bool(
        now - started < c['ttl'] * _MINUTE
        and used <= c['budget']
        and state.get('pace') != 'slow'
    )


def plan_urge(
    state: UrgeState,
    now: float,
    c: ResolvedUrgeConfig,
    rest_minutes: float = 0,
    unavailable: bool = False,
    random_fn=None,
) -> Dict[str, Any]:
    """上游 planUrge：采样下一次推进时间，返回 {state, nextAdvanceAt, minutes, reason}。"""
    if random_fn is None:
        random_fn = random.random

    def sample(range_: List[float]) -> float:
        return range_[0] + random_fn() * (range_[1] - range_[0])

    next_state: Dict[str, Any] = {**state}
    if rest_minutes > 0 or state.get('pace') == 'slow':
        suggested_value = state.get('suggested')
        if suggested_value is None:
            suggested = sample(c['slow'])
        else:
            suggested = max(
                c['slow'][0],
                min(c['slow'][1], suggested_value * (.9 + random_fn() * .2)),
            )
        minutes = max(rest_minutes, suggested)
        reason = 'rest-window' if rest_minutes else 'script-slow'
        next_state = {**next_state, 'burst': None, 'armed': None}
    elif (
        not unavailable
        and urge_burst_active(state, now, c)
        and state['burst']['used'] < c['budget']
    ):
        burst = state['burst']
        minutes = sample(c['burst']) * math.pow(2, burst['used'])
        minutes = min(minutes, max(1, (burst['started'] + c['ttl'] * _MINUTE - now) / _MINUTE))
        reason = 'confirmed-contact-burst'
        next_state['burst'] = {**burst, 'used': burst['used'] + 1}
    else:
        idle = sample(c['idle'])
        hot = min(idle, sample(c['hot']))
        minutes = idle - urge_density(state, now, c) * (idle - hot)
        reason = 'conversation-density-decay'
        next_state['burst'] = None
        if unavailable:
            next_state['armed'] = None
    next_state['reason'] = reason
    return {
        'state': next_state,
        'nextAdvanceAt': _js_iso_from_epoch_ms(now + minutes * _MINUTE),
        'minutes': minutes,
        'reason': reason,
    }


def urge_instruction(enabled: bool, phase: str) -> str:
    """上游 urgeInstruction：只在自动主叙事阶段给出调度交接说明。"""
    return _URGE_INSTRUCTION if enabled and phase in _URGE_PHASES else ''
