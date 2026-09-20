# -*- coding: utf-8 -*-
"""情绪偏移（Alter）系统，对应上游 src/alter.ts（HDS-Interlude 1.0.1-beta6-rebuild）。

移植约定：
- 同步函数；`Date` → `datetime`（UTC aware）；上游有默认时间的参数写 `None`，内部取 now_utc()；
- 数据形状一律 dict（TypedDict 仅做标注），持久化字段名保持 camelCase；
- 数值 clamp、阈值公式、历史条目裁剪、冷却毫秒数、字段名与默认值逐字对齐上游；
- JS 独有语义用私有 helper 复刻：Math.round（半值向 +∞）、Math.sign、isRecord（JS 里 `{}` 为真，
  utils.is_record 对空 dict 返回 False，故本文件用局部 _is_record）、`new Date(number)` 按 epoch 毫秒、
  `Date.prototype.toISOString()` 毫秒精度、`undefined` 等价于省略 key（JSON 序列化后一致）。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from .time_utils import ensure_aware, now_utc, parse_time
from .types import (
    AlterHistoryEntry,
    AlterPendingScope,
    AlterSystemConfig,
    AlterSystemState,
    EmotionalOffsetPrompt,
    NarrativePhase,
)
from .utils import clamp, finite_number

HOUR = 60 * 60 * 1000
HISTORY_LIMIT = 50

# `new Date(0).toISOString()`：归一化失败时所有时间字段的占位值。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EPOCH_ISO = '1970-01-01T00:00:00.000Z'
# normalizePhase 白名单，顺序与上游 ['advance','conversation-follow-up','user-message','intent-due'] 一致。
_NARRATIVE_PHASES = ('advance', 'conversation-follow-up', 'user-message', 'intent-due')

__all__ = [
    'DEFAULT_ALTER_SYSTEM_CONFIG',
    'AlterTurnResult',
    'resolve_alter_system_config',
    'normalize_alter_value',
    'create_alter_system_state',
    'normalize_alter_system_state',
    'calculate_alter_threshold',
    'adjust_alter_weight',
    'advance_alter_system',
    'complete_alter_analysis',
    'emotional_offset_for_prompt',
    'alter_analysis_cooling_down',
    'alter_scope_cooling_down',
    'mark_alter_scope_analysis_attempt',
    'alter_scope_value',
    'alter_history_for_scope',
]

DEFAULT_ALTER_SYSTEM_CONFIG: AlterSystemConfig = {
    'enabled': False,
    'baseThreshold': 10,
    'densityFactor': 0.3,
    'sameDirectionBoost': 0.05,
    'oppositeDecay': 0.15,
    'minWeight': 0.2,
    'maxIntensity': 2,
    'modelId': '',
    'providerId': '',
    'model': '',
    'temperature': 0.3,
    'topP': 1,
    'maxTokens': 400,
    'timeout': 30_000,
    'prompt': '',
}


class AlterTurnResult(TypedDict):
    state: AlterSystemState
    threshold: float
    offsetExpired: bool
    thresholdReached: bool
    sourceParticipantId: str
    triggerValue: float


def resolve_alter_system_config(value: Optional[AlterSystemConfig] = None) -> AlterSystemConfig:
    """TS: resolveAlterSystemConfig(value?: Partial<AlterSystemConfig>)"""
    return {**DEFAULT_ALTER_SYSTEM_CONFIG, **(value or {})}


def normalize_alter_value(value: Any) -> Optional[int]:
    """TS: normalizeAlterValue(value: unknown): number | undefined"""
    if finite_number(value) is None:
        return None
    return max(-5, min(5, _js_round(value)))


def create_alter_system_state(now: Optional[datetime] = None) -> AlterSystemState:
    """TS: createAlterSystemState(now = new Date())"""
    if now is None:
        now = now_utc()
    return {
        'alterValue': 0,
        'alterWeight': 0,
        'lastTriggerDirection': 0,
        'emotionalOffset': None,
        'history': [],
        'lastUpdatedAt': _to_iso(now),
    }


def normalize_alter_system_state(value: Any) -> Optional[AlterSystemState]:
    """TS: normalizeAlterSystemState(value: unknown): AlterSystemState | undefined"""
    if not _is_record(value):
        return None

    history: List[AlterHistoryEntry] = []
    raw_history = value.get('history')
    if isinstance(raw_history, list):
        entries: List[AlterHistoryEntry] = []
        for raw_entry in raw_history:
            if not _is_record(raw_entry):
                continue
            # 上游是 filter(isRecord).map((entry, index) => ...)，index 是过滤后的下标。
            index = len(entries)
            entry: AlterHistoryEntry = {
                'turn': max(1, int(math.floor(_finite_number(raw_entry.get('turn'), index + 1)))),
                'phase': _normalize_phase(raw_entry.get('phase')),
                'alter': _normalize_alter_value_or_zero(raw_entry.get('alter')),
                'alterValue': clamp(_finite_number(raw_entry.get('alterValue'), 0), -1_000, 1_000),
                'timestamp': _normalized_iso(raw_entry.get('timestamp')) or _EPOCH_ISO,
            }
            raw_participant_id = raw_entry.get('participantId')
            if isinstance(raw_participant_id, str) and raw_participant_id.strip():
                entry['participantId'] = raw_participant_id.strip()[:255]
            entries.append(entry)
        history = entries[-HISTORY_LIMIT:]

    emotional_offset = None
    raw_offset = value.get('emotionalOffset')
    if _is_record(raw_offset) and isinstance(raw_offset.get('description'), str):
        emotional_offset = {
            'direction': 'relaxed' if raw_offset.get('direction') == 'relaxed' else 'serious',
            'description': raw_offset['description'].strip()[:800],
            'intensity': clamp(_finite_number(raw_offset.get('intensity'), 1), 0, 3),
            'generatedAt': _normalized_iso(raw_offset.get('generatedAt')) or _EPOCH_ISO,
        }

    legacy_direction = _js_sign(_finite_number(value.get('lastTriggerAlter'), 0))
    direction = _js_sign(_finite_number(value.get('lastTriggerDirection'), legacy_direction))
    pending_scopes = _normalize_pending_scopes(value.get('pendingScopes'))
    # A persisted pre-scope state has only one safe interpretation: its prior
    # accumulated value belonged to the protagonist/global story stream.
    if not pending_scopes and abs(_finite_number(value.get('alterValue'), 0)) > 0:
        pending_scopes.append({
            'participantId': '',
            'alterValue': clamp(_finite_number(value.get('alterValue'), 0), -1_000, 1_000),
        })

    state: AlterSystemState = {
        'alterValue': clamp(_finite_number(value.get('alterValue'), 0), -1_000, 1_000),
        'alterWeight': clamp(_finite_number(value.get('alterWeight'), 0), 0, 1),
        'lastTriggerDirection': direction,
        'emotionalOffset': emotional_offset,
        'history': history,
        'pendingScopes': pending_scopes,
        'lastUpdatedAt': _normalized_iso(value.get('lastUpdatedAt')) or _EPOCH_ISO,
    }
    # 上游此处写 `lastAnalysisAttemptAt: normalizedIso(...)`（可能是 undefined）；
    # undefined 在 JSON 序列化后等于没有该 key，因此这里仅在解析成功时写入。
    last_analysis_attempt = _normalized_iso(value.get('lastAnalysisAttemptAt'))
    if last_analysis_attempt is not None:
        state['lastAnalysisAttemptAt'] = last_analysis_attempt
    return state


def calculate_alter_threshold(
    history: List[AlterHistoryEntry],
    config: AlterSystemConfig,
    now: Optional[datetime] = None,
) -> float:
    """TS: calculateAlterThreshold(history, config, now = new Date())"""
    if now is None:
        now = now_utc()
    one_hour_ago = _time_ms(now) - HOUR
    turns = 0
    for entry in history:
        timestamp = _date_value(entry.get('timestamp'))
        if (_time_ms(timestamp) if timestamp is not None else 0) >= one_hour_ago:
            turns += 1
    density = min(turns / 10, 1)
    base = max(1, _finite_number(config.get('baseThreshold'), 10))
    factor = clamp(_finite_number(config.get('densityFactor'), 0.3), 0, 1)
    return max(base * 0.5, base * (1 - density * factor))


def adjust_alter_weight(weight: float, same_direction: bool, magnitude: float, config: AlterSystemConfig) -> float:
    """TS: adjustAlterWeight(weight, sameDirection, magnitude, config)"""
    # 上游：sameDirection ? config.sameDirectionBoost : -config.oppositeDecay，再经 finiteNumber(rate, 0)。
    if same_direction:
        rate_number = finite_number(config.get('sameDirectionBoost'))
        rate = 0.0 if rate_number is None else rate_number
    else:
        rate_number = finite_number(config.get('oppositeDecay'))
        rate = 0.0 if rate_number is None else -rate_number
    return clamp(_finite_number(weight, 0) + _js_max_zero(magnitude) * rate, 0, 1)


def advance_alter_system(
    current: Optional[AlterSystemState],
    alter: float,
    phase: NarrativePhase,
    now: datetime,
    config: AlterSystemConfig,
    participant_id: str = '',
) -> AlterTurnResult:
    """TS: advanceAlterSystem(current, alter, phase, now, config, participantId = '')"""
    if current is not None:
        state: AlterSystemState = {
            **current,
            'history': list(current.get('history') or []),
            'pendingScopes': _normalize_pending_scopes(current.get('pendingScopes')),
        }
    else:
        state = create_alter_system_state(now)

    _materialize_legacy_pending_value(state)
    source_participant_id = _normalize_participant_id(participant_id)
    scope = _ensure_pending_scope(state, source_participant_id)
    scope['alterValue'] = clamp(scope['alterValue'] + alter, -1_000, 1_000)
    state['alterValue'] = _total_pending_alter(state.get('pendingScopes'))
    direction = _js_sign(alter)
    offset_expired = False
    if state.get('emotionalOffset') is not None and direction:
        state['alterWeight'] = adjust_alter_weight(
            _finite_number(state.get('alterWeight'), 0),
            direction == state.get('lastTriggerDirection'),
            abs(alter),
            config,
        )
        min_weight = _finite_number(config.get('minWeight'), DEFAULT_ALTER_SYSTEM_CONFIG['minWeight'])
        if state['alterWeight'] < min_weight:
            state['emotionalOffset'] = None
            state['alterWeight'] = 0
            offset_expired = True

    last_entry = state['history'][-1] if state['history'] else None
    previous_turn = last_entry.get('turn') if _is_record(last_entry) else None
    entry: AlterHistoryEntry = {
        'turn': (previous_turn if previous_turn is not None else 0) + 1,
        'phase': phase,
        'alter': alter,
        'alterValue': state['alterValue'],
        'timestamp': _to_iso(now),
    }
    if source_participant_id:
        entry['participantId'] = source_participant_id
    state['history'].append(entry)
    state['history'] = state['history'][-HISTORY_LIMIT:]
    state['lastUpdatedAt'] = _to_iso(now)

    scope_history = alter_history_for_scope(state['history'], source_participant_id)
    threshold = calculate_alter_threshold(scope_history, config, now)
    return {
        'state': state,
        'threshold': threshold,
        'offsetExpired': offset_expired,
        'thresholdReached': abs(scope['alterValue']) >= threshold,
        'sourceParticipantId': source_participant_id,
        'triggerValue': scope['alterValue'],
    }


def complete_alter_analysis(
    state: AlterSystemState,
    description: str,
    threshold: float,
    now: datetime,
    config: AlterSystemConfig,
    participant_id: str = '',
) -> AlterSystemState:
    """TS: completeAlterAnalysis(state, description, threshold, now, config, participantId = '')"""
    source_participant_id = _normalize_participant_id(participant_id)
    scopes = _normalize_pending_scopes(state.get('pendingScopes'))
    state_alter_value = _finite_number(state.get('alterValue'), 0)
    if not scopes and abs(state_alter_value) > 0:
        scopes.append({'participantId': '', 'alterValue': state_alter_value})
    scope = _ensure_pending_scope({**state, 'pendingScopes': scopes}, source_participant_id)
    trigger_value = _finite_number(scope.get('alterValue'), 0)
    direction = _js_sign(trigger_value)
    scope['alterValue'] = 0
    # 上游 scope.lastAnalysisAttemptAt = undefined，等价于删掉该 key。
    if 'lastAnalysisAttemptAt' in scope:
        del scope['lastAnalysisAttemptAt']
    return {
        **state,
        'alterValue': _total_pending_alter(scopes),
        'pendingScopes': scopes,
        'alterWeight': 1,
        'lastTriggerDirection': direction,
        'emotionalOffset': {
            'direction': 'serious' if direction > 0 else 'relaxed',
            'description': description.strip()[:800],
            'intensity': min(
                abs(trigger_value) / max(1, threshold),
                _finite_number(config.get('maxIntensity'), DEFAULT_ALTER_SYSTEM_CONFIG['maxIntensity']),
            ),
            'generatedAt': _to_iso(now),
        },
        'lastUpdatedAt': _to_iso(now),
    }


def emotional_offset_for_prompt(
    state: Optional[AlterSystemState],
    config: AlterSystemConfig,
) -> Optional[EmotionalOffsetPrompt]:
    """TS: emotionalOffsetForPrompt(state, config): EmotionalOffsetPrompt | null"""
    if not config.get('enabled') or state is None or state.get('emotionalOffset') is None:
        return None
    weight = _finite_number(state.get('alterWeight'), 0)
    if weight < _finite_number(config.get('minWeight'), DEFAULT_ALTER_SYSTEM_CONFIG['minWeight']):
        return None
    return {**state['emotionalOffset'], 'weight': weight}  # type: ignore[arg-type]


def alter_analysis_cooling_down(
    state: AlterSystemState,
    now: Optional[datetime] = None,
    cooldown_ms: float = 5 * 60 * 1000,
) -> bool:
    """TS: alterAnalysisCoolingDown(state, now = new Date(), cooldownMs = 5 * 60 * 1000)"""
    if now is None:
        now = now_utc()
    last_attempt = _date_value(state.get('lastAnalysisAttemptAt'))
    return last_attempt is not None and _time_ms(now) - _time_ms(last_attempt) < cooldown_ms


# The current scope has its own retry gate, so a failed relationship-local
# analysis does not suppress an unrelated piece of independent life.
def alter_scope_cooling_down(
    state: AlterSystemState,
    participant_id: str = '',
    now: Optional[datetime] = None,
    cooldown_ms: float = 5 * 60 * 1000,
) -> bool:
    """TS: alterScopeCoolingDown(state, participantId = '', now = new Date(), cooldownMs = 5 * 60 * 1000)"""
    if now is None:
        now = now_utc()
    scope = _find_pending_scope(state.get('pendingScopes'), _normalize_participant_id(participant_id))
    last_attempt = _date_value(
        scope.get('lastAnalysisAttemptAt') if scope is not None else state.get('lastAnalysisAttemptAt')
    )
    return last_attempt is not None and _time_ms(now) - _time_ms(last_attempt) < cooldown_ms


def mark_alter_scope_analysis_attempt(
    state: AlterSystemState,
    participant_id: str = '',
    now: Optional[datetime] = None,
) -> AlterSystemState:
    """TS: markAlterScopeAnalysisAttempt(state, participantId = '', now = new Date())"""
    if now is None:
        now = now_utc()
    scopes = _normalize_pending_scopes(state.get('pendingScopes'))
    scope = _ensure_pending_scope({**state, 'pendingScopes': scopes}, _normalize_participant_id(participant_id))
    scope['lastAnalysisAttemptAt'] = _to_iso(now)
    return {**state, 'pendingScopes': scopes}


def alter_scope_value(state: AlterSystemState, participant_id: str = '') -> float:
    """TS: alterScopeValue(state, participantId = '')"""
    scope = _find_pending_scope(state.get('pendingScopes'), _normalize_participant_id(participant_id))
    value = scope.get('alterValue') if scope is not None else None
    return value if value is not None else 0


def alter_history_for_scope(
    history: List[AlterHistoryEntry],
    participant_id: str = '',
) -> List[AlterHistoryEntry]:
    """TS: alterHistoryForScope(history, participantId = '')"""
    source_participant_id = _normalize_participant_id(participant_id)
    return [
        entry
        for entry in history
        if _normalize_participant_id(entry.get('participantId') if _is_record(entry) else '') == source_participant_id
    ]


# ========== 私有 helper（对应上游文件内局部函数 / 常量） ==========


def _is_record(value: Any) -> bool:
    """TS: isRecord(value)：`!!value && typeof value === 'object' && !Array.isArray(value)`。

    JS 里 `!!{}` 为 true；utils.is_record 用 bool(value) 判断，对空 dict 会返回 False，
    会让 `normalizeAlterSystemState({})` 错误地变成 None，因此这里单独复刻。
    """
    return isinstance(value, dict)


def _normalize_pending_scopes(value: Any) -> List[AlterPendingScope]:
    if not isinstance(value, list):
        return []
    by_participant: Dict[str, AlterPendingScope] = {}
    for item in value:
        if not _is_record(item):
            continue
        participant_id = _normalize_participant_id(item.get('participantId'))
        existing = by_participant.get(participant_id)
        alter_value = clamp(_finite_number(item.get('alterValue'), 0), -1_000, 1_000)
        scope: AlterPendingScope = {
            'participantId': participant_id,
            'alterValue': clamp(
                ((existing.get('alterValue') if existing is not None else 0) or 0) + alter_value,
                -1_000,
                1_000,
            ),
        }
        attempt = _normalized_iso(item.get('lastAnalysisAttemptAt'))
        if attempt is not None:
            scope['lastAnalysisAttemptAt'] = attempt
        by_participant[participant_id] = scope
    return list(by_participant.values())[:32]


def _normalize_participant_id(value: Any) -> str:
    return value.strip()[:255] if isinstance(value, str) else ''


def _find_pending_scope(
    scopes: Optional[List[AlterPendingScope]],
    participant_id: str,
) -> Optional[AlterPendingScope]:
    for scope in scopes or []:
        if _is_record(scope) and scope.get('participantId') == participant_id:
            return scope
    return None


def _ensure_pending_scope(state: Dict[str, Any], participant_id: str) -> AlterPendingScope:
    scopes = state.get('pendingScopes')
    if scopes is None:
        scopes = []
        state['pendingScopes'] = scopes
    scope = _find_pending_scope(scopes, participant_id)
    if scope is None:
        scope = {'participantId': participant_id, 'alterValue': 0}
        scopes.append(scope)
    return scope


def _total_pending_alter(scopes: Optional[List[AlterPendingScope]]) -> float:
    total = 0
    for scope in scopes or []:
        total += _finite_number(scope.get('alterValue'), 0)
    return clamp(total, -1_000, 1_000)


def _materialize_legacy_pending_value(state: AlterSystemState) -> None:
    if not state.get('pendingScopes') and abs(_finite_number(state.get('alterValue'), 0)) > 0:
        state['pendingScopes'] = [{'participantId': '', 'alterValue': state.get('alterValue')}]


def _normalize_phase(value: Any) -> NarrativePhase:
    text = str(value)
    return text if text in _NARRATIVE_PHASES else 'user-message'


def _normalize_alter_value_or_zero(value: Any) -> int:
    normalized = normalize_alter_value(value)
    return 0 if normalized is None else normalized


def _normalized_iso(value: Any) -> Optional[str]:
    parsed = _date_value(value)
    return _to_iso(parsed) if parsed is not None else None


def _date_value(value: Any) -> Optional[datetime]:
    """TS: dateValue(value)：字符串 / 数字（epoch 毫秒）/ Date，非法值返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, (int, float)):
        # JS 的 new Date(number) 按毫秒解释；time_utils.parse_time 会把小数当秒，故单独处理。
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        return parse_time(value)
    return None


def _to_iso(value: datetime) -> str:
    """TS: Date.prototype.toISOString()：UTC + 毫秒精度 + Z 结尾。"""
    aware = ensure_aware(value).astimezone(timezone.utc)
    return (
        f'{aware.year:04d}-{aware.month:02d}-{aware.day:02d}'
        f'T{aware.hour:02d}:{aware.minute:02d}:{aware.second:02d}'
        f'.{aware.microsecond // 1000:03d}Z'
    )


def _time_ms(value: datetime) -> int:
    """TS: Date.prototype.getTime()：epoch 毫秒（微秒截断）。"""
    delta = ensure_aware(value) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _finite_number(value: Any, fallback: float) -> Any:
    """TS: finiteNumber(value, fallback)：有限数值保留原值，否则回落 fallback。"""
    if finite_number(value) is None:
        return fallback
    return value


def _js_round(value: float) -> int:
    """TS: Math.round：半值向 +∞ 取整（Python round 是银行家舍入，不能直接用）。"""
    return int(math.floor(value + 0.5))


def _js_max_zero(value: float) -> float:
    """TS: Math.max(0, value)：NaN 保持 NaN（Python 的 max(0, NaN) 会返回 0），负数取 0。"""
    if isinstance(value, float) and math.isnan(value):
        return value
    return value if value > 0 else 0


def _js_sign(value: float) -> float:
    """TS: Math.sign：NaN → NaN，正 → 1，负 → -1，零 → 0。"""
    if value != value:  # NaN
        return float('nan')
    return 1 if value > 0 else -1 if value < 0 else 0
