# -*- coding: utf-8 -*-
"""决策落库（persistDecision）与 Alter 分析、脚本/记忆追加、contactThreads / facts。

上游 `.hdsi_reference/src/service.ts`（HDS-Interlude 1.0.1-beta6-rebuild）行号区间：
3833-4393，具体为
  persistDecision             3833-4181
  persistTimelineSceneAnchor  4186-4195
  adminSchedulePreplan        4197-4199
  requestSchedulePreplanRebuild 4201-4209
  emotionalOffsetForPrompt    4227-4229
  updateAlterSystem           4231-4246
  scheduleAlterAnalysis       4248-4257
  analyzeAlterSystem          4259-4310
  appendEntry                 4312-4334
  appendMemory                4336-4342
  contactThreads              4350-4391
以及 facts 的完整实现（4393-4444，本 mixin 方法列表要求交付）。

本文件同时移植上述区间调用、但上游未 export 的私有 helper（service.ts 173、7692-8257 等：
RECALLABLE_ENTRY_KINDS / signedNumber / isAutomaticNarrativePhase / normalizeDecision /
validMemory / validIntent / normalizeIntentUpdates / normalizeBrowserIntentDraftLoose /
normalizeConversationAction / permittedOrGlobal / pickParticipantStatePatch /
normalizeAutomaticDeliverySummary / normalizeFollowUpCommitment / inferredFollowUpCommitment /
interactionPromisesFollowUp / normalizeFollowUpResolutions / factScore / lexicalRecallKeys /
cosineSimilarity，以及 turn-persistence.ts 的 scriptEntryDraftForCommit）。
`normalizeGroupVisibleReply` / `historyLexicalScore` / `normalizeInteraction` 属上游 export，
按 SERVICE_PORTING_SPEC 由 hdsi/service_helpers.py 提供，未就绪时回退本文件内联副本。

约定：同步方法、Python 3.9、字段名 camelCase、日志与文案逐字保留；
`Date` → `datetime`（UTC aware），JS 的 `Math.round` / `Number.isInteger` / `toFixed` /
真值语义（空 dict、空数组为真）用文件内私有 helper 复刻。
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Set

from .agency import (
    active_agency_window,
    evaluate_agency_capacity,
    normalize_agency_window_draft,
    normalize_proactive_contact,
    proactive_recheck_at,
)
from .alter import (
    advance_alter_system,
    alter_history_for_scope,
    alter_scope_cooling_down,
    alter_scope_value,
    calculate_alter_threshold,
    complete_alter_analysis,
    mark_alter_scope_analysis_attempt,
    normalize_alter_system_state,
    normalize_alter_value,
)
from .alter import emotional_offset_for_prompt as _alter_emotional_offset_for_prompt
from .delivery import attach_message_event, prepare_outgoing_delivery, script_event_payload
from .narrator_prompts import prompt_visible_message_content, recent_script_ownership
from .script.authored_actions import resolve_authored_actions
from .script.commit_builder import (
    decision_to_script_commit,
    find_outgoing_script_event,
    unbound_immediate_message_events,
)
from .script.episode_index import grounded_episode_tags
from .script.knowledge_evidence import (
    contact_evidence_threads,
    knowledge_clauses,
    knowledge_related_ids,
    legacy_condition_cue,
)
from .script.life_handoff import normalize_life_handoff
from .script.scene_frame import advance_scene_frame, project_scene_frame, resolve_dialogue_burst
from .script.validator import validate_script_commit
from .store import normalize_database_row
from .story_state import decode_story_state, encode_story_state, normalize_continuity_snapshot
from .time_utils import format_log_time, iso, now_utc, parse_time
from .turn_persistence import script_entry_draft_for_commit
from .types import (
    EmotionalOffsetPrompt,
    InterludeParticipant,
    InterludeStory,
    NarrativeDecision,
    NarrativeProvider,
    ScriptEntry,
    TimelinePlan,
)
from .urge import commit_urge, normalize_urge_state, urge_burst_active
from .utils import clamp_number, clip

__all__ = ['ServiceDecisionMixin']

# 上游 service.ts 173：可进入语义史官检索的条目类型（hdsi/service_media.py 有同内容常量）。
_RECALLABLE_ENTRY_KINDS = [
    'user-message', 'character-message', 'script', 'group-message', 'character-group-message',
]

# 上游 service.ts 7763 起私有 helper 用到的固定枚举。
_FOLLOW_UP_KINDS = ('thinking', 'checking', 'decision', 'emotional-settle')
_FOLLOW_UP_OUTCOMES = ('fulfilled', 'rescheduled', 'cancelled')


# ===========================================================================
# JS 语义小 helper（Math / String / 真值 / 时间）
# ===========================================================================

def _is_record(value: Any) -> bool:
    """TS: isRecord(value)。

    JS 里 `!!{}` 为 true；utils.is_record 用 bool(value) 判断，对空 dict 会返回 False，
    而 upstream 多处用 isRecord 判定模型 JSON 字段是否“提供了对象”，因此单独复刻。
    """
    return isinstance(value, dict)


def _js_truthy(value: Any) -> bool:
    """TS 真值语义：null/undefined/false/0/''/NaN 为假；空 dict、空数组为真。"""
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return len(value) > 0
    return True


def _js_string(value: Any) -> str:
    """TS: String(value ?? '')（只覆盖本项目实际会遇到的标量）。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    return str(value)


def _is_js_integer(value: Any) -> bool:
    """TS: Number.isInteger(value)（1.0 也算整数，true/false 不算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number.is_integer()


def _is_safe_integer(value: Any) -> bool:
    """TS: Number.isSafeInteger(value)。"""
    return _is_js_integer(value) and abs(float(value)) <= 9007199254740991


def _number_or(value: Any, fallback: float) -> float:
    """TS: finiteNumber(value, fallback) 的宽松版本：非有限数值回落 fallback。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if math.isnan(number) or math.isinf(number):
        return fallback
    return number


def _js_to_fixed(value: Any, digits: int) -> str:
    """JS Number.prototype.toFixed：按精确值的 half-up 四舍五入（Python format 是 half-even）。"""
    number = _number_or(value, 0.0)
    if number == 0:
        number = 0.0
    quantizer = Decimal(1).scaleb(-digits)
    return format(Decimal(number).quantize(quantizer, rounding=ROUND_HALF_UP), 'f')


def _time_ms(value: Any) -> float:
    """TS: Date.prototype.getTime()：epoch 毫秒；非法/缺失按 0（调用点都已先校验）。"""
    parsed = value if isinstance(value, datetime) else parse_time(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp() * 1000.0


def _first_defined(*values: Any) -> Any:
    """TS: `a ?? b ?? c`：只跳过 None。"""
    for value in values:
        if value is not None:
            return value
    return None


def _resolved_runtime(runtime: Any) -> Dict[str, Any]:
    """补齐 runtime 里被上游 Schema 保证存在的字段，避免最小配置下取到 None。"""
    source = runtime if isinstance(runtime, dict) else {}
    separator = source.get('messageSeparator')
    return {
        **source,
        'messageSeparator': '<sep/>' if separator is None else separator,
        'maxScriptCharacters': _number_or(source.get('maxScriptCharacters'), 8000),
        'maxMessageCharacters': _number_or(source.get('maxMessageCharacters'), 2000),
        'minimumDelayedReplySeconds': _number_or(source.get('minimumDelayedReplySeconds'), 10),
        'maximumDelayedReplyMinutes': _number_or(source.get('maximumDelayedReplyMinutes'), 1440),
    }


def _strip_punctuation_symbols_space(text: str) -> str:
    """上游 /[\\p{P}\\p{S}\\s]+/gu 的等价实现（Unicode 类别 P* / S* + 空白）。"""
    return ''.join(
        char for char in text
        if not (unicodedata.category(char)[0] in ('P', 'S') or char.isspace())
    )


def _lexical_recall_keys(text: str) -> List[str]:
    """上游 lexicalRecallKeys：英文/数字词 + 中文相邻二字组，去重保序，最多 80 个。"""
    normalized = (text if isinstance(text, str) else '').lower()
    words = re.findall(r'[a-z0-9]{3,}', normalized)
    bigrams: List[str] = []
    for run in re.findall(r'[\u3400-\u9fff]{2,}', normalized):
        for index in range(len(run) - 1):
            bigrams.append(run[index:index + 2])
    return list(dict.fromkeys([*words, *bigrams]))[:80]


# ===========================================================================
# 上游 export 的可见文本/词法 helper（service_helpers.py 提供，缺失时内联回退）
# ===========================================================================

_VISIBLE_SEP_RE = re.compile(r'[<＜]\s*sep\s*/?\s*[>＞]', re.IGNORECASE)
_VISIBLE_TAG_RE = re.compile(r'</?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*/?>', re.IGNORECASE)
_VISIBLE_CQ_RE = re.compile(r'\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]', re.IGNORECASE)
_VISIBLE_DESC_RE = re.compile(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', re.IGNORECASE)
_VISIBLE_FACE_RE = re.compile(r'[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]', re.IGNORECASE)


def _normalize_visible_message_content(value: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 normalizeVisibleMessageContent：可见回复是纯文本合约，标记一旦漏出会被适配器发出去。"""
    text = _js_string(value)
    replacement = _js_string(separator).strip() or '<sep/>'
    text = _VISIBLE_SEP_RE.sub(lambda _match: replacement, text)
    text = _VISIBLE_TAG_RE.sub('', text)
    text = _VISIBLE_CQ_RE.sub('', text)
    text = _VISIBLE_DESC_RE.sub('', text)
    text = _VISIBLE_FACE_RE.sub('', text)
    limit = int(max(1.0, _number_or(max_characters, 2000)))
    return text.strip()[:limit]


def _normalize_group_reply(raw: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 normalizeGroupReply。"""
    if not _is_record(raw) or raw.get('mode') != 'immediate':
        return ''
    return _normalize_visible_message_content(raw.get('content'), max_characters, separator)


def _normalize_group_interaction_reply(raw: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 normalizeGroupInteractionReply。"""
    if not _is_record(raw) or not _is_record(raw.get('reply')):
        return ''
    if raw['reply'].get('mode') != 'immediate':
        return ''
    return _normalize_visible_message_content(raw['reply'].get('content'), max_characters, separator)


def _normalize_group_visible_reply_impl(raw: Any, interaction: Any, max_characters: Any,
                                        separator: Any = '<sep/>') -> str:
    """上游 7585-7587 normalizeGroupVisibleReply。"""
    return (
        _normalize_group_reply(raw, max_characters, separator)
        or _normalize_group_interaction_reply(interaction, max_characters, separator)
    )


def _history_lexical_score_impl(query: Any, content: Any) -> float:
    """上游 8247-8256 historyLexicalScore：字面召回通道。"""
    query_text = query if isinstance(query, str) else ''
    content_text = content if isinstance(content, str) else ''
    query_keys = _lexical_recall_keys(query_text)
    if not query_keys:
        return 0.0
    content_keys = set(_lexical_recall_keys(content_text))
    overlap = sum(1 for key in query_keys if key in content_keys)
    normalized_query = _strip_punctuation_symbols_space(query_text.lower())
    normalized_content = _strip_punctuation_symbols_space(content_text.lower())
    phrase = 0.5 if len(normalized_query) >= 3 and normalized_query in normalized_content else 0
    return min(1.0, overlap / len(query_keys) + phrase)


def _normalize_interaction(value: Any, now: datetime, runtime: Any) -> Optional[Dict[str, Any]]:
    """上游 7968-7984 normalizeInteraction。"""
    if not _is_record(value) or not isinstance(value.get('seen'), bool) or not _is_record(value.get('reply')):
        return None
    reply = value['reply']
    mode = reply.get('mode')
    if mode not in ('none', 'immediate', 'delayed'):
        return None
    content = (
        _normalize_visible_message_content(reply.get('content'), runtime.get('maxMessageCharacters'),
                                           runtime.get('messageSeparator'))
        if isinstance(reply.get('content'), str)
        else None
    )
    send_at = parse_time(reply.get('sendAt'))
    seen = value.get('seen') is True
    if mode == 'none':
        return {'seen': seen, 'reply': {'mode': 'none'}}
    if not content:
        return {'seen': seen, 'reply': {'mode': 'none'}}
    if mode == 'immediate':
        return {'seen': seen, 'reply': {'mode': mode, 'content': content}}
    if send_at is None:
        return {'seen': seen, 'reply': {'mode': 'none'}}
    delay = _time_ms(send_at) - _time_ms(now)
    minimum = _number_or(runtime.get('minimumDelayedReplySeconds'), 10) * 1000
    maximum = _number_or(runtime.get('maximumDelayedReplyMinutes'), 1440) * 60000
    if delay < minimum or delay > maximum:
        return {'seen': seen, 'reply': {'mode': 'none'}}
    return {'seen': seen, 'reply': {'mode': mode, 'content': content, 'sendAt': iso(send_at)}}


try:  # pragma: no cover - 取决于并行移植进度
    from .service_helpers import (  # noqa: F401
        history_lexical_score,
        normalize_group_visible_reply,
        normalize_interaction,
    )
except ImportError:  # TODO(并行移植): service_helpers 就绪后自动切换
    history_lexical_score = _history_lexical_score_impl
    normalize_group_visible_reply = _normalize_group_visible_reply_impl
    normalize_interaction = _normalize_interaction


# ===========================================================================
# 上游 service.ts 7692-8257 的私有 helper（仅本区间方法使用）
# ===========================================================================

_BASE_ANALYZE_ALTER = getattr(NarrativeProvider, 'analyze_alter', None)


def _resolve_alter_analyzer(narrator: Any):
    """上游 `this.narrator.analyzeAlter` 可选方法判定。

    返回可调用的 analyze_alter；缺失、不可调用，或只是继承 `NarrativeProvider`
    的 NotImplementedError 占位实现时返回 None（对应上游 `!analyzeAlter`）。
    """
    analyze_alter = getattr(narrator, 'analyze_alter', None)
    if not callable(analyze_alter):
        return None
    if getattr(analyze_alter, '__func__', None) is _BASE_ANALYZE_ALTER:
        return None
    return analyze_alter


def _signed_number(value: Any) -> str:
    """上游 7692-7694 signedNumber：带符号数值（整数不带小数，否则两位）。"""
    number = _number_or(value, 0.0)
    text = str(int(number)) if _is_js_integer(number) else _js_to_fixed(number, 2)
    return ('+' if number > 0 else '') + text


def _is_automatic_narrative_phase(phase: Any) -> bool:
    """上游 7725-7727 isAutomaticNarrativePhase。"""
    return phase == 'advance' or phase == 'conversation-follow-up'


def _normalize_automatic_delivery_summary(value: Any) -> str:
    """上游 7729-7731 normalizeAutomaticDeliverySummary。"""
    return clip(value, 240).strip() if isinstance(value, str) else ''


def _normalize_follow_up_commitment(value: Any, now: datetime) -> Optional[Dict[str, Any]]:
    """上游 7744-7761 normalizeFollowUpCommitment。"""
    if not _is_record(value):
        return None
    kind = value.get('kind')
    if kind not in _FOLLOW_UP_KINDS:
        return None
    summary = clip(value.get('summary'), 360).strip() if isinstance(value.get('summary'), str) else ''
    not_before = parse_time(value.get('notBefore'))
    if not kind or not summary or not_before is None:
        return None
    delta = _time_ms(not_before) - _time_ms(now)
    if delta < 5 * 60 * 1000 or delta > 12 * 60 * 60 * 1000:
        return None
    source_entry_ids: List[int] = []
    raw_ids = value.get('sourceEntryIds')
    if isinstance(raw_ids, list):
        for item in raw_ids:
            if isinstance(item, (int, float)) and not isinstance(item, bool) and _is_safe_integer(item) and item > 0:
                source_entry_ids.append(int(item))
            if len(source_entry_ids) >= 4:
                break
    expires_at = parse_time(value.get('expiresAt'))
    result: Dict[str, Any] = {'kind': kind, 'summary': summary, 'notBefore': iso(not_before)}
    if expires_at is not None and expires_at > not_before:
        result['expiresAt'] = iso(expires_at)
    if source_entry_ids:
        result['sourceEntryIds'] = source_entry_ids
    return result


def _inferred_follow_up_commitment(content: str, now: datetime) -> Dict[str, Any]:
    """上游 7763-7768 inferredFollowUpCommitment。"""
    return {
        'kind': 'thinking',
        'summary': clip('The character promised to return after thinking: ' + _js_string(content), 360),
        'notBefore': iso(now + timedelta(minutes=20)),
    }


_FOLLOW_UP_PROMISE_RE = re.compile(
    r'我(?:先)?想想|我去(?:想想|看看|查查|确认)|晚点(?:回|说|告诉)|之后(?:回|说|告诉)|'
    r'等我.{0,12}(?:回|说|告诉)|整理.{0,12}(?:回|说|告诉)'
)


def _interaction_promises_follow_up(content: Any) -> bool:
    """上游 7770-7773 interactionPromisesFollowUp。"""
    if not isinstance(content, str):
        return False
    return _FOLLOW_UP_PROMISE_RE.search(content) is not None


def _normalize_follow_up_resolutions(value: Any) -> List[Dict[str, Any]]:
    """上游 7775-7787 normalizeFollowUpResolutions。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not _is_record(item):
            continue
        raw_id = item.get('id')
        if not _is_js_integer(raw_id) or float(raw_id) <= 0:
            continue
        outcome = item.get('outcome')
        if outcome not in _FOLLOW_UP_OUTCOMES:
            continue
        entry: Dict[str, Any] = {'id': int(raw_id), 'outcome': outcome}
        if isinstance(item.get('notBefore'), str):
            entry['notBefore'] = item.get('notBefore')
        result.append(entry)
        if len(result) >= 2:
            break
    return result


def _valid_memory(value: Any) -> bool:
    """上游 7986-7988 validMemory。"""
    return (
        _is_record(value)
        and isinstance(value.get('category'), str)
        and isinstance(value.get('content'), str)
        and bool(value.get('content').strip())
    )


def _is_active_consequence_draft(value: Any) -> bool:
    """上游 8026-8028 isActiveConsequenceDraft。"""
    return (
        _is_record(value)
        and value.get('type') == 'active-consequence'
        and _is_record(value.get('payload'))
        and value['payload'].get('lifecycle') == 'active'
    )


def _consequence_expires_at(payload: Any) -> Optional[datetime]:
    """上游 8030-8033 consequenceExpiresAt。"""
    if not _is_record(payload):
        return None
    return parse_time(payload.get('expiresAt'))


def _valid_intent(value: Any, from_: datetime, now: datetime, memory: Any = None) -> bool:
    """上游 7990-8007 validIntent。"""
    if not _is_record(value) or not isinstance(value.get('type'), str) or not isinstance(value.get('summary'), str):
        return False
    not_before = parse_time(value.get('notBefore'))
    if not_before is None:
        return False
    if not _is_active_consequence_draft(value):
        return not_before > now
    payload = value.get('payload') if _is_record(value.get('payload')) else {}
    expires_at = _consequence_expires_at(payload)
    effect = payload.get('effect').strip() if isinstance(payload.get('effect'), str) else ''
    has_strength = 'strength' in payload
    strength = payload.get('strength')
    strength_ok = (not has_strength) or (
        isinstance(strength, (int, float)) and not isinstance(strength, bool)
        and math.isfinite(float(strength)) and 0 <= float(strength) <= 1
    )
    raw_days = memory.get('activeConsequenceMaxDays') if _is_record(memory) else None
    days = 7 if raw_days is None else raw_days
    maximum_lifetime_ms = max(1.0, _number_or(days, 7.0)) * 86400000
    return bool(
        _is_record(memory) and _js_truthy(memory.get('activeConsequencesEnabled'))
        and effect and strength_ok
        and not_before <= now and not_before >= from_
        and expires_at is not None and expires_at > now
        and (_time_ms(expires_at) - _time_ms(now)) <= maximum_lifetime_ms
    )


def _normalize_intent_updates(value: Any) -> List[Dict[str, Any]]:
    """上游 8009-8020 normalizeIntentUpdates。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not _is_record(item):
            continue
        raw_id = item.get('id')
        if not _is_js_integer(raw_id) or float(raw_id) <= 0:
            continue
        status = item.get('status')
        if status not in ('completed', 'cancelled'):
            continue
        entry: Dict[str, Any] = {'id': int(raw_id), 'status': status}
        resolution = item.get('resolution')
        if isinstance(resolution, str) and resolution.strip():
            entry['resolution'] = clip(resolution, 1000)
        result.append(entry)
        if len(result) >= 8:
            break
    return result


def _normalize_browser_intent_draft_loose(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7881-7894 normalizeBrowserIntentDraftLoose。"""
    if not _is_record(value) or value.get('mode') not in ('search', 'visit') or not isinstance(value.get('purpose'), str):
        return None
    query = clip(value.get('query'), 500) if isinstance(value.get('query'), str) else ''
    url = clip(value.get('url'), 2000) if isinstance(value.get('url'), str) else ''
    if value.get('mode') == 'search' and not query:
        return None
    if value.get('mode') == 'visit' and not url:
        return None
    result: Dict[str, Any] = {'mode': value.get('mode')}
    if query:
        result['query'] = query
    if url:
        result['url'] = url
    result['purpose'] = clip(value.get('purpose'), 500)
    result['timing'] = 'immediate' if value.get('timing') == 'immediate' else 'deferred'
    if isinstance(value.get('participantId'), str):
        result['participantId'] = value.get('participantId').strip()
    return result


def _permitted_or_global(value: Any, fallback: str, permitted_participant_ids: Set[str]) -> str:
    """上游 8065-8069 permittedOrGlobal。"""
    candidate = value.strip() if isinstance(value, str) else ''
    if candidate and candidate in permitted_participant_ids:
        return candidate
    return fallback if fallback and fallback in permitted_participant_ids else ''


def _normalize_conversation_action(
    value: Any,
    runtime: Dict[str, Any],
    permitted_participant_ids: Set[str],
    current_participant_id: str,
    now: Optional[datetime] = None,
    proactive: bool = False,
) -> Optional[Dict[str, Any]]:
    """上游 8045-8063 normalizeConversationAction。"""
    if now is None:
        now = now_utc()
    if not _is_record(value) or not isinstance(value.get('participantId'), str) \
            or not value.get('participantId') or value.get('participantId') == current_participant_id:
        return None
    if value.get('participantId') not in permitted_participant_ids or value.get('mode') not in ('immediate', 'delayed'):
        return None
    content = (
        _normalize_visible_message_content(value.get('content'), runtime.get('maxMessageCharacters'),
                                           runtime.get('messageSeparator'))
        if isinstance(value.get('content'), str)
        else ''
    )
    if not content:
        return None
    raw_willingness = value.get('willingness')
    if isinstance(raw_willingness, (int, float)) and not isinstance(raw_willingness, bool) \
            and math.isfinite(float(raw_willingness)):
        willingness: Optional[float] = clamp_number(raw_willingness, 0, 0, 1)
    else:
        willingness = None
    if proactive:
        threshold = _number_or(runtime.get('proactiveWillingnessThreshold'), 0.65)
        if willingness is None or willingness < threshold:
            return None
    reason = clip(value.get('reason'), 300) if isinstance(value.get('reason'), str) else None
    result: Dict[str, Any] = {
        'participantId': value.get('participantId'),
        'mode': value.get('mode'),
        'content': content,
    }
    if value.get('mode') == 'immediate':
        if willingness is not None:
            result['willingness'] = willingness
        if reason:
            result['reason'] = reason
        return result
    send_at = parse_time(value.get('sendAt'))
    if send_at is None:
        return None
    delay = _time_ms(send_at) - _time_ms(now)
    minimum = _number_or(runtime.get('minimumDelayedReplySeconds'), 10) * 1000
    maximum = _number_or(runtime.get('maximumDelayedReplyMinutes'), 1440) * 60000
    if delay < minimum or delay > maximum:
        return None
    result['sendAt'] = iso(send_at)
    if willingness is not None:
        result['willingness'] = willingness
    if reason:
        result['reason'] = reason
    return result


def _pick_participant_state_patch(value: Dict[str, Any]) -> Dict[str, Any]:
    """上游 8071-8076 pickParticipantStatePatch。"""
    patch: Dict[str, Any] = {}
    open_threads = value.get('openThreads')
    if isinstance(open_threads, list) and all(isinstance(item, str) for item in open_threads):
        patch['openThreads'] = [clip(item, 500) for item in open_threads][:50]
    relationship_notes = value.get('relationshipNotes')
    if isinstance(relationship_notes, list) and all(isinstance(item, str) for item in relationship_notes):
        patch['relationshipNotes'] = [clip(item, 500) for item in relationship_notes][:50]
    return patch


def _normalize_decision(
    raw: NarrativeDecision,
    from_: datetime,
    now: datetime,
    permit_messages: bool,
    runtime: Dict[str, Any],
    shared: Dict[str, Any],
    current_participant_id: str,
    permitted_participant_ids: Set[str],
    phase: str = 'advance',
    memory: Any = None,
    refresh_continuity: bool = False,
) -> Dict[str, Any]:
    """上游 7835-7866 normalizeDecision：先规范化，再写库。"""
    raw = resolve_authored_actions(raw, False, runtime.get('messageSeparator'))
    script_value = raw.get('script')
    max_script = int(max(0.0, _number_or(runtime.get('maxScriptCharacters'), 8000)))
    script = script_value.strip()[:max_script] if isinstance(script_value, str) else ''
    # 私聊回复的唯一通道是 interaction；自动生活 pass 没有实时参与者事件，不能发它。
    interaction = None if phase == 'advance' else normalize_interaction(raw.get('interaction'), now, runtime)
    memories: List[Dict[str, Any]] = []
    raw_memories = raw.get('memories')
    if isinstance(raw_memories, list):
        for item in raw_memories:
            if _valid_memory(item):
                memories.append({
                    **item,
                    'participantId': _permitted_or_global(
                        item.get('participantId'), current_participant_id, permitted_participant_ids
                    ),
                })
    intents: List[Dict[str, Any]] = []
    raw_intents = raw.get('intents')
    if isinstance(raw_intents, list):
        typed = [
            item for item in raw_intents
            if not _is_record(item) or item.get('type') != 'follow-up-commitment'
        ]
        for item in typed:
            if _valid_intent(item, from_, now, memory):
                intents.append({
                    **item,
                    'participantId': _permitted_or_global(
                        item.get('participantId'), current_participant_id, permitted_participant_ids
                    ),
                })
        intents = intents[:8]
    intent_updates = _normalize_intent_updates(raw.get('intentUpdates'))
    browser_intents: List[Dict[str, Any]] = []
    raw_browser = raw.get('browserIntents')
    if isinstance(raw_browser, list):
        for item in raw_browser:
            normalized = _normalize_browser_intent_draft_loose(item)
            if normalized:
                browser_intents.append(normalized)
        browser_intents = browser_intents[:1]
    proactive = phase == 'advance'
    agency_gated_proactive = proactive and not _is_record(raw.get('proactiveContact'))
    cross_conversation_actions: List[Dict[str, Any]] = []
    raw_cross = raw.get('crossConversationActions')
    if permit_messages and _js_truthy(shared.get('allowCrossConversationMessages')) and isinstance(raw_cross, list):
        for action in raw_cross:
            normalized = _normalize_conversation_action(
                action, runtime, permitted_participant_ids, current_participant_id, now, agency_gated_proactive,
            )
            if normalized:
                cross_conversation_actions.append(normalized)
        raw_limit = shared.get('maxCrossConversationActions')
        limit = int(max(0, raw_limit)) if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool) else 0
        cross_conversation_actions = cross_conversation_actions[:limit]
    state_patch = _pick_participant_state_patch(raw['statePatch']) if _is_record(raw.get('statePatch')) else None
    continuity = normalize_continuity_snapshot(raw.get('continuity')) if refresh_continuity else None
    alter = normalize_alter_value(raw.get('alter'))
    automatic_delivery_summary = None
    if _is_automatic_narrative_phase(phase):
        automatic_delivery_summary = _normalize_automatic_delivery_summary(raw.get('automaticDeliverySummary')) or None
    follow_up_commitment = (
        _normalize_follow_up_commitment(raw.get('followUpCommitment'), now) if phase == 'user-message' else None
    )
    follow_up_resolutions = (
        _normalize_follow_up_resolutions(raw.get('followUpResolutions'))
        if phase in ('user-message', 'intent-due')
        else []
    )
    agency_window = raw.get('agencyWindow') if _is_record(raw.get('agencyWindow')) else None
    proactive_contact = raw.get('proactiveContact') if _is_record(raw.get('proactiveContact')) else None
    return {
        'script': script,
        'authoredActions': raw.get('authoredActions'),
        'lifeHandoff': normalize_life_handoff(raw.get('lifeHandoff'), script),
        'alter': alter,
        'agencyWindow': agency_window,
        'proactiveContact': proactive_contact,
        'interaction': interaction,
        'automaticDeliverySummary': automatic_delivery_summary,
        'followUpCommitment': follow_up_commitment,
        'followUpResolutions': follow_up_resolutions,
        'continuity': continuity,
        'memories': memories,
        'intents': intents,
        'intentUpdates': intent_updates,
        'browserIntents': browser_intents,
        'statePatch': state_patch,
        'crossConversationActions': cross_conversation_actions,
    }


def _cosine_similarity(left: List[Any], right: List[Any]) -> Optional[float]:
    """上游 8269-8282 cosineSimilarity。"""
    if not left or len(left) != len(right):
        return None
    dot = 0.0
    left_magnitude = 0.0
    right_magnitude = 0.0
    for index in range(len(left)):
        dot += left[index] * right[index]
        left_magnitude += left[index] * left[index]
        right_magnitude += right[index] * right[index]
    if not left_magnitude or not right_magnitude:
        return None
    return dot / math.sqrt(left_magnitude * right_magnitude)


def _fact_score(fact: Dict[str, Any], config: Dict[str, Any],
                query_embedding: Optional[List[Any]] = None, query: str = '') -> float:
    """上游 8228-8245 factScore：语义只与叙事质量信号相加，不替代它们。"""
    if query_embedding is None:
        query_embedding = []
    last_seen = parse_time(fact.get('lastSeenAt'))
    age_days = max(0.0, (_time_ms(now_utc()) - _time_ms(last_seen)) / 86400000)
    recency = math.exp(-age_days / 30)
    similarity = _cosine_similarity(list(query_embedding), list(fact.get('embedding') or []))
    semantic = 0.0 if similarity is None else max(0.0, similarity)
    lexical = history_lexical_score(query, fact.get('content') or '')
    semantic_weight = _number_or(config.get('semanticWeight'), 0.0)
    return (
        _number_or(fact.get('importance'), 0.0) * _number_or(config.get('factImportanceWeight'), 0.0)
        + _number_or(fact.get('confidence'), 0.0) * _number_or(config.get('factConfidenceWeight'), 0.0)
        + recency * _number_or(config.get('factRecencyWeight'), 0.0)
        + semantic * semantic_weight
        + lexical * max(1.0, semantic_weight)
        + (1 if fact.get('scope') == 'promise' and _js_truthy(fact.get('unresolved')) else 0)
        * _number_or(config.get('unresolvedWeight'), 0.0)
    )


def _flatten_episode_tags(value: Any) -> List[Any]:
    """上游 `Object.values(dict).flat()`。"""
    if not _is_record(value):
        return []
    result: List[Any] = []
    for item in value.values():
        if isinstance(item, list):
            result.extend(item)
        else:
            result.append(item)
    return result


def _script_entry_draft_for_commit(
    commit: Dict[str, Any],
    interaction: Optional[Dict[str, Any]],
    timeline_plan: Optional[TimelinePlan] = None,
    life_handoff: Any = None,
) -> Dict[str, Any]:
    """上游 turn-persistence.ts scriptEntryDraftForCommit（统一走 hdsi/turn_persistence.py）。"""
    return script_entry_draft_for_commit(commit, interaction, timeline_plan, life_handoff)


class ServiceDecisionMixin:
    """上游 3833-4444：persistDecision / Alter / appendEntry / contactThreads / facts。"""

    # ================= 上游 3833-4181：persistDecision =================

    def persist_decision(
        self,
        story: InterludeStory,
        participant: Optional[InterludeParticipant],
        raw: NarrativeDecision,
        from_: datetime,
        now: datetime,
        permit_messages: bool,
        phase: str,
        context_intents: Optional[List[Dict[str, Any]]] = None,
        immediate_reply_already_delivered: bool = False,
        timeline_plan: Optional[TimelinePlan] = None,
    ) -> Dict[str, Any]:
        """上游 3833-4181 persistDecision：先规范化，再写库；逐分支复刻。"""
        if context_intents is None:
            context_intents = []
        runtime_config = self.config.get('runtime') if isinstance(self.config.get('runtime'), dict) else {}
        message_separator = runtime_config.get('messageSeparator')
        if message_separator is None:
            message_separator = '<sep/>'
        max_message_characters = _number_or(runtime_config.get('maxMessageCharacters'), 2000)
        split_reply_messages = runtime_config.get('splitReplyMessages')
        if split_reply_messages is None:
            split_reply_messages = True
        context_entry_limit = _number_or(runtime_config.get('contextEntryLimit'), 50)
        allow_proactive_messages = runtime_config.get('allowProactiveMessages') is True

        participant_id = participant.get('id') if participant else None
        # 3846：先规范化，再写库：不信任模型给出的时间、长度和结构，尤其不能让未来剧情落库。
        raw = resolve_authored_actions(raw, immediate_reply_already_delivered, message_separator)
        all_participants = self.participants(story.get('id'))
        permitted_participant_ids = {
            item.get('id') for item in all_participants if self.can_handle_participant(item)
        }
        refresh_continuity = self.should_refresh_continuity(story, phase)
        decision = _normalize_decision(
            raw, from_, now, permit_messages, _resolved_runtime(self.effective_urge_runtime),
            self.shared_story_config, participant_id if participant_id is not None else '',
            permitted_participant_ids, phase, self.memory_config, refresh_continuity,
        )
        state_before = decode_story_state(story.get('state'))
        active_scene = self.active_scene(story.get('id')) if decision.get('script') else None
        if decision.get('script'):
            scene_frame = project_scene_frame({
                'storyId': story.get('id'),
                'now': now,
                'scene': active_scene,
                'state': state_before,
                'workingDetails': self.prune_working_details(state_before.get('workingDetails'), now),
                'scenePresence': state_before.get('scenePresence'),
                'agencyWindow': active_agency_window(state_before.get('agencyWindow'), now),
            })
        else:
            scene_frame = None
        if scene_frame:
            dialogue_burst = resolve_dialogue_burst(scene_frame, state_before.get('dialogueBurst'), now, {
                'scope': participant_id if participant_id is not None else 'protagonist-life',
                'boundary': phase == 'advance',
            })
        else:
            dialogue_burst = None
        group_reply_content = normalize_group_visible_reply(
            raw.get('groupReply'), None, max_message_characters, message_separator,
        )
        if decision.get('script'):
            commit = decision_to_script_commit({
                'storyId': story.get('id'),
                'participantId': participant_id,
                'phase': phase,
                'from': from_,
                'now': now,
                'decision': {
                    **decision,
                    'groupReply': raw.get('groupReply'),
                    'messageReactions': raw.get('messageReactions'),
                    'localMedia': raw.get('localMedia'),
                    'nativeFace': raw.get('nativeFace'),
                },
                'messageSeparator': message_separator,
                'splitReplyMessages': split_reply_messages,
                'groupReplyContent': group_reply_content,
                'frameId': scene_frame.get('id') if scene_frame else None,
                'burstId': dialogue_burst.get('id') if dialogue_burst else None,
                'startsAfterEventId': dialogue_burst.get('lastEventId') if dialogue_burst else None,
            })
        else:
            commit = None
        if commit:
            validation = validate_script_commit(commit, message_separator)
            if not validation.get('valid'):
                raise Exception(
                    'ScriptCommit structural validation failed: %s'
                    % '; '.join(validation.get('errors') or [])
                )
            unbound = unbound_immediate_message_events(commit)
            if unbound:
                self.report_operation(
                    'diagnostic', 'debug', story, phase,
                    'M5 剧本行动绑定未确认 即时消息=%d；保留兼容投递，不重写已生成剧本', len(unbound),
                )
        script_entry: Optional[Dict[str, Any]] = None
        if commit:
            interaction_for_binding = decision.get('interaction') if _is_record(decision.get('interaction')) else None
            reply_for_binding = (
                interaction_for_binding.get('reply')
                if _is_record(interaction_for_binding) and _is_record(interaction_for_binding.get('reply'))
                else None
            )
            binding_content = reply_for_binding.get('content') if reply_for_binding else None
            # A resolution belongs to this exact message reaching the user.
            if participant and (permit_messages or immediate_reply_already_delivered) \
                    and decision.get('followUpResolutions'):
                event = find_outgoing_script_event(
                    commit, participant_id, 'immediate', binding_content, message_separator,
                )
                if event:
                    event['metadata'] = {
                        **(event.get('metadata') if _is_record(event.get('metadata')) else {}),
                        'followUpResolutions': decision.get('followUpResolutions'),
                    }
            if participant and phase == 'user-message' and (permit_messages or immediate_reply_already_delivered):
                event = find_outgoing_script_event(
                    commit, participant_id, 'immediate', binding_content, message_separator,
                )
                commitment = decision.get('followUpCommitment')
                if commitment is None and _interaction_promises_follow_up(binding_content):
                    commitment = _inferred_follow_up_commitment(binding_content, now)
                if event and commitment:
                    event['metadata'] = {
                        **(event.get('metadata') if _is_record(event.get('metadata')) else {}),
                        'followUpCommitment': commitment,
                    }
            script_entry = self.append_entry(
                story.get('id'),
                _script_entry_draft_for_commit(
                    commit, decision.get('interaction'), timeline_plan, decision.get('lifeHandoff'),
                ),
                now,
                participant_id if participant_id is not None else '',
            )
        resolved_consequences = self.apply_intent_updates(
            story.get('id'), decision.get('intentUpdates') or [], now, participant_id,
        )
        for memory in decision.get('memories') or []:
            self.append_memory(
                story.get('id'), memory, now,
                _first_defined(memory.get('participantId'), participant_id, ''),
                script_entry.get('id') if script_entry else None,
            )
        for intent in decision.get('intents') or []:
            # A reminder or promise created while handling a user's message is a
            # response to that relationship, even if it becomes due much later.
            # Carry that provenance into the shared intent ledger so its due-turn
            # is allowed to send the eventual message without enabling broad
            # background outreach.
            payload = intent.get('payload') if _is_record(intent.get('payload')) else {}
            if phase == 'user-message' and participant:
                payload = {**payload, 'userInitiated': payload.get('userInitiated') is not False}
            self.append_intent(
                story.get('id'),
                {**intent, 'payload': payload},
                now,
                _first_defined(intent.get('participantId'), participant_id, ''),
            )
        resolved_follow_ups: Set[int] = set()  # Settled only by confirmed delivery.
        for browser_intent in decision.get('browserIntents') or []:
            # An immediate intent is handled before the final narrator pass when
            # enabled. If it reaches this point (disabled mode, group turn, or a
            # second consecutive request), safely downgrade it to deferred work.
            if participant or phase != 'user-message' or self.browser_config.get('allowGroupTriggeredResearch'):
                self.append_browser_intent(
                    story.get('id'), browser_intent, now,
                    participant_id if participant_id is not None else '',
                )
        if participant and _js_truthy(decision.get('statePatch')):
            self.update_participant_state(participant, decision.get('statePatch'), now)

        is_agency_check = len(context_intents) > 0 and all(
            intent.get('type') == 'proactive-check' for intent in context_intents
        )
        agency_candidate: Optional[Dict[str, Any]] = None
        agency_allows_send = False
        agency_recheck: Optional[Dict[str, Any]] = None
        if decision.get('script'):
            state = state_before
            next_count = max(0, math.floor(_number_or(state.get('narrativeUpdateCount'), 0))) + 1
            next_state: Dict[str, Any] = {**state, 'narrativeUpdateCount': next_count}
            # Originals and execution annotations now supply continuity. A second
            # real-time prose summary must not certify an unexecuted sending action.
            if resolved_consequences or len(resolved_follow_ups):
                next_state['continuityDirty'] = True
            alter_turn = self.update_alter_system(
                story, state.get('alterSystem'), decision.get('alter'), phase, now,
                participant_id if participant_id is not None else '',
            )
            next_state['alterSystem'] = alter_turn.get('state') if alter_turn else state.get('alterSystem')
            next_state['timelineCarry'] = []  # Plans do not become persistent facts.
            if _js_truthy(decision.get('lifeHandoff')) and script_entry:
                handoff = decision.get('lifeHandoff')
                presence: Dict[Any, Any] = {}
                for item in (next_state.get('scenePresence') or []):
                    presence[item.get('name')] = item
                handoff_place = handoff.get('place') if _is_record(handoff.get('place')) else None
                if (
                    handoff.get('transition') is not None
                    or (
                        handoff_place is not None
                        and handoff_place.get('value') != (state_before.get('sceneFrame') or {}).get('place')
                    )
                    or handoff.get('presence') is not None
                ):
                    for name in list(presence.keys()):
                        presence[name] = {
                            **presence[name],
                            'status': 'off-scene',
                            'sourceEntryIds': [script_entry.get('id')],
                            'updatedAt': iso(now),
                            'basis': 'Local occupancy superseded by a new original-script handoff; not an invented departure.',
                        }
                handoff_presence = handoff.get('presence') if _is_record(handoff.get('presence')) else {}
                for name in (handoff_presence.get('names') or []):
                    presence[name] = {
                        'name': name,
                        'status': 'present',
                        'basis': handoff_presence.get('quote'),
                        'sourceEntryIds': [script_entry.get('id')],
                        'updatedAt': iso(now),
                    }
                next_state['scenePresence'] = list(presence.values())[-8:]
                resolved = {item.get('label') for item in (handoff.get('resolvedDetails') or [])}
                next_state['workingDetails'] = [
                    item for item in (next_state.get('workingDetails') or [])
                    if item.get('label') not in resolved
                ]
                next_state['workingDetailResolutions'] = {**(next_state.get('workingDetailResolutions') or {})}
                for label in resolved:
                    next_state['workingDetailResolutions'][label] = script_entry.get('id')
            if script_entry and _js_truthy(decision.get('lifeHandoff')):
                self.persist_timeline_scene_anchor(
                    story.get('id'), decision.get('lifeHandoff'), script_entry.get('id'), now,
                )
            if scene_frame and dialogue_burst and script_entry and commit:
                advanced = advance_scene_frame(
                    scene_frame, dialogue_burst, commit, script_entry.get('id'), now,
                    decision.get('lifeHandoff'),
                )
                next_state['sceneFrame'] = advanced.get('frame')
                next_state['dialogueBurst'] = advanced.get('burst')
            if self.agency_config.get('enabled') and (phase == 'advance' or is_agency_check):
                if _js_truthy(decision.get('agencyWindow')) or _js_truthy(decision.get('proactiveContact')):
                    source_entries = self.recent_entries(
                        story.get('id'), max(40, int(context_entry_limit * 2)),
                    )
                else:
                    source_entries = []
                valid_source_entry_ids = {entry.get('id') for entry in source_entries}
                if script_entry and script_entry.get('id'):
                    valid_source_entry_ids.add(script_entry.get('id'))
                fallback_source_entry_id = script_entry.get('id') if script_entry else None
                agency_window = normalize_agency_window_draft(
                    decision.get('agencyWindow'), now, self.agency_config, valid_source_entry_ids,
                    fallback_source_entry_id,
                )
                if agency_window is None:
                    agency_window = active_agency_window(state.get('agencyWindow'), now)
                next_state['agencyWindow'] = agency_window
                agency_candidate = normalize_proactive_contact(
                    decision.get('proactiveContact'), now, self.agency_config, permitted_participant_ids,
                    valid_source_entry_ids, fallback_source_entry_id,
                )
                if is_agency_check and (
                    (agency_candidate.get('participantId') if agency_candidate else None)
                    != (participant.get('id') if participant else None)
                ):
                    agency_candidate = None
                if agency_candidate and agency_window:
                    target = next(
                        (
                            item for item in all_participants
                            if item.get('id') == agency_candidate.get('participantId')
                        ),
                        None,
                    )
                    urge_state = (
                        (state.get('extensions') or {}).get('urge')
                        if _is_record(state.get('extensions')) else None
                    )
                    burst_active = bool(
                        self.urge_config.get('enabled')
                        and urge_burst_active(
                            normalize_urge_state(urge_state, _time_ms(now)), _time_ms(now),
                            self.urge_config, agency_candidate.get('participantId'),
                        )
                    )
                    capacity_config = (
                        {
                            **self.agency_config,
                            'minimumProactiveIntervalMinutes': self.urge_config.get('contactMin'),
                        }
                        if burst_active else self.agency_config
                    )
                    capacity = evaluate_agency_capacity(
                        agency_window, agency_candidate, now, capacity_config,
                        ((target or {}).get('state') or {}).get('lastCharacterMessageAt'),
                    )
                    willingness = agency_candidate.get('willingness')
                    if willingness is None:
                        willingness = 0
                    willingness_threshold = _number_or(
                        self.effective_urge_runtime.get('proactiveWillingnessThreshold'), 0.65,
                    )
                    willingness_passes = willingness >= willingness_threshold
                    agency_allows_send = bool(
                        agency_candidate.get('outcome') == 'send-now'
                        and capacity.get('allowed')
                        and willingness_passes
                    )
                    if not agency_allows_send and agency_candidate.get('outcome') != 'let-go' and willingness_passes:
                        agency_recheck = {
                            'candidate': agency_candidate,
                            'window': agency_window,
                            'reason': (
                                'model-requested-recheck'
                                if capacity.get('allowed') else capacity.get('reason')
                            ),
                            'at': proactive_recheck_at(agency_candidate, capacity, agency_window, now),
                        }
                    self.report_operation(
                        'standard', 'info', story, phase,
                        'Agency 主动联系判断 参与者=%s 结果=%s 原因=%s 意愿=%s',
                        agency_candidate.get('participantId'),
                        '立即联系' if agency_allows_send else ('稍后重查' if agency_recheck else '自然放下'),
                        capacity.get('reason'),
                        _js_to_fixed(willingness, 2),
                    )
                if agency_window:
                    self.report_operation(
                        'diagnostic', 'debug', story, phase,
                        'Agency Window 更新 负荷=%s 隐私=%s 设备=%s 有效至=%s',
                        agency_window.get('activityLoad'), agency_window.get('privacy'),
                        agency_window.get('deviceAccess'),
                        format_log_time(
                            parse_time(agency_window.get('validUntil')),
                            (story.get('setting') or {}).get('timezone'),
                        ),
                    )
            self.db_set(
                'interlude_story', {'id': story.get('id')},
                {'state': encode_story_state(next_state), 'updatedAt': now},
            )
            if alter_turn and alter_turn.get('thresholdReached'):
                self.schedule_alter_analysis(
                    story.get('id'), phase, alter_turn.get('sourceParticipantId'),
                )

        if agency_recheck:
            self.append_proactive_check(
                story, agency_recheck.get('candidate'), agency_recheck.get('at'),
                agency_recheck.get('reason'), now,
            )
        messages: List[Dict[str, Any]] = []
        if is_agency_check:
            interaction = (
                decision.get('interaction')
                if agency_allows_send
                and ((decision.get('interaction') or {}).get('reply') or {}).get('mode') == 'immediate'
                else None
            )
        else:
            interaction = decision.get('interaction')
        if (phase == 'intent-due' or phase == 'user-message') and participant:
            self.defer_unresolved_due_follow_ups(
                story.get('id'), participant.get('id'), context_intents, resolved_follow_ups,
                interaction, now,
            )
        automatic_delivery = None
        if (
            _is_automatic_narrative_phase(phase)
            or (self.urge_config.get('enabled') and is_agency_check)
        ) and script_entry:
            automatic_delivery = {
                'summary': decision.get('automaticDeliverySummary')
                or ('Background delivery based on script #%s.' % (script_entry.get('id'))),
                'sourceEntryId': script_entry.get('id'),
            }
        if participant and phase == 'user-message' and not is_agency_check \
                and _is_record(interaction) and interaction.get('seen'):
            self.mark_participant_seen(participant, now)
        reply = (
            interaction.get('reply')
            if _is_record(interaction) and _is_record(interaction.get('reply')) else None
        )
        if participant and permit_messages and not immediate_reply_already_delivered \
                and reply is not None and reply.get('mode') == 'immediate' and reply.get('content'):
            event = (
                find_outgoing_script_event(
                    commit, participant.get('id'), 'immediate', reply.get('content'), message_separator,
                )
                if commit else None
            )
            messages.append(attach_message_event({
                'participantId': participant.get('id'),
                'content': reply.get('content'),
                'automaticDelivery': automatic_delivery,
                'interaction': interaction,
                'userInitiated': phase == 'user-message',
            }, event, script_entry.get('id') if script_entry else None))
        if participant and permit_messages and reply is not None and reply.get('mode') == 'delayed' \
                and reply.get('content') and reply.get('sendAt'):
            send_at = parse_time(reply.get('sendAt'))
            payload: Dict[str, Any] = {
                'content': reply.get('content'),
                'userInitiated': phase == 'user-message',
                'interaction': True,
            }
            if commit:
                payload = {**payload, **script_event_payload(attach_message_event(
                    {'participantId': participant.get('id'), 'content': reply.get('content')},
                    find_outgoing_script_event(
                        commit, participant.get('id'), 'delayed', reply.get('content'), message_separator,
                    ),
                    script_entry.get('id') if script_entry else None,
                ))}
            self.append_intent(story.get('id'), {
                'type': 'delayed-reply',
                'summary': 'The character decided to send a delayed reply.',
                'notBefore': reply.get('sendAt'),
                'payload': payload,
            }, now, participant.get('id'))
            self.schedule_due_intent_wake(story.get('id'), send_at)

        # A cross-account message is itself proactive from the target's point of
        # view. Allow it during a live user event, or during background work only
        # when the global proactive-message switch is enabled.
        cross_actions: List[Dict[str, Any]] = []
        if phase == 'user-message':
            cross_actions = decision.get('crossConversationActions') or []
        elif phase == 'advance' and not self.agency_config.get('enabled') and allow_proactive_messages:
            cross_actions = decision.get('crossConversationActions') or []
        elif phase == 'advance' and agency_allows_send and agency_candidate:
            cross_actions = [
                action for action in (decision.get('crossConversationActions') or [])
                if action.get('participantId') == agency_candidate.get('participantId')
                and action.get('mode') == 'immediate'
            ][:1]
        if phase == 'advance' and (decision.get('crossConversationActions') or []) and not cross_actions:
            self.report_operation(
                'diagnostic', 'debug', story, phase,
                'Agency 拒绝未通过容量或来源验证的 crossConversationAction 数量=%d',
                len(decision.get('crossConversationActions') or []),
            )
        approved_automatic_outgoing_actions = (
            [
                {'participantId': action.get('participantId'), 'mode': action.get('mode')}
                for action in cross_actions if action.get('mode') == 'immediate'
            ]
            if phase == 'advance' else []
        )
        # Keep a compact record of actions that passed the local policy gate. The
        # actual visible receipt is written only after transport succeeds.
        if script_entry and approved_automatic_outgoing_actions:
            self.db_set('interlude_script_entry', {'id': script_entry.get('id')}, {
                'metadata': {
                    **(script_entry.get('metadata') or {}),
                    'approvedAutomaticOutgoingActions': approved_automatic_outgoing_actions,
                },
            })
        for action in cross_actions:
            if action.get('mode') == 'immediate':
                event = (
                    find_outgoing_script_event(
                        commit, action.get('participantId'), 'immediate', action.get('content'),
                        message_separator,
                    )
                    if commit else None
                )
                messages.append(attach_message_event({
                    'participantId': action.get('participantId'),
                    'content': action.get('content'),
                    'automaticDelivery': automatic_delivery,
                    'interaction': interaction,
                    'userInitiated': phase == 'user-message',
                }, event, script_entry.get('id') if script_entry else None))
            else:
                send_at_value = action.get('sendAt')
                if action.get('mode') != 'delayed' or not send_at_value:
                    continue
                send_at = parse_time(send_at_value)
                cross_payload: Dict[str, Any] = {
                    'content': action.get('content'),
                    'userInitiated': False,
                    'crossConversation': True,
                }
                if action.get('willingness') is not None:
                    cross_payload['willingness'] = action.get('willingness')
                if action.get('reason'):
                    cross_payload['reason'] = action.get('reason')
                if commit:
                    cross_payload = {**cross_payload, **script_event_payload(attach_message_event(
                        {'participantId': action.get('participantId'), 'content': action.get('content')},
                        find_outgoing_script_event(
                            commit, action.get('participantId'), 'delayed', action.get('content'),
                            message_separator,
                        ),
                        script_entry.get('id') if script_entry else None,
                    ))}
                self.append_intent(story.get('id'), {
                    'type': 'cross-conversation-message',
                    'summary': 'The character planned a message to another relationship branch.',
                    'notBefore': send_at_value,
                    'payload': cross_payload,
                }, now, action.get('participantId'))
                self.schedule_due_intent_wake(story.get('id'), send_at)

        # A visible message is confirmed only after transport succeeds. Keep later
        # bubbles in memory until the first bubble arrives; every bubble retains
        # the identity of the script event from which it was derived.
        prepared: List[Dict[str, Any]] = []
        for message in messages:
            prepared_message = prepare_outgoing_delivery(
                message, self.split_outgoing_message(message.get('content')),
            )
            if prepared_message:
                prepared.append(prepared_message)
        if self.urge_config.get('enabled') and script_entry and (
            _is_automatic_narrative_phase(phase)
            or (
                phase == 'intent-due'
                and not any(intent.get('type') == 'narrative-retry' for intent in context_intents)
            )
        ):
            try:
                current = self.get_story(story.get('id'))
                state = decode_story_state(current.get('state'))
                target = None
                if agency_allows_send and agency_candidate and any(
                    message.get('participantId') == agency_candidate.get('participantId')
                    for message in prepared
                ):
                    target = agency_candidate.get('participantId')
                urge_state = (
                    (state.get('extensions') or {}).get('urge')
                    if _is_record(state.get('extensions')) else None
                )
                urge = commit_urge(
                    normalize_urge_state(urge_state, _time_ms(now)), raw.get('urge'),
                    decision.get('script') or '', script_entry.get('id'), target, _time_ms(now),
                    self.urge_config,
                )
                self.db_set('interlude_story', {'id': story.get('id')}, {
                    'state': encode_story_state({
                        **state,
                        'extensions': {**(state.get('extensions') or {}), 'urge': urge},
                    }),
                    'updatedAt': now,
                })
            except Exception as error:  # noqa: BLE001 - 可选调度投影不得吞掉已提交台词
                self.report_standalone('warn', 'Urge 调度交接保存失败，保留既有剧本与投递 错误=%s', error)
        return {'messages': prepared, 'commit': commit, 'scriptEntry': script_entry}

    # ================= 上游 4183-4209：时间线场景锚点 / 日程 preplan 管理 =================

    def persist_timeline_scene_anchor(self, story_id: str, handoff: Any, entry_id: int, now: datetime) -> None:
        """上游 4183-4195 persistTimelineSceneAnchor。"""
        scene = self.active_scene(story_id)
        anchor = None
        if _is_record(handoff):
            anchor = _first_defined(handoff.get('activity'), handoff.get('place'))
        if scene is None or anchor is None:
            return
        self.db_set('interlude_scene', {'id': scene.get('id')}, {
            'hook': clip(
                'Original #%s: %s' % (entry_id, anchor.get('quote')),
                self.memory_config.get('sceneHookCharacters'),
            ),
            # The background editor owns the scene/arc summary and checkpoint.
            'updatedAt': now,
        })

    def admin_schedule_preplan(self, story_id: str) -> Any:
        """上游 4197-4199 adminSchedulePreplan。"""
        return self.get_schedule_preplan(story_id)

    def request_schedule_preplan_rebuild(self, story_id: str) -> bool:
        """上游 4201-4209 requestSchedulePreplanRebuild。"""
        current = self.get_schedule_preplan(story_id)
        if not current:
            return False
        self.db_set('interlude_schedule_preplan', {'storyId': story_id}, {
            'lastReviewedLocalDate': '', 'validThrough': '1970-01-01',
            'reviewReason': 'Administrator requested a rebuild.', 'updatedAt': now_utc(),
        })
        self.schedule_compaction(story_id)
        return True

    # ================= 上游 4227-4257：Alter 状态与分析调度 =================

    def emotional_offset_for_prompt(self, story: InterludeStory) -> Optional[EmotionalOffsetPrompt]:
        """上游 4227-4229 emotionalOffsetForPrompt。"""
        state = (story.get('state') or {}).get('alterSystem')
        return _alter_emotional_offset_for_prompt(
            normalize_alter_system_state(state), self.alter_system_config,
        )

    def update_alter_system(
        self,
        story: InterludeStory,
        current: Optional[Dict[str, Any]],
        alter: Any,
        phase: str,
        now: datetime,
        participant_id: str = '',
    ) -> Optional[Dict[str, Any]]:
        """上游 4231-4246 updateAlterSystem。"""
        config = self.alter_system_config
        if not config.get('enabled') or alter is None:
            return None
        result = advance_alter_system(current, alter, phase, now, config, participant_id)
        if result.get('offsetExpired'):
            self.report_operation('standard', 'info', story, phase, 'Alter 情绪偏移已自然消退')
        self.report_operation(
            'diagnostic', 'debug', story, phase,
            'Alter 状态已更新 来源=%s 本轮=%s 累计=%s 阈值=%s 权重=%s',
            result.get('sourceParticipantId') or '主角生活',
            alter,
            result.get('triggerValue'),
            _js_to_fixed(result.get('threshold'), 2),
            _js_to_fixed((result.get('state') or {}).get('alterWeight'), 2),
        )
        return result

    def schedule_alter_analysis(self, story_id: str, phase: str, participant_id: str = '') -> None:
        """上游 4248-4257 scheduleAlterAnalysis。"""
        task_key = f'{story_id}\u0000{participant_id}'
        if task_key in self.scheduled_alter_analyses:
            return
        self.scheduled_alter_analyses.add(task_key)

        def run() -> None:
            try:
                self.serial(
                    story_id,
                    lambda: self.analyze_alter_system(story_id, phase, participant_id),
                )
            except Exception as error:  # noqa: BLE001 - 后台任务失败只记录
                self.report_standalone(
                    'warn', 'Alter 后台分析任务失败 故事=%s 错误=%s', story_id, error,
                )
            finally:
                self.scheduled_alter_analyses.discard(task_key)

        self.ctx.set_timeout(run, 0)

    def analyze_alter_system(self, story_id: str, phase: str, participant_id: str = '') -> None:
        """上游 4259-4310 analyzeAlterSystem。"""
        config = self.alter_system_config
        if not config.get('enabled'):
            return
        story = self.get_story(story_id)
        state = normalize_alter_system_state((story.get('state') or {}).get('alterSystem'))
        if state is None:
            return
        now = now_utc()
        history = alter_history_for_scope(state.get('history') or [], participant_id)
        trigger_value = alter_scope_value(state, participant_id)
        threshold = calculate_alter_threshold(history, config, now)
        if abs(trigger_value) < threshold or alter_scope_cooling_down(state, participant_id, now):
            return
        marked = mark_alter_scope_analysis_attempt(state, participant_id, now)
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({
                **decode_story_state(story.get('state')),
                'alterSystem': marked,
            }),
            'updatedAt': now,
        })
        # 上游 `if (!this.narrator.analyzeAlter)`：可选方法。Python 的接口基类
        # NarrativeProvider 提供会抛 NotImplementedError 的占位实现，SilentNarrator
        # 继承它却并未真正实现侧端分析，因此这里把“未重写基类实现”也视为不支持。
        analyze_alter = _resolve_alter_analyzer(self.narrator)
        if analyze_alter is None:
            self.report(
                'warn', story, phase,
                'Alter 已达到阈值，但当前叙事服务不支持侧端分析；保留累计值等待重试',
            )
            return

        trigger_direction = 1 if trigger_value > 0 else -1
        try:
            scripts: List[Dict[str, Any]] = []
            for entry in self.recent_entries(story.get('id'), 50):
                if entry.get('kind') != 'script':
                    continue
                content = entry.get('content')
                if not isinstance(content, str) or not content.strip():
                    continue
                entry_participant_id = entry.get('participantId')
                if participant_id:
                    if entry_participant_id and entry_participant_id != participant_id:
                        continue
                elif entry_participant_id:
                    continue
                scripts.append({
                    'content': content[:4000],
                    'occurredAt': iso(entry.get('occurredAt')),
                })
            scripts = scripts[-10:]
            self.report_operation(
                'standard', 'info', story, phase,
                'Alter 累积触发 数值=%s 阈值=%s 方向=%s',
                _signed_number(trigger_value), _js_to_fixed(threshold, 2),
                '严肃' if trigger_direction > 0 else '放松',
            )
            setting = story.get('setting') if _is_record(story.get('setting')) else {}
            character = setting.get('character') if _is_record(setting.get('character')) else {}
            state_now = story.get('state') if _is_record(story.get('state')) else {}
            current_offset = state.get('emotionalOffset')
            result = analyze_alter({
                'characterName': character.get('name'),
                'triggerValue': trigger_value,
                'threshold': threshold,
                'direction': 'serious' if trigger_direction > 0 else 'relaxed',
                'recentScripts': scripts,
                'history': history[-10:],
                'settingOverlay': state_now.get('settingOverlay'),
                'currentOffset': (
                    {**current_offset, 'weight': state.get('alterWeight')}
                    if current_offset is not None else None
                ),
            }, config)
            description = ((result.get('description') if _is_record(result) else '') or '').strip()[:800]
            if not description:
                raise Exception('Alter analysis returned an empty description.')
            completed = complete_alter_analysis(
                marked, description, threshold, now, config, participant_id,
            )
            self.db_set('interlude_story', {'id': story.get('id')}, {
                'state': encode_story_state({
                    **decode_story_state(story.get('state')),
                    'alterSystem': completed,
                }),
                'updatedAt': now,
            })
            completed_offset = completed.get('emotionalOffset') or {}
            self.report_operation(
                'standard', 'info', story, phase,
                '情绪偏移生成完成 方向=%s 强度=%s 描述=%s',
                completed_offset.get('direction'),
                _js_to_fixed(completed_offset.get('intensity'), 2),
                description,
            )
            self.report_operation('standard', 'info', story, phase, '情绪偏移已注入后续主提示词 权重=1.00')
        except Exception as error:  # noqa: BLE001 - 失败保留累计值等待重试
            self.report('warn', story, phase, 'Alter 分析失败，已保留累计值等待重试：%s', error)

    # ================= 上游 4312-4342：脚本条目与记忆落库 =================

    def append_entry(self, story_id: str, entry: Dict[str, Any], now: datetime,
                     participant_id: str = '') -> ScriptEntry:
        """上游 4312-4334 appendEntry。"""
        occurred_at = parse_time(entry.get('occurredAt')) or now
        created = self.db_create('interlude_script_entry', {
            'storyId': story_id,
            'participantId': participant_id,
            'kind': clip(entry.get('kind'), 32) or 'life',
            'actor': clip(_first_defined(entry.get('actor'), 'character'), 32),
            'content': (
                _js_string(entry.get('content'))
                if entry.get('kind') == 'script'
                else clip(entry.get('content'), 12000)
            ),
            'occurredAt': occurred_at,
            'metadata': entry.get('metadata') if _is_record(entry.get('metadata')) else {},
            'createdAt': now,
        })
        # Scene entry counts are derived during compaction. Avoiding a second
        # SQLite write here keeps every durable script append atomic and cheap.
        row = normalize_database_row('interlude_script_entry', created)
        recall_cache = self.history_vectors.get(story_id)
        if recall_cache is not None and row.get('kind') in _RECALLABLE_ENTRY_KINDS:
            metadata = row.get('metadata') if _is_record(row.get('metadata')) else {}
            episode_tags = metadata.get('episodeTags')
            recall_entry: Dict[str, Any] = {
                'tags': _flatten_episode_tags(grounded_episode_tags(
                    row.get('content') or '',
                    episode_tags if _is_record(episode_tags) else {},
                )),
                'checkpoint': metadata.get('sceneCheckpoint'),
                'frameId': metadata.get('frameId') if isinstance(metadata.get('frameId'), str) else None,
                'content': prompt_visible_message_content(
                    row.get('content'), recent_script_ownership(row),
                ),
                'occurredAt': iso(row.get('occurredAt')),
                'participantId': row.get('participantId'),
                'kind': row.get('kind'),
            }
            if row.get('embedding'):
                recall_entry['vector'] = row.get('embedding')
            recall_cache[row.get('id')] = recall_entry
        return row

    def append_memory(self, story_id: str, memory: Dict[str, Any], now: datetime,
                      participant_id: str = '', source_entry_id: Optional[int] = None) -> None:
        """上游 4336-4342 appendMemory。"""
        self.db_create('interlude_memory', {
            'storyId': story_id,
            'participantId': participant_id,
            'category': clip(memory.get('category'), 32) or 'fact',
            'content': clip(memory.get('content'), 4000),
            'importance': clamp_number(memory.get('importance'), 0.5, 0, 1),
            'status': 'active',
            'sourceEntryId': source_entry_id if source_entry_id is not None else None,
            'createdAt': now,
            'updatedAt': now,
        })

    # ================= 上游 4344-4391：contactThreads 证据链 =================

    def contact_threads(self, story_id: str, selected: List[Dict[str, Any]],
                        participant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """上游 4344-4391 contactThreads：检索最小可用的持久事实切片。"""
        rows = self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'}, {
            'limit': 1000, 'sort': {'updatedAt': 'desc'},
        })
        # No private branch is exposed to an independent-life or group director.
        visible = [
            fact for fact in rows
            if not fact.get('participantId') or fact.get('participantId') == participant_id
        ]

        def contact(fact: Dict[str, Any]) -> bool:
            if fact.get('scope') == 'promise':
                return True
            knowledge = fact.get('knowledge') if _is_record(fact.get('knowledge')) else {}
            mode = knowledge.get('mode')
            if mode is None:
                mode = ''
            return mode in ('proposal', 'conditional', 'confirmed')

        combined = [
            fact for fact in selected
            if contact(fact) and (not fact.get('participantId') or fact.get('participantId') == participant_id)
        ]
        combined.extend(
            fact for fact in visible
            if contact(fact) and _js_truthy(fact.get('unresolved'))
        )
        seeds: List[Dict[str, Any]] = []
        seed_ids: Set[Any] = set()
        for fact in combined:
            fact_id = fact.get('id')
            if fact_id in seed_ids:
                continue
            seed_ids.add(fact_id)
            seeds.append(fact)
            if len(seeds) >= 4:
                break
        visible_ids = {fact.get('id') for fact in visible}
        linked_ids = [
            linked for linked in dict.fromkeys(
                linked_id
                for fact in seeds
                for linked_id in knowledge_related_ids(fact.get('knowledge'))
            )
            if linked not in visible_ids
        ]
        if linked_ids:
            archived = self.db_get('interlude_fact', {'storyId': story_id, 'id': {'$in': linked_ids}})
            visible.extend(
                fact for fact in archived
                if not fact.get('participantId') or fact.get('participantId') == participant_id
            )
        chain: Dict[Any, Dict[str, Any]] = {}
        for seed in seeds:
            chain[seed.get('id')] = seed
            seed_related = knowledge_related_ids(seed.get('knowledge'))
            seed_knowledge = seed.get('knowledge') if _is_record(seed.get('knowledge')) else None
            seed_topic = seed_knowledge.get('topic') if seed_knowledge else None

            def candidate(fact: Dict[str, Any]) -> bool:
                if fact.get('participantId') != seed.get('participantId'):
                    return False
                if fact.get('id') == seed.get('id'):
                    return False
                if fact.get('id') in seed_related:
                    return True
                if seed.get('id') in knowledge_related_ids(fact.get('knowledge')):
                    return True
                fact_knowledge = fact.get('knowledge') if _is_record(fact.get('knowledge')) else None
                fact_topic = fact_knowledge.get('topic') if fact_knowledge else None
                if _js_truthy(seed_topic) and fact_topic == seed_topic:
                    return True
                if not _js_truthy(seed_knowledge) and history_lexical_score(
                    seed.get('content') or '', fact.get('content') or '',
                ) >= 0.2:
                    return True
                return False

            candidates = [fact for fact in visible if candidate(fact)]
            candidates.sort(key=lambda fact: (
                -(1 if fact.get('id') in seed_related else 0),
                -history_lexical_score(seed.get('content') or '', fact.get('content') or ''),
            ))
            if _js_truthy(seed_knowledge):
                related = candidates[:3]
            else:
                related = [
                    fact for fact in candidates
                    if legacy_condition_cue(fact.get('content') or '')
                ][:2] + candidates[:2]
            for fact in related:
                chain[fact.get('id')] = fact
        facts = list(chain.values())[:12]
        flat_source_ids: List[Any] = []
        for fact in facts:
            source_entry_ids = fact.get('sourceEntryIds')
            if isinstance(source_entry_ids, list):
                flat_source_ids.extend(source_entry_ids)
            for clause in knowledge_clauses(fact.get('knowledge')):
                flat_source_ids.append(clause.get('sourceEntryId'))
        source_ids = list(dict.fromkeys(flat_source_ids))
        if not source_ids:
            return []
        ids: List[Any] = []
        for source_id in source_ids:
            if isinstance(source_id, bool) or not isinstance(source_id, (int, float)):
                continue
            for candidate_id in (source_id - 2, source_id - 1, source_id, source_id + 1, source_id + 2):
                if candidate_id > 0 and candidate_id not in ids:
                    ids.append(candidate_id)
        entries = self.db_get('interlude_script_entry', {'storyId': story_id, 'id': {'$in': ids}})
        # Keep whole originals. Omitted sources stay explicitly missing, never summarized into certainty.
        budget = 12000
        source_order = {source_id: index for index, source_id in enumerate(source_ids)}
        bounded_entries = [
            entry for entry in entries
            if not entry.get('participantId') or entry.get('participantId') == participant_id
        ]
        bounded_entries.sort(key=lambda entry: (
            source_order.get(entry.get('id'), math.inf),
            _number_or(entry.get('id'), 0),
        ))
        bounded: List[Dict[str, Any]] = []
        for entry in bounded_entries:
            content = entry.get('content')
            length = len(content) if isinstance(content, str) else 0
            if length > budget:
                continue
            budget -= length
            bounded.append(entry)
        return contact_evidence_threads(facts, bounded)

    # ================= 上游 4393-4444：facts =================
