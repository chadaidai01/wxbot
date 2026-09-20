# -*- coding: utf-8 -*-
"""Development 证据链与成长维度，对应上游 src/script/development.ts（HDS-Interlude 1.0.1-beta6-rebuild）。

逐 export 移植：
- `interactionEvidence` → `interaction_evidence`
- `reviewedDevelopmentSupport` → `reviewed_development_support`
- `developmentContextQuery` → `development_context_query`
- `developmentDimension` → `development_dimension`
- `developmentScenes` → `development_scenes`
- `promptReadyDevelopment` → `prompt_ready_development`

私有常量 `dimensions` 的成员与顺序逐字保留；`Number.isSafeInteger` 由 `_is_safe_integer` 复刻；
`Array.prototype.at(-1)` → `[-1]`；`Math.max(...ids)` → `max(ids)`；正则与上游逐字一致。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Set

from ..types import ScriptEntry, StatePatchDraft, StatePatchProposal
from ..utils import is_array, is_record

__all__ = [
    'interaction_evidence',
    'reviewed_development_support',
    'development_context_query',
    'development_dimension',
    'development_scenes',
    'prompt_ready_development',
]

# Number.MAX_SAFE_INTEGER
_MAX_SAFE_INTEGER = 9007199254740991

# 上游私有 `const dimensions`：canon 不变，新成长提案只允许这套小词表。
_DIMENSIONS: Dict[str, List[str]] = {
    'character': ['traits', 'preferences', 'coping'],
    'perspective': ['values', 'interpretation'],
    'relationship': ['trust', 'closeness', 'boundaries'],
    'world': ['established'],
}


def interaction_evidence(entries: List[ScriptEntry]) -> List[Dict[str, Any]]:
    """上游 interactionEvidence：把观察、解读与接收串成引用链，不另写心理总结。"""
    ordered = sorted(entries, key=_sort_id)
    # slice(-16)：只取最后 16 条 user-message 反馈。
    feedback_entries = [entry for entry in ordered if entry.get('kind') == 'user-message'][-16:]
    evidence: List[Dict[str, Any]] = []
    for feedback in feedback_entries:
        feedback_id = feedback.get('id')
        branch = [entry for entry in ordered if entry.get('participantId') == feedback.get('participantId')]
        # 上游：branch.find(...)?.id ?? Infinity，找不到后续 user-message 时用无穷大当上界。
        next_feedback_id: Any = math.inf
        for entry in branch:
            if entry.get('id') > feedback_id and entry.get('kind') == 'user-message':
                next_feedback_id = entry.get('id')
                break
        prior_communication = [
            entry for entry in branch
            if entry.get('id') < feedback_id and entry.get('kind') == 'character-message'
        ]
        evidence.append({
            'participantId': feedback.get('participantId'),
            'feedbackEntryId': feedback_id,
            # 上游是在对象里显式写 priorCommunicationEntryId: undefined，这里保留 key 并置 None。
            'priorCommunicationEntryId': prior_communication[-1].get('id') if prior_communication else None,
            'interpretationEntryIds': [
                entry.get('id') for entry in branch
                if feedback_id < entry.get('id') < next_feedback_id and entry.get('kind') == 'script'
            ],
            'responseEntryIds': [
                entry.get('id') for entry in branch
                if feedback_id < entry.get('id') < next_feedback_id and entry.get('kind') == 'character-message'
            ],
        })
    return evidence


def reviewed_development_support(draft: StatePatchDraft, entries: List[ScriptEntry], participant_id: str) -> bool:
    """上游 reviewedDevelopmentSupport：只有被引用的真实反馈/回应才支持关系成长。"""
    review = draft.get('interactionReview')
    if not is_record(review):
        return False
    if review.get('outcome') != 'supported':
        return False
    feedback_entry_ids = review.get('feedbackEntryIds')
    response_entry_ids = review.get('responseEntryIds')
    if not is_array(feedback_entry_ids) or not is_array(response_entry_ids):
        return False
    cited = set(draft.get('sourceEntryIds') or [])

    def valid(entry_id: Any, kind: str) -> bool:
        if entry_id not in cited:
            return False
        return any(
            entry.get('id') == entry_id
            and entry.get('kind') == kind
            and entry.get('participantId') == participant_id
            for entry in entries
        )

    return (
        len(feedback_entry_ids) > 0
        and len(response_entry_ids) > 0
        and all(valid(entry_id, 'user-message') for entry_id in feedback_entry_ids)
        and all(valid(entry_id, 'character-message') for entry_id in response_entry_ids)
        # 上游 Math.max(...review.feedbackEntryIds)：回应必须晚于最后一条被引用的反馈。
        and any(entry_id > max(feedback_entry_ids) for entry_id in response_entry_ids)
    )


def development_context_query(user_message: Optional[str], due_summaries: List[str], visible_entries: List[ScriptEntry]) -> str:
    """上游 developmentContextQuery：安静回合也有生活上下文，只用可见原文做相关性查询。"""
    if isinstance(user_message, str) and user_message.strip():
        return user_message.strip()
    last_script: Optional[ScriptEntry] = None
    for entry in visible_entries:
        if entry.get('kind') == 'script':
            last_script = entry
    # 上游 lastScript?.content.slice(-800) ?? ''
    tail = ''
    if last_script is not None and isinstance(last_script.get('content'), str):
        tail = last_script['content'][-800:]
    return '\n'.join(part for part in [*due_summaries, tail] if part)[:1200]


def development_dimension(target: str, path: str) -> Optional[str]:
    """上游 developmentDimension：先剥掉允许的前缀，再对照目标的维度小词表。"""
    # 上游 replace(/^(development|character|perspective|relationship|world)\./, '')：只替换首个匹配。
    normalized = re.sub(r'^(development|character|perspective|relationship|world)\.', '', path.strip(), count=1)
    values = _DIMENSIONS.get(target)
    return normalized if values is not None and normalized in values else None


def development_scenes(entries: List[ScriptEntry]) -> int:
    """上游 developmentScenes：一个完成场景只计一次，与篇幅、回合数无关。"""
    checkpoints: List[Dict[str, Any]] = []
    for entry in entries:
        metadata = entry.get('metadata')
        checkpoint = metadata.get('sceneCheckpoint') if is_record(metadata) else None
        if is_record(checkpoint):
            checkpoints.append(checkpoint)

    scenes: Set[str] = set()
    frame_scenes: Dict[str, str] = {}
    for entry in entries:
        checkpoint = _matching_checkpoint(checkpoints, entry.get('id'))
        metadata = entry.get('metadata')
        frame_id = metadata.get('frameId') if is_record(metadata) else None
        # 上游 typeof entry.metadata?.frameId === 'string'（空串也算 string）。
        if checkpoint is not None and isinstance(frame_id, str):
            frame_scenes[frame_id] = _scene_key(checkpoint)

    for entry in entries:
        if entry.get('kind') != 'script':
            continue
        checkpoint = _matching_checkpoint(checkpoints, entry.get('id'))
        metadata = entry.get('metadata')
        frame = metadata.get('frameId') if is_record(metadata) else None
        if checkpoint is not None:
            scenes.add(_scene_key(checkpoint))
        elif isinstance(frame, str) and frame:
            # 上游 frameScenes.get(frame) ?? frame
            scenes.add(frame_scenes[frame] if frame in frame_scenes else frame)
    return len(scenes)


def prompt_ready_development(candidate: StatePatchProposal, entries: List[ScriptEntry]) -> bool:
    """上游 promptReadyDevelopment：applied 直接放行，否则要跨过场景边界（>=2 个场景）。"""
    if candidate.get('status') == 'applied':
        return True
    sources = set(candidate.get('sourceEntryIds') or [])
    return development_scenes([entry for entry in entries if entry.get('id') in sources]) >= 2


def _matching_checkpoint(checkpoints: List[Dict[str, Any]], entry_id: Any) -> Optional[Dict[str, Any]]:
    """上游 checkpoints.find(item => Number.isSafeInteger(item.sceneId) && id 落在 [first, last])。"""
    if not _is_number(entry_id):
        return None
    for item in checkpoints:
        if not _is_safe_integer(item.get('sceneId')):
            continue
        first_entry_id = item.get('firstEntryId')
        last_entry_id = item.get('lastEntryId')
        if _is_number(first_entry_id) and _is_number(last_entry_id) and first_entry_id <= entry_id <= last_entry_id:
            return item
    return None


def _scene_key(checkpoint: Dict[str, Any]) -> str:
    """上游模板串 `scene:${checkpoint.sceneId}`；sceneId 已通过 Number.isSafeInteger。"""
    return 'scene:' + str(int(checkpoint.get('sceneId')))


def _sort_id(entry: Dict[str, Any]) -> Any:
    """上游 sort((a, b) => a.id - b.id)：id 缺失时按 0 处理，避免 Python 混类型排序报错。"""
    entry_id = entry.get('id')
    return entry_id if _is_number(entry_id) else 0


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _is_safe_integer(value: Any) -> bool:
    """上游 Number.isSafeInteger：排除 bool 与非整数值，范围 ±(2**53-1)。"""
    if not _is_number(value):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER
