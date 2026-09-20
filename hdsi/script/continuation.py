# -*- coding: utf-8 -*-
"""续写书签与字面复用观测，对应上游 `.hdsi_reference/src/script/continuation.ts`（1.0.1-beta6-rebuild，41 行）。

书签是「指向可见原文的指针」，不是第二份摘要，也不创作场景；通信引用只记录发生过的事，
绝不把沉默推断成「欠了一次未回复」。`proseReuseObservation` 只是诊断信号，
长字面复用是需要人看的证据，不是文学拒绝规则；相同的短问句永不视为故障。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..time_utils import parse_time
from ..types import ScriptEntry

__all__ = ['continuation_bookmark', 'prose_reuse_observation']

# 上游 recentCommunications 的 kind 白名单，顺序逐字保留。
_COMMUNICATION_KINDS = (
    'user-message',
    'character-message',
    'character-group-message',
    'character-platform-action',
)


def _to_iso_string(value: Any) -> Optional[str]:
    """`Date.prototype.toISOString()`：UTC、固定 3 位毫秒、Z 结尾。

    上游书签里的 `from.toISOString()` / `entry.occurredAt.toISOString()` 都是这个格式，
    它随 prompt payload 一起下发，所以不能换成 `hdsi/time_utils.iso()`
    （后者微秒为 0 时没有小数段、非 0 时保留 6 位），与 hdsi/script/scene_frame.py 的约定一致。
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


def _occurred_at(entry: Any) -> Optional[datetime]:
    """上游 `entry.occurredAt`；本移植的时间字段读出来是 datetime，也兼容 ISO 字符串。"""
    return parse_time(entry.get('occurredAt')) if isinstance(entry, dict) else None


def _occurred_at_or_before(entry: Any, moment: datetime) -> bool:
    """JS `entry.occurredAt <= moment`：缺失/非法时间在 JS 里恒为 false。"""
    occurred_at = _occurred_at(entry)
    return occurred_at is not None and occurred_at <= moment


def _occurred_after(entry: Any, moment: datetime) -> bool:
    """JS `entry.occurredAt > moment`：缺失/非法时间在 JS 里恒为 false。"""
    occurred_at = _occurred_at(entry)
    return occurred_at is not None and occurred_at > moment


def _content_length(entry: Any) -> int:
    content = entry.get('content') if isinstance(entry, dict) else None
    return len(content) if isinstance(content, str) else 0


def continuation_bookmark(entries: List[ScriptEntry], from_: datetime, now: datetime) -> Dict[str, Any]:
    """上游 continuationBookmark（上游形参名叫 `from`，Python 保留字改写为 from_）。"""
    visible = [entry for entry in entries if _occurred_at_or_before(entry, now)]
    last_script: Optional[ScriptEntry] = None
    for entry in visible:
        # 上游 `visible.filter(...).at(-1)`：保留最后一个满足条件的条目。
        if entry.get('kind') == 'script' and _occurred_at_or_before(entry, from_):
            last_script = entry

    def reference(entry: ScriptEntry) -> Dict[str, Any]:
        return {
            'entryId': entry.get('id'),
            'kind': entry.get('kind'),
            'participantId': entry.get('participantId'),
            'occurredAt': _to_iso_string(_occurred_at(entry)),
        }

    bookmark: Dict[str, Any] = {'establishedThrough': _to_iso_string(from_)}
    if last_script is not None:
        bookmark['lastScript'] = reference(last_script)
    bookmark['writingStart'] = 'after-last-completed-passage'
    if last_script is not None:
        bookmark['originalEndpoint'] = {
            'entryId': last_script.get('id'),
            'characterOffset': _content_length(last_script),
        }
    # 新批次不是最近对话的第二份拷贝。原文引用让短追问仍能保留它真正回应的异议 / 问题。
    bookmark['newEventEntryIds'] = [
        entry.get('id') for entry in visible
        if entry.get('kind') == 'user-message' and _occurred_after(entry, from_)
    ]
    # 这些指针只在 recentScript 范围内解析；不再拷贝一份对话。
    bookmark['recentCommunications'] = [
        reference(entry) for entry in visible
        if (entry.get('kind') != 'user-message' or _occurred_at_or_before(entry, from_))
        and entry.get('kind') in _COMMUNICATION_KINDS
    ][-4:]
    return bookmark


def prose_reuse_observation(previous: str, next: str, width: int = 40) -> float:
    """上游 proseReuseObservation：长字面复用覆盖率；任一侧短于 width 时返回 0。"""
    if len(previous) < width or len(next) < width:
        return 0
    spans = set()
    for index in range(len(previous) - width + 1):
        spans.add(previous[index:index + width])
    covered = 0
    end = 0
    for index in range(len(next) - width + 1):
        if next[index:index + width] not in spans:
            continue
        covered += max(0, index + width - max(end, index))
        end = index + width
    return covered / len(next)
