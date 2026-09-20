# -*- coding: utf-8 -*-
"""Authored actions 语法解包，对应上游 `.hdsi_reference/src/script/authored-actions.ts`（1.0.1-beta6-rebuild，86 行）。

一比一移植；同步函数 + 两个按「数组身份」记事的 WeakSet。上游语义：
- `<say id="...">` 只做语法解包，永不从散文推断行动；重复 id 不是可执行引用，原话逐字幸存。
- 「继承」只在同一数组实例上成立：伪造的、或 JSON 往返过的 authoredActions 都不算继承，
  以免把模型自己编的答案当成已授权原话；已提前流式发送过的原话也不再被二次加工。

约定：
- 函数名 snake_case；数据字段 id/start/end/content/authoredActions/... 保持 camelCase。
- 上游 `WeakSet<AuthoredAction[]>` 用可弱引用 + 身份哈希的 `_AuthoredActionList` 复刻：
  Python 的 list 既不能弱引用，默认又按内容比较，直接用会破坏「按身份」的判定。
- 上游对象字面量里显式写出的 `undefined`（`actionId: undefined`、`content: undefined`）在返回 dict 里
  保留 key 并置 None，读取方统一用 `.get()`（与 hdsi/agency.py 的既有约定一致）。
"""

from __future__ import annotations

import re
import weakref
from typing import Any, Dict, List, TypedDict

from ..types import NarrativeDecision

__all__ = [
    'AuthoredAction',
    'read_authored_actions',
    'resolve_authored_actions',
    'complete_legacy_bubble_block',
]

# 上游 `/<say id="([\w-]{1,64})">([\s\S]*?)<\/say>/g`。
# JS 的 \w 只匹配 [A-Za-z0-9_]；Python 默认 \w 含汉字等 Unicode 字符，故加 re.ASCII 保持同一字符集
# （[\s\S] 是全集，不受 ASCII 标志影响）。
_AUTHORED_SAY = re.compile(r'<say id="([\w-]{1,64})">([\s\S]*?)</say>', re.ASCII)
# 上游 `/\r?\n\s*\r?\n|\\n\\n/`：空行或字面量 \n\n 分隔（旧版终段气泡块）。
_LEGACY_BLOCK = re.compile(r'\r?\n\s*\r?\n|\\n\\n')
# 上游 `/[\r\n<>]|\\n/`：含换行、尖括号或字面量 \n 的段落不是合法运输块。
_LEGACY_PART_REJECT = re.compile(r'[\r\n<>]|\\n')


class AuthoredAction(TypedDict):
    """上游 export interface AuthoredAction。"""

    id: str
    start: int
    end: int
    content: str


class _AuthoredActionList(list):
    """可弱引用 + 身份哈希的 list，用来复刻上游 `WeakSet<AuthoredAction[]>`。

    普通 list 无法进入 weakref.WeakSet；不覆盖 `__eq__` 是为了保持「list 内容比较」的常规行为，
    身份判定由 WeakSet 的 id 哈希完成。
    """

    __slots__ = ('__weakref__',)
    __hash__ = object.__hash__


# 上游 `const resolvedActions = new WeakSet<AuthoredAction[]>()` /
# `const deliveredActions = new WeakSet<AuthoredAction[]>()`。
_resolved_actions: 'weakref.WeakSet' = weakref.WeakSet()
_delivered_actions: 'weakref.WeakSet' = weakref.WeakSet()


def _js_falsy(value: Any) -> bool:
    """JS 的 `!value`：注意空对象 / 空数组在 JS 里是 *真值*，不能直接用 Python 的 bool()。"""
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0 or value != value  # NaN → true
    if isinstance(value, str):
        return len(value) == 0
    return False


def read_authored_actions(script: str) -> Dict[str, Any]:
    """上游 readAuthoredActions：只做语法解包，返回 `{'prose': str, 'actions': List[AuthoredAction]}`。"""
    actions: List[Dict[str, Any]] = []
    prose = ''
    cursor = 0
    for match in _AUTHORED_SAY.finditer(script):
        prose += script[cursor:match.start()]
        start = len(prose)
        prose += match.group(2)
        actions.append({'id': match.group(1), 'start': start, 'end': len(prose), 'content': match.group(2)})
        cursor = match.end()
    prose += script[cursor:]
    # 上游 actions.filter(action => actions.filter(other => other.id === action.id).length === 1)：
    # 重复 id 的行动全部剔除，保序。
    return {
        'prose': prose,
        'actions': [
            action for action in actions
            if sum(1 for other in actions if other['id'] == action['id']) == 1
        ],
    }


def resolve_authored_actions(
    decision: NarrativeDecision,
    already_sent: bool = False,
    separator: str = '<sep/>',
) -> NarrativeDecision:
    """上游 resolveAuthoredActions：在任何运输层归一化之前解析同一份已授权原话。"""
    # 上游 `if (typeof decision?.script !== 'string') return decision`。
    if not isinstance(decision, dict) or not isinstance(decision.get('script'), str):
        return decision
    parsed = read_authored_actions(decision['script'])
    # 上游 trimStart() / trim()：这里用 Python 的 lstrip()/strip()（普通中英文文本等价）。
    leading = len(parsed['prose']) - len(parsed['prose'].lstrip())
    prose = parsed['prose'].strip()
    inherited_raw = decision.get('authoredActions')
    # 上游 resolvedActions.has(decision.authoredActions)：只有本模块返回过的数组实例才算继承。
    inherited = (
        inherited_raw
        if isinstance(inherited_raw, list) and inherited_raw in _resolved_actions
        else []
    )
    # 上游 `alreadySent ||= deliveredActions.has(inherited)`。
    already_sent = bool(already_sent) or (inherited in _delivered_actions)
    source_actions = parsed['actions'] if parsed['actions'] else inherited
    actions = _AuthoredActionList(
        {**action, 'start': action['start'] - leading, 'end': action['end'] - leading}
        for action in source_actions
        if isinstance(action, dict)
    )
    actions = _AuthoredActionList(
        action for action in actions
        if action['start'] >= 0
        and action['end'] <= len(prose)
        and prose[action['start']:action['end']] == action['content']
    )
    interaction_raw = decision.get('interaction')
    interaction = interaction_raw if isinstance(interaction_raw, dict) else None
    private_reply = interaction.get('reply') if interaction is not None else None
    group_reply = decision.get('groupReply')
    cross_actions = decision.get('crossConversationActions')
    # 上游 `(!decision.groupReply || decision.groupReply.mode === 'none') && !decision.crossConversationActions?.length`。
    # JS 里 `{}` / `[]` 是 truthy，故 groupReply 的「缺失」用 _js_falsy 判定，cross 取 `.length`。
    cross_length = len(cross_actions) if isinstance(cross_actions, (list, tuple, str)) else 0
    one_private_recipient = (
        _js_falsy(group_reply) or (isinstance(group_reply, dict) and group_reply.get('mode') == 'none')
    ) and not cross_length
    # 唯一行动 + 单私聊接收者：把「终段运输块」补回原话（旧版镜像常丢气泡）。
    if (
        not already_sent
        and one_private_recipient
        and len(actions) == 1
        and isinstance(private_reply, dict)
        and private_reply.get('mode') == 'immediate'
        and private_reply.get('actionId') == actions[0]['id']
    ):
        tail = complete_legacy_bubble_block(prose, actions[0]['content'], separator)
        if tail and actions[0]['start'] == len(prose) - len(tail):
            actions[0] = {**actions[0], 'content': tail, 'end': len(prose)}

    def resolve(reply: Any) -> Any:
        if not isinstance(reply, dict):
            return reply
        if already_sent:
            # 上游 `{ ...reply, actionId: undefined }`。
            return {**reply, 'actionId': None}
        if not reply.get('actionId') or reply.get('mode') != 'immediate':
            return reply
        action = next(
            (
                item for item in actions
                if item['id'] == reply.get('actionId')
                and prose[item['start']:item['end']] == item['content']
                and item['content'].strip()
            ),
            None,
        )
        if action is not None:
            return {**reply, 'content': action['content']}
        # 上游 `{ ...reply, mode: 'none', content: undefined }`。
        return {**reply, 'mode': 'none', 'content': None}

    # 引用失配的保守兜底：整份剧本只有一个已授权 say 行动、本回合只有一个私聊
    # 接收者、且回复没有可用 content 时，该行动就是这条回复的本体——模型常照抄
    # 协议示例里的 id 字面量导致引用对不上。零行动、重复 id、伪造继承与已提前
    # 流式发送的情况都不适用，保持原有的 none 语义。
    def sole_action_reply(reply: Any) -> Any:
        if not isinstance(reply, dict):
            # 上游此处对 undefined 会抛 TypeError；本移植保持不崩，原样返回。
            return reply
        if (
            already_sent
            or not one_private_recipient
            or reply.get('mode') != 'immediate'
            or reply.get('content')
            or len(actions) != 1
        ):
            return reply
        only = actions[0]
        if not only['content'].strip() or prose[only['start']:only['end']] != only['content']:
            return reply
        return {**reply, 'actionId': only['id'], 'content': only['content']}

    # Recover a legacy mirror's missing bubbles only from its explicit terminal
    # transport block, never ordinary narration or another recipient's action.
    legacy_tail = None
    if (
        not already_sent
        and one_private_recipient
        and not actions
        and isinstance(private_reply, dict)
        and private_reply.get('mode') == 'immediate'
        and not private_reply.get('actionId')
        and private_reply.get('content')
    ):
        legacy_tail = complete_legacy_bubble_block(prose, private_reply['content'], separator)
    _resolved_actions.add(actions)
    if already_sent:
        _delivered_actions.add(actions)
    result: Dict[str, Any] = {**decision, 'script': prose, 'authoredActions': actions}
    if interaction is not None:
        reply = interaction.get('reply')
        if legacy_tail:
            reply = {**reply, 'content': legacy_tail} if isinstance(reply, dict) else reply
        else:
            reply = sole_action_reply(reply)
        # 上游 `{ ...decision.interaction, reply: resolve(...) }`：interaction 存在时一定写入 reply key。
        result['interaction'] = {**interaction, 'reply': resolve(reply)}
    if isinstance(group_reply, dict):
        result['groupReply'] = resolve(group_reply)
    if isinstance(cross_actions, list):
        result['crossConversationActions'] = [resolve(item) for item in cross_actions]
    return result


def complete_legacy_bubble_block(prose: str, content: str, separator: str):
    """上游 completeLegacyBubbleBlock：只从显式终段运输块补气泡，返回补全后的尾段或 None。"""
    if not separator or not content.strip():
        return None
    parts = _LEGACY_BLOCK.split(prose.strip())
    tail = parts[-1].strip() if parts else ''
    if not tail.startswith(content.strip() + separator) or len(tail) > 4_000:
        return None
    segments = tail.split(separator)
    if len(segments) < 2 or any(not part.strip() or _LEGACY_PART_REJECT.search(part) for part in segments):
        return None
    return tail
