# -*- coding: utf-8 -*-
"""Script 事件契约，对应上游 src/script/contract.ts（1.0.1-beta6-rebuild）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

ScriptEventKind = str  # narrative / message-perceived / outgoing-message / group-message / ...
ScriptDeliveryMode = str  # immediate | delayed


def is_outgoing_script_event(event: Any) -> bool:
    """上游 isOutgoingScriptEvent 的类型谓词，这里是运行时判定。"""
    if not isinstance(event, dict):
        return False
    return (
        event.get('kind') in ('outgoing-message', 'group-message')
        and isinstance(event.get('content'), str)
        and isinstance(event.get('bubbles'), list)
        and event.get('deliveryMode') in ('immediate', 'delayed')
    )


def message_event_reference(event: Dict[str, Any], bubble_index: int = 0, script_entry_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if not is_outgoing_script_event(event):
        return None
    reference: Dict[str, Any] = {
        'commitId': event.get('commitId'),
        'eventId': event.get('eventId'),
        'eventKind': event.get('kind'),
        'causedByEventIds': list(event.get('causedByEventIds') or []),
        'fullContent': event.get('content'),
        'bubbleIndex': bubble_index,
        'bubbleCount': len(event.get('bubbles') or []),
    }
    if isinstance(script_entry_id, int) and not isinstance(script_entry_id, bool):
        reference['scriptEntryId'] = script_entry_id
    return reference


class ScriptActionBinding(TypedDict, total=False):
    status: str  # 'bound' | 'unbound' | 'future'
    spans: List[Dict[str, int]]


class ScriptEventDraft(TypedDict, total=False):
    eventId: str
    commitId: str
    kind: str
    actor: str
    occurredAt: str
    causedByEventIds: List[str]
    participantId: str
    content: str
    bubbles: List[str]
    deliveryMode: str
    scriptBinding: ScriptActionBinding
    metadata: Dict[str, Any]


class ScriptCommitDraft(TypedDict, total=False):
    commitId: str
    storyId: str
    participantId: str
    phase: str
    window: Dict[str, str]
    prose: str
    events: List[ScriptEventDraft]
    sceneDelta: Dict[str, Any]
    sourceFormat: str


class ScriptMessageEventReference(TypedDict, total=False):
    commitId: str
    eventId: str
    scriptEntryId: int
    eventKind: str
    causedByEventIds: List[str]
    fullContent: str
    bubbleIndex: int
    bubbleCount: int
