# -*- coding: utf-8 -*-
"""Script 提交构建，对应上游 `.hdsi_reference/src/script/commit-builder.ts`（1.0.1-beta6-rebuild，248 行）。

一比一移植；同步纯函数。稳定的 id 语义是本文件的重点：
- `stableCommitId` 用 `sha256`，摘要输入是
  `[storyId, phase, from.toISOString(), now.toISOString(), JSON.stringify(semanticPayload)].join('\\u001f')`。
  `toISOString` 是毫秒精度（见 `_to_iso_string`）；`JSON.stringify` 是紧凑分隔符、键序=插入序、
  非 ASCII 原样、整数值浮点省去 `.0`（见 `_stringify_json`）。两者都会改变摘要，
  因此不能用 `time_utils.iso()` / `utils.json_dumps()` 顶替。
- 事件 id 是位置化的 ``f'{commitId}:e{len(events) + 1}'``，同一输入天然稳定。
- 上游没有 `createHash('sha1')`、也没有 `randomUUID`：本文件不含随机分量，不需要 `uuid`。
- `bindImmediateMessageAction` 的下标按 JS `String.prototype.slice` 语义（见 `_js_slice`）。
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, TypedDict

from ..time_utils import parse_time
from ..types import NarrativeDecision, NarrativePhase
from .authored_actions import AuthoredAction, resolve_authored_actions
from .contract import ScriptDeliveryMode, is_outgoing_script_event

# JS `slice` 用的两个端点哨兵（±Infinity 会被钳到字符串两端）。
_JS_MAX_INDEX = sys.maxsize


class ScriptFirstDecisionInput(TypedDict, total=False):
    """上游 interface ScriptFirstDecisionInput（运行期就是 dict）。

    注意：上游字段 `from` 是 Python 保留字，注解里写作 `from_` 仅作占位，
    真实 dict 的 key 仍是 `'from'`（取用一律 `input.get('from')`）。
    """

    storyId: str
    participantId: str
    phase: NarrativePhase
    from_: datetime
    now: datetime
    decision: NarrativeDecision
    messageSeparator: str
    splitReplyMessages: bool
    groupReplyContent: str
    frameId: str
    burstId: str
    startsAfterEventId: str


def decision_to_script_commit(input: ScriptFirstDecisionInput) -> Dict[str, Any]:
    """上游 decisionToScriptCommit：beta10 → V2 的唯一桥接。"""
    separator = input.get('messageSeparator')
    # 上游是 `resolveAuthoredActions(input.decision, false, input.messageSeparator)`；
    # JS 的 undefined 会落到默认参数 '<sep/>'，Python 里要显式还原这个默认值。
    decision = resolve_authored_actions(
        input.get('decision'),
        False,
        '<sep/>' if separator is None else separator,
    )
    input = {**input, 'decision': decision}

    raw_participant_id = input.get('participantId')
    participant_id = raw_participant_id.strip() if isinstance(raw_participant_id, str) else ''
    script = decision.get('script') if isinstance(decision, dict) else None
    prose = script.strip() if isinstance(script, str) else ''
    commit_id = _stable_commit_id(input, participant_id, prose)
    now_iso = _to_iso_string(input.get('now')) or ''
    events: List[Dict[str, Any]] = []

    def add(event: Dict[str, Any]) -> Dict[str, Any]:
        result = {**event, 'commitId': commit_id, 'eventId': f'{commit_id}:e{len(events) + 1}'}
        events.append(result)
        return result

    interaction = decision.get('interaction') if isinstance(decision, dict) else None
    perceived: Optional[Dict[str, Any]] = None
    if input.get('phase') == 'user-message' and isinstance(interaction, dict) \
            and interaction.get('seen') and participant_id:
        perceived = add({
            'kind': 'message-perceived',
            'actor': 'protagonist',
            'occurredAt': now_iso,
            'causedByEventIds': [],
            'participantId': participant_id,
        })
    narrative = add({
        'kind': 'narrative',
        'actor': 'protagonist',
        'occurredAt': now_iso,
        'causedByEventIds': [perceived.get('eventId')] if perceived else [],
        'content': prose,
    })
    reply = interaction.get('reply') if isinstance(interaction, dict) and isinstance(interaction.get('reply'), dict) else None
    if reply and reply.get('content') and reply.get('mode') in ('immediate', 'delayed'):
        caused_by = perceived.get('eventId') if perceived else None
        if caused_by is None:
            caused_by = narrative.get('eventId')
        _add_message_event(add, {
            'participantId': participant_id,
            'content': reply.get('content'),
            'mode': reply.get('mode'),
            'occurredAt': reply.get('sendAt') if reply.get('mode') == 'delayed' and reply.get('sendAt') else now_iso,
            'causedByEventIds': [caused_by],
            'separator': input.get('messageSeparator'),
            'split': input.get('splitReplyMessages'),
            'prose': prose,
            'actions': _authored_actions(decision),
        })
    cross_actions = decision.get('crossConversationActions') if isinstance(decision, dict) else None
    for action in cross_actions if isinstance(cross_actions, list) else []:
        if not isinstance(action, dict):
            continue
        if not action.get('content') or action.get('mode') not in ('immediate', 'delayed'):
            continue
        _add_message_event(add, {
            'participantId': action.get('participantId'),
            'content': action.get('content'),
            'mode': action.get('mode'),
            'occurredAt': action.get('sendAt') if action.get('mode') == 'delayed' and action.get('sendAt') else now_iso,
            'causedByEventIds': [narrative.get('eventId')],
            'separator': input.get('messageSeparator'),
            'split': input.get('splitReplyMessages'),
            'prose': prose,
            'actions': _authored_actions(decision),
        })
    group_reply_content = input.get('groupReplyContent')
    if group_reply_content:
        bubbles = _split_bubbles(group_reply_content, input.get('messageSeparator'), input.get('splitReplyMessages'))
        content = _canonical_bubble_content(
            group_reply_content, bubbles, input.get('messageSeparator'), input.get('splitReplyMessages')
        )
        add({
            'kind': 'group-message',
            'actor': 'protagonist',
            'occurredAt': now_iso,
            'causedByEventIds': [narrative.get('eventId')],
            'content': content,
            'bubbles': bubbles,
            'deliveryMode': 'immediate',
            'scriptBinding': bind_immediate_message_action(prose, bubbles, _authored_actions(decision)),
        })
    commitment = decision.get('followUpCommitment') if isinstance(decision, dict) else None
    if isinstance(commitment, dict):
        add({
            'kind': 'follow-up-promise',
            'actor': 'protagonist',
            'occurredAt': now_iso,
            'causedByEventIds': [narrative.get('eventId')],
            'participantId': participant_id,
            'content': commitment.get('summary'),
            'metadata': {'notBefore': commitment.get('notBefore'), 'kind': commitment.get('kind')},
        })
    intents = decision.get('intents') if isinstance(decision, dict) else None
    for intent in intents if isinstance(intents, list) else []:
        if not isinstance(intent, dict):
            continue
        add({
            'kind': 'future-intent',
            'actor': 'protagonist',
            'occurredAt': now_iso,
            'causedByEventIds': [narrative.get('eventId')],
            'participantId': intent.get('participantId') or participant_id,
            'content': intent.get('summary'),
            'metadata': {'type': intent.get('type'), 'notBefore': intent.get('notBefore')},
        })
    browser_intents = decision.get('browserIntents') if isinstance(decision, dict) else None
    for browser in browser_intents if isinstance(browser_intents, list) else []:
        if not isinstance(browser, dict):
            continue
        add({
            'kind': 'browser-intent',
            'actor': 'protagonist',
            'occurredAt': now_iso,
            'causedByEventIds': [narrative.get('eventId')],
            'participantId': browser.get('participantId') or participant_id,
            'content': browser.get('purpose'),
            'metadata': {
                'mode': browser.get('mode'),
                'timing': browser.get('timing') if browser.get('timing') is not None else 'deferred',
            },
        })
    local_media = decision.get('localMedia') if isinstance(decision, dict) else None
    native_face = decision.get('nativeFace') if isinstance(decision, dict) else None
    message_reactions = decision.get('messageReactions') if isinstance(decision, dict) else None
    reactions = message_reactions if isinstance(message_reactions, list) else []
    if local_media is not None or native_face is not None or reactions:
        metadata: Dict[str, Any] = {}
        if local_media is not None:
            metadata['localMedia'] = local_media
        if native_face is not None:
            metadata['nativeFace'] = native_face
        if reactions:
            metadata['messageReactions'] = message_reactions
        add({
            'kind': 'platform-action',
            'actor': 'protagonist',
            'occurredAt': now_iso,
            'causedByEventIds': [narrative.get('eventId')],
            'participantId': participant_id,
            'metadata': metadata,
        })

    scene_delta: Dict[str, Any] = {
        'frameId': input.get('frameId') if input.get('frameId') is not None else '',
        'burstId': input.get('burstId') if input.get('burstId') is not None else '',
    }
    if input.get('startsAfterEventId'):
        scene_delta['startsAfterEventId'] = input.get('startsAfterEventId')
    scene_delta['proseAppend'] = prose
    scene_delta['eventIds'] = [event.get('eventId') for event in events]
    return {
        'commitId': commit_id,
        'storyId': input.get('storyId'),
        'participantId': participant_id,
        'phase': input.get('phase'),
        'window': {'from': _to_iso_string(input.get('from')), 'to': now_iso},
        'prose': prose,
        'events': events,
        'sceneDelta': scene_delta,
        'sourceFormat': 'script-first-v1',
    }


def find_outgoing_script_event(
    commit: Dict[str, Any],
    participant_id: str,
    mode: Optional[ScriptDeliveryMode] = None,
    content: Optional[str] = None,
    separator: Optional[str] = '<sep/>',
) -> Optional[Dict[str, Any]]:
    """上游 findOutgoingScriptEvent（返回首个匹配事件，否则 None）。"""
    sep = '<sep/>' if separator is None else separator
    for event in commit.get('events') or []:
        if not is_outgoing_script_event(event):
            continue
        if event.get('kind') != 'outgoing-message':
            continue
        if event.get('participantId') != participant_id:
            continue
        if mode and event.get('deliveryMode') != mode:
            continue
        if content is None:
            return event
        if event.get('content') == content:
            return event
        if sep.join(event.get('bubbles') or []) == sep.join(_split_bubbles(content, sep, True)):
            return event
    return None


def find_group_script_event(commit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """上游 findGroupScriptEvent。"""
    for event in commit.get('events') or []:
        if is_outgoing_script_event(event) and event.get('kind') == 'group-message':
            return event
    return None


def bind_immediate_message_action(
    prose: str,
    bubbles: List[str],
    actions: Optional[List[AuthoredAction]] = None,
) -> Dict[str, Any]:
    """上游 bindImmediateMessageAction：把即时投递动作绑到剧本里唯一的字面文本。"""
    actions = actions or []
    matching = [
        action for action in actions
        if isinstance(action, dict)
        and action.get('content') is not None
        and _js_slice(prose, action.get('start'), action.get('end')) == action.get('content')
        and all(str(action.get('content')).find(bubble.strip()) >= 0 for bubble in (bubbles or []))
    ]
    if len(matching) == 1:
        action = matching[0]
        binding = bind_immediate_message_action(action.get('content'), bubbles)
        if binding.get('status') == 'bound':
            return {
                **binding,
                'spans': [
                    {**span, 'start': span.get('start') + action.get('start'), 'end': span.get('end') + action.get('start')}
                    for span in binding.get('spans') or []
                ],
            }
    spans: List[Dict[str, int]] = []
    cursor = 0
    for bubble_index, raw_bubble in enumerate(bubbles or []):
        bubble = raw_bubble.strip()
        if not bubble:
            return {'status': 'unbound'}
        start = prose.find(bubble, cursor)
        if start < 0 or prose.find(bubble, start + 1) >= 0:
            return {'status': 'unbound'}
        end = start + len(bubble)
        spans.append({'bubbleIndex': bubble_index, 'start': start, 'end': end})
        cursor = end
    return {'status': 'bound', 'spans': spans}


def unbound_immediate_message_events(commit: Dict[str, Any]) -> List[Dict[str, Any]]:
    """上游 unboundImmediateMessageEvents。"""
    return [
        event for event in (commit.get('events') or [])
        if is_outgoing_script_event(event)
        and event.get('deliveryMode') == 'immediate'
        and not (isinstance(event.get('scriptBinding'), dict)
                 and event['scriptBinding'].get('status') == 'bound')
    ]


def _add_message_event(add: Any, input: Dict[str, Any]) -> None:
    """上游私有 addMessageEvent。"""
    bubbles = _split_bubbles(input.get('content'), input.get('separator'), input.get('split'))
    add({
        'kind': 'outgoing-message',
        'actor': 'protagonist',
        'occurredAt': input.get('occurredAt'),
        'causedByEventIds': input.get('causedByEventIds'),
        'participantId': input.get('participantId'),
        'content': _canonical_bubble_content(
            input.get('content'), bubbles, input.get('separator'), input.get('split')
        ),
        'bubbles': bubbles,
        'deliveryMode': input.get('mode'),
        'scriptBinding': {'status': 'future'} if input.get('mode') == 'delayed'
        else bind_immediate_message_action(input.get('prose'), bubbles, input.get('actions') or []),
    })


def _canonical_bubble_content(
    content: str,
    bubbles: List[str],
    separator: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> str:
    """上游私有 canonicalBubbleContent。"""
    on = True if enabled is None else enabled
    if not on:
        return content
    return (separator or '<sep/>').join(bubbles)


def _split_bubbles(content: str, separator: Optional[str] = None, enabled: Optional[bool] = None) -> List[str]:
    """上游私有 splitBubbles。"""
    sep = '<sep/>' if separator is None else separator
    on = True if enabled is None else enabled
    if not on or not sep or sep not in content:
        return [content]
    bubbles = [item.strip() for item in content.split(sep) if item.strip()]
    return bubbles if bubbles else [content]


def _stable_commit_id(input: ScriptFirstDecisionInput, participant_id: str, prose: str) -> str:
    """上游私有 stableCommitId。"""
    decision = input.get('decision') if isinstance(input.get('decision'), dict) else {}
    group_reply_content = input.get('groupReplyContent')
    if group_reply_content is None:
        group_reply_content = ''
    frame_id = input.get('frameId')
    if frame_id is None:
        frame_id = ''
    burst_id = input.get('burstId')
    if burst_id is None:
        burst_id = ''
    starts_after_event_id = input.get('startsAfterEventId')
    if starts_after_event_id is None:
        starts_after_event_id = ''
    cross_actions = decision.get('crossConversationActions')
    intents = decision.get('intents')
    browser_intents = decision.get('browserIntents')
    message_reactions = decision.get('messageReactions')
    semantic_payload = {
        'participantId': participant_id,
        'prose': prose,
        'interaction': decision.get('interaction'),
        'groupReplyContent': group_reply_content,
        'crossConversationActions': cross_actions if cross_actions is not None else [],
        'intents': intents if intents is not None else [],
        'browserIntents': browser_intents if browser_intents is not None else [],
        'followUpCommitment': decision.get('followUpCommitment'),
        'messageReactions': message_reactions if message_reactions is not None else [],
        'localMedia': decision.get('localMedia'),
        'nativeFace': decision.get('nativeFace'),
        'frameId': frame_id,
        'burstId': burst_id,
        'startsAfterEventId': starts_after_event_id,
    }
    digest = hashlib.sha256('\u001f'.join([
        _join_part(input.get('storyId')),
        _join_part(input.get('phase')),
        _join_part(_to_iso_string(input.get('from'))),
        _join_part(_to_iso_string(input.get('now'))),
        _stringify_json(semantic_payload),
    ]).encode('utf-8')).hexdigest()[:20]
    return f'commit:{digest}'


def _authored_actions(decision: Any) -> List[AuthoredAction]:
    """上游直接传 `input.decision.authoredActions`，undefined 落到默认参数 []。"""
    if not isinstance(decision, dict):
        return []
    actions = decision.get('authoredActions')
    return actions if isinstance(actions, list) else []


def _to_iso_string(value: Any) -> Optional[str]:
    """`Date.prototype.toISOString()`：UTC、固定 3 位毫秒、Z 结尾（sha256 输入的一部分）。"""
    parsed = parse_time(value)
    if parsed is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return (
        f'{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}'
        f'T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}'
        f'.{parsed.microsecond // 1000:03d}Z'
    )


def _stringify_json(value: Any) -> str:
    """`JSON.stringify` 的等价实现。

    摘要里嵌套的数字/字符串必须与 JS 逐字节一致，所以不能直接用
    `json.dumps`（整数值浮点会写成 `1.0`、指数写法也不同，见 `_js_number_to_string`）。
    键序=插入序；非 ASCII 原样；NaN/Infinity → null。
    """
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return 'null'
        return _js_number_to_string(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(_stringify_json(item) for item in value) + ']'
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text_key = key if isinstance(key, str) else str(key)
            parts.append(json.dumps(text_key, ensure_ascii=False) + ':' + _stringify_json(item))
        return '{' + ','.join(parts) + '}'
    return json.dumps(str(value), ensure_ascii=False)


def _js_number_to_string(value: float) -> str:
    """ECMAScript `Number::toString`（JSON.stringify 的数字写法）。

    Python `repr(float)` 与 JS 都取最短可回读十进制，但固定/指数记法的切换点不同
    （JS：1e-6 ≤ |x| < 1e21 用定点；指数不补零），这里按 ECMAScript 规则重新排版。
    """
    if value == 0:
        return '0'
    sign = '-' if value < 0 else ''
    digits_tuple = Decimal(repr(abs(value))).as_tuple()
    digits = ''.join(str(digit) for digit in digits_tuple.digits)
    # value = digits × 10^exponent；n 是小数点相对 digits 起点的位置（0.digits × 10^n）。
    n = len(digits) + digits_tuple.exponent
    digits = digits.rstrip('0') or '0'
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + '0' * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + '.' + digits[n:]
    if -6 < n <= 0:
        return sign + '0.' + '0' * (-n) + digits
    exponent = n - 1
    mantissa = digits if k == 1 else digits[0] + '.' + digits[1:]
    return sign + mantissa + 'e' + ('+' if exponent >= 0 else '-') + str(abs(exponent))


def _js_slice(value: str, start: Any, end: Any) -> str:
    """`String.prototype.slice`：负索引从尾部计、越界钳制、undefined 表示该端省略。"""
    return value[_js_slice_index(start):_js_slice_index(end)]


def _js_slice_index(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return 0
        if math.isinf(value):
            return _JS_MAX_INDEX if value > 0 else -_JS_MAX_INDEX
        return int(value)  # JS ToIntegerOrInfinity 向零取整
    return 0  # 非数字经 ToNumber 变 NaN，等价于 0


def _join_part(value: Any) -> str:
    """`Array.prototype.join` 对 undefined/null 输出空串。"""
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)
