# -*- coding: utf-8 -*-
"""投递现实（execution annotation），对应上游 `.hdsi_reference/src/script/delivery-reality.ts`
（1.0.1-beta6-rebuild，24 行）。

一比一移植；同步纯函数。语义要点：
- 只能传入已经按当前关系过滤过的 entries；这是同一份 script 的执行注解，永远不是替换散文。
- `deliveryActions` 为空但有 `commitId` 时，输出 `no-outgoing-action-recorded` 占位。
- 段状态到 outcome 的映射：pending → `not-confirmed`，failed → `delivery-not-confirmed-after-error`，
  其余（delivered / cancelled）原样透出；全部 delivered 的动作整条丢弃。
- 末尾 `slice(-limit)` 在这里就是 Python 的 `result[-limit:]`（`-0` 与 `0` 同义，语义一致）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..types import ScriptEntry

# 上游 ['pending', 'delivered', 'failed', 'cancelled']。
_SEGMENT_STATUSES = ('pending', 'delivered', 'failed', 'cancelled')


def delivery_reality(
    entries: List[ScriptEntry],
    participant_id: Optional[str] = None,
    share_participant_details: bool = False,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """上游 deliveryReality。"""
    result: List[Dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get('kind') != 'script':
            continue
        metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
        actions = metadata.get('deliveryActions')
        if not isinstance(actions, list):
            continue
        if not actions and metadata.get('commitId'):
            result.append({
                'sourceEntryId': entry.get('id'),
                'eventId': '',
                'segments': [],
                'communicationOutcome': 'no-outgoing-action-recorded',
            })
            continue
        for action in actions:
            if not isinstance(action, dict) or action.get('commitId') != metadata.get('commitId') \
                    or not isinstance(action.get('segments'), list):
                continue
            if action.get('participantId') and action.get('participantId') != participant_id \
                    and not share_participant_details:
                continue
            segments = [
                item for item in action.get('segments') or []
                if isinstance(item, dict)
                and isinstance(item.get('content'), str)
                and item.get('status') in _SEGMENT_STATUSES
            ]
            if not any(item.get('status') != 'delivered' for item in segments):
                continue
            result.append({
                'sourceEntryId': entry.get('id'),
                'eventId': action.get('eventId'),
                'segments': [
                    {
                        'kind': item.get('kind'),
                        'content': item.get('content'),
                        'outcome': 'not-confirmed' if item.get('status') == 'pending'
                        else 'delivery-not-confirmed-after-error' if item.get('status') == 'failed'
                        else item.get('status'),
                    }
                    for item in segments
                ],
            })
    return result[-limit:]
