# -*- coding: utf-8 -*-
"""群聊意愿层，对应上游 .hdsi_reference/src/group-willingness.ts（1.0.1-beta6-rebuild）。

上游说明（逐字意译）：一个刻意保持很小、无需模型的群聊意愿层。灵感来自 YesImBot v3
的本地 score / decay / probability 模式，但只作用于单个 HDSI 群，绝不影响私聊回合、
Agency Window、Alter、prompt 或持久化故事状态。

本文件保持上游 clamp 的运算嵌套与顺序：clamp(v, min, max) = max(min, min(max, v))；
DEFAULT_GROUP_WILLINGNESS 的默认值一个字未改。配置 key、状态字段与判定字段
（shouldCall/probability/reason/updatedAt 等）保持 camelCase。
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Literal, Optional, TypedDict

__all__ = [
    'GroupWillingnessConfig',
    'GroupWillingnessState',
    'GroupWillingnessReason',
    'GroupWillingnessDecision',
    'DEFAULT_GROUP_WILLINGNESS',
    'resolve_group_willingness',
    'evaluate_group_willingness',
    'consume_group_willingness',
]


class GroupWillingnessConfig(TypedDict):
    """上游 interface GroupWillingnessConfig。"""

    enabled: bool
    maxScore: float
    threshold: float
    probabilityAmplifier: float
    decayHalfLifeSeconds: float
    replyCost: float
    baseGain: float
    quoteGain: float
    keywordGain: float
    keywords: List[str]


class GroupWillingnessState(TypedDict):
    """上游 interface GroupWillingnessState。"""

    score: float
    updatedAt: float


GroupWillingnessReason = Literal['disabled', 'forced-mention', 'below-threshold', 'probability-roll']


class GroupWillingnessDecision(TypedDict):
    """上游 interface GroupWillingnessDecision。"""

    state: GroupWillingnessState
    shouldCall: bool
    probability: float
    reason: GroupWillingnessReason


DEFAULT_GROUP_WILLINGNESS: GroupWillingnessConfig = {
    'enabled': False,
    'maxScore': 1,
    'threshold': 0.24,
    'probabilityAmplifier': 1.3,
    'decayHalfLifeSeconds': 180,
    'replyCost': 0.55,
    'baseGain': 0.12,
    'quoteGain': 0.12,
    'keywordGain': 0.18,
    'keywords': [],
}


def _javascript_string(value: Any) -> str:
    """最小实现 JS ``String(value)``，用于 keywords 归一化的逐字语义。"""
    if isinstance(value, str):
        return value
    if value is None:
        # 上游 TS 未显式支持 undefined；Python 的 None 对应 JSON null → 'null'。
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return str(value).replace('e-0', 'e-').replace('e+0', 'e+')
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None else _javascript_string(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    return str(value)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """上游私有 clamp：Math.max(min, Math.min(max, value))，嵌套与顺序逐字一致。"""
    return max(minimum, min(maximum, value))


def _decay(
    previous: Optional[GroupWillingnessState],
    config: GroupWillingnessConfig,
    now: float,
) -> GroupWillingnessState:
    """上游私有 decay：按半衰期衰减，新状态携带本次 updatedAt。"""
    score = previous.get('score') if isinstance(previous, dict) else None
    if score is None:
        score = 0
    updated_at = previous.get('updatedAt') if isinstance(previous, dict) else None
    if updated_at is None:
        updated_at = now
    elapsed_seconds = max(0, now - updated_at) / 1_000
    factor = math.pow(0.5, elapsed_seconds / max(1, config['decayHalfLifeSeconds']))
    decayed = score * factor
    return {'score': 0 if decayed < 0.001 else decayed, 'updatedAt': now}


def resolve_group_willingness(config: Optional[Dict[str, Any]] = None) -> GroupWillingnessConfig:
    """上游 resolveGroupWillingness：默认配置 + 局部覆盖，keywords 去空白/去空/截断 30。"""
    source = config if isinstance(config, dict) else {}
    merged: Dict[str, Any] = {**DEFAULT_GROUP_WILLINGNESS, **source}
    configured_keywords = source.get('keywords')
    if configured_keywords is None:
        configured_keywords = DEFAULT_GROUP_WILLINGNESS['keywords']
    keywords: List[str] = []
    for item in configured_keywords:
        text = _javascript_string(item).strip()
        if not text:
            continue
        keywords.append(text)
        if len(keywords) >= 30:
            break
    merged['keywords'] = keywords
    return merged


def evaluate_group_willingness(
    previous: Optional[GroupWillingnessState],
    config_input: Optional[Dict[str, Any]],
    input: Dict[str, Any],
) -> GroupWillingnessDecision:
    """上游 evaluateGroupWillingness：累积/衰减/阈值/概率判定。

    参数 ``input`` 沿用上游命名（dict：now/messageCount/content/quotedBot/mentionedBot/random）。
    """
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, input['now'])
    if not config['enabled']:
        return {'state': state, 'shouldCall': True, 'probability': 1, 'reason': 'disabled'}

    content = input['content']
    keyword_hit = False
    for keyword in config['keywords']:
        if keyword in content:
            keyword_hit = True
            break
    raw_gain = (
        config['baseGain'] * max(1, min(3, input['messageCount']))
        + (config['quoteGain'] if input.get('quotedBot') else 0)
        + (config['keywordGain'] if keyword_hit else 0)
    )
    marginal = 1 - math.pow(min(1, state['score'] / config['maxScore']), 2)
    state['score'] = _clamp(
        state['score'] + raw_gain * max(0, marginal),
        0,
        config['maxScore'],
    )

    if input.get('mentionedBot'):
        return {'state': state, 'shouldCall': True, 'probability': 1, 'reason': 'forced-mention'}
    if state['score'] <= config['threshold']:
        return {'state': state, 'shouldCall': False, 'probability': 0, 'reason': 'below-threshold'}
    probability = _clamp((state['score'] - config['threshold']) * config['probabilityAmplifier'], 0, 1)
    roll = input.get('random')
    if roll is None:
        roll = random.random()
    return {
        'state': state,
        'shouldCall': roll < probability,
        'probability': probability,
        'reason': 'probability-roll',
    }


def consume_group_willingness(
    previous: Optional[GroupWillingnessState],
    config_input: Optional[Dict[str, Any]],
    now: float,
) -> GroupWillingnessState:
    """上游 consumeGroupWillingness：成功群发言后扣除 replyCost，不低于 0。"""
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, now)
    return {'score': max(0, state['score'] - config['replyCost']), 'updatedAt': now}
