# -*- coding: utf-8 -*-
"""消息投递装配，对应上游 .hdsi_reference/src/delivery.ts（1.0.1-beta6-rebuild）。

每条气泡都保留 scriptEvent 身份（commitId/eventId/bubbleIndex/bubbleCount），
投递回执恢复时严格校验事件种类与安全整数。所有 JSON key 保持 camelCase。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .types import OutgoingMessageDraft

# 依赖主线移植的 hdsi/script/contract.py：该模块导出
# message_event_reference(event, bubble_index=0, script_entry_id=None) -> dict 与相关常量。
# 该文件暂时可能不存在；py_compile 只编译不执行 import，所以仍可通过编译，运行时请先补齐主线模块。
from .script.contract import message_event_reference

__all__ = [
    'attach_message_event',
    'prepare_outgoing_delivery',
    'script_event_payload',
    'restore_message_event',
    'delivery_entry_metadata',
]

_MESSAGE_EVENT_KINDS = ('outgoing-message', 'group-message')
_MAX_SAFE_INTEGER = 2 ** 53 - 1


def _is_record(value: Any) -> bool:
    """TS 私有 isRecord(value)：对象且非数组。"""
    return isinstance(value, dict)


def _is_safe_integer(value: Any) -> bool:
    """TS: typeof value === 'number' && Number.isSafeInteger(value)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        if not math.isfinite(value):
            return False
    except (TypeError, OverflowError):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= _MAX_SAFE_INTEGER


def attach_message_event(
    message: OutgoingMessageDraft,
    event: Optional[Dict[str, Any]],
    script_entry_id: Optional[int] = None,
) -> OutgoingMessageDraft:
    """上游 attachMessageEvent：把 ScriptEventDraft 转成消息身份引用并附加。"""
    script_event = message_event_reference(event, 0, script_entry_id) if _is_record(event) else None
    if script_event is not None:
        return {**message, 'scriptEvent': script_event}
    return message


def prepare_outgoing_delivery(
    message: OutgoingMessageDraft,
    bubbles: List[str],
) -> Optional[OutgoingMessageDraft]:
    """上游 prepareOutgoingDelivery：拆首条/后续气泡并刷新 bubbleIndex/bubbleCount/fullContent。"""
    if not bubbles:
        return None
    first = bubbles[0]
    later = list(bubbles[1:])
    if not first:
        return None
    script_event = message.get('scriptEvent')
    if _is_record(script_event):
        script_event = {
            **script_event,
            'bubbleIndex': 0,
            'bubbleCount': len(bubbles),
            'fullContent': script_event.get('fullContent') or message.get('content'),
        }
    else:
        script_event = None
    result: Dict[str, Any] = {**message, 'content': first}
    if later:
        result['laterSegments'] = later
    if script_event is not None:
        result['scriptEvent'] = script_event
    return result


def script_event_payload(message: OutgoingMessageDraft, bubble_index: int = 0) -> Dict[str, Any]:
    """上游 scriptEventPayload：给单条气泡投递载荷附上指定 bubbleIndex。"""
    script_event = message.get('scriptEvent')
    if not _is_record(script_event):
        return {}
    return {'scriptEvent': {**script_event, 'bubbleIndex': bubble_index}}


def restore_message_event(value: Any, content: str) -> Optional[Dict[str, Any]]:
    """上游 restoreMessageEvent：从载荷里严格恢复 ScriptMessageEventReference。"""
    if not _is_record(value):
        return None
    event = value.get('scriptEvent')
    if not _is_record(event):
        return None
    commit_id = event.get('commitId')
    event_id = event.get('eventId')
    if not isinstance(commit_id, str) or not isinstance(event_id, str):
        return None
    event_kind = event.get('eventKind')
    if event_kind not in _MESSAGE_EVENT_KINDS:
        return None
    raw_caused = event.get('causedByEventIds')
    caused_by = [item for item in raw_caused if isinstance(item, str)] if isinstance(raw_caused, (list, tuple)) else []

    result: Dict[str, Any] = {'commitId': commit_id, 'eventId': event_id}
    script_entry_id = event.get('scriptEntryId')
    if _is_safe_integer(script_entry_id):
        result['scriptEntryId'] = script_entry_id
    result['eventKind'] = event_kind
    result['causedByEventIds'] = caused_by
    full_content = event.get('fullContent')
    result['fullContent'] = full_content if isinstance(full_content, str) else content
    bubble_index = event.get('bubbleIndex')
    result['bubbleIndex'] = bubble_index if _is_safe_integer(bubble_index) else 0
    bubble_count = event.get('bubbleCount')
    result['bubbleCount'] = bubble_count if _is_safe_integer(bubble_count) else 1
    return result


def delivery_entry_metadata(
    message: OutgoingMessageDraft,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """上游 deliveryEntryMetadata：投递行元数据（scriptEvent 平铺后 extra 覆盖）。"""
    extra = extra if isinstance(extra, dict) else {}
    result: Dict[str, Any] = {
        'visible': True,
        'interaction': message.get('interaction'),
    }
    script_event = message.get('scriptEvent')
    if _is_record(script_event):
        merged = {**script_event}
        bubble_index = merged.get('bubbleIndex')
        merged['bubbleIndex'] = 0 if bubble_index is None else bubble_index
        result.update(merged)
    result.update(extra)
    return result
