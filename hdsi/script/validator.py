# -*- coding: utf-8 -*-
"""脚本提交结构校验，对应上游 `.hdsi_reference/src/script/validator.ts`（1.0.1-beta6-rebuild，35 行）。

只做结构校验；文学风格永不成为拒绝规则。错误文案逐字保留，包括上游模板字符串里
`${event.eventId}` 的 JS 字符串化语义（缺失 → `'undefined'`）。

约定：
- `new Date(x)` → `time_utils.parse_time`；两者都把非法时间视为 Invalid Date / None。
- 上游 `ScriptCommitDraft` interface 定义在 src/script/contract.ts，本移植的 contract.py 只搬了
  类型别名与两个函数、未搬该接口，故运行时数据用 Dict[str, Any] 承接（字段名与上游一致）。
- 上游用 `\u001f` 拼接事件 id 序列，这里用同一个码元。
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from ..time_utils import parse_time
from .contract import is_outgoing_script_event

__all__ = ['ScriptCommitValidation', 'validate_script_commit']

# 上游 `commit.sceneDelta.eventIds.join('\u001f')`。
_UNIT_SEPARATOR = '\u001f'


class ScriptCommitValidation(TypedDict):
    """上游 export interface ScriptCommitValidation。"""

    valid: bool
    errors: List[str]


def _js_text(value: Any) -> str:
    """JS 模板字符串 `${value}` 的字符串化：undefined/null/布尔/整数值浮点的字面量形式。"""
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float) and value != value:
        return 'NaN'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _js_join(values: Any, separator: str) -> str:
    """Array.prototype.join：undefined/null → 空串，其余按 JS 字符串化。"""
    if not isinstance(values, (list, tuple)):
        return ''
    return separator.join('' if item is None else _js_text(item) for item in values)


def validate_script_commit(commit: Dict[str, Any], separator: str = '<sep/>') -> ScriptCommitValidation:
    """上游 validateScriptCommit：结构校验，返回 `{'valid': bool, 'errors': List[str]}`。"""
    errors: List[str] = []
    record = commit if isinstance(commit, dict) else {}
    prose = record.get('prose')
    if not (isinstance(prose, str) and prose.strip()):
        errors.append('script prose is empty')
    scene_delta = record.get('sceneDelta') if isinstance(record.get('sceneDelta'), dict) else {}
    if not scene_delta.get('frameId') or not scene_delta.get('burstId'):
        errors.append('scene delta identity is missing')
    if scene_delta.get('proseAppend') != prose:
        errors.append('scene delta prose differs from commit prose')
    events = record.get('events') if isinstance(record.get('events'), list) else []
    event_ids = [event.get('eventId') if isinstance(event, dict) else None for event in events]
    if _js_join(scene_delta.get('eventIds'), _UNIT_SEPARATOR) != _js_join(event_ids, _UNIT_SEPARATOR):
        errors.append('scene delta event order differs from commit events')
    # 上游 `new Date(commit.window.from)` / `new Date(commit.window.to)`。
    window = record.get('window') if isinstance(record.get('window'), dict) else {}
    window_from = parse_time(window.get('from'))
    window_to = parse_time(window.get('to'))
    if window_from is None or window_to is None or window_from > window_to:
        errors.append('commit window is invalid')
    ids = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get('eventId')
        if event.get('commitId') != record.get('commitId'):
            errors.append(f'event {_js_text(event_id)} belongs to another commit')
        if not event_id or event_id in ids:
            errors.append(f'event id is missing or duplicated: {_js_text(event_id)}')
        ids.add(event_id)
        if is_outgoing_script_event(event):
            if not event.get('participantId') and event.get('kind') != 'group-message':
                errors.append(f'message event {_js_text(event_id)} has no participant')
            bubbles = event.get('bubbles')
            if not isinstance(bubbles, list) or not bubbles:
                errors.append(f'message event {_js_text(event_id)} has empty bubbles')
            elif any(not isinstance(item, str) or not item.strip() for item in bubbles):
                errors.append(f'message event {_js_text(event_id)} has empty bubbles')
            if _js_join(bubbles, separator) != event.get('content'):
                errors.append(f'message event {_js_text(event_id)} cannot reconstruct its content')
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get('eventId')
        parents = event.get('causedByEventIds') if isinstance(event.get('causedByEventIds'), list) else []
        for parent in parents:
            if parent not in ids:
                errors.append(f'event {_js_text(event_id)} has unknown cause {_js_text(parent)}')
    return {'valid': len(errors) == 0, 'errors': errors}
