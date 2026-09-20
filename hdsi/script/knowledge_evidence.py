# -*- coding: utf-8 -*-
"""Knowledge evidence 归一化与证据线程，对应上游 src/script/knowledge-evidence.ts
（HDS-Interlude 1.0.1-beta6-rebuild）。

逐 export 移植：
- `KnowledgeEvidence`
- `normalizeKnowledgeEvidence` → `normalize_knowledge_evidence`
- `knowledgeClauses` → `knowledge_clauses`
- `knowledgeRelatedIds` → `knowledge_related_ids`
- `factEvidenceForPrompt` → `fact_evidence_for_prompt`
- `supportsRecordedOutcome` → `supports_recorded_outcome`
- `legacyConditionCue` → `legacy_condition_cue`
- `ContactEvidenceThread`
- `contactEvidenceThreads` → `contact_evidence_threads`
- `KNOWLEDGE_WRITING_FRAME`（长英文常量逐字复制）

语义约定：
- `entry.occurredAt.toISOString()` → `iso(entry['occurredAt'])`（hdsi.time_utils.iso，UTC + `Z`）。
- JS `Set` 的插入顺序用 `dict.fromkeys(...)` 复刻（去重且保序），`[...ids]` 的顺序与上游一致。
- 上游 `undefined` 等价于 `None`；对象字面量显式写出的字段保留 key。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, TypedDict

from ..time_utils import iso
from ..types import NarrativeFact, ScriptEntry
from ..utils import is_array, is_record

__all__ = [
    'KnowledgeEvidence',
    'normalize_knowledge_evidence',
    'knowledge_clauses',
    'knowledge_related_ids',
    'fact_evidence_for_prompt',
    'supports_recorded_outcome',
    'legacy_condition_cue',
    'ContactEvidenceThread',
    'contact_evidence_threads',
    'KNOWLEDGE_WRITING_FRAME',
]

# 上游私有白名单，成员与顺序逐字保留。
_MODES = ['observed', 'reported', 'belief', 'proposal', 'conditional', 'confirmed', 'unclassified']
_ROLES = ['observation', 'interpretation', 'proposal', 'condition', 'confirmation']


class _KnowledgeClause(TypedDict):
    """上游 KnowledgeEvidence.clauses 的内联元素形状（未单独导出）。"""

    role: str  # observation | interpretation | proposal | condition | confirmation
    sourceEntryId: int
    quote: str


class KnowledgeEvidence(TypedDict, total=False):
    """上游 export interface KnowledgeEvidence；holder/topic 可选，故 total=False。"""

    mode: str  # observed | reported | belief | proposal | conditional | confirmed | unclassified
    holder: str
    topic: str
    clauses: List[_KnowledgeClause]
    relatedFactIds: List[int]


class _ContactEvidenceOriginal(TypedDict):
    """上游 ContactEvidenceThread.originals 的内联元素形状（未单独导出）。"""

    id: int
    kind: str
    participantId: str
    content: str
    occurredAt: str


class ContactEvidenceThread(TypedDict):
    """上游 export interface ContactEvidenceThread。"""

    fact: Dict[str, Any]  # ReturnType<typeof factEvidenceForPrompt>
    originals: List[_ContactEvidenceOriginal]
    originalEntryIds: List[int]
    missingSourceEntryIds: List[int]


def normalize_knowledge_evidence(
    raw: Any,
    entries: List[ScriptEntry],
    source_entry_ids: List[int],
    related_fact_ids: Optional[List[int]] = None,
) -> KnowledgeEvidence:
    """上游 normalizeKnowledgeEvidence：校验引用而不是自然语言含义。

    引语是其作者话语的证据；叙事方的引语不能确认另一个人的行为。
    """
    # 上游默认参数 relatedFactIds: number[] = []（每次调用一个新数组），Python 用 None 复刻。
    if related_fact_ids is None:
        related_fact_ids = []
    value = raw if is_record(raw) else {}
    source_ids = source_entry_ids or []
    by_id: Dict[Any, ScriptEntry] = {}
    for entry in entries:
        if entry.get('id') in source_ids:
            by_id[entry.get('id')] = entry

    clauses: List[_KnowledgeClause] = []
    raw_clauses = value.get('clauses')
    for clause in (raw_clauses[:12] if is_array(raw_clauses) else []):
        if not is_record(clause):
            continue
        source_entry_id = clause.get('sourceEntryId')
        entry = by_id.get(source_entry_id) if _is_map_lookup_key(source_entry_id) else None
        role = clause.get('role')
        quote = clause.get('quote')
        if entry is None or role not in _ROLES:
            continue
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 800:
            continue
        content = entry.get('content')
        if not isinstance(content, str) or quote not in content:
            continue
        delivered = entry.get('kind') in (
            'user-message', 'character-message', 'group-message', 'character-group-message',
        )
        # Keep the quote, with its actual evidential role, instead of rejecting prose.
        clauses.append({
            'role': 'interpretation' if role == 'confirmation' and not delivered else role,
            'sourceEntryId': entry.get('id'),
            'quote': quote,
        })

    raw_mode = value.get('mode') or ''
    mode: str = raw_mode if raw_mode in _MODES else 'unclassified'
    if not clauses:
        mode = 'unclassified'
    if mode == 'confirmed':
        proposed = [clause for clause in clauses if clause.get('role') == 'proposal']
        confirmed = [clause for clause in clauses if clause.get('role') == 'confirmation']

        def exchanged(proposal: _KnowledgeClause, confirmation: _KnowledgeClause) -> bool:
            a = by_id.get(proposal.get('sourceEntryId'))
            b = by_id.get(confirmation.get('sourceEntryId'))
            if not is_record(a) or not is_record(b):
                return False
            return (
                a.get('kind') in ('user-message', 'character-message')
                and b.get('kind') in ('user-message', 'character-message')
                and a.get('kind') != b.get('kind')
                and a.get('participantId') == b.get('participantId')
                and b.get('id') > a.get('id')
            )

        has_exchange = any(
            exchanged(proposal, confirmation)
            for proposal in proposed
            for confirmation in confirmed
        )
        if not has_exchange:
            mode = 'conditional' if any(clause.get('role') == 'condition' for clause in clauses) else 'unclassified'
    if mode == 'observed' and any(clause.get('role') == 'interpretation' for clause in clauses):
        mode = 'belief'

    result: KnowledgeEvidence = {'mode': mode}
    holder = value.get('holder')
    topic = value.get('topic')
    if isinstance(holder, str) and holder.strip():
        result['holder'] = holder.strip()[:127]
    elif mode == 'belief':
        result['holder'] = 'protagonist'
    if isinstance(topic, str) and len(topic) <= 80 and any(topic in clause['quote'] for clause in clauses):
        result['topic'] = topic
    result['clauses'] = clauses
    # 上游 [...new Set(relatedFactIds)].slice(0, 12)：去重保序后取前 12 个。
    result['relatedFactIds'] = _unique_values(related_fact_ids)[:12]
    return result


def knowledge_clauses(knowledge: Optional[KnowledgeEvidence]) -> List[_KnowledgeClause]:
    """上游 knowledgeClauses：证据字段出现前落库的旧行可能是 {} 或部分对象。"""
    if is_record(knowledge) and is_array(knowledge.get('clauses')):
        return knowledge['clauses']
    return []


def knowledge_related_ids(knowledge: Optional[KnowledgeEvidence]) -> List[int]:
    """上游 knowledgeRelatedIds。"""
    if is_record(knowledge) and is_array(knowledge.get('relatedFactIds')):
        return knowledge['relatedFactIds']
    return []


def fact_evidence_for_prompt(fact: NarrativeFact) -> Dict[str, Any]:
    """上游 factEvidenceForPrompt：knowledge 的 clauses / 仅 relatedFactIds / 缺失三种分支。"""
    knowledge = fact.get('knowledge')
    resolved: Optional[Dict[str, Any]]
    if is_record(knowledge) and is_array(knowledge.get('clauses')):
        resolved = knowledge
    elif is_record(knowledge) and is_array(knowledge.get('relatedFactIds')):
        # 上游 { ...fact.knowledge, clauses: [] }
        resolved = {**knowledge, 'clauses': []}
    else:
        resolved = None
    return {
        'id': fact.get('id'),
        'participantId': fact.get('participantId'),
        'scope': fact.get('scope'),
        'content': fact.get('content'),
        'unresolved': fact.get('unresolved'),
        'status': fact.get('status'),
        'sourceEntryIds': fact.get('sourceEntryIds'),
        'authority': 'attributed-belief' if resolved is not None and resolved.get('mode') == 'belief' else 'derived-record',
        'knowledge': resolved if resolved is not None else {'mode': 'unclassified', 'clauses': [], 'relatedFactIds': []},
    }


def supports_recorded_outcome(knowledge: KnowledgeEvidence) -> bool:
    """上游 supportsRecordedOutcome：observed/reported/confirmed 且不含解读。"""
    return (
        knowledge.get('mode') in ('observed', 'reported', 'confirmed')
        and any(
            clause.get('role') in ('observation', 'confirmation')
            for clause in knowledge_clauses(knowledge)
        )
        and not any(
            clause.get('role') == 'interpretation'
            for clause in knowledge_clauses(knowledge)
        )
    )


def legacy_condition_cue(content: str) -> bool:
    """上游 legacyConditionCue：仅给证据出现前的旧记录一条检索通道，不判定条件成立。"""
    return re.search(r'前置|前提|门槛|至少|除非', content) is not None


def contact_evidence_threads(facts: List[NarrativeFact], entries: List[ScriptEntry]) -> List[ContactEvidenceThread]:
    """上游 contactEvidenceThreads：附上原始条件与邻近回复，范围在这里再校验一次。"""
    emitted: Set[int] = set()
    threads: List[ContactEvidenceThread] = []
    for fact in facts:
        # 上游 new Set([...fact.sourceEntryIds, ...knowledgeClauses(...).map(c => c.sourceEntryId)])：
        # dict.fromkeys 复刻 Set 的去重 + 插入顺序。
        ids: Dict[Any, None] = dict.fromkeys([
            *(fact.get('sourceEntryIds') or []),
            *(clause.get('sourceEntryId') for clause in knowledge_clauses(fact.get('knowledge'))),
        ])
        originals = [
            entry for entry in entries
            if entry.get('kind') in ('script', 'user-message', 'character-message', 'group-message', 'character-group-message')
            and (not entry.get('participantId') or entry.get('participantId') == fact.get('participantId'))
            and (
                entry.get('id') in ids
                or (
                    entry.get('kind') in ('user-message', 'character-message')
                    # 上游 Math.abs(id - entry.id) <= 2：非数字相减得 NaN，比较为 false，这里显式跳过。
                    and any(
                        _is_number(source_id) and _is_number(entry.get('id'))
                        and abs(source_id - entry.get('id')) <= 2
                        for source_id in ids
                    )
                )
            )
        ]
        originals.sort(key=_sort_id)
        mapped: List[_ContactEvidenceOriginal] = [
            {
                'id': entry.get('id'),
                'kind': entry.get('kind'),
                'participantId': entry.get('participantId'),
                'content': entry.get('content'),
                # 上游 entry.occurredAt.toISOString()
                'occurredAt': iso(entry['occurredAt']),
            }
            for entry in originals
        ]
        original_entry_ids = [entry['id'] for entry in mapped]
        # 上游 originals.filter(entry => { if (emitted.has(entry.id)) return false; ... })：
        # emitted 跨 fact 共享，后面的 fact 不再重复输出同一条原文。
        visible: List[_ContactEvidenceOriginal] = []
        for entry in mapped:
            if entry['id'] in emitted:
                continue
            emitted.add(entry['id'])
            visible.append(entry)
        # 上游 missingSourceEntryIds 用的是过滤掉 emitted 之前的 originals。
        missing_source_entry_ids = [
            source_id for source_id in ids
            if not any(entry['id'] == source_id for entry in mapped)
        ]
        threads.append({
            'fact': fact_evidence_for_prompt(fact),
            'originalEntryIds': original_entry_ids,
            'originals': visible,
            'missingSourceEntryIds': missing_source_entry_ids,
        })
    return threads


def _unique_values(values: Any) -> List[Any]:
    """上游 [...new Set(values)]：去重并保留首次出现的顺序。"""
    result: List[Any] = []
    seen = set()
    for item in (values or []):
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _is_map_lookup_key(value: Any) -> bool:
    """Map.get 的 key 可以是任意 JSON 值；这里挡掉 unhashable 与 bool（避免 True 撞上 id=1）。"""
    if isinstance(value, bool):
        return False
    return not isinstance(value, (list, dict, set))


def _is_number(value: Any) -> bool:
    """JS number 运行时判定（排除 bool）。"""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _sort_id(entry: Dict[str, Any]) -> Any:
    """上游 sort((a, b) => a.id - b.id)：id 缺失时按 0 处理，避免 Python 混类型排序报错。"""
    entry_id = entry.get('id')
    return entry_id if _is_number(entry_id) else 0


# 上游长英文常量：逐字复制，一个字符都不能改（含全部标点、空格与大小写）。
KNOWLEDGE_WRITING_FRAME = 'EVIDENCE AND EXPECTATION: The original remains the life script. Within it, her belief, wish and imagined explanation belong to her perspective; an observed action belongs to the actor who performed it. Derived records retain these roles and their original conditions. contactThreads supplies original proposals, conditions and replies, not a second plot. Let unfinished contact motivate another question or private anticipation while its confirmation and timing remain open. Elapsed silence can change her feelings without changing what the other person promised. A confirmed exchange still carries its conditions; platform delivery alone establishes neither reading nor agreement.'
