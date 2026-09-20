# -*- coding: utf-8 -*-
"""narrator 的模型侧 payload 构造，对应上游 `.hdsi_reference/src/narrator.ts` 第 1592-2155 行
（HDS-Interlude 1.0.1-beta6-rebuild，narrator.ts 尾部全部顶层函数）。

上游 → 本模块：
  toPromptPayload（export）            → to_prompt_payload
  compactScriptTag（export）           → compact_script_tag
  promptVisibleMessageContent（export）→ prompt_visible_message_content
  compactPromptEntries（export）       → compact_prompt_entries
  buildRecentExchange（私有）          → _build_recent_exchange
  parseDate（私有）                    → _parse_date
  compactPromptRecords（私有）         → _compact_prompt_records
  participantPromptPayload（私有）     → _participant_prompt_payload
  alterAnalysisPrompt（私有）          → _alter_analysis_prompt
  compactionPrompt（私有）             → _compaction_prompt
  schedulePreplanPrompt（私有）        → _schedule_preplan_prompt
  timelineDirectorPrompt（私有）       → _timeline_director_prompt
  toTimelinePlanPayload（私有）        → _to_timeline_plan_payload
  overlayCompactionPrompt（私有）      → _overlay_compaction_prompt
  toOverlayCompactionPayload（私有）   → _to_overlay_compaction_payload
  toCompactionPayload（私有）          → _to_compaction_payload
  toSchedulePreplanPayload（私有）     → _to_schedule_preplan_payload

约定（与 PORTING_GUIDE 及 hdsi/script 下已移植文件一致）：
- 同步纯函数；数据结构一律 dict，字段名沿用上游 camelCase。
- JSON 字段顺序 = 上游对象字面量键顺序（顺序参与前缀缓存）：dict 字面量逐字段排布；
  `{...a, ...b}` → `{**a, **b}`；重复 key 不改位置，与 JS 对象字面量一致。
- 上游 `undefined` 与本包统一约定：对象字面量里显式写出的 key 保留并置 `None`
  （读取方统一 .get()，JSON 渲染为 null）；只有源码里写 `undefined` 的表达式
  （条件展开 `...(... ? {...} : {})`、`?.map`、`?? undefined`）才整体省略/置 None。
- `.toISOString()` → `iso()`（hdsi/time_utils，UTC + `Z`），与 hdsi/script/knowledge_evidence.py 一致。
- JS `Math.round` → `_js_round`（Python round 是银行家舍入）；`??` → `is None` 兜底；
  `||` 用 Python 真值；`=== true/false` → `is True` / `is False`。
- 提示词原文逐字复制。带 `# >>> TS:` / `# <<< TS:` 标注的块是逐字校验区
  （由工具从上游 narrator.ts 对应行号直接回填，不要手改）。

本模块从并行移植的 hdsi/narrator_prompts.py 取 4 个已导出名字
（system_prompt / writing_affordances 在本区间没有调用点，按任务约定保留导入，
保证 hub 侧名字集合不漂移）。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .narrator_prompts import (  # noqa: F401 - system_prompt/writing_affordances 仅按约定保留
    RecentScriptOwnership,
    recent_script_ownership,
    story_state_for_prompt,
    system_prompt,
    writing_affordances,
)
from .script.context_compiler import CompiledNarrativeContext, compile_narrative_context
from .script.continuation import continuation_bookmark
from .script.delivery_reality import delivery_reality
from .script.development import interaction_evidence
from .script.knowledge_evidence import KNOWLEDGE_WRITING_FRAME, fact_evidence_for_prompt
from .script.life_handoff import narrative_evidence
from .time_utils import iso, parse_time, story_local_time_context
from .types import (
    CompactionRequest,
    InterludeParticipant,
    NarrativeRequest,
    OverlayCompactionRequest,
    SchedulePreplanReviewRequest,
    ScriptEntry,
    TimelinePlanRequest,
)
from .utils import is_record

__all__ = [
    'compact_prompt_entries',
    'compact_script_tag',
    'prompt_visible_message_content',
    'to_prompt_payload',
]

# 上游 `['user-message', 'character-message', 'group-message', 'character-group-message']`。
_RAW_ENTRY_KINDS = ('user-message', 'character-message', 'group-message', 'character-group-message')

# 上游 interactionEvidence / deliveryReality 里 `Infinity` 的替代（slice(-Infinity) 全取）。
_JS_MAX_INDEX = 9007199254740991

# 排序时 occurredAt 缺失（上游 NaN 比较恒假）的兜底，保证 sort 不因混合类型报错。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# promptVisibleMessageContent 的原生表情替换表（正则与替换文案逐字对应上游）。
# `[\[【]` / `[\]】]` 是「半角或全角括号」；最后一条带 gi 旗标。
_NATIVE_FACE_REPLACEMENTS: Tuple[Tuple[Any, str], ...] = (
    (re.compile(r'[\[【]流汗[\]】]'), '〈附带汗颜表情〉'),
    (re.compile(r'[\[【]微笑[\]】]'), '〈附带微笑表情〉'),
    (re.compile(r'[\[【]笑哭[\]】]'), '〈附带笑哭表情〉'),
    (re.compile(r'[\[【]尴尬[\]】]'), '〈附带尴尬表情〉'),
    (re.compile(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', re.IGNORECASE), '〈附带未识别媒体表达〉'),
)


# ========== JS 语义小工具 ==========

def _js_round(value: float) -> int:
    """TS `Math.round`：.5 一律向 +∞ 取整（Python round 是银行家舍入，不能用）。"""
    return math.floor(value + 0.5)


def _js_length(value: Any) -> int:
    """TS `value?.length ?? 0`：非数组/字符串（undefined）按 0 处理。"""
    if isinstance(value, (list, tuple, str)):
        return len(value)
    return 0


def _js_text_length(value: Any) -> int:
    """TS `value.length`（string）：非字符串按 0 处理，不污染字符预算。"""
    return len(value) if isinstance(value, str) else 0


def _trimmed_or(value: Any, fallback: str) -> str:
    """TS `value?.trim() || fallback`（空串也回落）。"""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return fallback


def _mapped_or_none(items: Any, mapper: Any) -> Optional[List[Any]]:
    """TS `items?.map(mapper)`：undefined（None）保持 undefined（本包置 None）。"""
    if items is None:
        return None
    return [mapper(item) for item in items]


def _entry_sort_key(entry: ScriptEntry) -> Tuple[Any, Any]:
    """上游 sort 比较器 `left.occurredAt.getTime() - right.occurredAt.getTime() || left.id - right.id`。"""
    occurred_at = entry.get('occurredAt')
    if not isinstance(occurred_at, datetime):
        # 上游此处是 NaN，比较恒假（视作相等）；这里用统一最小值保持稳定排序。
        occurred_at = _EPOCH
    entry_id = entry.get('id')
    if isinstance(entry_id, bool) or not isinstance(entry_id, (int, float)):
        entry_id = 0
    return (occurred_at, entry_id)


# ========== 上游 export：compactScriptTag ==========

def compact_script_tag(kind: str, actor: str) -> str:
    """上游 export compactScriptTag：把 kind/actor/participantId 三元组压成一个短标签。"""
    ownership = recent_script_ownership({'kind': kind, 'actor': actor})
    if ownership == 'protagonist-delivered-message':
        if kind == 'character-group-message':
            return 'protagonist(group)'
        if kind == 'character-platform-action':
            return 'protagonist(action)'
        return 'protagonist'
    if ownership == 'user-delivered-message':
        return 'user'
    if ownership == 'protagonist-narrative':
        return 'protagonist-narration'
    if ownership == 'external-group-message':
        return 'group-member'
    return 'system'


# ========== 上游 export：promptVisibleMessageContent ==========

def prompt_visible_message_content(content: str, ownership: RecentScriptOwnership) -> str:
    """上游 export promptVisibleMessageContent：只对主角自己发的消息做表情/媒体标注替换。"""
    if ownership != 'protagonist-delivered-message':
        return content
    # 上游 `String(content ?? '')`
    text = '' if content is None else str(content)
    for pattern, replacement in _NATIVE_FACE_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


# ========== 上游 export：compactPromptEntries ==========

def compact_prompt_entries(
    entries: List[ScriptEntry],
    character_budget: int,
    protected_since: Optional[datetime] = None,
) -> List[ScriptEntry]:
    """上游 export compactPromptEntries：按字符预算从尾部回收最近记录。

    `protectedSince` 之后的原文类记录永不被裁；窗口边缘保留完整因果段落
    （软预算：单条原文可以超预算，但不改写）。
    """
    remaining = max(1000, character_budget)
    raw_entries = entries or []
    protected_ids = set()
    for entry in raw_entries:
        # 上游 `!!protectedSince && entry.occurredAt >= protectedSince && rawKinds.has(entry.kind)`
        occurred_at = entry.get('occurredAt')
        if protected_since is not None and isinstance(occurred_at, datetime) \
                and occurred_at >= protected_since and entry.get('kind') in _RAW_ENTRY_KINDS:
            protected_ids.add(entry.get('id'))
    selected: List[ScriptEntry] = [entry for entry in raw_entries if entry.get('id') in protected_ids]
    remaining = max(0, remaining - sum(_js_text_length(entry.get('content')) for entry in selected))
    index = len(raw_entries) - 1
    while index >= 0 and remaining > 0:
        entry = raw_entries[index]
        index -= 1
        if entry.get('id') in protected_ids:
            continue
        # 上游把 `entry.content` 存进 content 后再比较，两边恒等，因此
        # `[前文截断]${content}` 分支在 TS 里是死代码；这里逐字复刻该行为。
        content = entry.get('content')
        if content == entry.get('content'):
            selected.append(entry)
        else:
            selected.append({**entry, 'content': '[前文截断]' + content})
        remaining -= _js_text_length(content)
    selected.sort(key=_entry_sort_key)
    return selected


# ========== 上游私有：compactPromptRecords ==========

def _compact_prompt_records(records: List[Dict[str, Any]], character_budget: int) -> List[Dict[str, Any]]:
    """上游私有 compactPromptRecords：超预算的 content 截断后追加「[已截断]」。

    上游用的是后缀（`${content}[已截断]`），不是前缀；截断时才复制新对象。
    """
    remaining = max(1000, character_budget)
    selected: List[Dict[str, Any]] = []
    for record in records or []:
        if remaining <= 0:
            break
        content = record.get('content')
        text = content if isinstance(content, str) else ''
        clipped = text[:remaining] if len(text) > remaining else text
        selected.append(record if clipped == text else {**record, 'content': clipped + '[已截断]'})
        remaining -= len(clipped)
    return selected


# ========== 上游私有：participantPromptPayload ==========

def _participant_prompt_payload(
    participant: InterludeParticipant,
    include_current_details: bool,
    include_relationship_details: bool = False,
) -> Dict[str, Any]:
    """上游私有 participantPromptPayload。

    关系块 → 当前细节块的顺序即 JSON 键顺序；未提供的人字段与上游一样写出 key 并置 None
    （本包约定：None 同时表示 undefined/null，读取方用 .get()）。
    """
    state = participant.get('state') if is_record(participant.get('state')) else {}
    payload: Dict[str, Any] = {'id': participant.get('id')}
    if include_relationship_details:
        payload.update({
            'displayName': participant.get('displayName'),
            'profile': participant.get('profile'),
            'relationship': participant.get('relationship'),
            'relationshipOverlay': state.get('relationshipOverlay'),
            'lastUserMessageAt': state.get('lastUserMessageAt'),
            'lastCharacterMessageAt': state.get('lastCharacterMessageAt'),
        })
    if include_current_details:
        payload.update({
            'personId': participant.get('personId'),
            'openThreads': state.get('openThreads'),
            'relationshipNotes': state.get('relationshipNotes'),
            'relationshipNotesAuthority': 'protagonist-last-interpretation; actual new feedback may revise it',
        })
    payload.update({
        'unreadMessageCount': state.get('unreadMessageCount'),
        'pendingReplyCount': state.get('pendingReplyCount'),
        'updatedAt': iso(participant.get('updatedAt')),
    })
    return payload


# ========== 上游私有：buildRecentExchange ==========

def _build_recent_exchange(request: NarrativeRequest, max_characters: int = 1600) -> List[Dict[str, str]]:
    """上游私有 buildRecentExchange（cache-first 尾部块）。

    这是紧邻决策点的「最近交流锚点」，不是第二份叙事散文：只取最后 3 条
    用户/主角消息，倒序累积到字符预算；群聊回合直接返回空数组。
    """
    if request.get('groupContext') is not None:
        return []
    items: List[Dict[str, str]] = []
    remaining = max_characters
    entries = request.get('recentEntries') or []
    index = len(entries) - 1
    while index >= 0 and len(items) < 3:
        entry = entries[index]
        index -= 1
        if entry.get('kind') not in ('user-message', 'character-message', 'character-platform-action'):
            continue
        if request.get('phase') == 'user-message' and entry.get('kind') == 'user-message' \
                and entry.get('content') == request.get('userMessage'):
            continue
        ownership = recent_script_ownership(entry)
        content = prompt_visible_message_content(entry.get('content'), ownership)
        if not isinstance(content, str):
            content = ''
        if not content.strip():
            continue
        clipped = content[:remaining] if len(content) > remaining else content
        if not clipped.strip():
            break
        items.insert(0, {'tag': compact_script_tag(entry.get('kind'), entry.get('actor')), 'content': clipped})
        remaining -= len(clipped)
        if remaining <= 0:
            break
    return items


# ========== 上游私有：parseDate ==========

def _parse_date(value: Any) -> Optional[datetime]:
    """上游私有 parseDate：类型不符或 Invalid Date 返回 undefined（None）。

    注意 `new Date(number)` 一律按 epoch 毫秒解释，不能直接用
    hdsi/time_utils.parse_time 的秒/毫秒启发式（它会把 1e10 当秒）。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float, datetime)):
        return None
    if isinstance(value, datetime):
        # 上游 `value instanceof Date` 原样返回同一对象；这里只补齐缺失的 tzinfo。
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return parse_time(value)


# ========== 上游 export：toPromptPayload ==========

def to_prompt_payload(request: NarrativeRequest, options: Optional[Dict[str, Any]] = None) -> CompiledNarrativeContext:
    """上游 export toPromptPayload（narrator.ts 1592-1851）。

    两种键顺序（语义证据与 continuation 书签完全相同）：
    - 默认顺序：历史在前，逐轮字段在后；
    - `options.cacheFirst`：按变异频率分组——稳定身份块与只追加的历史在前，
      逐轮变化区靠近决策点，尾部补 `recentExchange` 锚点。

    JS 对象字面量里重复 key 只更新值、不改变位置；本函数用
    `cache_payload[key] = ...` 复刻该语义（键顺序影响 provider 前缀缓存命中）。
    """
    options = options if is_record(options) else {}
    story = request.get('story') if is_record(request.get('story')) else {}
    setting = story.get('setting') if is_record(story.get('setting')) else {}
    story_state = story.get('state') if is_record(story.get('state')) else {}
    store_timezone = setting.get('timezone')
    phase = request.get('phase')
    from_at = request.get('from')
    now = request.get('now')
    recent_entries = request.get('recentEntries') or []
    participant = request.get('participant')
    participant_id = participant.get('id') if is_record(participant) else None
    chat_capabilities = request.get('chatCapabilities')
    sticker_catalog = request.get('stickerCatalog')
    share_participant_details = request.get('shareParticipantDetails')
    timeline_plan = request.get('timelinePlan')
    group_context = request.get('groupContext')

    # 这是 token 预算后的连续性快照：近处使用原文，远处使用摘要和事实，而非全量历史。
    from_local_context = story_local_time_context(from_at, store_timezone)
    now_local_context = story_local_time_context(now, store_timezone)
    continuity_updated_at = _parse_date(story_state.get('lastContinuityUpdateAt'))

    # In shared mode the legacy setting.user/relationship fields are only defaults.
    # Replace them with the current relationship so one account never receives
    # another account's private relationship context.
    perspective_source = setting.get('perspective')
    perspective = perspective_source.strip()[:1200] if isinstance(perspective_source, str) else ''
    setting_payload: Dict[str, Any] = {**setting, 'perspective': perspective}
    if is_record(participant):
        # 上游把 user/relationship 写在同一个字面量里；setting 已含这两个 key 时
        # dict 赋值同样保持原位置，与 JS 一致。
        setting_payload['user'] = {
            'displayName': participant.get('displayName'),
            'profile': participant.get('profile'),
        }
        setting_payload['relationship'] = participant.get('relationship')

    continuity_snapshot_source = story_state.get('continuitySnapshot')
    continuity_snapshot = (
        {**continuity_snapshot_source, 'next': []}
        if not any(entry.get('kind') == 'script' for entry in recent_entries)
        and is_record(continuity_snapshot_source)
        else None
    )
    continuity_snapshot_age_minutes = (
        max(0, _js_round((now - continuity_updated_at).total_seconds() / 60.0))
        if continuity_updated_at is not None
        else None
    )

    automatic_delivery_summaries: Optional[List[Dict[str, Any]]] = None
    if phase == 'advance' or phase == 'conversation-follow-up':
        automatic_delivery_summaries = []
        for item in request.get('automaticDeliverySummaries') or []:
            if not (phase == 'advance' or share_participant_details or item.get('participantId') == participant_id):
                continue
            automatic_delivery_summaries.append({
                'participantId': item.get('participantId'),
                'summary': item.get('summary'),
                'sourceEntryId': item.get('sourceEntryId'),
                'deliveredAt': item.get('deliveredAt'),
            })

    current_event: Dict[str, Any]
    if phase == 'advance' or phase == 'conversation-follow-up':
        current_event = {'type': 'none'}
    elif group_context is not None:
        current_event = {'type': 'group-message-batch'}
    elif phase == 'user-message':
        images = request.get('images')
        audio = request.get('audio')
        visual_observations = request.get('visualObservations')
        quoted_messages = request.get('quotedMessages')
        user_reported_times = request.get('userReportedTimes')
        user_message = request.get('userMessage')
        current_event = {
            'type': 'private-message-batch',
            'content': user_message if user_message is not None else '',
            'imageCount': _js_length(images),
            'audioCount': _js_length(audio),
            'visualEvidenceMode': 'native-images' if _js_length(images)
            else 'sidecar-observations' if _js_length(visual_observations)
            else 'none',
            'observedAt': iso(now),
            'observedAtLocal': now_local_context['local'],
            **({'userReportedTimes': user_reported_times} if _js_length(user_reported_times) else {}),
            **({'visualObservations': visual_observations} if _js_length(visual_observations) else {}),
            **({'quotedMessages': quoted_messages} if _js_length(quoted_messages) else {}),
        }
    else:
        current_event = {'type': 'due-intents'}

    group_context_payload: Optional[Dict[str, Any]] = None
    if group_context is not None:
        messages_payload: List[Dict[str, Any]] = []
        for message in group_context.get('messages') or []:
            item: Dict[str, Any] = {'speaker': message.get('speaker')}
            if chat_capabilities is not None and message.get('messageRef'):
                item['messageRef'] = message.get('messageRef')
            item['senderId'] = message.get('senderId')
            item['senderName'] = message.get('senderName')
            item['content'] = message.get('content')
            if message.get('quote'):
                item['quote'] = message.get('quote')
            item['occurredAt'] = iso(message.get('occurredAt'))
            item['direction'] = message.get('direction')
            messages_payload.append(item)
        group_context_payload = {**group_context, 'messages': messages_payload}

    due_intents = [
        {
            'id': intent.get('id'),
            'type': intent.get('type'),
            'participantId': intent.get('participantId'),
            'summary': intent.get('summary'),
            'notBefore': iso(intent.get('notBefore')),
            'payload': intent.get('payload'),
        }
        for intent in request.get('dueIntents') or []
    ]
    upcoming_plans = [
        {
            'id': intent.get('id'),
            'type': intent.get('type'),
            'participantId': intent.get('participantId'),
            'summary': intent.get('summary'),
            'notBefore': iso(intent.get('notBefore')),
        }
        for intent in (request.get('upcomingIntents') if request.get('upcomingIntents') is not None else [])
    ]

    follow_up_commitments: Optional[List[Dict[str, Any]]] = None
    if phase == 'user-message' or phase == 'intent-due':
        follow_up_commitments = []
        for intent in (request.get('followUpCommitments') if request.get('followUpCommitments') is not None else []):
            intent_payload = intent.get('payload') if is_record(intent.get('payload')) else {}
            kind = intent_payload.get('kind')
            expires_at = intent_payload.get('expiresAt')
            source_entry_ids = intent_payload.get('sourceEntryIds')
            follow_up_commitments.append({
                'id': intent.get('id'),
                'kind': kind if kind is not None else 'thinking',
                'summary': intent.get('summary'),
                'notBefore': iso(intent.get('notBefore')),
                'expiresAt': expires_at if isinstance(expires_at, str) else '',
                'sourceEntryIds': source_entry_ids if isinstance(source_entry_ids, list) else [],
            })

    active_consequences = []
    for intent in request.get('activeConsequences') or []:
        intent_payload = intent.get('payload') if is_record(intent.get('payload')) else {}
        strength = intent_payload.get('strength')
        active_consequences.append({
            'id': intent.get('id'),
            'participantId': intent.get('participantId'),
            'summary': intent.get('summary'),
            'startedAt': iso(intent.get('notBefore')),
            'effect': intent_payload.get('effect') if isinstance(intent_payload.get('effect'), str) else '',
            'strength': strength if isinstance(strength, (int, float)) and not isinstance(strength, bool) else 0.5,
            'expiresAt': intent_payload.get('expiresAt') if isinstance(intent_payload.get('expiresAt'), str) else '',
        })

    interrupted_outgoing_drafts = []
    superseded_delayed_replies = []
    for intent in request.get('supersededIntents') or []:
        if intent.get('type') == 'split-message':
            intent_payload = intent.get('payload') if is_record(intent.get('payload')) else {}
            content = intent_payload.get('content')
            content = content.strip()[:2000] if isinstance(content, str) else ''
            draft = {
                'participantId': intent.get('participantId'),
                'content': content,
                'narrativeContext': '主角本来想发送 ' + json.dumps(content, ensure_ascii=False)
                + '，但是还没打完字，用户的新消息就发来了。',
                'interruptedAt': iso(now),
            }
            if content:
                interrupted_outgoing_drafts.append(draft)
        else:
            superseded_delayed_replies.append({
                'participantId': intent.get('participantId'),
                'summary': intent.get('summary'),
                'notBefore': iso(intent.get('notBefore')),
                'payload': intent.get('payload'),
            })

    payload: Dict[str, Any] = {
        'phase': phase,
        'refreshContinuity': request.get('refreshContinuity') is True,
        'outputRecovery': request.get('outputRecovery') is True,
        'interval': {
            'from': iso(from_at), 'now': iso(now),
            'storyTimezone': now_local_context['timezone'],
            'fromLocal': from_local_context['local'],
            'nowLocal': now_local_context['local'],
            'fromLocalContext': from_local_context,
            'nowLocalContext': now_local_context,
            'elapsedSeconds': max(0, _js_round((now - from_at).total_seconds())),
        },
        'timelinePlan': ({
            'beats': [
                {'at': beat.get('at'), 'kind': beat.get('kind'), 'summary': beat.get('summary')}
                for beat in (timeline_plan.get('beats') or [])
            ],
            **({'carry': timeline_plan['carry']} if _js_length(timeline_plan.get('carry')) else {}),
        } if timeline_plan is not None else None),
        'timelineCarry': _mapped_or_none(request.get('timelineCarry'), lambda item: item[:240]),
        'setting': setting_payload,
        'state': story_state_for_prompt(story_state),
        'continuitySnapshot': continuity_snapshot,
        'continuitySnapshotAgeMinutes': continuity_snapshot_age_minutes,
        'emotionalOffset': request.get('emotionalOffset'),
        'agencyWindow': request.get('agencyWindow'),
        'schedulePreplan': request.get('schedulePreplan'),
        'automaticDeliverySummaries': automatic_delivery_summaries,
        'currentParticipant': (_participant_prompt_payload(participant, True, True)
                               if is_record(participant) else None),
        'participants': [
            _participant_prompt_payload(
                item,
                False,
                share_participant_details or (phase == 'advance' and request.get('agencyEnabled') is True),
            )
            for item in (request.get('participants') or [])
        ],
        'sceneContext': (request.get('sceneContext') if request.get('sceneContext') is not None
                         else {'scene': None, 'arc': None}),
        'currentEvent': current_event,
        'groupContext': group_context_payload,
        **({'chatCapabilities': chat_capabilities} if chat_capabilities is not None else {}),
        **({'stickerCatalog': sticker_catalog} if _js_length(sticker_catalog) else {}),
        'dueIntents': due_intents,
        'upcomingPlans': upcoming_plans,
        'followUpCommitments': follow_up_commitments,
        'activeConsequences': active_consequences,
        'contactThreads': request.get('contactThreads'),
        'workingDetails': _mapped_or_none(request.get('workingDetails'), lambda item: {
            'label': item.get('label'), 'value': item.get('value'),
            **({'expiresAt': item.get('expiresAt')} if item.get('expiresAt') else {}),
            'sourceEntryIds': item.get('sourceEntryIds'), 'recordedAt': item.get('createdAt'),
            'authority': 'last-known-detail',
            'knowledge': item.get('knowledge') if item.get('knowledge') is not None else {'mode': 'unclassified'},
        }),
        'developmentTendencies': (request.get('developmentTendencies')
                                  if _js_length(request.get('developmentTendencies')) else None),
        'recalledHistory': _mapped_or_none(request.get('recalledHistory'), lambda item: {
            'id': item.get('id'), 'occurredAt': item.get('occurredAt'),
            'content': item.get('content'), 'sourceEntryIds': item.get('sourceEntryIds'),
        }),
        'deliveryReality': delivery_reality(recent_entries, participant_id, bool(share_participant_details)),
        'interruptedOutgoingDrafts': interrupted_outgoing_drafts,
        'supersededDelayedReplies': superseded_delayed_replies,
        'memories': [
            {
                'participantId': memory.get('participantId'), 'category': memory.get('category'),
                'content': memory.get('content'), 'importance': memory.get('importance'),
                'sourceEntryId': memory.get('sourceEntryId'),
                'authority': 'derived-memory; original events and execution outcomes take precedence',
            }
            for memory in _compact_prompt_records(request.get('memories') or [], 6000)
        ],
        'durableFacts': [
            {
                **fact_evidence_for_prompt(fact),
                'participantId': fact.get('participantId'), 'scope': fact.get('scope'),
                'content': fact.get('content'), 'importance': fact.get('importance'),
                'confidence': fact.get('confidence'), 'sourceEntryIds': fact.get('sourceEntryIds'),
                'unresolved': fact.get('unresolved'),
            }
            for fact in _compact_prompt_records(request.get('facts') or [], 8000)
        ],
        'overlayEvolution': _compact_prompt_records([
            {
                'content': snapshot.get('summary'), 'target': snapshot.get('target'),
                'tier': snapshot.get('tier'), 'participantId': snapshot.get('participantId'),
                'periodStart': iso(snapshot.get('periodStart')), 'periodEnd': iso(snapshot.get('periodEnd')),
                'majorEvents': snapshot.get('majorEvents'),
            }
            for snapshot in (request.get('overlaySnapshots') or [])
        ], 8000),
        'webContext': [
            {
                'mode': observation.get('mode'), 'query': observation.get('query'),
                'url': observation.get('url'), 'title': observation.get('title'),
                'excerpt': observation.get('excerpt'), 'summary': observation.get('summary'),
                'status': observation.get('status'), 'accessedAt': iso(observation.get('accessedAt')),
            }
            for observation in _compact_prompt_records([
                # Reuse the generic budgeter without exposing a separate unbounded
                # copy of the same page text in the prompt payload.
                {**observation, 'content': observation.get('excerpt') or observation.get('summary')}
                for observation in (request.get('webContext') or [])
            ], 8000)
        ],
        # Keep the live request bounded even when old configurations contain very
        # high context limits.  Stored entries remain untouched; only the copy
        # sent over the wire is shortened.  This materially reduces both prompt
        # upload time and model prefill latency.
        'recentScript': [
            {
                'id': entry.get('id'),
                'participantId': entry.get('participantId'), 'kind': entry.get('kind'), 'actor': entry.get('actor'),
                'ownership': recent_script_ownership(entry),
                'content': prompt_visible_message_content(entry.get('content'), recent_script_ownership(entry)),
                **narrative_evidence(entry),
                'occurredAt': iso(entry.get('occurredAt')),
                'occurredAtLocal': story_local_time_context(entry.get('occurredAt'), store_timezone)['local'],
            }
            for entry in compact_prompt_entries(recent_entries, 24000, request.get('recentProtectionSince'))
        ],
    }
    if phase == 'user-message':
        payload['liveTimeBoundary'] = {
            'fromLocal': from_local_context['local'],
            'nowLocal': now_local_context['local'],
            'mustStopAtNow': True,
            'forbidFutureScheduleTransitions': True,
        }

    visible_entry_ids = {entry.get('id') for entry in payload['recentScript']}
    continuation = continuation_bookmark(
        [entry for entry in recent_entries if entry.get('id') in visible_entry_ids],
        from_at, now,
    )
    # Both orders carry the same semantic evidence and continuation bookmark.
    # Fields are grouped by mutation frequency so provider prefix caches can hit
    # across consecutive turns: the stable identity block and the append-only
    # history lead, per-turn fields close near the decision point.
    if not options.get('cacheFirst'):
        return compile_narrative_context(
            {**payload, 'continuation': continuation},
            request.get('sceneFrame'), request.get('dialogueBurst'),
        )

    # Compact script tags collapse the kind/actor/participantId triple into one
    # label; participantId is kept only when the history actually spans several
    # relationship branches (shared mode with details sharing).
    participant_ids = set()
    for entry in recent_entries:
        raw_participant_id = entry.get('participantId')
        text = '' if raw_participant_id is None else str(raw_participant_id)
        text = text.strip()
        if text:
            participant_ids.add(text)
    keep_participant_id = len(participant_ids) > 1
    cache_recent_script = []
    for entry in payload['recentScript']:
        cache_entry: Dict[str, Any] = {
            'id': entry.get('id'),
            'tag': compact_script_tag(entry.get('kind'), entry.get('actor')),
        }
        if keep_participant_id:
            cache_entry['participantId'] = entry.get('participantId')
        cache_entry['content'] = prompt_visible_message_content(entry.get('content'), recent_script_ownership(entry))
        if entry.get('timelineEvidence'):
            cache_entry['timelineEvidence'] = entry.get('timelineEvidence')
        if entry.get('narrativeAuthority'):
            cache_entry['narrativeAuthority'] = entry.get('narrativeAuthority')
            cache_entry['lifeHandoff'] = entry.get('lifeHandoff')
            cache_entry['proposedTimeline'] = entry.get('proposedTimeline')
        if entry.get('communicationOutcome'):
            cache_entry['communicationOutcome'] = entry.get('communicationOutcome')
        cache_entry['occurredAt'] = entry.get('occurredAt')
        cache_entry['occurredAtLocal'] = entry.get('occurredAtLocal')
        cache_recent_script.append(cache_entry)

    cache_payload: Dict[str, Any] = {**payload}
    # —— 缓存稳定区（变异频率升序）——
    cache_payload['setting'] = payload['setting']
    cache_payload['recentScript'] = cache_recent_script
    cache_payload['durableFacts'] = payload['durableFacts']
    cache_payload['memories'] = payload['memories']
    cache_payload['overlayEvolution'] = payload['overlayEvolution']
    if _js_length(sticker_catalog):
        cache_payload['stickerCatalog'] = payload.get('stickerCatalog')
    cache_payload['sceneContext'] = payload['sceneContext']
    cache_payload['continuitySnapshot'] = payload['continuitySnapshot']
    cache_payload['workingDetails'] = payload['workingDetails']
    cache_payload['schedulePreplan'] = payload['schedulePreplan']
    cache_payload['webContext'] = payload['webContext']
    # —— 每轮变化区（越靠后越接近生成点）——
    cache_payload['currentParticipant'] = payload['currentParticipant']
    cache_payload['participants'] = payload['participants']
    cache_payload['state'] = payload['state']
    cache_payload['emotionalOffset'] = payload['emotionalOffset']
    cache_payload['agencyWindow'] = payload['agencyWindow']
    cache_payload['automaticDeliverySummaries'] = payload['automaticDeliverySummaries']
    cache_payload['followUpCommitments'] = payload['followUpCommitments']
    cache_payload['dueIntents'] = payload['dueIntents']
    cache_payload['upcomingPlans'] = payload['upcomingPlans']
    cache_payload['activeConsequences'] = payload['activeConsequences']
    cache_payload['interruptedOutgoingDrafts'] = payload['interruptedOutgoingDrafts']
    cache_payload['supersededDelayedReplies'] = payload['supersededDelayedReplies']
    cache_payload['groupContext'] = payload['groupContext']
    if chat_capabilities is not None:
        cache_payload['chatCapabilities'] = payload.get('chatCapabilities')
    cache_payload['phase'] = payload['phase']
    cache_payload['refreshContinuity'] = payload['refreshContinuity']
    cache_payload['outputRecovery'] = payload['outputRecovery']
    cache_payload['interval'] = payload['interval']
    cache_payload['continuation'] = continuation
    cache_payload['timelinePlan'] = payload['timelinePlan']
    cache_payload['timelineCarry'] = payload['timelineCarry']
    cache_payload['continuitySnapshotAgeMinutes'] = payload['continuitySnapshotAgeMinutes']
    cache_payload['recalledHistory'] = payload['recalledHistory']
    cache_payload['deliveryReality'] = payload['deliveryReality']
    cache_payload['developmentTendencies'] = payload['developmentTendencies']
    cache_payload['currentEvent'] = payload['currentEvent']
    cache_payload['recentExchange'] = _build_recent_exchange(request)
    if payload.get('liveTimeBoundary'):
        cache_payload['liveTimeBoundary'] = payload['liveTimeBoundary']
    return compile_narrative_context(cache_payload, request.get('sceneFrame'), request.get('dialogueBurst'))


# ========== 上游私有：alterAnalysisPrompt ==========

def _alter_analysis_prompt(custom_prompt: str = '') -> str:
    """上游私有 alterAnalysisPrompt：低频氛围分析器的固定契约 + 用户自定义补充。"""
    return '\n'.join([
        # >>> TS:alterAnalysisHead 1973-1978
        'You are the low-frequency atmosphere analyst for a long-running life narrative.',
        'Return exactly one JSON object: {"description":"one or two concise sentences"}.',
        'Describe the newly established overall atmosphere shift as a bounded present condition: its concrete cause in the recent life, what it changes in energy, attention, pace, ease or reserve, and how later events may naturally supersede it.',
        'The description is temporary narrative context, not a speaking instruction, personality rewrite, relationship verdict, character label, or fixed style template.',
        'Use scene conditions and changed stakes rather than recurring banter, reply forms, archetypes, or a prediction of what either person will say next. Do not include names, quotations, private message details, suggested wording, or claims unsupported by the scripts.',
        'Do not decide direction or intensity; those are calculated by the plugin.',
        # <<< TS:alterAnalysisHead
        _trimmed_or(custom_prompt, 'Keep the description open, concrete, and suitable for natural continuation.'),
    ])


# ========== 上游私有：compactionPrompt ==========

def _compaction_prompt(fixed_prompt: Any, compaction_main_prompt: Any = '', compaction_fixed_prompt: Any = '',
                       compaction_style_prompt: Any = '') -> str:
    """上游私有 compactionPrompt：低成本连续性编辑器的完整契约（长 prompt，逐字复制）。"""
    return '\n'.join([
        # >>> TS:compactionMain 1985-2000
        'You are the low-cost continuity editor for HDS Interlude.',
        'Compress only events that have already happened. Never invent future events.',
        'Return JSON with scene.summary and arc.summary on every review; facts and statePatches are optional. If the arc has not changed, carry its established summary forward.',
        '{"scene":{"hook":"short active-scene hook","summary":"compact scene summary","close":false,"boundary":{"reason":"explicit structural transition","sourceEntryIds":[1]},"presence":[{"name":"named supporting character","status":"present|off-scene|expected","basis":"explicit observed transition","sourceEntryIds":[1]}]},"arc":{"title":"...","summary":"..."},"facts":[{"scope":"character|world|relationship|event|promise","participantId":"optional relationship id","content":"...","importance":0.0,"confidence":0.0,"unresolved":false,"sourceEntryIds":[1],"resolvesFactIds":[12]}],"statePatches":[{"target":"character|perspective|world|relationship","participantId":"relationship id when target is relationship","path":"...","proposedValue":"...","evidence":"...","confidence":0.0,"impact":"minor|major","sourceEntryIds":[1]}],"workingDetails":[{"label":"short label","value":"concrete detail","expiresAt":"future ISO-8601 or omit","sourceEntryIds":[1]}]}',
        'workingDetails capture only small concrete present-state details from the supplied entries (pickup codes, orders, errands, tiny pending promises) that do not warrant a durable fact. Carry the same matter forward under its existing label, with newer sourceEntryIds and the current literal value. If a clearer label is useful, replacesLabel may name exactly one existing label for the SAME participant and matter; supply observed/reported knowledge with exact source clauses showing the transition. Keep distinct matters separate. Preserve conditions and the difference between a wish and an observed state. Never store a future checkpoint, prediction, hoped-for outcome, planned inspection or unobserved deadline as a workingDetail. Do not duplicate durable facts.',
        'New entries labelled original-v2 are the committed original; proposedTimeline is only the preceding plan. Read lifeHandoff as quotes into that original. Older timelineEvidence bounds legacy automatic passages. Actual incoming messages and deliveryReality decide communication, including no-outgoing-action-recorded: a narrative mention of sending alone does not establish a sent message. Distinguish another person’s dated report from the protagonist’s ongoing guess.',
        'Facts must be durable and non-redundant. Set participantId for relationship-specific facts; leave it empty for world-wide facts. Use unresolved=true only while a promise or concrete open matter is genuinely pending. When supplied entries fulfill, cancel or otherwise close an existing unresolved fact, include its visible id in resolvesFactIds and describe the completed outcome in the new fact. State patches are proposals, not direct rewrites. Use them only for a gradual, durable personality, perspective, world, or relationship change supported by repeated behavior across separate narrative turns. perspective is the protagonist’s separate individual values and way of seeing the world; propose it only for a sustained change in how she naturally understands people or events, never for a mood, theme, moral lesson, or one isolated choice. Keep the same target/path/proposedValue when the same change is observed again so the host can accumulate evidence.',
        'scene.presence is a tiny current-scene roster, not a cast list. Omit it unless supplied entries explicitly show a named supporting character arriving, being present, leaving, or expected later. Each update needs sourceEntryIds and a concrete basis. A Canon character is available to the story but is not automatically present in the current scene. Never infer a goodbye, departure, arrival, or reunion from mood, omission, or convenience.',
        'Set scene.close=true only for a structural boundary explicitly present in the supplied entries, and include scene.boundary with its reason and sourceEntryIds. Elapsed time, message count, prose rhythm, or a convenient summary ending are not scene boundaries.',
        'Read precedingEntries as original-script context before the checkpoint, and entries as the new chronological evidence. Continue the existing arc from these passages: preserve the initiating cause, consequential choices, relationship changes and unresolved commitments with their exact conditions. Update outcomes only where new evidence settles them. The arc is an index of established causality that helps return to original text, not a future plot assignment or a style model.',
        'After completing scene and arc summaries, optionally return episodeTags:[{sourceEntryId,people:[],places:[],objects:[],topics:[],commitments:[],outcomes:[],dates:[]}]. Select up to three eventful source entries and a few useful tags, omitting empty categories. Each tag is a short exact substring of that source entry. These are navigation labels for finding the original passage, not assertions that a plan was fulfilled.',
        'Development uses only these target/path pairs: character/traits|preferences|coping; perspective/values|interpretation; relationship/trust|closeness|boundaries; world/established. Propose a concise, conditional tendency rooted in a repeatable choice, boundary, practical coordination, or explicitly received support, preserving exceptions. Each scene contributes once; repeated wording or many chat turns is one observation. A response pattern, teasing routine, pet name, prose cadence, or temporary emotional weather is evidence about this scene, not a development tendency. existingDevelopmentCandidates are sourced observations, not Canon. Cite contradictsProposalIds with new sourceEntryIds only when observed behavior actually contradicts the same claim in comparable circumstances, keeping its target/path. A mood or contextual exception is not a contradiction. A supported contradiction lowers confidence and retires that tendency from projection; future support starts a new observation cycle.',
        'For relationship development, read each interactionEvidence chain as prior speech -> actual user feedback -> her interpretation -> actual response. Her interpretation is not the user’s endorsement. An explicit objection changes what that interaction supports; preserve its literal meaning even if she initially misunderstands it. Include interactionReview:{outcome:"supported|contested|unresolved",feedbackEntryIds:[actual user ids],responseEntryIds:[actual sent-message ids]} and include those ids in sourceEntryIds. Choose unresolved when reception is absent. Learn the adjustment or boundary where supported, rather than converting protest into proof of closeness. Existing candidates must be reconsidered against feedback before receiving more support.',
        'deliveryReality describes execution of the protagonist’s outgoing actions. Delivered means platform acceptance, not reading or agreement. Pending, failed and cancelled actions do not establish receipt. Preserve an unfulfilled promise as open and separate a planned action from its observed result. workingDetails may use resolved:true with the same label and sourceEntryIds when an action has actually ended.',
        'Actively review scene boundaries when the original script establishes departure, arrival, a completed activity followed by another, or an explicit end to a relationship encounter. Summarize the full supplied increment and close at its final entry when the earlier scene has given way to a new situation; cite the observed transition. A scene closure advances the existing arc rather than restarting it. Supply a concrete arc title once its central ongoing concern is evident.',
        'When schedulePreplanReview is supplied, also review the protagonist\'s Schedule Preplan. Return schedulePreplan with outcome unchanged|extend|patch|replace, a concise reason, confidence, sourceEntryIds, and only the regimes/exceptions needed by that outcome. A regime is {"id":"stable-id","label":"life phase","from":"YYYY-MM-DD","to":"optional YYYY-MM-DD","weekly":{"monday":[{"id":"stable-block-id","start":"HH:mm","end":"HH:mm","label":"planned activity","kind":"fixed|routine|flexible|open","location":"optional","sourceEntryIds":[1]}]},"sourceEntryIds":[1]}. An exception is {"date":"YYYY-MM-DD","mode":"replace|patch","reason":"...","removeBlockIds":[],"blocks":[],"sourceEntryIds":[1]}. When schedulePreplanReview.current is null, create the initial plan: return outcome=replace with regimes derived strictly from the evidence entries, or an empty regimes array when the entries establish no concrete structure — always return the schedulePreplan field. Keep the current plan unchanged unless evidence establishes a real change or its horizon needs extension. Plans are not completed events. Do not invent school dates, lessons or obligations; flexible hobbies remain flexible.',
        # <<< TS:compactionMain
        KNOWLEDGE_WRITING_FRAME,
        # >>> TS:compactionKnowledge 2002
        'For each fact and workingDetail add knowledge:{mode:"observed|reported|belief|proposal|conditional|confirmed",holder:"protagonist or reporting participant id when relevant",topic:"short literal topic from a quoted source",clauses:[{role:"observation|interpretation|proposal|condition|confirmation",sourceEntryId:1,quote:"exact original words"}],relatedFactIds:[existing fact ids about this same matter]}. Preserve the speaker, modality and conditions in content itself: "wants to" stays an intention, not a promise. A belief is valuable character continuity, attributed to its holder, not an external outcome. A confirmation cites the actual proposal and the later explicit reply from the other speaker; an imagined reply, a teasing response or silence belongs to interpretation, not acceptance. Confirmation retains conditions unless an actual exchange changed them. Link a new proposal to existing conditions through relatedFactIds, even when they were recorded in an earlier scene. Keep existing uncertain records uncertain; repeated narration is not new corroboration. Only use resolvesFactIds for an evidenced completion or explicit withdrawal, never merely because somebody now hopes for a different outcome.',
        # <<< TS:compactionKnowledge
        'COMPACTION MAIN PROMPT (user-configurable):',
        _trimmed_or(compaction_main_prompt, 'Compress completed scenes into concise continuity notes while preserving causality, promises, unresolved matters, and gradual character change.'),
        'ADDITIONAL FIXED INSTRUCTIONS:',
        _trimmed_or(fixed_prompt, 'None.'),
        'COMPACTION-SPECIFIC FIXED INSTRUCTIONS:',
        _trimmed_or(compaction_fixed_prompt, 'None.'),
        'COMPACTION WRITING STYLE (applies only to summaries, not to the main script):',
        _trimmed_or(compaction_style_prompt, 'Concise, factual, chronological, and concrete.'),
    ])


# ========== 上游私有：schedulePreplanPrompt ==========

def _schedule_preplan_prompt(variation_level: str) -> str:
    """上游私有 schedulePreplanPrompt：Preplan 调用的唯一职责（刻意收窄的契约）。

    刻意独立于 scene/fact 压缩，避免小模型在写完长摘要后静默漏掉深层 schedule 字段。
    """
    if variation_level == 'stable':
        # >>> TS:schedulePreplanStable 2022 -> variation_instruction
        variation_instruction = 'Variation level is stable. Keep only the repeating backbone. Do not return tentative blocks.'
        # <<< TS:schedulePreplanStable
    elif variation_level == 'contextual':
        # >>> TS:schedulePreplanContextual 2024 -> variation_instruction
        variation_instruction = 'Variation level is contextual. Preserve evidence-backed life-stage boundaries and near dated exceptions. Do not return tentative blocks.'
        # <<< TS:schedulePreplanContextual
    else:
        # >>> TS:schedulePreplanGranular 2025 -> variation_instruction
        variation_instruction = 'Variation level is granular. You may mark a small number of evidence-backed flexible or open blocks with tentative:true when they represent a plausible variation, not a confirmed event. Never make fixed or routine blocks tentative, and never use tentative to invent people, appointments, or outcomes.'
        # <<< TS:schedulePreplanGranular
    return '\n'.join([
        # >>> TS:schedulePreplanHead 2015-2020
        'You maintain a small, factual Schedule Preplan for one protagonist.',
        'Return exactly one JSON object and no Markdown. The object itself must have outcome, reason, confidence, sourceEntryIds, regimes, and exceptions.',
        'outcome is one of unchanged, extend, patch, replace. For an initial plan use replace. If the evidence proves no recurring structure, use replace with regimes:[] and exceptions:[]; this is a valid answer.',
        'Use only stable, explicitly observed recurring commitments or routines from evidence: school, work, regular lessons, fixed trips, or clearly repeated habits. Do not infer a timetable from one ordinary scene. Do not invent school dates, lessons, obligations, locations, or future events.',
        'A regime is {"id":"stable-id","label":"life phase","from":"YYYY-MM-DD","to":"optional YYYY-MM-DD","weekly":{"monday":[{"id":"stable-block-id","start":"HH:mm","end":"HH:mm","label":"planned activity","kind":"fixed|routine|flexible|open","location":"optional","sourceEntryIds":[1]}]},"sourceEntryIds":[1]}. Use only weekday keys that have evidence.',
        'An exception is {"date":"YYYY-MM-DD","mode":"replace|patch","reason":"...","removeBlockIds":[],"blocks":[],"sourceEntryIds":[1]}. Keep it empty unless evidence proves a date-specific change.',
        # <<< TS:schedulePreplanHead
        variation_instruction,
        # >>> TS:schedulePreplanTail 2026
        'The plan is a forecast of structure, never proof that an activity happened. Prefer an empty valid plan to a guessed plan.',
        # <<< TS:schedulePreplanTail
    ])


# ========== 上游私有：timelineDirectorPrompt ==========

def _timeline_director_prompt() -> str:
    """上游私有 timelineDirectorPrompt：只规划相对时间结构的导演契约。"""
    return '\n'.join([
        # >>> TS:timelineDirector 2032-2038
        'You are the timeline director for an automatic narrative window. You plan only relative time structure; the main author writes all prose.',
        'Return JSON only: {"beats":[{"at":0.0,"kind":"activity|thought|state","summary":"short factual Chinese movement"}],"carry":["optional short unresolved current-state note"]}. Keep the whole JSON small.',
        'The host owns time. Every beat is a relative position inside interval.from through interval.now: at=0 is the start and at=1 is the end. Never create an event after interval.now, never skip to a later class, meal, appointment, reply, or notification, and never turn a future hope into an event.',
        'Report objective time facts and possible time logic - never deterministic predictions. State what is established (schedule blocks, ongoing activity, rest windows, elapsed time, tiredness, an early commitment) and how it plausibly moves: tired or a free evening may mean longer sleep; something scheduled early next day may mean shorter sleep. Do NOT assert any fixed wake-up, completion, or arrival time as settled fact; sleep and open activities may end anywhere inside this window.',
        'Incoming user messages are objective arrival facts only. Whether they reach, disturb, or wake the protagonist is NOT yours to decide - leave that open for the main author, who judges from her established state. Never create beats like being woken by messages; just let the window facts carry their arrival times.',
        'Use 1-4 beats. Describe only what can naturally occur inside this exact window. Due intents and schedule blocks are constraints, not permission to invent their completion. carry records a present unresolved condition only; no future plans, deadlines, or predictions.',
        'recentScriptContinuation is the tail of the latest original-script handoff; preserve its concrete endpoint and unfinished movement. hostTimelineLedger, when present, constrains legacy history only. Your beats are a proposal the main author renders and may adjust to the established original.',
        # <<< TS:timelineDirector
    ])


# ========== 上游私有：toTimelinePlanPayload ==========

def _to_timeline_plan_payload(request: TimelinePlanRequest) -> Dict[str, Any]:
    """上游私有 toTimelinePlanPayload：只给导演结构信号，不给全文。"""
    story = request.get('story') if is_record(request.get('story')) else {}
    setting = story.get('setting') if is_record(story.get('setting')) else {}
    participant = request.get('participant')
    scene = request.get('scene')
    continuation = request.get('recentScriptContinuation')
    recent_entries = request.get('recentEntries') or []
    return {
        'interval': {
            'from': iso(request.get('from')), 'now': iso(request.get('now')),
            'timezone': setting.get('timezone'),
        },
        'phase': request.get('phase'),
        'currentParticipant': ({'id': participant.get('id'), 'displayName': participant.get('displayName')}
                               if is_record(participant) else None),
        'activeScene': ({'hook': scene.get('hook'), 'summary': scene.get('summary')}
                        if is_record(scene) else None),
        'schedule': request.get('schedulePreplan'),
        'dueIntents': [
            {'type': intent.get('type'), 'summary': intent.get('summary'), 'notBefore': iso(intent.get('notBefore'))}
            for intent in (request.get('dueIntents') or [])
        ],
        'facts': [fact_evidence_for_prompt(fact) for fact in (request.get('facts') or [])[:8]],
        'recalledHistory': _mapped_or_none(request.get('recalledHistory'), lambda item: {
            **item, 'authority': 'historical-original-excerpt; not a new event or current confirmation',
        }),
        'contactThreads': request.get('contactThreads'),
        # 结构信号而非全文：导演只需要知道窗口里发生过什么、何时发生；
        # 内容渲染是主作者的职责。条目取尾部短投影，剧本续写只留末段。
        'recentEntries': [
            {
                'id': entry.get('id'), 'kind': entry.get('kind'), 'actor': entry.get('actor'),
                'content': (entry.get('content') if isinstance(entry.get('content'), str) else '')[-200:],
                **narrative_evidence(entry),
                'occurredAt': iso(entry.get('occurredAt')),
            }
            for entry in recent_entries[-6:]
        ],
        'deliveryReality': delivery_reality(
            recent_entries, participant.get('id') if is_record(participant) else None, False,
        ),
        'recentScriptContinuation': ({
            'content': (continuation.get('content') if isinstance(continuation.get('content'), str) else '')[-600:],
            'occurredAt': iso(continuation.get('occurredAt')),
            **({'hostTimelineLedger': continuation['hostTimelineLedger']}
               if continuation.get('hostTimelineLedger') else {}),
        } if continuation is not None else None),
    }


# ========== 上游私有：overlayCompactionPrompt ==========

def _overlay_compaction_prompt(fixed_prompt: Any, compaction_fixed_prompt: Any = '',
                               compaction_style_prompt: Any = '') -> str:
    """上游私有 overlayCompactionPrompt：旧设定演化的短期/长期压缩契约。"""
    return '\n'.join([
        # >>> TS:overlayCompactionHead 2072-2075
        'You are a continuity editor compressing older setting evolution for HDS Interlude.',
        'All supplied changes already happened. Preserve their present effect, causal evolution, explicit major events, and unresolved consequences. Do not invent events.',
        'Return JSON only: {"summary":"concise current-state evolution","majorEvents":["important enduring event or turning point"]}.',
        'Short-window compression keeps concrete progression and causes. Long-window compression keeps stable current state and major turning points while merging repetitive detail.',
        # <<< TS:overlayCompactionHead
        'FIXED INSTRUCTIONS:',
        _trimmed_or(fixed_prompt, 'None.'),
        'COMPACTION FIXED INSTRUCTIONS:',
        _trimmed_or(compaction_fixed_prompt, 'None.'),
        'SUMMARY STYLE:',
        _trimmed_or(compaction_style_prompt, 'Concise, factual, chronological, and concrete.'),
    ])


# ========== 上游私有：toOverlayCompactionPayload ==========

def _to_overlay_compaction_payload(request: OverlayCompactionRequest) -> Dict[str, Any]:
    """上游私有 toOverlayCompactionPayload：canon 按 target 分支取对应设定。"""
    story = request.get('story') if is_record(request.get('story')) else {}
    setting = story.get('setting') if is_record(story.get('setting')) else {}
    character = setting.get('character') if is_record(setting.get('character')) else {}
    participant = request.get('participant')
    target = request.get('target')
    if target == 'character':
        canon = character.get('profile')
    elif target == 'perspective':
        canon = setting.get('perspective')
    elif target == 'world':
        canon = setting.get('world')
    else:
        # 上游 `request.participant?.relationship || request.story.setting.relationship`
        canon = (participant.get('relationship') if is_record(participant) else None) or setting.get('relationship')
    return {
        'tier': request.get('tier'), 'target': target,
        'participantId': (participant.get('id') if is_record(participant) else None) or '',
        'period': {'from': iso(request.get('from')), 'to': iso(request.get('to'))},
        'canon': canon,
        'patches': [
            {
                'id': patch.get('id'), 'value': patch.get('proposedValue'),
                'evidence': patch.get('evidence'), 'impact': patch.get('impact'),
                'appliedAt': iso(patch.get('appliedAt')),
            }
            for patch in (request.get('patches') or [])
        ],
        'earlierSnapshots': [
            {
                'summary': snapshot.get('summary'), 'majorEvents': snapshot.get('majorEvents'),
                'periodEnd': iso(snapshot.get('periodEnd')),
            }
            for snapshot in (request.get('snapshots') or [])
        ],
    }


# ========== 上游私有：toCompactionPayload ==========

def _to_compaction_payload(request: CompactionRequest) -> Dict[str, Any]:
    """上游私有 toCompactionPayload：压缩器的 user 侧 payload。"""
    story = request.get('story') if is_record(request.get('story')) else {}
    state = story.get('state') if is_record(story.get('state')) else {}
    preceding_entries = request.get('precedingEntries') or []
    entries = request.get('entries') or []
    schedule_preplan = request.get('schedulePreplan')
    development_candidates = request.get('developmentCandidates')
    payload: Dict[str, Any] = {
        'interval': {'from': iso(request.get('from')), 'now': iso(request.get('now'))},
        'setting': {
            **(story.get('setting') if is_record(story.get('setting')) else {}),
            'user': {'displayName': 'Multiple participants', 'profile': ''},
            'relationship': '',
        },
        'evolvingState': story_state_for_prompt(state),
        'existingWorkingDetails': state.get('workingDetails') or [],
        'scene': request.get('scene'),
        'arc': request.get('arc'),
        'existingDevelopmentCandidates': [
            {
                'id': item.get('id'), 'status': item.get('status'), 'target': item.get('target'),
                'path': item.get('path'), 'participantId': item.get('participantId'),
                'proposedValue': (item.get('proposedValue') if isinstance(item.get('proposedValue'), str) else '')[:300],
                'confidence': item.get('confidence'),
                'sourceEntryIds': (item.get('sourceEntryIds') or [])[-12:],
            }
            for item in (development_candidates or [])[:12]
        ] if development_candidates is not None else None,
        # 上游传 undefined 与 Infinity：这里分别落成 None 与一个足够大的 limit（slice(-limit) 全取）。
        'deliveryReality': delivery_reality([*preceding_entries, *entries], None, True, _JS_MAX_INDEX),
        'interactionEvidence': interaction_evidence([*preceding_entries, *entries]),
        'precedingEntries': [
            {
                'id': entry.get('id'), 'kind': entry.get('kind'), 'actor': entry.get('actor'),
                'participantId': entry.get('participantId'), 'content': entry.get('content'),
                'occurredAt': iso(entry.get('occurredAt')),
            }
            for entry in preceding_entries
        ],
        'participants': [_participant_prompt_payload(item, False) for item in (request.get('participants') or [])],
        'existingFacts': [
            {**fact_evidence_for_prompt(fact), 'importance': fact.get('importance'), 'confidence': fact.get('confidence')}
            for fact in (request.get('facts') or [])
        ],
        'entries': [
            {
                'id': entry.get('id'), 'participantId': entry.get('participantId'),
                'kind': entry.get('kind'), 'actor': entry.get('actor'), 'content': entry.get('content'),
                'occurredAt': iso(entry.get('occurredAt')),
                **narrative_evidence(entry),
            }
            for entry in entries
        ],
    }
    if schedule_preplan is not None:
        # 上游此处写 `: undefined`（JSON.stringify 后 key 消失），只有存在时才写入。
        current = schedule_preplan.get('current')
        payload['schedulePreplanReview'] = {
            'localDate': schedule_preplan.get('localDate'),
            'horizonDays': schedule_preplan.get('horizonDays'),
            'current': ({
                'revision': current.get('revision'), 'timezone': current.get('timezone'),
                'validFrom': current.get('validFrom'), 'validThrough': current.get('validThrough'),
                'regimes': current.get('regimes'), 'exceptions': current.get('exceptions'),
                'reviewReason': current.get('reviewReason'),
            } if is_record(current) else None),
            'evidenceEntries': [
                {
                    'id': entry.get('id'), 'kind': entry.get('kind'), 'actor': entry.get('actor'),
                    'content': entry.get('content'), 'occurredAt': iso(entry.get('occurredAt')),
                }
                for entry in (schedule_preplan.get('evidenceEntries') or [])
            ],
        }
    return payload


# ========== 上游私有：toSchedulePreplanPayload ==========

def _to_schedule_preplan_payload(request: SchedulePreplanReviewRequest) -> Dict[str, Any]:
    """上游私有 toSchedulePreplanPayload：schedule 证据刻意收敛到最近 30 条、每条 900 字。"""
    current = request.get('current')
    return {
        'localDate': request.get('localDate'),
        'horizonDays': request.get('horizonDays'),
        'variationLevel': request.get('variationLevel') if request.get('variationLevel') is not None else 'stable',
        'current': ({
            'revision': current.get('revision'), 'timezone': current.get('timezone'),
            'validFrom': current.get('validFrom'), 'validThrough': current.get('validThrough'),
            'regimes': current.get('regimes'), 'exceptions': current.get('exceptions'),
            'reviewReason': current.get('reviewReason'),
        } if is_record(current) else None),
        # Schedule evidence is intentionally bounded. It needs concrete anchors,
        # not full prose history; retaining the newest 30 preserves timeliness.
        'evidenceEntries': [
            {
                'id': entry.get('id'),
                'occurredAt': iso(entry.get('occurredAt')),
                'content': (entry.get('content') if isinstance(entry.get('content'), str) else '')[:900],
                **narrative_evidence(entry),
            }
            for entry in (request.get('evidenceEntries') or [])[-30:]
        ],
    }


# ---------------------------------------------------------------------------
# 跨模块兼容别名：上游这些私有函数只被 narrator.ts 同文件的 provider 调用；
# 本包把 provider 拆到 hdsi/narrator_providers.py 后，同名 helper 需要靠公开名字导入。
# （hdsi/ 内不允许 `from x import _y` 风格，因此这里提供不带下划线的别名。）
# ---------------------------------------------------------------------------

alter_analysis_prompt = _alter_analysis_prompt
compaction_prompt = _compaction_prompt
schedule_preplan_prompt = _schedule_preplan_prompt
timeline_director_prompt = _timeline_director_prompt
to_timeline_plan_payload = _to_timeline_plan_payload
overlay_compaction_prompt = _overlay_compaction_prompt
to_overlay_compaction_payload = _to_overlay_compaction_payload
to_compaction_payload = _to_compaction_payload
to_schedule_preplan_payload = _to_schedule_preplan_payload
participant_prompt_payload = _participant_prompt_payload
compact_prompt_records = _compact_prompt_records
build_recent_exchange = _build_recent_exchange
parse_date = _parse_date

