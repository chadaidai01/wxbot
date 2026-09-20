# -*- coding: utf-8 -*-
"""生命交接指针，对应上游 `.hdsi_reference/src/script/life-handoff.ts`（1.0.1-beta6-rebuild，51 行）。

上游语义：handoff 只是「指向本次已提交原文的小指针」，不是第二份场景摘要；
quote 用于证明出处（必须逐字出现在 prose 里），不构成语义证明，推断仍归文学侧。
旧版账本保持可区分（communicationOutcome / timelineEvidence），不在原地重解释。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from ..types import ScriptEntry
from ..utils import unique_strings

__all__ = [
    'QuotedLifeValue',
    'LifeHandoff',
    'normalize_life_handoff',
    'entry_life_handoff',
    'narrative_evidence',
]


class QuotedLifeValue(TypedDict):
    """上游 export interface QuotedLifeValue。"""

    value: str
    quote: str


class _LifePresence(TypedDict):
    """上游 LifeHandoff 内联的 `{ names: string[]; quote: string }`（含显式空名册）。"""

    names: List[str]
    quote: str


class _LifeTransition(TypedDict):
    """上游 LifeHandoff 内联的 `{ quote: string }`。"""

    quote: str


class _LifeResolvedDetail(TypedDict):
    """上游 LifeHandoff 内联的 `{ label: string; quote: string }`。"""

    label: str
    quote: str


class LifeHandoff(TypedDict, total=False):
    """上游 export interface LifeHandoff（字段全部可选）。"""

    place: QuotedLifeValue
    activity: QuotedLifeValue
    presence: _LifePresence
    transition: _LifeTransition
    resolvedDetails: List[_LifeResolvedDetail]


def _quoted(quote: Any, prose: str) -> bool:
    """上游闭包 quoted：字符串、trim 后至少 2 字、原文不超过 500 字、且逐字出现在 prose 中。"""
    return (
        isinstance(quote, str)
        and len(quote.strip()) >= 2
        and len(quote) <= 500
        and quote in prose
    )


def normalize_life_handoff(raw: Any, prose: str) -> Optional[LifeHandoff]:
    """上游 normalizeLifeHandoff：grounded 的细节指针；没有任何有效字段时返回 None（上游 undefined）。"""
    # 上游 `if (!raw || typeof raw !== 'object') return undefined`。JS 里 `{}` 是 truthy，
    # 所以只有 None / 非 dict 才算缺失。
    if raw is None or not isinstance(raw, dict):
        return None
    value = raw
    result: Dict[str, Any] = {}
    for key in ('place', 'activity'):
        item = value.get(key)
        if (
            isinstance(item, dict)
            and isinstance(item.get('value'), str)
            and item['value'].strip()
            and len(item['value']) <= 160
            and _quoted(item.get('quote'), prose)
        ):
            result[key] = {'value': item['value'].strip(), 'quote': item['quote']}
    presence = value.get('presence')
    if isinstance(presence, dict) and isinstance(presence.get('names'), list):
        names = presence['names']
        quote = presence.get('quote')
        # 上游 names.every(name => typeof name === 'string' && name.trim() && quote.includes(name))；
        # 空数组 every 恒真，所以「显式举证的空名册」是合法的。
        if _quoted(quote, prose) and all(
            isinstance(name, str) and name.strip() and name in quote for name in names
        ):
            # 上游 `[...new Set(names)].slice(0, 8)`：先按首次出现顺序去重，再取前 8 个。
            result['presence'] = {'names': unique_strings(names, 8), 'quote': quote}
    transition = value.get('transition')
    if isinstance(transition, dict) and _quoted(transition.get('quote'), prose):
        result['transition'] = {'quote': transition['quote']}
    details = value.get('resolvedDetails')
    if isinstance(details, list):
        # 上游先 filter（label 可为空串，只限长度），再 slice(0, 10)，最后 map 成 {label, quote}。
        grounded = [
            item for item in details
            if isinstance(item, dict)
            and isinstance(item.get('label'), str)
            and len(item['label']) <= 80
            and _quoted(item.get('quote'), prose)
        ]
        result['resolvedDetails'] = [
            {'label': item['label'], 'quote': item['quote']} for item in grounded[:10]
        ]
    # 上游 `Object.keys(result).length ? result : undefined`。
    return result or None


def entry_life_handoff(entry: ScriptEntry) -> Optional[LifeHandoff]:
    """上游 entryLifeHandoff：只有 kind === 'script' 的原文条目才携带 lifeHandoff。"""
    if not isinstance(entry, dict) or entry.get('kind') != 'script':
        return None
    metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
    content = entry.get('content') if isinstance(entry.get('content'), str) else ''
    return normalize_life_handoff(metadata.get('lifeHandoff'), content)


def narrative_evidence(entry: ScriptEntry) -> Dict[str, Any]:
    """上游 narrativeEvidence：新原文是完成态记录，导演计划仍只是计划；旧账本保持可区分。"""
    record = entry if isinstance(entry, dict) else {}
    metadata = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
    result: Dict[str, Any] = {}
    delivery_actions = metadata.get('deliveryActions')
    # 上游条件：metadata.commitId 存在 && deliveryActions 是数组 && 数组为空。
    if metadata.get('commitId') and isinstance(delivery_actions, list) and len(delivery_actions) == 0:
        result['communicationOutcome'] = 'no-outgoing-action-recorded'
    if metadata.get('narrativeAuthority') == 'original-v2':
        result['narrativeAuthority'] = 'original-v2'
        # 上游是显式写 key 的 `lifeHandoff: entryLifeHandoff(entry)`；值为 undefined 时
        # JSON 序列化会丢 key，这里保留 key 并置 None，读取方统一用 .get()。
        result['lifeHandoff'] = entry_life_handoff(record)
        if metadata.get('timelinePlan'):
            result['proposedTimeline'] = metadata['timelinePlan']
    elif metadata.get('timelinePlan'):
        result['timelineEvidence'] = metadata['timelinePlan']
    return result
