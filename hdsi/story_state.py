# -*- coding: utf-8 -*-
"""StoryState 编解码/迁移，对应上游 src/story-state.ts（1.0.1-beta6-rebuild）。

额外：normalize_participant_state / participant_relevance 上游内联在 service.ts，
为了让 store.py 解码参与者行时复用，集中放在本文件（语义逐字一致）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from .agency import normalize_agency_window_state
from .alter import normalize_alter_system_state
from .time_utils import iso, parse_time
from .types import (
    AutomaticDeliverySummary, ContinuitySnapshot, DialogueBurstState, ParticipantState,
    SceneFrame, ScenePresenceState, StoryState, WorkingDetail,
)
from .utils import clip, is_record

CURRENT_STORY_STATE_VERSION = 4

KNOWN_STORY_STATE_KEYS = {
    'schemaVersion', 'extensions', 'settingOverlay', 'activeSceneId', 'activeArcId',
    'continuitySnapshot', 'narrativeUpdateCount', 'lastContinuityUpdateAt',
    'continuityDirty', 'automation', 'alterSystem', 'agencyWindow', 'scenePresence',
    'automaticDeliverySummaries', 'workingDetails', 'timelineCarry', 'chatRhythm',
    'sceneFrame', 'dialogueBurst', 'workingDetailResolutions',
}

SCENE_FRAME_FIELDS = [
    'place', 'presentPeople', 'ongoingActivity', 'attention',
    'deviceAccess', 'privacy', 'openMotions', 'openTopics',
]


class StoryStateMigrationInspection(TypedDict, total=False):
    sourceVersion: int
    targetVersion: int
    upgraded: bool
    unknownKeys: List[str]
    perspectiveDefaultAvailable: bool


def inspect_story_state_migration(value: Any, configured_perspective: str = '') -> Dict[str, Any]:
    record = value if is_record(value) else {}
    source_version = _finite_integer(record.get('schemaVersion'))
    source_version = source_version if source_version is not None else 0
    return {
        'sourceVersion': source_version,
        'targetVersion': CURRENT_STORY_STATE_VERSION,
        'upgraded': source_version != CURRENT_STORY_STATE_VERSION,
        'unknownKeys': sorted(key for key in record.keys() if key not in KNOWN_STORY_STATE_KEYS),
        'perspectiveDefaultAvailable': bool((configured_perspective or '').strip()),
    }


def decode_story_state(value: Any) -> StoryState:
    return upgrade_story_state(value)


def upgrade_story_state(value: Any) -> StoryState:
    record = value if is_record(value) else {}
    overlay = record.get('settingOverlay') if is_record(record.get('settingOverlay')) else {}
    automation = record.get('automation') if is_record(record.get('automation')) else {}
    existing_extensions = record.get('extensions') if is_record(record.get('extensions')) else {}
    unknown_extensions = {key: item for key, item in record.items() if key not in KNOWN_STORY_STATE_KEYS}
    extensions = {**existing_extensions, **unknown_extensions}
    continuity = normalize_continuity_snapshot(record.get('continuitySnapshot'))

    character_traits = [
        item for item in (overlay.get('characterTraits') if isinstance(overlay.get('characterTraits'), list) else [])
        if isinstance(item, str)
    ]
    state: StoryState = {
        'schemaVersion': CURRENT_STORY_STATE_VERSION,
        'settingOverlay': {
            'characterProfile': _text_or_undefined(overlay.get('characterProfile')),
            'perspective': _clipped_text_or_undefined(overlay.get('perspective'), 1000),
            'relationship': _text_or_undefined(overlay.get('relationship')),
            'world': _text_or_undefined(overlay.get('world')),
            'supportingCast': _text_or_undefined(overlay.get('supportingCast')),
            'location': _text_or_undefined(overlay.get('location')),
            'characterTraits': character_traits,
        },
        'activeSceneId': _finite_number(record.get('activeSceneId')),
        'activeArcId': _finite_number(record.get('activeArcId')),
        'continuitySnapshot': continuity,
        'narrativeUpdateCount': max(0, int(_finite_number(record.get('narrativeUpdateCount')) or 0)),
        'lastContinuityUpdateAt': _text_or_undefined(record.get('lastContinuityUpdateAt')),
        'continuityDirty': record.get('continuityDirty') is True,
        'alterSystem': normalize_alter_system_state(record.get('alterSystem')),
        'agencyWindow': normalize_agency_window_state(record.get('agencyWindow')),
        'scenePresence': normalize_scene_presence_state(record.get('scenePresence')),
        'workingDetails': normalize_working_details(record.get('workingDetails')),
        'workingDetailResolutions': _normalize_working_detail_resolutions(record.get('workingDetailResolutions')),
        'timelineCarry': normalize_timeline_carry(record.get('timelineCarry')),
        'automaticDeliverySummaries': normalize_automatic_delivery_summaries(record.get('automaticDeliverySummaries')),
        'chatRhythm': record.get('chatRhythm') if is_record(record.get('chatRhythm')) else None,
        'sceneFrame': normalize_scene_frame(record.get('sceneFrame')),
        'dialogueBurst': normalize_dialogue_burst(record.get('dialogueBurst')),
        'automation': {
            'quietUntil': _text_or_undefined(automation.get('quietUntil')),
            'nextAdvanceAt': _text_or_undefined(automation.get('nextAdvanceAt')),
            'timelineRetryAt': _text_or_undefined(automation.get('timelineRetryAt')),
            'timelineRetryFrom': _text_or_undefined(automation.get('timelineRetryFrom')),
            'lastAutoAdvanceAt': _text_or_undefined(automation.get('lastAutoAdvanceAt')),
            'lastUserMessageAt': _text_or_undefined(automation.get('lastUserMessageAt')),
            'conversationFollowUpAt': [
                item for item in (automation.get('conversationFollowUpAt') if isinstance(automation.get('conversationFollowUpAt'), list) else [])
                if isinstance(item, str)
            ][:8],
            'conversationFollowUpParticipantId': _clipped_text_or_undefined(automation.get('conversationFollowUpParticipantId'), 255),
        },
    }
    if extensions:
        state['extensions'] = extensions
    else:
        state.pop('extensions', None)
    state['settingOverlay'] = {key: item for key, item in state['settingOverlay'].items() if item is not None or key == 'characterTraits'}
    for key in ('activeSceneId', 'activeArcId', 'continuitySnapshot', 'lastContinuityUpdateAt',
                'alterSystem', 'agencyWindow', 'sceneFrame', 'dialogueBurst', 'chatRhythm'):
        if state.get(key) is None:
            state.pop(key, None)
    return state


def normalize_scene_frame(value: Any) -> Optional[SceneFrame]:
    if not is_record(value) or not isinstance(value.get('id'), str) or not value.get('id').strip():
        return None
    raw_sources = value.get('sources') if is_record(value.get('sources')) else {}
    sources: Dict[str, List[int]] = {}
    for field in SCENE_FRAME_FIELDS:
        ids = _integer_array(raw_sources.get(field), 24)
        if ids:
            sources[field] = ids
    source_entry_ids = list(dict.fromkeys(
        item for ids in sources.values() for item in ids
    ))[:80]

    def grounded_text(field: str, raw: Any, limit: int) -> Optional[str]:
        return _clipped_text_or_undefined(raw, limit) if sources.get(field) else None

    def grounded_list(field: str, raw: Any, count: int, limit: int) -> List[str]:
        return _string_array(raw, count, limit) if sources.get(field) else []

    local_boundary = _finite_integer(value.get('localBoundaryEntryId'))
    scene_id = _finite_integer(value.get('sceneId'))
    frame: SceneFrame = {
        'id': value.get('id').strip()[:120],
        'place': grounded_text('place', value.get('place'), 240),
        'presentPeople': grounded_list('presentPeople', value.get('presentPeople'), 16, 80),
        'ongoingActivity': grounded_text('ongoingActivity', value.get('ongoingActivity'), 320),
        'attention': grounded_text('attention', value.get('attention'), 320),
        'deviceAccess': grounded_text('deviceAccess', value.get('deviceAccess'), 80),
        'privacy': grounded_text('privacy', value.get('privacy'), 80),
        'openMotions': grounded_list('openMotions', value.get('openMotions'), 8, 320),
        'openTopics': grounded_list('openTopics', value.get('openTopics'), 8, 320),
        'sourceEntryIds': source_entry_ids,
        'sources': sources,
        'updatedAt': _valid_iso_string(value.get('updatedAt')) or iso(datetime(1970, 1, 1, tzinfo=timezone.utc)),
    }
    if local_boundary:
        frame['localBoundaryEntryId'] = local_boundary
    if scene_id:
        frame['sceneId'] = scene_id
    return {key: item for key, item in frame.items() if item is not None}


def normalize_dialogue_burst(value: Any) -> Optional[DialogueBurstState]:
    if not is_record(value) or not isinstance(value.get('id'), str) or not isinstance(value.get('frameId'), str):
        return None
    burst_id = value.get('id').strip()[:160]
    frame_id = value.get('frameId').strip()[:120]
    started_at = _valid_iso_string(value.get('startedAt'))
    if not burst_id or not frame_id or not started_at:
        return None
    burst: DialogueBurstState = {
        'id': burst_id,
        'frameId': frame_id,
        'startedAt': started_at,
        'sourceEntryIds': _integer_array(value.get('sourceEntryIds'), 80),
        'topicKeys': _string_array(value.get('topicKeys'), 12, 120),
    }
    last_event_id = _clipped_text_or_undefined(value.get('lastEventId'), 180)
    scope_key = _clipped_text_or_undefined(value.get('scopeKey'), 120)
    if last_event_id:
        burst['lastEventId'] = last_event_id
    if scope_key:
        burst['scopeKey'] = scope_key
    return burst


def encode_story_state(value: StoryState) -> StoryState:
    return upgrade_story_state(value)


def normalize_automatic_delivery_summaries(value: Any) -> List[AutomaticDeliverySummary]:
    if not isinstance(value, list):
        return []
    seen = set()
    normalized: List[AutomaticDeliverySummary] = []
    for item in value:
        if not is_record(item):
            continue
        participant_id = _clipped_text_or_undefined(item.get('participantId'), 255) or ''
        summary = _clipped_text_or_undefined(item.get('summary'), 240) or ''
        delivered_at = _valid_iso_string(item.get('deliveredAt')) or ''
        source_entry_id = _finite_integer(item.get('sourceEntryId'))
        key = f'{participant_id}|{source_entry_id if source_entry_id is not None else 0}|{summary}'
        if not participant_id or not summary or not delivered_at or key in seen:
            continue
        seen.add(key)
        entry: AutomaticDeliverySummary = {'participantId': participant_id, 'summary': summary, 'deliveredAt': delivered_at}
        if source_entry_id:
            entry['sourceEntryId'] = source_entry_id
        normalized.append(entry)
    return normalized[-6:]


def normalize_scene_presence_state(value: Any) -> List[ScenePresenceState]:
    if not isinstance(value, list):
        return []
    latest: Dict[str, ScenePresenceState] = {}
    order: List[str] = []
    for item in value:
        if not is_record(item):
            continue
        name = _clipped_text_or_undefined(item.get('name'), 80) or ''
        status = item.get('status') if item.get('status') in ('present', 'off-scene', 'expected') else None
        basis = _clipped_text_or_undefined(item.get('basis'), 300) or ''
        source_entry_ids = _integer_array(item.get('sourceEntryIds'), 8)
        updated_at = _valid_iso_string(item.get('updatedAt')) or ''
        if not name or not status or not basis or not source_entry_ids or not updated_at:
            continue
        if name not in latest:
            order.append(name)
        latest[name] = {
            'name': name, 'status': status, 'basis': basis,
            'sourceEntryIds': source_entry_ids, 'updatedAt': updated_at,
        }
    ordered = [latest[name] for name in order if name in latest]
    return ordered[-8:]


def normalize_working_details(value: Any) -> List[WorkingDetail]:
    if not isinstance(value, list):
        return []
    latest: Dict[str, WorkingDetail] = {}
    order: List[str] = []
    for item in value:
        if not is_record(item):
            continue
        label = _clipped_text_or_undefined(item.get('label'), 80) or ''
        detail_value = _clipped_text_or_undefined(item.get('value'), 300) or ''
        expires_at = _valid_iso_string(item.get('expiresAt'))
        created_at = _valid_iso_string(item.get('createdAt')) or iso(datetime(1970, 1, 1, tzinfo=timezone.utc))
        source_entry_ids = _integer_array(item.get('sourceEntryIds'), 8)
        if not label or not detail_value:
            continue
        if label not in latest:
            order.append(label)
        detail: WorkingDetail = {'label': label, 'value': detail_value, 'createdAt': created_at}
        if isinstance(item.get('participantId'), str):
            detail['participantId'] = item.get('participantId')[:255]
        if expires_at:
            detail['expiresAt'] = expires_at
        if source_entry_ids:
            detail['sourceEntryIds'] = source_entry_ids
        if is_record(item.get('knowledge')):
            detail['knowledge'] = item.get('knowledge')
        latest[label] = detail
    ordered = [latest[label] for label in order if label in latest]
    return ordered[-10:]


def normalize_timeline_carry(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    seen = set()
    result: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:240]
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= 4:
            break
    return result


def normalize_continuity_snapshot(value: Any) -> Optional[ContinuitySnapshot]:
    if not is_record(value):
        return None

    def text(item: Any, limit: int) -> str:
        return item.strip()[:limit] if isinstance(item, str) else ''

    def items(item: Any, limit: int) -> List[str]:
        if not isinstance(item, list):
            return []
        return [entry for entry in (text(raw, limit) for raw in item) if entry][:5]

    current = text(value.get('current'), 500)
    recent = items(value.get('recent'), 300)
    salient = items(value.get('salient'), 400)
    if not current and not recent and not salient:
        return None
    return {'current': current, 'next': [], 'recent': recent, 'salient': salient}


def normalize_participant_state(value: Any) -> ParticipantState:
    """上游 service.ts 内联函数，语义逐字一致。"""
    record = value if is_record(value) else {}

    def note_list(item: Any) -> List[str]:
        if not isinstance(item, list):
            return []
        return [clip(raw, 500) for raw in item if isinstance(raw, str)][:50]

    state: ParticipantState = {
        'openThreads': note_list(record.get('openThreads')),
        'relationshipNotes': note_list(record.get('relationshipNotes')),
        'unreadMessageCount': max(0, int(record.get('unreadMessageCount') if isinstance(record.get('unreadMessageCount'), (int, float)) else 0)),
        'pendingReplyCount': max(0, int(record.get('pendingReplyCount') if isinstance(record.get('pendingReplyCount'), (int, float)) else 0)),
    }
    if isinstance(record.get('relationshipOverlay'), str):
        state['relationshipOverlay'] = clip(record.get('relationshipOverlay'), 4000)
    if isinstance(record.get('lastUserMessageAt'), str):
        state['lastUserMessageAt'] = record.get('lastUserMessageAt')
    if isinstance(record.get('lastCharacterMessageAt'), str):
        state['lastCharacterMessageAt'] = record.get('lastCharacterMessageAt')
    return state


def participant_relevance(participant: Dict[str, Any]) -> float:
    state = normalize_participant_state(participant.get('state'))
    pending = state.get('pendingReplyCount', 0) * 2 + state.get('unreadMessageCount', 0)
    last = parse_time(state.get('lastUserMessageAt'))
    if last is None:
        last = parse_time(participant.get('updatedAt'))
    last_ms = last.timestamp() * 1000 if last else 0
    return pending * 1_000_000_000 + last_ms


def _normalize_working_detail_resolutions(value: Any) -> Dict[str, int]:
    source = value if is_record(value) else {}
    pairs = [
        (label, item) for label, item in source.items()
        if isinstance(label, str) and len(label) <= 80 and isinstance(item, int) and not isinstance(item, bool) and item > 0
    ][-32:]
    return {label: int(item) for label, item in pairs}


def _text_or_undefined(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _clipped_text_or_undefined(value: Any, limit: int) -> Optional[str]:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _valid_iso_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    parsed = parse_time(value)
    if parsed is None:
        return None
    return value


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _finite_integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _integer_array(value: Any, limit: int) -> List[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and not isinstance(item, bool)][:limit]


def _string_array(value: Any, limit: int, item_limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:item_limit]
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result
