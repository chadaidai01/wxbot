# -*- coding: utf-8 -*-
"""投递台账，对应上游 `.hdsi_reference/src/script/delivery-ledger.ts`（1.0.1-beta6-rebuild，155 行）。

一比一移植；全部同步纯函数。语义要点：
- 台账直接物化在权威 script 行里，本文件只做 metadata（没有第二个发送者、没有 schema 迁移）。
- `updateScriptDeliveryActions` 的 `at.toISOString()` 用毫秒精度（`_to_iso_string`），与上游逐字一致。
- 已投递段是终态：之后的记账失败永不把 delivered 退回未发送；status 与 reason 都没变时不写库。
- `isRecord` 用 `hdsi/utils.py` 的统一实现（上游本文件底部有一份同语义局部函数）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from ..time_utils import parse_time
from ..utils import is_record
from .contract import is_outgoing_script_event

ScriptDeliveryStatus = str  # 'pending' | 'delivered' | 'partial' | 'failed' | 'cancelled'
ScriptDeliverySegmentStatus = str  # 'pending' | 'delivered' | 'failed' | 'cancelled'
ScriptDeliverySegmentKind = str  # 'message' | 'local-media' | 'native-face' | 'message-reaction'

_MAX_SAFE_INTEGER = 9007199254740991


class ScriptDeliverySegment(TypedDict, total=False):
    """上游 interface ScriptDeliverySegment（运行期就是 dict）。"""

    index: int
    kind: ScriptDeliverySegmentKind
    content: str
    status: ScriptDeliverySegmentStatus
    attemptedAt: str
    completedAt: str
    reason: str


class ScriptDeliveryAction(TypedDict, total=False):
    """上游 interface ScriptDeliveryAction（运行期就是 dict）。"""

    commitId: str
    eventId: str
    eventKind: str
    participantId: str
    status: ScriptDeliveryStatus
    segments: List[ScriptDeliverySegment]
    updatedAt: str


class ScriptDeliveryReference(TypedDict, total=False):
    """上游 interface ScriptDeliveryReference（运行期就是 dict）。"""

    commitId: str
    eventId: str
    scriptEntryId: int
    segmentIndex: int


def create_script_delivery_actions(commit: Dict[str, Any]) -> List[ScriptDeliveryAction]:
    """上游 createScriptDeliveryActions（commit: ScriptCommitDraft，运行期即 dict）。"""
    actions: List[ScriptDeliveryAction] = []
    for event in commit.get('events') or []:
        if not isinstance(event, dict):
            continue
        segments = _delivery_segments(event)
        if not segments:
            continue
        action: ScriptDeliveryAction = {
            'commitId': commit.get('commitId'),
            'eventId': event.get('eventId'),
            'eventKind': event.get('kind'),
        }
        if event.get('participantId'):
            action['participantId'] = event.get('participantId')
        action['status'] = 'pending'
        action['segments'] = segments
        action['updatedAt'] = (commit.get('window') or {}).get('to')
        actions.append(action)
    return actions


def update_script_delivery_actions(
    current: Any,
    reference: ScriptDeliveryReference,
    status: ScriptDeliverySegmentStatus,
    at: datetime,
    reason: Optional[str] = None,
) -> Optional[List[ScriptDeliveryAction]]:
    """上游 updateScriptDeliveryActions：没有实际变化时返回 None（不写库）。"""
    if not isinstance(current, list):
        return None
    changed = False
    timestamp = _to_iso_string(at)
    actions: List[Any] = []
    for raw in current:
        if not _is_delivery_action(raw) or raw.get('commitId') != reference.get('commitId') \
                or raw.get('eventId') != reference.get('eventId'):
            actions.append(raw)
            continue
        action_changed = False
        segments: List[ScriptDeliverySegment] = []
        for segment in raw.get('segments') or []:
            if segment.get('index') != reference.get('segmentIndex'):
                segments.append(segment)
                continue
            # 平台已接收的段是终态；之后的记账失败绝不能把已说出口的话变回未发送。
            if segment.get('status') == 'delivered':
                segments.append(segment)
                continue
            if segment.get('status') == status and segment.get('reason') == reason:
                segments.append(segment)
                continue
            changed = True
            action_changed = True
            next_segment: ScriptDeliverySegment = {
                **segment,
                'status': status,
                'attemptedAt': segment.get('attemptedAt') if segment.get('attemptedAt') is not None else timestamp,
            }
            if status != 'pending':
                next_segment['completedAt'] = timestamp
            if reason:
                next_segment['reason'] = reason
            else:
                next_segment.pop('reason', None)
            segments.append(next_segment)
        if not action_changed:
            actions.append(raw)
            continue
        actions.append({
            **raw,
            'segments': segments,
            'status': aggregate_delivery_status(segments),
            'updatedAt': timestamp,
        })
    return actions if changed else None


def delivery_reference(
    event: Optional[Dict[str, Any]],
    script_entry_id: Optional[int],
    segment_index: int = 0,
) -> Optional[ScriptDeliveryReference]:
    """上游 deliveryReference。"""
    if not isinstance(event, dict) or not _is_safe_integer(script_entry_id):
        return None
    return {
        'commitId': event.get('commitId'),
        'eventId': event.get('eventId'),
        # JS 只有一个 number 类型：3.0 通过了 isSafeInteger，JSON 里也序列化成 3。
        'scriptEntryId': int(script_entry_id),
        'segmentIndex': int(segment_index) if _is_safe_integer(segment_index) else segment_index,
    }


def platform_action_reference(
    commit: Optional[Dict[str, Any]],
    script_entry_id: Optional[int],
    kind: ScriptDeliverySegmentKind,
    content: Optional[str] = None,
) -> Optional[ScriptDeliveryReference]:
    """上游 platformActionReference。"""
    event = None
    if isinstance(commit, dict):
        for item in commit.get('events') or []:
            if isinstance(item, dict) and item.get('kind') == 'platform-action':
                event = item
                break
    if event is None or not _is_safe_integer(script_entry_id):
        return None
    segment = None
    for item in _delivery_segments(event):
        if item.get('kind') == kind and (content is None or item.get('content') == content):
            segment = item
            break
    if segment is None:
        return None
    return delivery_reference(event, script_entry_id, segment.get('index'))


def aggregate_delivery_status(segments: List[ScriptDeliverySegment]) -> ScriptDeliveryStatus:
    """上游 aggregateDeliveryStatus。"""
    items = segments or []
    if not items or all(item.get('status') == 'pending' for item in items):
        return 'pending'
    if all(item.get('status') == 'delivered' for item in items):
        return 'delivered'
    if any(item.get('status') == 'delivered' for item in items):
        return 'partial'
    if any(item.get('status') == 'pending' for item in items):
        return 'pending'
    if any(item.get('status') == 'failed' for item in items):
        return 'failed'
    return 'cancelled'


def _delivery_segments(event: Dict[str, Any]) -> List[ScriptDeliverySegment]:
    """上游私有 deliverySegments。"""
    if is_outgoing_script_event(event):
        return [
            {'index': index, 'kind': 'message', 'content': content, 'status': 'pending'}
            for index, content in enumerate(event.get('bubbles') or [])
        ]
    if event.get('kind') != 'platform-action' or not event.get('metadata'):
        return []
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    segments: List[ScriptDeliverySegment] = []
    local_media = metadata.get('localMedia')
    if is_record(local_media) and isinstance(local_media.get('assetId'), str):
        segments.append({
            'index': len(segments), 'kind': 'local-media', 'content': local_media.get('assetId'), 'status': 'pending',
        })
    native_face = metadata.get('nativeFace')
    if is_record(native_face) and isinstance(native_face.get('semantic'), str):
        segments.append({
            'index': len(segments), 'kind': 'native-face', 'content': native_face.get('semantic'), 'status': 'pending',
        })
    reactions = metadata.get('messageReactions') if isinstance(metadata.get('messageReactions'), list) else []
    for reaction in reactions:
        if not is_record(reaction) or not isinstance(reaction.get('messageRef'), str) \
                or not isinstance(reaction.get('reaction'), str):
            continue
        segments.append({
            'index': len(segments),
            'kind': 'message-reaction',
            'content': f'{reaction.get("messageRef")}:{reaction.get("reaction")}',
            'status': 'pending',
        })
    return segments


def _is_delivery_action(value: Any) -> bool:
    """上游私有 isDeliveryAction。"""
    if not is_record(value) or not isinstance(value.get('commitId'), str) \
            or not isinstance(value.get('eventId'), str) or not isinstance(value.get('segments'), list):
        return False
    return all(
        is_record(segment)
        and _is_safe_integer(segment.get('index'))
        and isinstance(segment.get('kind'), str)
        and isinstance(segment.get('content'), str)
        and isinstance(segment.get('status'), str)
        for segment in value.get('segments') or []
    )


def _is_safe_integer(value: Any) -> bool:
    """JS `Number.isSafeInteger`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return -_MAX_SAFE_INTEGER <= int(value) <= _MAX_SAFE_INTEGER


def _to_iso_string(value: Any) -> Optional[str]:
    """`Date.prototype.toISOString()`：UTC、固定 3 位毫秒、Z 结尾。"""
    parsed = parse_time(value)
    if parsed is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return (
        f'{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}'
        f'T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}'
        f'.{parsed.microsecond // 1000:03d}Z'
    )
