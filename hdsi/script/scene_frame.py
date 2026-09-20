# -*- coding: utf-8 -*-
"""Scene 帧投影，对应上游 `.hdsi_reference/src/script/scene-frame.ts`（1.0.1-beta6-rebuild，222 行）。

一比一移植；上游本文件没有 async/await 与全局状态，这里同样全部是同步纯函数。关键语义：
- `stableId`：sha256(`parts.join('\\u001f')`) 的 hex 前 18 位，前缀 `frame:` / `scope:` / `burst:` / `topic:`。
- `Date.prototype.toISOString()` 是毫秒精度（固定 3 位小数 + `Z`），它既写进 `updatedAt` / `startedAt`，
  也参与 sha256 摘要，所以用 `_to_iso_string`，而**不能**换成 `hdsi/time_utils.iso()`
  （后者保留微秒、微秒为 0 时没有小数段，会改变稳定 id）。
- `unionIds`：去重 → 数字升序 → 取末 80 个；`unionStrings`：去重（保留首次出现顺序）→ 取末 limit 个。
- 上游用 sha256（不是 sha1），本文件也没有 randomUUID / 随机分量：同一输入恒等同一 id。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from ..time_utils import parse_time
from ..types import (
    AgencyWindowState,
    DialogueBurstState,
    InterludeScene,
    SceneFrame,
    SceneFrameField,
    ScenePresenceState,
    ScriptEntry,
    StoryState,
    WorkingDetail,
)
from .life_handoff import LifeHandoff, entry_life_handoff

# JS Number.isSafeInteger 的边界（2^53 - 1）。
_MAX_SAFE_INTEGER = 9007199254740991
# cloneFrame 明确丢弃的三个 beta1 遗留字段（值不复制，sources 也一并过滤）。
_DROPPED_FRAME_SOURCE_FIELDS = ('postureOrMotion', 'affectiveBaseline', 'narrativeFocus')
# 上游 /^(那|这|所以|然后|但是|可是|怎么|为什么|你|我|刚才|昨天|前面|不是|对啊|嗯|啊)/u
_CONVERSATIONAL_FOLLOW_UP_RE = re.compile(
    r'^(?:那|这|所以|然后|但是|可是|怎么|为什么|你|我|刚才|昨天|前面|不是|对啊|嗯|啊)'
)


class SceneFrameProjectionInput(TypedDict, total=False):
    """上游 interface SceneFrameProjectionInput（运行期就是 dict）。"""

    storyId: str
    now: datetime
    scene: Optional[InterludeScene]
    state: StoryState
    recentEntries: List[ScriptEntry]
    workingDetails: List[WorkingDetail]
    scenePresence: List[ScenePresenceState]
    agencyWindow: Optional[AgencyWindowState]


class DialogueBurstSignal(TypedDict, total=False):
    """上游 interface DialogueBurstSignal（运行期就是 dict）。"""

    scope: str
    topicText: str
    boundary: bool


def project_scene_frame(input: SceneFrameProjectionInput) -> SceneFrame:
    """上游 projectSceneFrame：确定性地把既有证据投影成只读场景骨架。"""
    # 活跃场景查询是权威来源；持久化的 activeSceneId 可能短暂指向刚关闭的场景。
    scene = input.get('scene') if isinstance(input.get('scene'), dict) else None
    scene_id = scene.get('id') if scene else None
    frame_id = _stable_id(
        'frame', _join_part(input.get('storyId')), 'root' if scene_id is None else _join_part(scene_id)
    )
    # 每回合都从事实来源重新投影。持久化的 beta1 frame 可能残留散文尾巴，
    # 克隆它们会让那条反馈回路继续存在。
    story_state = input.get('state') if isinstance(input.get('state'), dict) else {}
    scene_frame_state = story_state.get('sceneFrame') if isinstance(story_state.get('sceneFrame'), dict) else {}
    local_boundary_entry_id = scene_frame_state.get('localBoundaryEntryId')

    values: SceneFrame = {'id': frame_id}
    if scene_id:
        values['sceneId'] = scene_id
    values['presentPeople'] = []
    values['openMotions'] = []
    values['openTopics'] = []
    if local_boundary_entry_id:
        values['localBoundaryEntryId'] = local_boundary_entry_id
    values['sourceEntryIds'] = []
    values['sources'] = {}
    values['updatedAt'] = _to_iso_string(input.get('now'))

    presence = input.get('scenePresence')
    if presence is None:
        presence = story_state.get('scenePresence')
    if presence is None:
        presence = []
    recent_entries = input.get('recentEntries')
    visible = None
    if recent_entries is not None:
        visible = set()
        for entry in recent_entries:
            visible.add(entry.get('id') if isinstance(entry, dict) else None)

    def grounded(ids: Any) -> bool:
        items = ids if isinstance(ids, (list, tuple)) else []
        if not items:
            return False
        if visible is None:
            return True
        return any(item in visible for item in items)

    present = []
    for item in presence if isinstance(presence, list) else []:
        if not isinstance(item, dict):
            continue
        source_ids = item.get('sourceEntryIds') if isinstance(item.get('sourceEntryIds'), list) else []
        if item.get('status') != 'present' or not grounded(source_ids):
            continue
        boundary = values.get('localBoundaryEntryId')
        if max(source_ids) < (boundary if boundary is not None else 0):
            continue
        present.append(item)
    if present:
        _assign(
            values,
            'presentPeople',
            [item.get('name') for item in present],
            [entry_id for item in present for entry_id in (item.get('sourceEntryIds') or [])],
        )

    details = input.get('workingDetails')
    if details is None:
        details = story_state.get('workingDetails')
    if details is None:
        details = []
    grounded_details = [
        item for item in details if isinstance(item, dict) and grounded(item.get('sourceEntryIds') or [])
    ]
    if grounded_details:
        _assign(
            values,
            'openMotions',
            [f'{item.get("label")}：{item.get("value")}' for item in grounded_details],
            [entry_id for item in grounded_details for entry_id in (item.get('sourceEntryIds') or [])],
        )

    agency = input.get('agencyWindow')
    if agency is None:
        agency = story_state.get('agencyWindow')
    if isinstance(agency, dict) and (agency.get('sourceEntryIds') or []):
        _assign(values, 'deviceAccess', agency.get('deviceAccess'), agency.get('sourceEntryIds'))
        _assign(values, 'privacy', agency.get('privacy'), agency.get('sourceEntryIds'))
        _assign(values, 'attention', agency.get('activityLoad'), agency.get('sourceEntryIds'))

    # 每条 handoff 都指向原始记录。新的地点/结构性转变替换本地占用，
    # 而不是长久的关系或剧情弧线。
    entries = input.get('recentEntries')
    if entries is None:
        entries = []
    for entry in sorted([item for item in entries if isinstance(item, dict)], key=_entry_sort_key):
        handoff = entry_life_handoff(entry)
        if not handoff:
            continue
        _apply_life_handoff(values, handoff, entry.get('id'))

    values['sourceEntryIds'] = _union_ids([
        entry_id
        for ids in (values.get('sources') or {}).values()
        for entry_id in (ids or [])
    ])
    values['updatedAt'] = _to_iso_string(input.get('now'))
    return values


def resolve_dialogue_burst(
    frame: SceneFrame,
    previous: Optional[DialogueBurstState],
    started_at: datetime,
    signal: Optional[DialogueBurstSignal] = None,
) -> DialogueBurstState:
    """上游 resolveDialogueBurst。"""
    if signal is None:
        signal = {}
    raw_scope = signal.get('scope')
    scope_key = _stable_id('scope', raw_scope.strip()) if isinstance(raw_scope, str) and raw_scope.strip() else None
    topic_text = signal.get('topicText') or ''
    next_topic_keys = _dialogue_topic_keys(topic_text)
    previous_scope_key = previous.get('scopeKey') if isinstance(previous, dict) else None
    same_scope = not scope_key or not previous_scope_key or scope_key == previous_scope_key
    previous_topic_keys = (previous.get('topicKeys') or []) if isinstance(previous, dict) else []
    if not next_topic_keys or not previous_topic_keys:
        same_topic = True
    else:
        same_topic = _topic_keys_overlap(previous_topic_keys, next_topic_keys) or _is_conversational_follow_up(topic_text)
    if not signal.get('boundary') and isinstance(previous, dict) and previous.get('frameId') == frame.get('id') \
            and same_scope and same_topic:
        return {
            **previous,
            'sourceEntryIds': _union_ids(
                list(previous.get('sourceEntryIds') or []) + list(frame.get('sourceEntryIds') or [])
            ),
            **({'scopeKey': scope_key} if scope_key else {}),
            **({'topicKeys': _union_strings(list(previous.get('topicKeys') or []) + next_topic_keys, 12)}
               if next_topic_keys else {}),
        }
    burst: DialogueBurstState = {
        'id': _stable_id('burst', _join_part(frame.get('id')), _join_part(_to_iso_string(started_at))),
        'frameId': frame.get('id'),
        'startedAt': _to_iso_string(started_at),
        'sourceEntryIds': list(frame.get('sourceEntryIds') or []),
    }
    if scope_key:
        burst['scopeKey'] = scope_key
    if next_topic_keys:
        burst['topicKeys'] = next_topic_keys
    return burst


def advance_scene_frame(
    frame: SceneFrame,
    burst: DialogueBurstState,
    commit: Dict[str, Any],
    source_entry_id: int,
    now: datetime,
    handoff: Optional[LifeHandoff] = None,
) -> Dict[str, Any]:
    """上游 advanceSceneFrame（commit: ScriptCommitDraft，运行期即 dict）。"""
    # Script 散文是不可变的叙事证据，永远不会被投影回 frame 字段，
    # 即使面对全局 automatic-life 提交也是如此。
    next_frame = _clone_frame(frame, now)
    if handoff:
        _apply_life_handoff(next_frame, handoff, source_entry_id)
    next_frame['sourceEntryIds'] = _union_ids([
        entry_id
        for ids in (next_frame.get('sources') or {}).values()
        for entry_id in (ids or [])
    ])
    events = commit.get('events') or []
    next_burst: DialogueBurstState = {**burst}
    next_burst['sourceEntryIds'] = _union_ids(list(burst.get('sourceEntryIds') or []) + [source_entry_id])
    # 上游写的是 `lastEventId: commit.events.at(-1)?.eventId`；值为 undefined 时
    # JSON 序列化会丢 key，这里等价地删除旧值，避免留下过期事件 id。
    last_event_id = None
    if events and isinstance(events[-1], dict):
        last_event_id = events[-1].get('eventId')
    if last_event_id is not None:
        next_burst['lastEventId'] = last_event_id
    else:
        next_burst.pop('lastEventId', None)
    return {'frame': next_frame, 'burst': next_burst}


def scene_frame_provenance_errors(frame: SceneFrame) -> List[str]:
    """上游 sceneFrameProvenanceErrors：有值却没有来源 entry 的字段。"""
    errors: List[str] = []
    fields: List[SceneFrameField] = [
        'place', 'presentPeople', 'ongoingActivity', 'postureOrMotion', 'attention', 'deviceAccess',
        'privacy', 'affectiveBaseline', 'openMotions', 'openTopics', 'narrativeFocus',
    ]
    sources = frame.get('sources') if isinstance(frame.get('sources'), dict) else {}
    for field in fields:
        value = frame.get(field)
        populated = len(value) > 0 if isinstance(value, (list, tuple)) \
            else isinstance(value, str) and bool(value.strip())
        if populated and not (sources.get(field) or []):
            errors.append(f'{field} has no source entry')
    return errors


def _assign(frame: SceneFrame, field: str, value: Any, source_ids: Any) -> None:
    """上游私有 assign：来源 id 为空时整次赋值都不发生。"""
    ids = _positive_ids(source_ids)
    if not ids:
        return
    frame[field] = value
    frame['sources'][field] = ids


def _apply_life_handoff(values: SceneFrame, handoff: LifeHandoff, entry_id: int) -> None:
    """上游私有 applyLifeHandoff。"""
    if not isinstance(values.get('sources'), dict):
        values['sources'] = {}
    sources = values['sources']
    boundary = values.get('localBoundaryEntryId')
    if entry_id < (boundary if boundary is not None else 0):
        return
    place = handoff.get('place')
    if handoff.get('transition') or (place and values.get('place') != place.get('value')):
        values['localBoundaryEntryId'] = entry_id
        values['presentPeople'] = []
        sources.pop('presentPeople', None)
        values.pop('ongoingActivity', None)
        sources.pop('ongoingActivity', None)
        values['openMotions'] = []
        sources.pop('openMotions', None)
    if place:
        _assign(values, 'place', place.get('value'), [entry_id])
    activity = handoff.get('activity')
    if activity:
        _assign(values, 'ongoingActivity', activity.get('value'), [entry_id])
    presence = handoff.get('presence')
    if presence:
        _assign(values, 'presentPeople', presence.get('names'), [entry_id])
    resolved_details = handoff.get('resolvedDetails')
    if resolved_details:
        labels = [f'{item.get("label")}：' for item in resolved_details]
        values['openMotions'] = [
            item for item in (values.get('openMotions') or [])
            if not any(item.startswith(label) for label in labels)
        ]
        if not values['openMotions']:
            sources.pop('openMotions', None)


def _clone_frame(frame: SceneFrame, now: datetime) -> SceneFrame:
    """上游私有 cloneFrame：只保留列出的字段，丢弃三个遗留 prose 字段。"""
    sources = frame.get('sources') if isinstance(frame.get('sources'), dict) else {}
    next_frame: SceneFrame = {'id': frame.get('id')}
    if frame.get('sceneId'):
        next_frame['sceneId'] = frame.get('sceneId')
    if frame.get('localBoundaryEntryId'):
        next_frame['localBoundaryEntryId'] = frame.get('localBoundaryEntryId')
    if frame.get('place'):
        next_frame['place'] = frame.get('place')
    if frame.get('ongoingActivity'):
        next_frame['ongoingActivity'] = frame.get('ongoingActivity')
    if frame.get('attention'):
        next_frame['attention'] = frame.get('attention')
    if frame.get('deviceAccess'):
        next_frame['deviceAccess'] = frame.get('deviceAccess')
    if frame.get('privacy'):
        next_frame['privacy'] = frame.get('privacy')
    next_frame['presentPeople'] = list(frame.get('presentPeople') or [])
    next_frame['openMotions'] = list(frame.get('openMotions') or [])
    next_frame['openTopics'] = list(frame.get('openTopics') or [])
    next_frame['sourceEntryIds'] = list(frame.get('sourceEntryIds') or [])
    next_frame['sources'] = {
        key: list(ids or [])
        for key, ids in sources.items()
        if key not in _DROPPED_FRAME_SOURCE_FIELDS
    }
    next_frame['updatedAt'] = _to_iso_string(now)
    return next_frame


def _positive_ids(values: Any) -> List[int]:
    """上游私有 positiveIds：Number.isSafeInteger && > 0，再 unionIds。"""
    result: List[int] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not value.is_integer():
            continue
        number = int(value)
        if 0 < number <= _MAX_SAFE_INTEGER:
            result.append(number)
    return _union_ids(result)


def _union_ids(values: List[int]) -> List[int]:
    """上游私有 unionIds：去重、升序、取末 80。"""
    return sorted(set(values))[-80:]


def _dialogue_topic_keys(text: str) -> List[str]:
    """上游私有 dialogueTopicKeys：先剥标点/符号/空白，再取 CJK 双字组或 3+ 词元。"""
    normalized = _strip_punctuation_symbols_space(text.lower())
    if not normalized:
        return []
    if re.search(r'[\u3400-\u9fff]', normalized):
        raw = [normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))]
    else:
        raw = _letter_number_runs(text.lower())
    return _union_strings([_stable_id('topic', value) for value in raw], 12)


def _topic_keys_overlap(left: List[str], right: List[str]) -> bool:
    """上游私有 topicKeysOverlap。"""
    known = set(left)
    return any(key in known for key in right)


def _is_conversational_follow_up(text: str) -> bool:
    """上游私有 isConversationalFollowUp。"""
    normalized = text.strip()
    if not normalized:
        return True
    return _CONVERSATIONAL_FOLLOW_UP_RE.match(normalized) is not None


def _union_strings(values: List[str], limit: int) -> List[str]:
    """上游私有 unionStrings：按首次出现去重后取末 limit 个（slice(-limit)）。"""
    seen = set()
    unique: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique[-limit:]


def _stable_id(prefix: str, *parts: str) -> str:
    """上游私有 stableId：sha256(parts.join('\\u001f')).hexdigest()[:18]。"""
    digest = hashlib.sha256('\u001f'.join(parts).encode('utf-8')).hexdigest()[:18]
    return f'{prefix}:{digest}'


def _to_iso_string(value: Any) -> Optional[str]:
    """`Date.prototype.toISOString()`：UTC、固定 3 位毫秒、Z 结尾。

    与 `hdsi/time_utils.iso()` 的区别是微秒处理（这里截断到毫秒并补齐 3 位），
    因为该字符串参与 sha256 稳定 id，必须逐字复刻上游。
    """
    parsed = parse_time(value)
    if parsed is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return (
        f'{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}'
        f'T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}'
        f'.{parsed.microsecond // 1000:03d}Z'
    )


def _strip_punctuation_symbols_space(text: str) -> str:
    """上游 /[\\p{P}\\p{S}\\s]+/gu 的等价实现（Unicode 类别 P* / S* + 空白）。"""
    return ''.join(
        char for char in text
        if not (unicodedata.category(char)[0] in ('P', 'S') or char.isspace())
    )


def _letter_number_runs(text: str) -> List[str]:
    """上游 /[\\p{L}\\p{N}]{3,}/gu 的等价实现：3 个及以上的字母/数字连续段。"""
    runs: List[str] = []
    current: List[str] = []
    for char in text:
        if unicodedata.category(char)[0] in ('L', 'N'):
            current.append(char)
        else:
            if len(current) >= 3:
                runs.append(''.join(current))
            current = []
    if len(current) >= 3:
        runs.append(''.join(current))
    return runs


def _entry_sort_key(entry: Dict[str, Any]) -> Any:
    """上游 `[...entries].sort((a, b) => a.id - b.id)`；缺失 id 按 0（JS NaN 比较器视为 0）。"""
    value = entry.get('id')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _join_part(value: Any) -> str:
    """`Array.prototype.join` 对 undefined/null 输出空串。"""
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)
