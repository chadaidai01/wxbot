# -*- coding: utf-8 -*-
"""叙事 context 编译器，对应上游 src/script/context-compiler.ts（HDS-Interlude 1.0.1-beta6-rebuild）。

逐 export 移植：
- `CompiledNarrativeContext`
- `compileNarrativeContext` → `compile_narrative_context`
- `compiledContextConflicts` → `compiled_context_conflicts`

语义约定：
- 上游 `undefined` 等价于 `None`；`compactObject` 只丢弃 None 值
  （`Object.entries(...).filter(([, item]) => item !== undefined)`）。
- `payload.setting` 这类对象字面量里显式写出的字段即使是 undefined 也保留 key，
  这里同样保留 key 并置 None（读取方统一用 `.get()`）。
- `Number.isSafeInteger` 由 `_is_safe_integer` 复刻（排除 bool，要求落在 ±(2**53-1)）。
- JS 的 `!==` 在这六对里比较的都是对象/数组引用，用 `is not` 复刻严格不等。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

from ..types import DialogueBurstState, SceneFrame
from ..utils import is_array, is_record

__all__ = [
    'CompiledNarrativeContext',
    'compile_narrative_context',
    'compiled_context_conflicts',
]

# Number.MAX_SAFE_INTEGER
_MAX_SAFE_INTEGER = 9007199254740991


class CompiledNarrativeContext(TypedDict):
    """上游 export interface CompiledNarrativeContext（7 个语义分组全部必填）。"""

    storyIdentity: Dict[str, Any]
    relevantEstablishedEpisodes: Dict[str, Any]
    currentSceneEvidence: Dict[str, Any]
    ongoingThreads: Dict[str, Any]
    availableNearFuture: Dict[str, Any]
    incomingEvent: Dict[str, Any]
    authoringWindow: Dict[str, Any]


def compile_narrative_context(
    payload: Dict[str, Any],
    frame: Optional[SceneFrame],
    _burst: Optional[DialogueBurstState],
) -> CompiledNarrativeContext:
    """上游 compileNarrativeContext：M4 bridge。

    把 beta10 已验证的字段编译成正向续写脚手架：每个 prepared value 只进入一个语义分组，
    不重算、不在面向模型的 payload 里重复。
    """
    return {
        'storyIdentity': {
            'setting': payload.get('setting'),
        },
        'relevantEstablishedEpisodes': _compact_object({
            'recentScript': payload.get('recentScript'),
            'sceneContext': payload.get('sceneContext'),
            'continuitySnapshot': payload.get('continuitySnapshot'),
            'continuitySnapshotAgeMinutes': payload.get('continuitySnapshotAgeMinutes'),
            'durableFacts': payload.get('durableFacts'),
            'memories': payload.get('memories'),
            'overlayEvolution': payload.get('overlayEvolution'),
            # M4.1: a recall hit points back to the original script neighbourhood.
            # The model-facing name makes clear that this is prose evidence, not a
            # second abstract memory summary.
            'recalledScript': payload.get('recalledHistory'),
            'webContext': payload.get('webContext'),
            'recentExchange': payload.get('recentExchange'),
        }),
        'currentSceneEvidence': _project_scene_evidence(
            frame,
            {
                entry.get('id')
                for entry in (payload.get('recentScript') or [])
                if is_record(entry) and _is_safe_integer(entry.get('id'))
            },
        ),
        'ongoingThreads': _compact_object({
            'state': payload.get('state'),
            'currentParticipant': payload.get('currentParticipant'),
            'participants': payload.get('participants'),
            'activeConsequences': payload.get('activeConsequences'),
            'followUpCommitments': payload.get('followUpCommitments'),
            'contactThreads': payload.get('contactThreads'),
            'workingDetails': payload.get('workingDetails'),
            'interruptedOutgoingDrafts': payload.get('interruptedOutgoingDrafts'),
            'supersededDelayedReplies': payload.get('supersededDelayedReplies'),
            'automaticDeliverySummaries': payload.get('automaticDeliverySummaries'),
            'deliveryReality': payload.get('deliveryReality'),
            'developmentTendencies': payload.get('developmentTendencies'),
        }),
        'availableNearFuture': _compact_object({
            'timelinePlan': payload.get('timelinePlan'),
            'timelineCarry': payload.get('timelineCarry'),
            'schedulePreplan': payload.get('schedulePreplan'),
            'dueIntents': payload.get('dueIntents'),
            'upcomingPlans': payload.get('upcomingPlans'),
        }),
        'incomingEvent': _compact_object({
            'event': payload.get('currentEvent'),
            'groupContext': payload.get('groupContext'),
            'chatCapabilities': payload.get('chatCapabilities'),
            'stickerCatalog': payload.get('stickerCatalog'),
        }),
        'authoringWindow': _compact_object({
            'phase': payload.get('phase'),
            'interval': payload.get('interval'),
            'continuation': payload.get('continuation'),
            'liveTimeBoundary': payload.get('liveTimeBoundary'),
            'refreshContinuity': payload.get('refreshContinuity'),
            'outputRecovery': payload.get('outputRecovery'),
            'emotionalOffset': payload.get('emotionalOffset'),
            'agencyWindow': payload.get('agencyWindow'),
        }),
    }


def compiled_context_conflicts(payload: Dict[str, Any], compiled: CompiledNarrativeContext) -> List[str]:
    """上游 compiledContextConflicts：编译产物若重算过某个 legacy 字段就报冲突。"""
    pairs: List[Tuple[Any, Any, str]] = [
        (payload.get('setting'), compiled['storyIdentity'].get('setting'), 'setting'),
        (payload.get('state'), compiled['ongoingThreads'].get('state'), 'state'),
        (payload.get('recentScript'), compiled['relevantEstablishedEpisodes'].get('recentScript'), 'recentScript'),
        (payload.get('currentEvent'), compiled['incomingEvent'].get('event'), 'currentEvent'),
        (payload.get('interval'), compiled['authoringWindow'].get('interval'), 'interval'),
        (payload.get('timelinePlan'), compiled['availableNearFuture'].get('timelinePlan'), 'timelinePlan'),
    ]
    # 上游 `legacy !== next`：这六对都是对象/数组引用，用 `is not` 复刻严格不等。
    return [f'{label} was recomputed' for legacy, next_value, label in pairs if legacy is not next_value]


def _project_scene_evidence(frame: Optional[SceneFrame], recent_entry_ids: Set[int]) -> Dict[str, Any]:
    """上游 projectSceneEvidence：场景状态只作小型、有来源的导航辅助。"""
    if not frame:
        return {}
    # 上游直接读 frame.sourceEntryIds.filter(...)：已由 story-state 归一化保证存在，这里防御性取 []。
    source_entry_ids = frame.get('sourceEntryIds') if is_array(frame.get('sourceEntryIds')) else []
    outside_source_entry_ids = [
        entry_id for entry_id in source_entry_ids
        if entry_id not in recent_entry_ids
    ]
    return _compact_object({
        'sceneId': frame.get('sceneId'),
        'place': _sourced(frame, 'place', recent_entry_ids),
        'presentPeople': _sourced(frame, 'presentPeople', recent_entry_ids),
        'ongoingActivity': _sourced(frame, 'ongoingActivity', recent_entry_ids),
        'attention': _sourced(frame, 'attention', recent_entry_ids),
        'deviceAccess': _sourced(frame, 'deviceAccess', recent_entry_ids),
        'privacy': _sourced(frame, 'privacy', recent_entry_ids),
        'openLoops': _sourced(frame, 'openMotions', recent_entry_ids),
        'sourceEntryIds': outside_source_entry_ids if outside_source_entry_ids else None,
    })


def _sourced(frame: SceneFrame, field: str, recent_entry_ids: Set[int]) -> Optional[Dict[str, Any]]:
    """上游 sourced：只有「字段已填充且来源里含圈外 entry」才暴露 value + sourceEntryIds。"""
    value = frame.get(field)
    sources = frame.get('sources') if is_record(frame.get('sources')) else {}
    source_entry_ids = sources.get(field)
    populated = len(value) > 0 if is_array(value) else isinstance(value, str) and bool(value.strip())
    if not populated or not is_array(source_entry_ids) or len(source_entry_ids) == 0:
        return None
    if not any(entry_id not in recent_entry_ids for entry_id in source_entry_ids):
        return None
    return {'value': value, 'sourceEntryIds': source_entry_ids}


def _compact_object(value: Dict[str, Any]) -> Dict[str, Any]:
    """上游 compactObject：只丢弃 undefined；Python 里 None 表示 undefined。"""
    return {key: item for key, item in value.items() if item is not None}


def _is_safe_integer(value: Any) -> bool:
    """上游 Number.isSafeInteger：排除 bool 与非整数值，范围 ±(2**53-1)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER
