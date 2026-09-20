# -*- coding: utf-8 -*-
"""HDS-Interlude 移植：上游 src/service.ts 行号 4393-5600（1.0.1-beta6-rebuild）。

ServiceScheduleMixin —— 事实检索 / 网页观察 / 意图与主动联系调度 / 到期唤醒 /
分段投递与投递台账 / 承诺回访 / 自动推进（含 urge 与对话后续）调度。

上游区间内的方法（按上游顺序）：
    facts(4393) / webObservations(4449) / activeScene(4468) / activeArc(4476) /
    appendIntent(4484) / activeConsequencesAndExpire(4512) / applyIntentUpdates(4537) /
    appendBrowserIntent(4562) / executeDeferredBrowserIntent(4593) / collectWebObservation(4603) /
    saveWebObservation(4672) / persistCollectedWebObservation(4691) / findCachedWebObservation(4699) /
    withBrowserSlot(4711) / scheduleNarrativeRetry(4724) / dueIntents(4746) /
    upcomingNarrativeIntents(4759) / scheduleDueIntentWake(4769) / scheduleNextSplitWake(4805) /
    deliverDueSplitSegments(4814) / pendingFollowUpCommitments(4893) /
    appendFollowUpCommitment(4899) / applyFollowUpResolutions(4940) /
    deferUnresolvedDueFollowUps(4978) / appendProactiveCheck(5002) /
    cancelPendingOutgoingMessages(5046) / sendScheduledMessages(5095) /
    sendOutgoingMessages(5107) / confirmOutgoingDeliveries(5193) /
    recordOutgoingDeliveryFailure(5233) / updateScriptDeliveryOutcome(5253) /
    recordPlatformDeliveryOutcome(5300) / resolveLiteralQuoteMessageId(5313) /
    recordAutomaticDelivery(5325) / splitOutgoingMessage(5372) / typingDelayMilliseconds(5379) /
    findBotForParticipant(5389) / scheduleUrgeAdvance(5419) / isAutomaticAdvancePaused(5442) /
    dueConversationFollowUps(5447) / completeConversationFollowUps(5459) /
    isAutomaticAdvanceDue(5475) / pauseAutomaticAdvanceAfterUserMessage(5485) /
    pauseAutomaticAdvanceAfterDelayedReply(5505) /
    scheduleConversationFollowUpsAfterTurn(5511) / scheduleNextAutomaticAdvance(5543) /
    schedulePreplanAnchoredTime(5563)。

区间内引用、但定义在 service.ts 其它位置的私有 helper（factScore / isActiveConsequence(Draft) /
consequenceExpiresAt / consequenceStrength / toDate / randomInteger /
normalizeBrowserIntentDraft(Loose) / browserIntentFromPayload / resolveBrowserTarget /
isSafePublicWebUrl(normalizeDomains/domainMatches/isPrivateHost) / webObservationEntryContent /
literalQuoteText / isLiteralQuoteOnly / normalizeFollowUpSummary / followUpExpiresAt /
normalizeFollowUpCommitment / normalizeFollowUpResolutions / automaticDeliveryFromPayload /
mergeDeliverySummary / targetableMessageId / activeRestWindow / clockMinutes /
automaticIntervalMinutes / scheduleConversationFollowUps / cosineSimilarity / lexicalRecallKeys /
normalizeVisibleMessageContent）优先取自并行移植的 hdsi/service_helpers.py；该文件或其名字缺失时，
按文件头 "上游顶层私有 helper 的内联副本" 一节逐行回退到本文件内联副本（语义与上游一致）。

共享约定（SERVICE_PORTING_SPEC.md / PORTING_GUIDE.md）：
- 同步；不写 __init__；self.config / self.ctx / self.platform / self.store /
  self.db_get / self.db_create / self.db_set / self.db_remove / self.serial /
  self.report / self.report_operation / self.report_standalone /
  self.report_standalone_operation / self.memory_config / self.browser_config /
  self.shared_story_config / self.auto_advance_config / self.urge_config /
  self.agency_config / self.schedule_preplan_config / self.due_intent_wake_timers /
  self.interrupted_typing_participants / self.browser_active / self.browser_waiters
  等由 service_base.py 提供（契约以它为准）。
- 定时：ctx.setTimeout/clearTimeout → self.ctx.set_timeout / self.ctx.clear_timer；
  到期唤醒结构 {'cancel': handle, 'dueAt': ms}。
- 投递：上游 session.bot.sendMessage / bot.sendMessage（findBotForParticipant 找到的账号）
  一律改成 self.platform.send_private / send_group；返回值 None 即上游的
  "没有可用机器人账号 / bot-not-found" 分支，非 None 当作已投递（返回值即 messageId）。
  实时会话路径（session.send）同样走平台发送，群会话优先 send_group。
- Browser/puppeteer：ctx.puppeteer → self.platform.search_web/render_page/http_get；
  三者都不可用或都返回 None 时走上游"浏览器未启用 / 读取失败"分支记录 WebObservation。
- 上游 5570-5600 的 sharedStoryConfig getter 已由 service_base.py 提供，本文件不重复实现；
  上游 4350 contactThreads / 4312 appendEntry / 3833 persistDecision 属 service_decision.py，
  3833-4393 的 persistDecision 等亦不在本文件重复实现。

跨 mixin 依赖（运行期由其它 mixin 提供，py_compile 不校验）：
- self.append_entry（service.ts 4312，service_decision.py）
- self.embed_text（service.ts 6582，service_memory.py）
- self.get_schedule_preplan（service.ts 6003，service_memory.py）
- self.sweep（service.ts 3188，service_narrative.py）
- self.has_pending_narrative（service.ts 2926，service_media.py）
- self.get_participant / can_handle_participant / record_character_message（service_story.py）
- self.get_story / db_get / db_create / db_set / serial（service_base.py）
- hdsi/service_helpers.py：顶层纯函数与私有 helper（已落地；缺失时本文件自动回退内联副本）。
"""

from __future__ import annotations

import json
import math
import random
import re
import threading
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Union

from .agency import active_agency_window, proactive_candidate_fingerprint
from .delivery import delivery_entry_metadata, restore_message_event, script_event_payload
from .script.delivery_ledger import update_script_delivery_actions
from .schedule_preplan import next_schedule_preplan_transition
from .story_state import decode_story_state, encode_story_state
from .time_utils import format_log_time, iso, local_clock_minutes, now_utc, parse_time
from .types import (
    FollowUpCommitmentDraft,
    FollowUpResolutionDraft,
    InterludeStory,
    NarrativeInteraction,
    OutgoingMessageDraft,
)
from .urge import acknowledge_urge, normalize_urge_state, plan_urge, urge_user_event
from .utils import clamp_number, clip, is_record

# ===================== 上游时间常量（Time.*） =====================

_MINUTE_MS = 60 * 1000
_SECOND_MS = 1000
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS

_MAX_SAFE_INTEGER = 9007199254740991  # Number.MAX_SAFE_INTEGER

# 上游 4763：upcomingNarrativeIntents 内部任务类型（不做叙事提示）。
_INTERNAL_INTENT_TYPES = ('split-message', 'browser-research', 'narrative-retry', 'proactive-check', 'active-consequence')

# 上游 5250：投递失败描述的长度上限（clip 用）。
_REASON_LIMIT = 500

# 上游 7744-7787：承诺回访的局部枚举白名单。
_FOLLOW_UP_KINDS = ('thinking', 'checking', 'decision', 'emotional-settle')
_FOLLOW_UP_OUTCOMES = ('fulfilled', 'rescheduled', 'cancelled')


# ===================== 通用小工具（JS 语义） =====================

def _nullish(value: Any, fallback: Any) -> Any:
    """复刻 TS 的 `value ?? fallback`（None 视作 null/undefined）。"""
    return fallback if value is None else value


def _nullish_number(value: Any, fallback: float) -> float:
    """复刻 `(value ?? fallback)` 并做 JS 数值化；不可转换时用 fallback。"""
    resolved = fallback if value is None else value
    number = _to_number(resolved)
    if number is None:
        return fallback
    return number


def _to_number(value: Any) -> Optional[float]:
    """复刻 JS `Number(value)`；不可转换返回 None（等价 NaN）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) else number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _number_or(value: Any, fallback: float) -> float:
    """复刻 `Number(value) || fallback`（NaN 与 0 都取 fallback）。"""
    number = _to_number(value)
    if number is None or number == 0:
        return fallback
    return number


def _clean_int(value: float) -> Union[int, float]:
    """JS 数字没有整数/浮点之分：整数值按 int 存，保证落库 JSON 与上游一致。"""
    return int(value) if float(value).is_integer() else float(value)


def _is_safe_integer(value: Any) -> bool:
    """复刻 JS Number.isSafeInteger。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if math.isnan(number) or math.isinf(number) or not number.is_integer():
        return False
    return abs(number) <= _MAX_SAFE_INTEGER


def _js_round(value: float) -> int:
    """复刻 Math.round：正数半值向上（Python round 是银行家舍入，不能用）。"""
    return int(math.floor(value + 0.5))


def _slice_limit(value: Any) -> int:
    """JS `slice(0, n)` 的 n：数值化后取整（ToIntegerOrInfinity）。"""
    number = _to_number(value)
    if number is None:
        return 0
    return int(number)


def _js_string(value: Any) -> str:
    """TS `String(value ?? '')` 的最小等价实现。"""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return str(value)


def _js_string_length(value: str) -> int:
    """JS `str.length` 是 UTF-16 code unit 数。"""
    return len(value.encode('utf-16-le', 'surrogatepass')) // 2


def _now_ms() -> int:
    """``Date.now()`` 等价：epoch 毫秒。"""
    return int(now_utc().timestamp() * 1000 + 0.5)


def _epoch_ms(value: Any) -> Optional[int]:
    """``Date.prototype.getTime()`` 等价；非法时间返回 None（JS NaN）。"""
    parsed = parse_time(value)
    if parsed is None:
        return None
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000 + 0.5)


def _from_epoch_ms(value: int) -> datetime:
    """``new Date(ms)`` 等价（UTC、毫秒精度）。"""
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def _to_date(value: Any) -> Optional[datetime]:
    """上游 8143-8148 toDate：Date/ISO 串/epoch 毫秒；非法返回 None。"""
    return parse_time(value)


def _date_lte(left: Any, right: datetime) -> bool:
    """JS `Date <= Date`：左值非法时比较为 false（NaN）。"""
    parsed = parse_time(left)
    return parsed is not None and parsed <= right


def _object_field(value: Any, key: str, default: Any = None) -> Any:
    """读取平台对象 / dict 的字段（ctx.bots 的 bot 既可能是对象也可能是 dict）。"""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


# ===================== 上游顶层私有 helper 的内联副本 =====================

def _fact_score(fact: Dict[str, Any], config: Dict[str, Any], query_embedding: Optional[List[float]] = None, query: str = '') -> float:
    """上游 8228-8243 factScore。"""
    last_seen_ms = _epoch_ms(fact.get('lastSeenAt'))
    if last_seen_ms is None:
        last_seen_ms = 0
    age_days = max(0.0, (_now_ms() - last_seen_ms) / _DAY_MS)
    recency = math.exp(-age_days / 30)
    similarity = _cosine_similarity(query_embedding or [], fact.get('embedding') or [])
    # Negative similarity is treated as no semantic support. This prevents an
    # unrelated fact from receiving a half-score merely because cosine values
    # mathematically range from -1 to 1.
    semantic = 0 if similarity is None else max(0, similarity)
    lexical = history_lexical_score(query, fact.get('content') or '')
    return (
        _number_or(fact.get('importance'), 0) * _number_or(config.get('factImportanceWeight'), 0)
        + _number_or(fact.get('confidence'), 0) * _number_or(config.get('factConfidenceWeight'), 0)
        + recency * _number_or(config.get('factRecencyWeight'), 0)
        + semantic * _number_or(config.get('semanticWeight'), 0)
        + lexical * max(1, _number_or(config.get('semanticWeight'), 0))
        + (1 if fact.get('scope') == 'promise' and fact.get('unresolved') else 0) * _number_or(config.get('unresolvedWeight'), 0)
    )


def _cosine_similarity(left: List[float], right: List[float]) -> Optional[float]:
    """上游 8269-8281 cosineSimilarity；长度不一致或无模长返回 None（undefined）。"""
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


def _strip_punct_symbols(value: str) -> str:
    """复刻 JS `replace(/[\\p{P}\\p{S}\\s]+/gu, '')`（Unicode 标点/符号/空白）。"""
    return ''.join(
        char for char in value
        if not (unicodedata.category(char)[0] in ('P', 'S') or char.isspace())
    )


def _lexical_recall_keys(text: str) -> List[str]:
    """上游 8258-8267 lexicalRecallKeys（保留插入顺序，最多 80 个）。"""
    normalized = (text or '').lower()
    words = re.findall(r'[a-z0-9]{3,}', normalized)
    chinese_runs = re.findall(r'[\u3400-\u9fff]{2,}', normalized)
    bigrams = [
        run[index:index + 2]
        for run in chinese_runs
        for index in range(max(0, len(run) - 1))
    ]
    return list(dict.fromkeys([*words, *bigrams]))[:80]


def _is_active_consequence_draft(intent: Any) -> bool:
    """上游 8026-8028 isActiveConsequenceDraft。"""
    if not is_record(intent):
        return False
    return intent.get('type') == 'active-consequence' and is_record(intent.get('payload')) and intent['payload'].get('lifecycle') == 'active'


def _is_active_consequence(intent: Any) -> bool:
    """上游 8022-8024 isActiveConsequence。"""
    if not is_record(intent):
        return False
    return intent.get('type') == 'active-consequence' and is_record(intent.get('payload')) and intent['payload'].get('lifecycle') == 'active'


def _consequence_expires_at(payload: Any) -> Optional[datetime]:
    """上游 8030-8033 consequenceExpiresAt。"""
    if not is_record(payload):
        return None
    return _to_date(payload.get('expiresAt'))


def _consequence_strength(payload: Any, fallback: float = 0.55) -> float:
    """上游 8035-8037 consequenceStrength。"""
    return clamp_number(payload.get('strength') if is_record(payload) else None, fallback, 0, 1)


def _literal_quote_text(value: Any) -> str:
    """上游 7665-7668 literalQuoteText。"""
    matched = re.match(r'^\s*[「\[]引用[:：]\s*(.*?)\s*[」\]]\s*$', _js_string(value))
    if not matched:
        return ''
    return (matched.group(1) or '').strip()


def _is_literal_quote_only(value: Any) -> bool:
    """上游 7670-7672 isLiteralQuoteOnly。"""
    return bool(_literal_quote_text(value))


def _normalize_automatic_delivery_summary(value: Any) -> str:
    """上游 7729-7731 normalizeAutomaticDeliverySummary。"""
    return clip(value, 240).strip() if isinstance(value, str) else ''


def _normalize_follow_up_summary(value: Any) -> str:
    """上游 7733-7735 normalizeFollowUpSummary。"""
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', clip(value, 360).strip()).lower()


def _follow_up_expires_at(value: Any, now: datetime) -> datetime:
    """上游 7737-7742 followUpExpiresAt。"""
    requested = _to_date(value)
    maximum = now + timedelta(hours=24)
    if requested is None or requested <= now:
        return maximum
    return requested if requested < maximum else maximum


def _normalize_follow_up_commitment(value: Any, now: datetime) -> Optional[FollowUpCommitmentDraft]:
    """上游 7744-7761 normalizeFollowUpCommitment。"""
    if not is_record(value):
        return None
    kind = value.get('kind') if value.get('kind') in _FOLLOW_UP_KINDS else None
    summary = clip(value.get('summary'), 360).strip() if isinstance(value.get('summary'), str) else ''
    not_before = _to_date(value.get('notBefore'))
    if not kind or not summary or not_before is None:
        return None
    not_before_ms = _epoch_ms(not_before)
    now_ms = _epoch_ms(now) or 0
    if not_before_ms is None or not_before_ms - now_ms < 5 * _MINUTE_MS or not_before_ms - now_ms > 12 * _HOUR_MS:
        return None
    raw_ids = value.get('sourceEntryIds')
    source_entry_ids: List[int] = []
    if isinstance(raw_ids, (list, tuple)):
        for item in raw_ids:
            if not isinstance(item, bool) and _is_safe_integer(item) and item > 0:
                source_entry_ids.append(int(item))
    source_entry_ids = source_entry_ids[:4]
    expires_at = _to_date(value.get('expiresAt'))
    result: FollowUpCommitmentDraft = {'kind': kind, 'summary': summary, 'notBefore': iso(not_before)}
    if expires_at is not None and expires_at > not_before:
        result['expiresAt'] = iso(expires_at)
    if source_entry_ids:
        result['sourceEntryIds'] = source_entry_ids
    return result


def _normalize_follow_up_resolutions(value: Any) -> List[FollowUpResolutionDraft]:
    """上游 7775-7787 normalizeFollowUpResolutions。"""
    if not isinstance(value, (list, tuple)):
        return []
    result: List[FollowUpResolutionDraft] = []
    for item in value:
        if not is_record(item):
            continue
        if not (_is_safe_integer(item.get('id')) and item.get('id') > 0):
            continue
        if item.get('outcome') not in _FOLLOW_UP_OUTCOMES:
            continue
        resolution: FollowUpResolutionDraft = {'id': int(item.get('id')), 'outcome': item.get('outcome')}
        if isinstance(item.get('notBefore'), str):
            resolution['notBefore'] = item.get('notBefore')
        result.append(resolution)
    return result[:2]


def _automatic_delivery_from_payload(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7789-7796 automaticDeliveryFromPayload。"""
    record = value.get('automaticDelivery') if is_record(value) and is_record(value.get('automaticDelivery')) else None
    summary = _normalize_automatic_delivery_summary(record.get('summary')) if record is not None else ''
    source_entry_id = None
    if record is not None and _is_safe_integer(record.get('sourceEntryId')):
        source_entry_id = int(record.get('sourceEntryId'))
    if not summary:
        return None
    return {'summary': summary, **({'sourceEntryId': source_entry_id} if source_entry_id else {})}


def _merge_delivery_summary(left: str, right: str) -> str:
    """上游 7798-7802 mergeDeliverySummary。"""
    if not left or left == right or right in left:
        return left or right
    if left in right:
        return right
    return clip('%s；%s' % (left, right), 240)


def _normalize_domains(values: Any) -> List[str]:
    """上游 7941-7943 normalizeDomains。"""
    if not isinstance(values, (list, tuple)):
        return []
    result: List[str] = []
    for value in values:
        text = _js_string(value).strip().lower()
        text = re.sub(r'^\.+|\.+$', '', text)
        if text:
            result.append(text)
    return result


def _domain_matches(host: str, domain: str) -> bool:
    """上游 7945 domainMatches。"""
    return host == domain or host.endswith('.%s' % domain)


def _is_private_host(host: str) -> bool:
    """上游 7947-7955 isPrivateHost。"""
    if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', host):
        parts = host.split('.')
        first = int(parts[0])
        second = int(parts[1])
        return (
            first == 10 or first == 127 or first == 0
            or (first == 169 and second == 254)
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168)
        )
    # Literal IPv6 and IPv4-mapped addresses are not needed for public-web
    # narration and are safest treated as local/private destinations.
    return ':' in host


def _is_safe_public_web_url(value: Any, config: Dict[str, Any]) -> bool:
    """上游 7924-7939 isSafePublicWebUrl（new URL → urlsplit）。"""
    try:
        parsed = urllib.parse.urlsplit(_js_string(value))
        # JS `new URL(value).protocol` 带冒号；Python urlsplit().scheme 不带。
        if parsed.scheme != 'https' and parsed.scheme != 'http':
            return False
        if parsed.username or parsed.password:
            return False
        host = (parsed.hostname or '').lower()
        if host.endswith('.'):
            host = host[:-1]
        if not host or host == 'localhost' or host.endswith('.localhost') or host == '::1':
            return False
        if _is_private_host(host):
            return False
        blocked = _normalize_domains(config.get('blockedDomains'))
        allowed = _normalize_domains(config.get('allowedDomains'))
        if any(_domain_matches(host, domain) for domain in blocked):
            return False
        return not allowed or any(_domain_matches(host, domain) for domain in allowed)
    except ValueError:
        return False


def _normalize_browser_intent_draft_loose(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7881-7894 normalizeBrowserIntentDraftLoose。"""
    if not is_record(value) or (value.get('mode') != 'search' and value.get('mode') != 'visit'):
        return None
    if not isinstance(value.get('purpose'), str):
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


def _normalize_browser_intent_draft(draft: Any, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """上游 7896-7902 normalizeBrowserIntentDraft。"""
    normalized = _normalize_browser_intent_draft_loose(draft)
    if not normalized:
        return None
    if normalized.get('mode') == 'search' and not config.get('allowSearch'):
        return None
    if normalized.get('mode') == 'visit' and not config.get('allowVisit'):
        return None
    return normalized


def _browser_intent_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """上游 7904-7912 browserIntentFromPayload。"""
    if not isinstance(payload, dict):
        payload = {}
    return _normalize_browser_intent_draft_loose({
        'mode': payload.get('mode'),
        'query': payload.get('query'),
        'url': payload.get('url'),
        'purpose': payload.get('purpose') or 'The character planned to read a public web page.',
        'timing': 'deferred',
    })


def _resolve_browser_target(draft: Dict[str, Any], config: Dict[str, Any]) -> Optional[str]:
    """上游 7914-7922 resolveBrowserTarget。"""
    if draft.get('mode') == 'search':
        template = config.get('searchUrlTemplate')
        template = template.strip() if isinstance(template, str) else None
        if not template or '{query}' not in template:
            return None
        # encodeURIComponent：! ' ( ) * 不转义（quote 始终保留 A-Za-z0-9 -_.~）。
        target = template.replace('{query}', urllib.parse.quote(_js_string(draft.get('query')), safe="!'()*"))
        return target if _is_safe_public_web_url(target, config) else None
    url = draft.get('url')
    return url if url and _is_safe_public_web_url(url, config) else None


def _web_observation_entry_content(observation: Dict[str, Any]) -> str:
    """上游 7957-7966 webObservationEntryContent。"""
    if observation.get('status') == 'success':
        source = observation.get('title') or observation.get('url') or 'a public web page'
        # The full bounded excerpt is supplied through webContext. Keeping it out
        # of the ordinary script stream avoids duplicating tokens and prevents
        # page text from being mistaken for a first-party narrative instruction.
        return 'The character read a public web page: %s.' % source
    return "The character's attempted web lookup did not complete: %s" % clip(observation.get('summary'), 800)


def _normalize_visible_message_content(value: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 7649-7663 normalizeVisibleMessageContent。"""
    fallback = (separator or '').strip() if isinstance(separator, str) else ''
    text = _js_string(value)
    text = re.sub(r'[<＜]\s*sep\s*/?\s*[>＞]', lambda _match: fallback or '<sep/>', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*/?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]', '', text)
    try:
        limit = max(1, int(max_characters))
    except (TypeError, ValueError):
        limit = 1
    return text.strip()[:limit]


def _random_integer(minimum: Any, maximum: Any) -> int:
    """上游 8454-8458 randomInteger。"""
    lower = math.floor(min(_to_number(minimum) or 0, _to_number(maximum) or 0))
    upper = math.floor(max(_to_number(minimum) or 0, _to_number(maximum) or 0))
    return lower + math.floor(random.random() * (upper - lower + 1))


def _clock_minutes(value: Any) -> Optional[int]:
    """上游 8366-8372 clockMinutes。"""
    text = value.strip() if isinstance(value, str) else ''
    matched = re.match(r'^(\d{1,2}):(\d{2})$', text)
    if not matched:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2))
    if 0 <= hour < 24 and 0 <= minute < 60:
        return hour * 60 + minute
    return None


def _active_rest_window(windows: Any, timezone_name: str, now: datetime) -> Optional[Dict[str, Any]]:
    """上游 8353-8364 activeRestWindow。"""
    local_minutes = local_clock_minutes(now, timezone_name)
    if not isinstance(windows, (list, tuple)):
        return None
    for window in windows:
        if not is_record(window) or not window.get('enabled'):
            continue
        start = _clock_minutes(window.get('start'))
        end = _clock_minutes(window.get('end'))
        if start is None or end is None:
            continue
        if start <= end:
            if start <= local_minutes < end:
                return window
        elif local_minutes >= start or local_minutes < end:
            return window
    return None


def _automatic_interval_minutes(story: InterludeStory, now: datetime, config: Dict[str, Any]) -> float:
    """上游 8325-8329 automaticIntervalMinutes。"""
    timezone_name = ((story.get('setting') or {}).get('timezone')) or 'UTC'
    rest_window = _active_rest_window(config.get('restWindows') or [], timezone_name, now)
    if rest_window is not None:
        return _random_integer(rest_window.get('minIntervalMinutes'), rest_window.get('maxIntervalMinutes'))
    jitter = _to_number(config.get('jitterMinutes'))
    jitter = 0 if jitter is None else jitter
    return max(1, _to_number(config.get('intervalMinutes')) or 0) + _random_integer(-jitter, jitter)


def _schedule_conversation_follow_ups(anchor: datetime, config: Dict[str, Any]) -> List[datetime]:
    """上游 8339-8351 scheduleConversationFollowUps。"""
    anchor_ms = _epoch_ms(anchor) or 0
    previous = anchor_ms
    jitter_range = _to_number(config.get('followUpJitterMinutes'))
    jitter_range = 0 if jitter_range is None else jitter_range
    follow_up_minutes = config.get('followUpMinutes')
    if not isinstance(follow_up_minutes, (list, tuple)):
        follow_up_minutes = []
    result: List[datetime] = []
    for minutes in follow_up_minutes:
        jitter = _random_integer(-jitter_range, jitter_range) if jitter_range else 0
        # Never place a later configured pass before an earlier one, even when
        # jitter is enabled or the owner provides a close custom sequence.
        at = max(previous + 1000, anchor_ms + max(1, (_to_number(minutes) or 0) + jitter) * _MINUTE_MS)
        previous = at
        result.append(_from_epoch_ms(at))
    return result


def _story_automation(story: Any) -> Dict[str, Any]:
    """`story.state.automation ?? {}`（story.state 已由 store 解码）。"""
    if not is_record(story):
        return {}
    state = story.get('state')
    if not is_record(state):
        return {}
    automation = state.get('automation')
    return automation if is_record(automation) else {}


def _error_message(error: Any) -> str:
    """`error instanceof Error ? error.message : String(error)` 的 Python 等价。"""
    if isinstance(error, BaseException):
        return str(error)
    return _js_string(error)


# ===================== 上游 service_helpers 的共享实现（缺失时回退本文件副本） =====================
# 上游 service.ts 7084-8524 的顶层纯函数（含大量非导出私有 helper）由 hdsi/service_helpers.py
# 统一提供（并行移植）。这里优先复用共享实现，保证各 mixin 只有一份语义；文件名缺失或
# 少任何一个名字时，全部回退到本文件前面已定义的内联副本（逐行对应上游）。

try:  # 上游 7026 / 7300 / 7665 / 7733-7802 / 7881-8037 / 8143 / 8228-8358 / 8454
    from .service_helpers import (  # type: ignore
        active_rest_window,
        automatic_delivery_from_payload,
        automatic_interval_minutes,
        browser_intent_from_payload,
        consequence_expires_at,
        consequence_strength,
        fact_score,
        follow_up_expires_at,
        history_lexical_score,
        is_active_consequence,
        is_active_consequence_draft,
        is_literal_quote_only,
        is_one_bot_platform,
        is_safe_public_web_url,
        literal_quote_text,
        merge_delivery_summary,
        normalize_browser_intent_draft,
        normalize_follow_up_commitment,
        normalize_follow_up_resolutions,
        normalize_follow_up_summary,
        normalize_interaction,
        random_integer,
        resolve_browser_target,
        schedule_conversation_follow_ups,
        targetable_message_id,
        to_date,
        web_observation_entry_content,
    )
except ImportError:  # TODO(并行移植): service_helpers 暂缺这些名字时回退到本文件内联副本
    # —— 顶层非导出 helper：本文件已按上游逐行复刻，别名即可 ——
    active_rest_window = _active_rest_window
    automatic_delivery_from_payload = _automatic_delivery_from_payload
    automatic_interval_minutes = _automatic_interval_minutes
    browser_intent_from_payload = _browser_intent_from_payload
    consequence_expires_at = _consequence_expires_at
    consequence_strength = _consequence_strength
    fact_score = _fact_score
    follow_up_expires_at = _follow_up_expires_at
    is_active_consequence = _is_active_consequence
    is_active_consequence_draft = _is_active_consequence_draft
    is_literal_quote_only = _is_literal_quote_only
    is_safe_public_web_url = _is_safe_public_web_url
    literal_quote_text = _literal_quote_text
    merge_delivery_summary = _merge_delivery_summary
    normalize_browser_intent_draft = _normalize_browser_intent_draft
    normalize_follow_up_commitment = _normalize_follow_up_commitment
    normalize_follow_up_resolutions = _normalize_follow_up_resolutions
    normalize_follow_up_summary = _normalize_follow_up_summary
    random_integer = _random_integer
    resolve_browser_target = _resolve_browser_target
    schedule_conversation_follow_ups = _schedule_conversation_follow_ups
    to_date = _to_date
    web_observation_entry_content = _web_observation_entry_content

    def targetable_message_id(value: Any) -> Optional[str]:
        """上游 7300-7303 targetableMessageId。"""
        identifier = _js_string(value).strip()
        return identifier if re.match(r'^-?\d+$', identifier) and identifier != '0' else None

    def is_one_bot_platform(platform: Any) -> bool:
        """上游 7026-7034 isOneBotPlatform。"""
        value = str(platform if platform is not None else '').lower()
        return (
            value == 'onebot'
            or value.startswith('onebot:')
            or value == 'napcat'
            or value.startswith('napcat:')
            or value == 'qq:onebot'
            or value.startswith('qq:onebot:')
        )

    def normalize_interaction(value: Any, now: datetime, runtime: Dict[str, Any]) -> Optional[NarrativeInteraction]:
        """上游 7968-7984 normalizeInteraction。"""
        if not is_record(value) or not isinstance(value.get('seen'), bool) or not is_record(value.get('reply')):
            return None
        reply = value.get('reply') or {}
        mode = reply.get('mode')
        if mode != 'none' and mode != 'immediate' and mode != 'delayed':
            return None
        content = (
            _normalize_visible_message_content(
                reply.get('content'), runtime.get('maxMessageCharacters'), runtime.get('messageSeparator'),
            )
            if isinstance(reply.get('content'), str) else None
        )
        send_at = _to_date(reply.get('sendAt'))
        # seen 只描述是否读了新消息；reply 是独立的发送通道。跟进/到期回合协议
        # 规定 seen=false，若在此处因 seen 抹掉回复，“稍后读到再回”的自救路径
        # 会被无声斩断（模型写进了剧本的发送与投递现实分裂）。
        seen = value.get('seen') is True
        if mode == 'none':
            return {'seen': seen, 'reply': {'mode': 'none'}}
        if not content:
            return {'seen': seen, 'reply': {'mode': 'none'}}
        if mode == 'immediate':
            return {'seen': seen, 'reply': {'mode': mode, 'content': content}}
        delay = (_epoch_ms(send_at) - _epoch_ms(now)) if send_at is not None else None
        minimum = _to_number(runtime.get('minimumDelayedReplySeconds'))
        maximum = _to_number(runtime.get('maximumDelayedReplyMinutes'))
        too_early = minimum is not None and delay is not None and delay < minimum * 1000
        too_late = maximum is not None and delay is not None and delay > maximum * _MINUTE_MS
        if send_at is None or delay is None or too_early or too_late:
            return {'seen': seen, 'reply': {'mode': 'none'}}
        return {'seen': seen, 'reply': {'mode': mode, 'content': content, 'sendAt': iso(send_at)}}

    def history_lexical_score(query: str, content: str) -> float:
        """上游 8247-8256 historyLexicalScore。"""
        query_keys = _lexical_recall_keys(query)
        if not query_keys:
            return 0
        content_keys = set(_lexical_recall_keys(content))
        overlap = len([key for key in query_keys if key in content_keys])
        normalized_query = _strip_punct_symbols((query or '').lower())
        normalized_content = _strip_punct_symbols((content or '').lower())
        phrase = 0.5 if len(normalized_query) >= 3 and normalized_query in normalized_content else 0
        return min(1, overlap / len(query_keys) + phrase)



class ServiceScheduleMixin:
    """上游 InterludeService 4393-5600：事实/网页/意图/投递/自动推进调度。"""

    # ================= 上游 4393-4444：facts =================

    def facts(self, story_id: str, limit: Optional[int] = None, query: str = '',
              participant_id: Optional[str] = None,
              turn_query_embedding: Optional[List[float]] = None) -> List[Dict[str, Any]]:
        """上游 4393-4444 facts：持久事实的语义 + 叙事质量混合排序。"""
        if limit is None:
            limit = self.memory_config.get('factLimit')
        # The previous floor of 50 caused every live turn to scan a large slice of
        # the facts table, even when the narrator only needed a handful of facts.
        # Keep enough candidates for semantic re-ranking without making the
        # latency-sensitive path do unnecessary database work.
        # Existing stories may contain more rows than a newly configured cap. A
        # broad bounded pool prevents an exact old fact from becoming unreachable
        # merely because 120 more-important rows sort ahead of it.
        # memoryConfig.factLimit 来自 runtime.memoryLimit（Schema 默认 20）；
        # 配置段缺失时按同一默认值兜底，避免 NaN 污染 limit。
        limit = _nullish_number(limit, 20)
        candidate_limit = max(300, min(max(limit * 10, _nullish_number(self.memory_config.get('maxFactsPerStory'), 200) * 3), 1000))
        lane_limit = max(1, min(5, math.floor(limit / 4) or 1))
        rows = self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'}, {
            'limit': _clean_int(candidate_limit), 'sort': {'importance': 'desc', 'updatedAt': 'desc'},
        })
        recent_resolved_events = self.db_get(
            'interlude_fact', {'storyId': story_id, 'status': 'active', 'scope': 'event', 'unresolved': False},
            {'limit': lane_limit * 2, 'sort': {'updatedAt': 'desc'}},
        )
        open_promises = self.db_get(
            'interlude_fact', {'storyId': story_id, 'status': 'active', 'scope': 'promise', 'unresolved': True},
            {'limit': lane_limit * 2, 'sort': {'updatedAt': 'desc'}},
        )
        # Live embedding adds an extra HTTP request to every user turn. Keep it
        # opt-in; stored fact vectors and background backfill still work normally.
        # A precomputed turn-level vector is preferred when the caller already
        # built one, so multiple semantic consumers share a single request.
        if turn_query_embedding:
            query_embedding = list(turn_query_embedding)
        elif (query or '').strip() and (((self.config.get('model') or {}).get('embedding') or {}).get('liveQuery')):
            query_embedding = self.embed_text(query)
        else:
            query_embedding = []

        def visible(fact: Dict[str, Any]) -> bool:
            return participant_id is None or not fact.get('participantId') or fact.get('participantId') == participant_id

        ranked = sorted(
            ((fact, fact_score(fact, self.memory_config, query_embedding, query)) for fact in rows if visible(fact)),
            key=lambda item: (-item[1], -(_epoch_ms(item[0].get('updatedAt')) or 0), -(item[0].get('id') or 0)),
        )
        selected: List[Dict[str, Any]] = []
        seen: Set[Any] = set()
        for fact in [
            *[fact for fact in recent_resolved_events if visible(fact)][:lane_limit],
            *[fact for fact in open_promises if visible(fact)][:lane_limit],
            *[item[0] for item in ranked],
        ]:
            if fact.get('id') in seen:
                continue
            seen.add(fact.get('id'))
            selected.append(fact)
            if len(selected) >= limit:
                break
        return selected

    # ================= 上游 4449-4466：webObservations =================

    def web_observations(self, story_id: str, participant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """上游 4449-4466 webObservations。

        Returns only observations that are safe for this narration branch. A
        participant's browsing is not shown to another private participant unless
        the owner has explicitly enabled shared relationship details.
        """
        # Browsing is optional. Avoid a database read on every live turn when the
        # feature is disabled, which is the default for most installations.
        if not self.browser_config.get('enabled'):
            return []
        limit = max(1, min(_nullish_number(self.browser_config.get('maxObservationsInPrompt'), 4), 20))
        rows = self.db_get('interlude_web_observation', {'storyId': story_id}, {
            'limit': int(max(limit * 4, 20)), 'sort': {'accessedAt': 'desc'},
        })
        return [
            observation for observation in rows
            # Failed/blocked attempts already have a terse script event. Keeping
            # their error text in every later prompt wastes tokens and can crowd
            # out useful successful observations.
            if observation.get('status') == 'success'
            and (
                self.shared_story_config.get('shareParticipantDetails')
                or not observation.get('participantId')
                or observation.get('participantId') == (participant_id or '')
            )
        ][:int(limit)][::-1]

    # ================= 上游 4468-4474：activeScene =================

    def active_scene(self, story_id: str) -> Optional[Dict[str, Any]]:
        """上游 4468-4474 activeScene。"""
        rows = self.db_get('interlude_scene', {'storyId': story_id, 'status': 'active'}, {
            'limit': 1,
            'sort': {'updatedAt': 'desc'},
        })
        return rows[0] if rows else None

    # ================= 上游 4476-4482：activeArc =================

    def active_arc(self, story_id: str) -> Optional[Dict[str, Any]]:
        """上游 4476-4482 activeArc。"""
        rows = self.db_get('interlude_arc', {'storyId': story_id, 'status': 'active'}, {
            'limit': 1,
            'sort': {'updatedAt': 'desc'},
        })
        return rows[0] if rows else None

    # ================= 上游 4484-4507：appendIntent =================

    def append_intent(self, story_id: str, intent: Dict[str, Any], now: datetime, participant_id: str = '') -> None:
        """上游 4484-4507 appendIntent。"""
        not_before = to_date(intent.get('notBefore'))
        payload = intent.get('payload') if is_record(intent.get('payload')) else {}
        active_consequence = is_active_consequence_draft(intent)
        if active_consequence and not self.memory_config.get('activeConsequencesEnabled'):
            return
        requested_expires_at = consequence_expires_at(payload) if active_consequence else None
        max_lifetime = max(1, _nullish_number(self.memory_config.get('activeConsequenceMaxDays'), 7)) * _DAY_MS
        expires_at = (
            min(requested_expires_at, now + timedelta(milliseconds=max_lifetime))
            if requested_expires_at is not None and requested_expires_at > now
            else None
        )
        # Scheduled plans always remain future-facing. An active consequence is
        # different: it is a present condition caused by something already in
        # the script, so it begins at now and only needs a bounded expiry.
        if (
            not_before is None
            or (not active_consequence and not_before <= now)
            or (active_consequence and expires_at is None)
        ):
            return
        normalized_payload = (
            {
                **payload,
                'strength': consequence_strength(payload, _nullish_number(self.memory_config.get('activeConsequenceDefaultStrength'), 0.55)),
                'expiresAt': iso(expires_at),
            }
            if active_consequence else payload
        )
        self.db_create('interlude_intent', {
            'storyId': story_id, 'participantId': participant_id,
            'type': clip(intent.get('type'), 32) or 'follow-up', 'summary': clip(intent.get('summary'), 4000),
            'notBefore': not_before, 'status': 'pending', 'payload': normalized_payload,
            'createdAt': now, 'updatedAt': now,
        })

    # ================= 上游 4512-4533：activeConsequencesAndExpire =================

    def active_consequences_and_expire(self, story_id: str, now: datetime,
                                       participant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """上游 4512-4533 activeConsequencesAndExpire。

        Active consequences share the intent table but are never scheduler work.
        Their payload keeps the lifecycle explicit so old scheduled intents keep
        their existing behaviour without a migration.
        """
        if not self.memory_config.get('activeConsequencesEnabled'):
            return []
        rows = self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending'}, {
            'limit': 100, 'sort': {'updatedAt': 'desc'},
        })
        consequences = [intent for intent in rows if is_active_consequence(intent)]
        expired = [
            intent for intent in consequences
            if (_epoch_ms(consequence_expires_at(intent.get('payload'))) or 0) <= (_epoch_ms(now) or 0)
        ]
        if expired:
            self.db_set(
                'interlude_intent', {'id': {'$in': [intent.get('id') for intent in expired]}},
                {'status': 'completed', 'updatedAt': now},
            )
        candidates = [
            intent for intent in rows
            if is_active_consequence(intent)
            and _date_lte(intent.get('notBefore'), now)
            and (consequence_expires_at(intent.get('payload')) is not None
                 and consequence_expires_at(intent.get('payload')) > now)
            and (
                participant_id is None
                or not intent.get('participantId')
                or intent.get('participantId') == participant_id
            )
        ]
        candidates.sort(key=lambda intent: (
            -consequence_strength(intent.get('payload')),
            -(_epoch_ms(intent.get('updatedAt')) or 0),
        ))
        return candidates[:int(max(1, _nullish_number(self.memory_config.get('activeConsequencePromptLimit'), 6)))]

    # ================= 上游 4537-4557：applyIntentUpdates =================

    def apply_intent_updates(self, story_id: str, updates: List[Dict[str, Any]], now: datetime,
                             participant_id: Optional[str] = None) -> bool:
        """上游 4537-4557 applyIntentUpdates。

        Only active consequences visible to the writer may be resolved. This
        prevents a remote model from changing arbitrary future plans by id.
        """
        if not updates:
            return False
        ids = [update.get('id') for update in updates]
        rows = self.db_get('interlude_intent', {'storyId': story_id, 'id': {'$in': ids}, 'status': 'pending'})
        allowed: Dict[Any, Dict[str, Any]] = {
            intent.get('id'): intent
            for intent in rows
            if is_active_consequence(intent)
            and (
                not participant_id
                or not intent.get('participantId')
                or intent.get('participantId') == participant_id
            )
        }
        changed = False
        for update in updates:
            intent = allowed.get(update.get('id'))
            if not intent:
                continue
            payload = {
                **(intent.get('payload') or {}),
                **({'resolution': update.get('resolution')} if update.get('resolution') else {}),
            }
            self.db_set(
                'interlude_intent', {'id': intent.get('id')},
                {'status': update.get('status'), 'payload': payload, 'updatedAt': now},
            )
            changed = True
        return changed

    # ================= 上游 4562-4587：appendBrowserIntent =================

    def append_browser_intent(self, story_id: str, draft: Dict[str, Any], now: datetime,
                              fallback_participant_id: str = '') -> None:
        """上游 4562-4587 appendBrowserIntent。

        Stores a narrator-proposed browser action as a future intent. The model
        never writes page content directly; a separate Puppeteer task creates the
        observation later.
        """
        config = self.browser_config
        if not config.get('enabled'):
            return
        normalized = normalize_browser_intent_draft(draft, config)
        if not normalized:
            return
        # A model may describe a reason involving another person, but it may not
        # silently attach a web observation to another relationship branch. The
        # active participant owns a live-turn browse; unattended life browsing is
        # world-level. This is both a privacy boundary and a simpler mental model.
        participant_id = fallback_participant_id
        allowed_participant = self.get_participant(participant_id) if participant_id else None
        if participant_id and (not allowed_participant or not self.can_handle_participant(allowed_participant)):
            return
        not_before = now + timedelta(seconds=1)
        self.append_intent(story_id, {
            'type': 'browser-research',
            'summary': clip(normalized.get('purpose'), 500) or 'The character planned to read a public web page.',
            'notBefore': iso(not_before),
            'payload': {
                'mode': normalized.get('mode'),
                'query': normalized.get('query') or '',
                'url': normalized.get('url') or '',
                'purpose': normalized.get('purpose'),
            },
        }, now, participant_id)
        self.report_standalone_operation('diagnostic', 'debug', '已创建网页浏览意图：故事=%s 模式=%s', story_id, normalized.get('mode'))

    # ================= 上游 4593-4598：executeDeferredBrowserIntent =================

    def execute_deferred_browser_intent(self, story: InterludeStory, intent: Dict[str, Any],
                                        now: datetime) -> Dict[str, Any]:
        """上游 4593-4598 executeDeferredBrowserIntent。

        Executes a due browser intent once, records its bounded observation, and
        marks the future plan complete regardless of success. A failed browser is
        still an event (the character could not access the page), but it never
        blocks later dialogue or background life updates.
        """
        payload = browser_intent_from_payload(intent.get('payload'))
        observation = self.collect_web_observation(story, payload, intent.get('participantId'), intent.get('id'), now)
        self.db_set('interlude_intent', {'id': intent.get('id')}, {'status': 'completed', 'updatedAt': now_utc()})
        return observation

    # ================= 上游 4603-4670：collectWebObservation =================

    def collect_web_observation(self, story: InterludeStory, draft: Optional[Dict[str, Any]],
                                participant_id: str, intent_id: Optional[int], now: datetime,
                                persist: bool = True) -> Dict[str, Any]:
        """上游 4603-4670 collectWebObservation。

        Read a page through Koishi Puppeteer. This is intentionally read-only:
        it rejects non-public destinations, extracts visible text only, and closes
        the page after every observation.

        本移植的平台映射：puppeteer.page + goto + evaluate → self.platform.search_web
        （搜索模式）/ render_page（访问模式，可返回 {'title','text','url'} 或纯文本）/
        http_get（裸 HTTP 兜底）；三者都不可用或都返回 None 时，走上游
        "浏览器未启用 / 读取失败" 分支写出 blocked/failed WebObservation。
        """
        config = self.browser_config
        normalized = normalize_browser_intent_draft(draft, config) if draft else None
        if not normalized or not config.get('enabled'):
            return self.save_web_observation(
                story.get('id'), participant_id, intent_id,
                (normalized or {}).get('mode') or 'visit',
                (normalized or {}).get('query') or '',
                (normalized or {}).get('url') or '',
                '', '', '浏览未执行：功能未启用或请求不符合安全规则。', 'blocked', now, persist,
            )

        target = resolve_browser_target(normalized, config)
        if not target:
            self.report('warn', story, 'intent-due', '网页浏览被安全策略拦截：模式=%s', normalized.get('mode'))
            return self.save_web_observation(
                story.get('id'), participant_id, intent_id, normalized.get('mode'),
                normalized.get('query') or '', normalized.get('url') or '',
                '', '', '浏览目标未通过公开网页安全校验。', 'blocked', now, persist,
            )

        cached = self.find_cached_web_observation(story.get('id'), participant_id, normalized, now)
        if cached:
            if not persist:
                return {**cached, 'id': 0, 'intentId': intent_id, 'accessedAt': now, 'createdAt': now}
            self.append_entry(story.get('id'), {
                'kind': 'web-observation', 'actor': 'system',
                'content': 'The character revisited a recent web observation: %s.' % (cached.get('title') or cached.get('url')),
                'occurredAt': iso(now), 'metadata': {'observationId': cached.get('id'), 'cached': True, 'status': cached.get('status')},
            }, now, participant_id)
            return cached

        # 上游 `(this.ctx as any).puppeteer; if (!puppeteer?.page)`：本移植用平台
        # 层的三个可选能力代替，全缺时即"浏览器未启用"。
        search_web = getattr(self.platform, 'search_web', None)
        render_page = getattr(self.platform, 'render_page', None)
        http_get = getattr(self.platform, 'http_get', None)
        if not callable(search_web) and not callable(render_page) and not callable(http_get):
            self.report('warn', story, 'intent-due', '网页浏览服务不可用：请安装并启用 koishi-plugin-puppeteer。')
            return self.save_web_observation(
                story.get('id'), participant_id, intent_id, normalized.get('mode'),
                normalized.get('query') or '', target, '', '', '浏览器服务不可用。', 'failed', now, persist,
            )

        def read_page() -> Any:
            """上游 puppeteer.page().goto(target) + page.evaluate()。"""
            if normalized.get('mode') == 'search' and callable(search_web):
                text = search_web(normalized.get('query') or '')
                if text is not None:
                    return '', text, target
            if callable(render_page):
                page = render_page(target)
                if page is not None:
                    if is_record(page):
                        return page.get('title'), page.get('text'), page.get('url') or target
                    return '', page, target
            if callable(http_get):
                text = http_get(target)
                if text is not None:
                    return '', text, target
            raise RuntimeError('浏览器未返回页面内容。')

        def task() -> Dict[str, Any]:
            try:
                raw_title, raw_text, final_url = read_page()
                final_url = final_url or target
                if not is_safe_public_web_url(final_url, config):
                    raise RuntimeError('页面重定向到了不允许的地址。')
                title_text = _js_string(raw_title).strip()
                # 上游 evaluate：document.body?.innerText.replace(/\r/g,'').replace(/\n{3,}/g,'\n\n').trim()
                body = _js_string(raw_text or '').replace('\r', '')
                body = re.sub(r'\n{3,}', '\n\n', body).strip()
                text = clip(body, _slice_limit(config.get('maxTextCharacters')))
                title = clip(title_text, 500)
                excerpt = clip(text, _slice_limit(config.get('maxExcerptCharacters')))
                summary = clip('%s%s' % (('%s。' % title) if title else '', excerpt), _slice_limit(config.get('maxExcerptCharacters')))
                observation = self.save_web_observation(
                    story.get('id'), participant_id, intent_id, normalized.get('mode'),
                    normalized.get('query') or '', final_url, title, excerpt,
                    summary or '页面没有可提取的正文。', 'success', now_utc(), persist,
                )
                self.report_operation('standard', 'info', story, 'intent-due', '网页读取完成 标题=%s 正文=%d字', title or '未命名页面', _js_string_length(text))
                if config.get('logObservationPreview'):
                    self.report('debug', story, 'intent-due', '网页观察节选：%s', excerpt)
                return observation
            except Exception as error:  # noqa: BLE001 - 读取失败与上游 catch 分支一一对应
                self.report('warn', story, 'intent-due', '网页读取失败：%s', error)
                return self.save_web_observation(
                    story.get('id'), participant_id, intent_id, normalized.get('mode'),
                    normalized.get('query') or '', target, '', '',
                    '网页读取失败：%s' % clip(_error_message(error), 500), 'failed', now_utc(), persist,
                )

        return self.with_browser_slot(task)

    # ================= 上游 4672-4686：saveWebObservation =================

    def save_web_observation(self, story_id: str, participant_id: str, intent_id: Optional[int],
                             mode: str, query: str, url: str, title: str, excerpt: str, summary: str,
                             status: str, now: datetime, persist: bool = True) -> Dict[str, Any]:
        """上游 4672-4686 saveWebObservation。"""
        candidate: Dict[str, Any] = {
            'id': 0, 'storyId': story_id, 'participantId': participant_id, 'intentId': intent_id,
            'mode': mode, 'query': clip(query, 500), 'url': clip(url, 2000), 'title': clip(title, 500),
            'excerpt': clip(excerpt, _slice_limit(self.browser_config.get('maxExcerptCharacters'))),
            'summary': clip(summary, _slice_limit(self.browser_config.get('maxExcerptCharacters'))),
            'status': status, 'accessedAt': now, 'createdAt': now,
        }
        if not persist:
            return candidate
        observation = self.db_create('interlude_web_observation', candidate)
        self.append_entry(story_id, {
            'kind': 'web-observation', 'actor': 'system',
            'content': web_observation_entry_content(observation), 'occurredAt': iso(now),
            'metadata': {'observationId': observation.get('id'), 'status': status, 'mode': mode, 'url': observation.get('url')},
        }, now, participant_id)
        return observation

    # ================= 上游 4691-4697：persistCollectedWebObservation =================

    def persist_collected_web_observation(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        """上游 4691-4697 persistCollectedWebObservation。

        Immediate browser reads are intentionally held in memory until the
        final narrator result survives the stale-request check. This prevents an
        obsolete two-second message burst from leaving a durable web event behind.
        """
        return self.save_web_observation(
            observation.get('storyId'), observation.get('participantId'), observation.get('intentId'),
            observation.get('mode'), observation.get('query'), observation.get('url'),
            observation.get('title'), observation.get('excerpt'), observation.get('summary'),
            observation.get('status'), observation.get('accessedAt'),
        )

    # ================= 上游 4699-4709：findCachedWebObservation =================

    def find_cached_web_observation(self, story_id: str, participant_id: str,
                                    draft: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        """上游 4699-4709 findCachedWebObservation。"""
        minutes = _nullish_number(self.browser_config.get('cacheMinutes'), 0)
        if minutes <= 0:
            return None
        cutoff = now - timedelta(milliseconds=minutes * _MINUTE_MS)
        rows = self.db_get('interlude_web_observation', {
            'storyId': story_id, 'participantId': participant_id, 'status': 'success',
        }, {
            'limit': 20, 'sort': {'accessedAt': 'desc'},
        })
        for observation in rows:
            accessed_at = to_date(observation.get('accessedAt'))
            if accessed_at is None or accessed_at < cutoff:
                continue
            if observation.get('mode') != draft.get('mode'):
                continue
            if draft.get('mode') == 'search':
                if observation.get('query') == (draft.get('query') or ''):
                    return observation
            elif observation.get('url') == (draft.get('url') or ''):
                return observation
        return None

    # ================= 上游 4711-4721：withBrowserSlot =================

    def with_browser_slot(self, task: Callable[[], Any]) -> Any:
        """上游 4711-4721 withBrowserSlot（页面并发信号量）。"""
        maximum = max(1, int(_nullish_number(self.browser_config.get('maxConcurrentPages'), 1)))
        if self.browser_active >= maximum:
            # 上游 await new Promise<void>(resolve => this.browserWaiters.push(resolve))
            event = threading.Event()
            self.browser_waiters.append(event.set)
            event.wait()
        self.browser_active += 1
        try:
            return task()
        finally:
            self.browser_active -= 1
            waiter = self.browser_waiters.pop(0) if self.browser_waiters else None
            if waiter is not None:
                waiter()

    # ================= 上游 4724-4744：scheduleNarrativeRetry =================

    def schedule_narrative_retry(self, story_id: str, participant_id: str, now: datetime,
                                 previous_attempts: int = 0) -> bool:
        """上游 4724-4744 scheduleNarrativeRetry：持久化有界重试。"""
        runtime = self.config.get('runtime') or {}
        delay_seconds = max(5, _nullish_number(runtime.get('narrativeRetryDelaySeconds'), 60))
        max_attempts = max(0, _nullish_number(runtime.get('narrativeRetryMaxAttempts'), 6))
        pending = self.db_get('interlude_intent', {'storyId': story_id, 'participantId': participant_id, 'status': 'pending'})
        existing = [intent for intent in pending if intent.get('type') == 'narrative-retry']
        if existing:
            self.db_set(
                'interlude_intent', {'id': {'$in': [intent.get('id') for intent in existing]}},
                {'status': 'cancelled', 'updatedAt': now},
            )
        if not participant_id or previous_attempts >= max_attempts:
            self.report_standalone(
                'warn', '叙事模型自动重试已停止 故事=%s 参与者=%s 已尝试=%d 上限=%d',
                story_id, participant_id or '全局', previous_attempts, _clean_int(max_attempts),
            )
            return False
        attempt = previous_attempts + 1
        not_before = now + timedelta(milliseconds=delay_seconds * _SECOND_MS)
        self.append_intent(story_id, {
            'type': 'narrative-retry',
            'summary': 'Retry the interrupted narrative turn after provider failure (attempt %d/%d).' % (attempt, _clean_int(max_attempts)),
            'notBefore': iso(not_before),
            'payload': {'narrativeRetry': True, 'userInitiated': True, 'attempt': attempt},
        }, now, participant_id)
        self.report_standalone(
            'warn', '叙事模型请求失败，已安排自动重试 故事=%s 参与者=%s 次数=%d/%d 等待=%d秒',
            story_id, participant_id, attempt, _clean_int(max_attempts), _clean_int(delay_seconds),
        )
        return True

    # ================= 上游 4746-4757：dueIntents =================

    def due_intents(self, story_id: str, now: datetime) -> List[Dict[str, Any]]:
        """上游 4746-4757 dueIntents（过期的 proactive-check 顺带取消）。"""
        intents = self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending', 'notBefore': {'$lte': now}}, {
            'sort': {'notBefore': 'asc'},
        })

        def agency_expired(intent: Dict[str, Any]) -> bool:
            if intent.get('type') != 'proactive-check':
                return False
            if not self.agency_config.get('enabled'):
                return True
            expires_at = to_date((intent.get('payload') or {}).get('expiresAt'))
            return expires_at is None or expires_at <= now

        expired_agency = [intent for intent in intents if agency_expired(intent)]
        if expired_agency:
            self.db_set(
                'interlude_intent', {'id': {'$in': [intent.get('id') for intent in expired_agency]}},
                {'status': 'cancelled', 'updatedAt': now},
            )
        expired_ids = {intent.get('id') for intent in expired_agency}
        return [
            intent for intent in intents
            if intent.get('id') not in expired_ids and not is_active_consequence(intent)
        ]

    # ================= 上游 4759-4765：upcomingNarrativeIntents =================

    def upcoming_narrative_intents(self, story_id: str, now: datetime) -> List[Dict[str, Any]]:
        """上游 4759-4765 upcomingNarrativeIntents。"""
        rows = self.db_get('interlude_intent', {
            'storyId': story_id, 'status': 'pending', 'notBefore': {'$gt': now},
        }, {'sort': {'notBefore': 'asc'}, 'limit': 30})
        internal = set(_INTERNAL_INTENT_TYPES)
        return [intent for intent in rows if intent.get('type') not in internal][:8]

    # ================= 上游 4769-4803：scheduleDueIntentWake =================

    def schedule_due_intent_wake(self, story_id: str, not_before: datetime) -> None:
        """上游 4769-4803 scheduleDueIntentWake。

        Wake the scheduler close to a short typing delay instead of waiting for
        the normal background sweep. The due intent remains the source of truth.
        """
        not_before_ms = _epoch_ms(not_before) or 0
        delay = max(0, not_before_ms - _now_ms())
        existing = self.due_intent_wake_timers.get(story_id)
        # Several <sep/> segments can be scheduled at once. Keep the earliest
        # wake-up; the next due segment schedules the following wake as needed.
        if existing is not None and existing.get('dueAt') is not None and existing.get('dueAt') <= not_before_ms:
            return
        if existing is not None:
            self.ctx.clear_timer(existing.get('cancel'))

        def wake() -> None:
            self.due_intent_wake_timers.pop(story_id, None)
            # A long narrative request can overlap the simulated typing delay. Keep
            # the intent pending and retry shortly after the scheduler is free,
            # rather than waiting for the next normal sweep.
            if self.database_resetting:
                return
            try:
                due = self.due_intents(story_id, now_utc())
                # Split segments are already committed transport events. Deliver them
                # through the story queue directly; do not make them wait for the
                # five-minute sweep or start another narrator request.
                if any(intent.get('type') == 'split-message' for intent in due):
                    self.deliver_due_split_segments(story_id)
                    if all(intent.get('type') == 'split-message' for intent in due):
                        return
                if self.sweep_running or self.has_pending_narrative(story_id):
                    retry_at = _now_ms() + _SECOND_MS
                    retry = self.ctx.set_timeout(wake, _SECOND_MS)
                    self.due_intent_wake_timers[story_id] = {'cancel': retry, 'dueAt': retry_at}
                    return
                self.sweep()
            except Exception as error:  # noqa: BLE001 - 上游 .catch(reportStandaloneOperation)
                self.report_standalone_operation('diagnostic', 'debug', '到期消息唤醒失败 错误=%s', error)

        timer = self.ctx.set_timeout(wake, delay)
        self.due_intent_wake_timers[story_id] = {'cancel': timer, 'dueAt': not_before_ms}
        self.report_standalone_operation(
            'diagnostic', 'debug', '已设置到期计时器 故事=%s 触发时间=%s 等待=%dms',
            story_id, format_log_time(not_before, 'Asia/Shanghai'), delay,
        )

    # ================= 上游 4805-4811：scheduleNextSplitWake =================

    def schedule_next_split_wake(self, story_id: str) -> None:
        """上游 4805-4811 scheduleNextSplitWake。"""
        pending = self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending', 'type': 'split-message'}, {
            'sort': {'notBefore': 'asc'}, 'limit': 1,
        })
        if pending:
            not_before = pending[0].get('notBefore')
            if not_before is not None:
                self.schedule_due_intent_wake(story_id, not_before)

    # ================= 上游 4814-4890：deliverDueSplitSegments =================

    def deliver_due_split_segments(self, story_id: str) -> None:
        """上游 4814-4890 deliverDueSplitSegments：不调用叙事者，直接投递 <sep/> 段。"""

        def task() -> None:
            story = self.get_story(story_id)
            now = now_utc()
            runtime = self.config.get('runtime') or {}
            due = self.db_get('interlude_intent', {
                'storyId': story_id, 'status': 'pending', 'type': 'split-message', 'notBefore': {'$lte': now},
            }, {'sort': {'notBefore': 'asc'}, 'limit': 20})
            next_intent = due[0] if due else None
            if next_intent:
                intent = next_intent
                content = clip((intent.get('payload') or {}).get('content'), _slice_limit(runtime.get('maxMessageCharacters')))
                automatic_delivery = automatic_delivery_from_payload(intent.get('payload'))
                participant = self.get_participant(intent.get('participantId')) if intent.get('participantId') else None
                if intent.get('participantId') and intent.get('participantId') in self.interrupted_typing_participants:
                    # The incoming-message transaction is queued behind this one. Leave
                    # every split intent pending so it can cancel them and carry their
                    # exact draft contents into the replacement writing request.
                    return
                if not content or not participant or participant.get('status') != 'active':
                    self.db_set('interlude_intent', {'id': intent.get('id')}, {'status': 'cancelled', 'updatedAt': now})
                    reference = restore_message_event(intent.get('payload'), content or '')
                    if reference:
                        self.update_script_delivery_outcome(story_id, reference, 'cancelled', now, 'delivery-target-unavailable')
                else:
                    # Start transport while still holding the story queue. Input that
                    # arrived before this point sets interruptedTypingParticipants and
                    # cancels the chain; input after this point cannot retract a message
                    # whose adapter send has already begun.
                    message: OutgoingMessageDraft = {
                        'participantId': participant.get('id'),
                        'content': content,
                        'automaticDelivery': automatic_delivery,
                        'scriptEvent': restore_message_event(intent.get('payload'), content),
                    }
                    delivered = self.send_outgoing_messages(
                        story,
                        [message],
                        None,
                        None,
                        lambda target: target.get('id') in self.interrupted_typing_participants,
                        False,
                    )
                    if not delivered:
                        if participant.get('id') in self.interrupted_typing_participants:
                            return
                        if message.get('scriptEvent'):
                            self.update_script_delivery_outcome(story_id, message['scriptEvent'], 'pending', now, 'delivery-unconfirmed-retry-scheduled')
                        retry_at = now + timedelta(seconds=30)
                        self.db_set('interlude_intent', {'id': intent.get('id')}, {'notBefore': retry_at, 'updatedAt': now})
                        self.schedule_due_intent_wake(story_id, retry_at)
                        return
                    self.append_entry(story_id, {
                        'kind': 'character-message', 'actor': 'character', 'content': content,
                        'occurredAt': iso(now), 'metadata': delivery_entry_metadata(message, {'splitSegment': True}),
                    }, now, participant.get('id'))
                    if message.get('scriptEvent'):
                        self.update_script_delivery_outcome(story_id, message['scriptEvent'], 'delivered', now)
                    if message.get('automaticDelivery'):
                        self.record_automatic_delivery(story_id, participant.get('id'), message['automaticDelivery'], now)
                    self.record_character_message(participant, now)
                    self.db_set('interlude_intent', {'id': intent.get('id')}, {'status': 'completed', 'updatedAt': now})
            # When several segments became overdue together, restore a fresh
            # typing interval instead of immediately draining the backlog.
            remaining = due[1:]
            if remaining:
                following = remaining[0]
                next_at = following.get('notBefore')
                if next_at is not None and next_at <= now:
                    following_content = clip((following.get('payload') or {}).get('content'), _slice_limit(runtime.get('maxMessageCharacters')))
                    if following_content:
                        self.db_set('interlude_intent', {'id': following.get('id')}, {
                            'notBefore': now + timedelta(milliseconds=self.typing_delay_milliseconds(following_content)),
                            'updatedAt': now,
                        })
            self.schedule_next_split_wake(story_id)

        self.serial(story_id, task)

    # ================= 上游 4893-4897：pendingFollowUpCommitments =================

    def pending_follow_up_commitments(self, story_id: str, participant_id: str) -> List[Dict[str, Any]]:
        """上游 4893-4897 pendingFollowUpCommitments（只有极小的关系局部承诺）。"""
        return self.db_get('interlude_intent', {
            'storyId': story_id, 'participantId': participant_id, 'type': 'follow-up-commitment', 'status': 'pending',
        }, {'limit': 2, 'sort': {'notBefore': 'asc'}})

    # ================= 上游 4899-4938：appendFollowUpCommitment =================

    def append_follow_up_commitment(self, story: InterludeStory, participant_id: str,
                                    draft: Dict[str, Any], fallback_source_entry_id: Optional[int],
                                    now: datetime, origin_delivery_event_id: Optional[str] = None) -> None:
        """上游 4899-4938 appendFollowUpCommitment。"""
        story_id = story.get('id')
        if origin_delivery_event_id:
            previous = self.db_get('interlude_intent', {
                'storyId': story_id, 'participantId': participant_id,
                'type': 'follow-up-commitment', 'summary': draft.get('summary'),
            })
            if any((intent.get('payload') or {}).get('originDeliveryEventId') == origin_delivery_event_id for intent in previous):
                return
        pending = self.db_get('interlude_intent', {
            'storyId': story_id, 'participantId': participant_id, 'type': 'follow-up-commitment', 'status': 'pending',
        }, {'limit': 3, 'sort': {'notBefore': 'asc'}})
        key = normalize_follow_up_summary(draft.get('summary'))
        duplicate = next(
            (intent for intent in pending if normalize_follow_up_summary(intent.get('summary')) == key),
            None,
        )
        if duplicate is not None or len(pending) >= 2:
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '承诺回访未重复创建 参与者=%s 原因=%s', participant_id,
                '同一事项待处理' if duplicate is not None else '待处理上限',
            )
            return
        source_entry_ids = [
            *[
                int(item) for item in (draft.get('sourceEntryIds') or [])
                if _is_safe_integer(item) and item > 0
            ],
            *([fallback_source_entry_id] if fallback_source_entry_id else []),
        ][-4:]
        expires_at = follow_up_expires_at(draft.get('expiresAt'), now)
        self.append_intent(story_id, {
            'type': 'follow-up-commitment', 'summary': draft.get('summary'), 'notBefore': draft.get('notBefore'),
            'payload': {
                'kind': draft.get('kind'), 'sourceEntryIds': source_entry_ids, 'expiresAt': iso(expires_at),
                'requiresVisibleOutcome': True, 'userInitiated': True, 'originDeliveryEventId': origin_delivery_event_id,
            },
        }, now, participant_id)
        not_before = to_date(draft.get('notBefore'))
        if not_before is not None:
            self.schedule_due_intent_wake(story_id, not_before)
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '已登记承诺回访 参与者=%s 类型=%s 到期=%s',
            participant_id, draft.get('kind'), format_log_time(not_before, (story.get('setting') or {}).get('timezone')),
        )

    # ================= 上游 4940-4976：applyFollowUpResolutions =================

    def apply_follow_up_resolutions(self, story_id: str, participant_id: str,
                                    resolutions: List[Dict[str, Any]],
                                    interaction: Optional[NarrativeInteraction], now: datetime,
                                    delivery_event_id: Optional[str] = None) -> Set[int]:
        """上游 4940-4976 applyFollowUpResolutions。"""
        reply = (interaction.get('reply') or {}) if is_record(interaction) else {}
        if (
            not resolutions
            or reply.get('mode') != 'immediate'
            or not (reply.get('content') or '').strip()
        ):
            return set()
        ids = [item.get('id') for item in resolutions]
        rows = self.db_get('interlude_intent', {
            'storyId': story_id, 'participantId': participant_id, 'type': 'follow-up-commitment',
            'status': 'pending', 'id': {'$in': ids},
        })
        resolved: Set[int] = set()
        for resolution in resolutions:
            intent = next((item for item in rows if item.get('id') == resolution.get('id')), None)
            if intent is None:
                continue
            if delivery_event_id and (intent.get('payload') or {}).get('resolutionEventId') == delivery_event_id:
                resolved.add(intent.get('id'))
                continue
            if resolution.get('outcome') == 'rescheduled':
                next_at = to_date(resolution.get('notBefore'))
                if next_at is None or next_at <= now or (_epoch_ms(next_at) - _epoch_ms(now)) > 12 * _HOUR_MS:
                    continue
                payload = intent.get('payload') or {}
                self.db_set('interlude_intent', {'id': intent.get('id')}, {
                    'notBefore': next_at,
                    'payload': {
                        **payload,
                        'reschedules': _clean_int(_nullish_number(payload.get('reschedules'), 0) + 1),
                        'resolutionEventId': delivery_event_id,
                    },
                    'updatedAt': now,
                })
                self.schedule_due_intent_wake(story_id, next_at)
            else:
                self.db_set('interlude_intent', {'id': intent.get('id')}, {
                    'status': 'cancelled' if resolution.get('outcome') == 'cancelled' else 'completed',
                    'updatedAt': now,
                })
            resolved.add(intent.get('id'))
        return resolved

    # ================= 上游 4978-5000：deferUnresolvedDueFollowUps =================

    def defer_unresolved_due_follow_ups(self, story_id: str, participant_id: str,
                                        context_intents: List[Dict[str, Any]], resolved_ids: Set[int],
                                        interaction: Optional[NarrativeInteraction], now: datetime) -> None:
        """上游 4978-5000 deferUnresolvedDueFollowUps。"""
        due = [
            intent for intent in context_intents
            if intent.get('type') == 'follow-up-commitment' and intent.get('participantId') == participant_id
        ]
        if not due:
            return
        for intent in due:
            if intent.get('id') in resolved_ids:
                continue
            payload = intent.get('payload') or {}
            retry_at = now + timedelta(minutes=20)
            self.db_set('interlude_intent', {'id': intent.get('id')}, {
                'notBefore': retry_at,
                'payload': {
                    **payload,
                    'deferredChecks': _clean_int(_nullish_number(payload.get('deferredChecks'), 0) + 1),
                },
                'updatedAt': now,
            })
            self.schedule_due_intent_wake(story_id, retry_at)
            self.report_operation(
                'diagnostic', 'debug', self.get_story(story_id), 'intent-due',
                '承诺回访等待明确结算及完整投递确认，已保留重查 参与者=%s', participant_id,
            )

    # ================= 上游 5002-5044：appendProactiveCheck =================

    def append_proactive_check(self, story: InterludeStory, candidate: Dict[str, Any],
                               not_before: datetime, reason: str, now: datetime) -> None:
        """上游 5002-5044 appendProactiveCheck。"""
        expires_at = to_date(candidate.get('expiresAt'))
        if expires_at is None or expires_at <= now or not_before >= expires_at:
            return
        fingerprint = proactive_candidate_fingerprint(candidate)
        pending = self.db_get('interlude_intent', {
            'storyId': story.get('id'),
            'participantId': candidate.get('participantId'),
            'status': 'pending',
            'type': 'proactive-check',
        })
        if any((intent.get('payload') or {}).get('fingerprint') == fingerprint for intent in pending):
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                'Agency 主动联系候选去重 参与者=%s 指纹=%s', candidate.get('participantId'), fingerprint,
            )
            return
        self.append_intent(story.get('id'), {
            'type': 'proactive-check',
            'summary': 'Re-evaluate a life-grounded contact motive: %s' % candidate.get('motive'),
            'notBefore': iso(not_before),
            'participantId': candidate.get('participantId'),
            'payload': {
                'origin': candidate.get('origin'),
                'motive': candidate.get('motive'),
                'disclosure': candidate.get('disclosure'),
                'sourceEntryIds': candidate.get('sourceEntryIds') or [],
                'willingness': candidate.get('willingness'),
                'expiresAt': candidate.get('expiresAt'),
                'fingerprint': fingerprint,
                'agencyReason': reason,
                'userInitiated': False,
            },
        }, now, candidate.get('participantId') or '')
        self.schedule_due_intent_wake(story.get('id'), not_before)
        self.report_operation(
            'standard', 'info', story, 'advance',
            'Agency 已安排主动联系重查 参与者=%s 时间=%s 原因=%s',
            candidate.get('participantId'), format_log_time(not_before, (story.get('setting') or {}).get('timezone')), reason,
        )

    # ================= 上游 5046-5093：cancelPendingOutgoingMessages =================

    def cancel_pending_outgoing_messages(self, story_id: str, participant_id: str, now: datetime,
                                         cancel_planned: bool = True) -> List[Dict[str, Any]]:
        """上游 5046-5093 cancelPendingOutgoingMessages。"""
        completed = False
        try:
            intents = self.db_get('interlude_intent', {'storyId': story_id, 'participantId': participant_id, 'status': 'pending'})
            matching = [
                intent for intent in intents
                if intent.get('participantId') == participant_id and (
                    intent.get('type') == 'split-message'
                    or (
                        cancel_planned
                        and (intent.get('type') == 'delayed-reply' or intent.get('type') == 'cross-conversation-message')
                    )
                )
            ]
            if not matching:
                completed = True
                return matching

            self.db_set('interlude_intent', {'id': {'$in': [intent.get('id') for intent in matching]}}, {
                'status': 'cancelled',
                'updatedAt': now,
            })
            for intent in matching:
                content = clip(
                    (intent.get('payload') or {}).get('content'),
                    _slice_limit((self.config.get('runtime') or {}).get('maxMessageCharacters')),
                )
                reference = restore_message_event(intent.get('payload'), content)
                if reference:
                    self.update_script_delivery_outcome(story_id, reference, 'cancelled', now, 'superseded-by-new-message')
            wake = self.due_intent_wake_timers.get(story_id)
            if wake is not None:
                self.ctx.clear_timer(wake.get('cancel'))
                self.due_intent_wake_timers.pop(story_id, None)
            self.schedule_next_split_wake(story_id)
            interrupted_drafts = [
                clip((intent.get('payload') or {}).get('content'), _slice_limit((self.config.get('runtime') or {}).get('maxMessageCharacters')))
                for intent in matching
                if intent.get('type') == 'split-message'
            ]
            interrupted_drafts = [draft for draft in interrupted_drafts if draft]
            content = (
                "The protagonist wanted to send %s, but had not finished typing before the user's new message arrived."
                % ' and '.join(json.dumps(draft, ensure_ascii=False) for draft in interrupted_drafts)
                if interrupted_drafts
                else 'A newer user message superseded a planned outgoing message before it was sent.'
            )
            self.append_entry(story_id, {
                'kind': 'intent-cancelled',
                'actor': 'system',
                'content': content,
                'occurredAt': iso(now),
                'metadata': {'intentIds': [intent.get('id') for intent in matching], 'interruptedDrafts': interrupted_drafts},
            }, now, participant_id)
            completed = True
            return matching
        finally:
            if completed:
                self.interrupted_typing_participants.discard(participant_id)

    # ================= 上游 5095-5099：sendScheduledMessages =================

    def send_scheduled_messages(self, story: InterludeStory, messages: List[OutgoingMessageDraft]) -> List[OutgoingMessageDraft]:
        """上游 5095-5099 sendScheduledMessages。"""
        delivered = self.send_outgoing_messages(story, messages)
        self.confirm_outgoing_deliveries(story, delivered)
        return delivered

    # ================= 上游 5107-5187：sendOutgoingMessages =================

    def send_outgoing_messages(self, story: InterludeStory, messages: List[OutgoingMessageDraft],
                               current: Optional[Dict[str, Any]] = None, session: Any = None,
                               should_cancel: Optional[Callable[[Dict[str, Any]], bool]] = None,
                               record_failures: bool = True) -> List[OutgoingMessageDraft]:
        """上游 5107-5187 sendOutgoingMessages。

        Immediate replies may reuse the incoming Session; cross-account and timed
        messages are delivered through the target participant's channel instead.
        This is the boundary that prevents a shared story from accidentally
        sending every reply back to the account that happened to trigger the turn.

        平台映射：session.send / bot.sendMessage → self.platform.send_private / send_group；
        返回 None 等价上游“没有可用机器人账号”，非 None 视为已投递（返回值即 messageId）。
        """
        delivered: List[OutgoingMessageDraft] = []
        if not messages:
            return delivered
        ids = list(dict.fromkeys(message.get('participantId') for message in messages if message.get('participantId')))
        by_id: Dict[Any, Dict[str, Any]] = {}
        if current and current.get('id') in ids:
            by_id[current.get('id')] = current
        missing_ids = [identifier for identifier in ids if identifier not in by_id]
        for participant in [self.get_participant(identifier) for identifier in missing_ids]:
            if participant:
                by_id[participant.get('id')] = participant
        for message in messages:
            target = by_id.get(message.get('participantId'))
            if not target:
                self.report('warn', story, 'intent-due', '无法投递消息：参与者不存在 %s', message.get('participantId'))
                if record_failures:
                    self.record_outgoing_delivery_failure(story, message.get('participantId'), message, 'participant-not-found')
                continue
            if not self.can_handle_participant(target):
                self.report('warn', story, 'intent-due', '消息被当前账号白名单拦截 参与者=%s', target.get('id'))
                if record_failures:
                    self.record_outgoing_delivery_failure(story, target.get('id'), message, 'participant-not-allowed')
                continue
            if should_cancel is not None and should_cancel(target):
                self.report_operation('standard', 'info', story, 'user-message', '新消息打断主角输入，停止发送后续分段 参与者=%s', target.get('id'))
                continue
            try:
                self.report_operation('standard', 'info', story, 'intent-due', '消息投递开始 参与者=%s', target.get('id'))
                literal_quote_message_id = self.resolve_literal_quote_message_id(story.get('id'), target.get('id'), message.get('content'))
                literal_quote_only = is_literal_quote_only(message.get('content'))
                if literal_quote_only and not literal_quote_message_id:
                    self.report('warn', story, 'intent-due', '已阻止无法映射的伪引用文本 参与者=%s', target.get('id'))
                    if record_failures:
                        self.record_outgoing_delivery_failure(story, target.get('id'), message, 'literal-quote-target-not-found')
                    continue
                if literal_quote_message_id:
                    message['quoteMessageId'] = literal_quote_message_id
                outgoing_content = '\u200b' if literal_quote_message_id else message.get('content')
                if (self.config.get('logging') or {}).get('logMessageContent'):
                    self.report(
                        'info', story, 'intent-due', '主角消息内容：%s',
                        (message.get('content') or '')[:_slice_limit((self.config.get('logging') or {}).get('previewLength'))],
                    )
                if session is not None and current is not None and current.get('id') == target.get('id'):
                    self._send_via_session(story, target, outgoing_content, literal_quote_message_id, session)
                    delivered.append(message)
                    continue
                if self.desktop_delivery_handler:
                    # typ-0 worker：没有 adapter bot，后台投递统一走宿主渠道。结果语义与
                    # bot.sendMessage 一致——成功 resolve 进 delivered 由账本确认，失败
                    # reject 进 catch 由 recordOutgoingDeliveryFailure 记录。
                    outcome = self.desktop_delivery_handler({
                        'participantId': target.get('id'), 'selfId': target.get('selfId'), 'platform': target.get('platform'),
                        'channelId': target.get('channelId'), 'kind': 'private', 'content': message.get('content'),
                        **({'quoteMessageId': message.get('quoteMessageId')} if message.get('quoteMessageId') else {}),
                    })
                    if outcome.get('ok'):
                        delivered.append(message)
                    else:
                        raise RuntimeError(outcome.get('error') or 'typ-0 宿主投递失败。')
                    continue
                message_id = self._send_participant_message(story, target, outgoing_content, literal_quote_message_id, session)
                if not message_id:
                    self.report('warn', story, 'intent-due', '没有可用机器人账号投递消息 参与者=%s', target.get('id'))
                    if record_failures:
                        self.record_outgoing_delivery_failure(story, target.get('id'), message, 'bot-not-found')
                    continue
                delivered.append(message)
            except Exception as error:  # noqa: BLE001 - 投递失败按上游 warn + 记账分支
                self.report('warn', story, 'intent-due', '消息投递失败 参与者=%s 错误=%s', target.get('id'), error)
                if record_failures:
                    self.record_outgoing_delivery_failure(story, target.get('id'), message, 'transport-error: %s' % error)
        return delivered

    def _send_via_session(self, story: InterludeStory, target: Dict[str, Any], content: Any,
                          quote_message_id: Optional[str], session: Any) -> Optional[str]:
        """上游 `session.send(outgoingContent)`：群会话走 send_group，私聊走 send_private。"""
        if not getattr(session, 'isDirect', True):
            channel_id = getattr(session, 'channelId', '') or target.get('channelId')
            return self.platform.send_group(story, channel_id, content, quote_message_id, session)
        return self.platform.send_private(target, content, quote_message_id)

    def _send_participant_message(self, story: InterludeStory, target: Dict[str, Any], content: Any,
                                  quote_message_id: Optional[str], session: Any = None) -> Optional[str]:
        """上游 `bot.sendMessage(target.channelId, outgoingContent)` 的平台层等价物。"""
        if session is not None and not getattr(session, 'isDirect', True):
            channel_id = getattr(session, 'channelId', '') or target.get('channelId')
            return self.platform.send_group(story, channel_id, content, quote_message_id, session)
        return self.platform.send_private(target, content, quote_message_id)

    # ================= 上游 5193-5231：confirmOutgoingDeliveries =================

    def confirm_outgoing_deliveries(self, story: InterludeStory, delivered: List[OutgoingMessageDraft]) -> List[Dict[str, Any]]:
        """上游 5193-5231 confirmOutgoingDeliveries。

        Confirm visible delivery only after the platform accepted the message.
        Failed attempts become explicit system evidence rather than fictional
        character speech, and deliberately do not auto-retry to avoid duplicates
        when an adapter fails after it has already accepted a request.
        """
        confirmed: List[Dict[str, Any]] = []
        for message in delivered:

            def task(message: OutgoingMessageDraft = message) -> Optional[Dict[str, Any]]:
                participant = self.get_participant(message.get('participantId'))
                if not participant:
                    return None
                now = now_utc()
                content = '[主角引用了此前的一条消息]' if message.get('quoteMessageId') else message.get('content')
                extra = (
                    {'quoteMessageId': message.get('quoteMessageId'), 'quoteTransport': True}
                    if message.get('quoteMessageId') else {}
                )
                persisted_entry = self.append_entry(story.get('id'), {
                    'kind': 'character-message', 'actor': 'character', 'content': content,
                    'occurredAt': iso(now),
                    'metadata': delivery_entry_metadata(message, extra),
                }, now, participant.get('id'))
                if message.get('scriptEvent'):
                    self.update_script_delivery_outcome(story.get('id'), message['scriptEvent'], 'delivered', now)
                self.record_character_message(participant, now)
                if message.get('automaticDelivery'):
                    self.record_automatic_delivery(story.get('id'), participant.get('id'), message['automaticDelivery'], now)
                delay = 0
                for index, segment in enumerate(message.get('laterSegments') or []):
                    delay += self.typing_delay_milliseconds(segment)
                    send_at = now + timedelta(milliseconds=delay)
                    self.append_intent(story.get('id'), {
                        'type': 'split-message', 'summary': 'The character is still typing the next message segment.',
                        'notBefore': iso(send_at),
                        'payload': {
                            'content': segment, 'visibleMessage': True, 'userInitiated': message.get('userInitiated') is True,
                            **script_event_payload(message, index + 1),
                            **({'automaticDelivery': message['automaticDelivery']} if message.get('automaticDelivery') else {}),
                        },
                    }, now, participant.get('id'))
                    self.schedule_due_intent_wake(story.get('id'), send_at)
                return persisted_entry

            entry = self.serial(story.get('id'), task)
            if entry:
                confirmed.append(entry)
        return confirmed

    # ================= 上游 5233-5248：recordOutgoingDeliveryFailure =================

    def record_outgoing_delivery_failure(self, story: InterludeStory, participant_id: str,
                                         message: OutgoingMessageDraft, reason: str) -> None:
        """上游 5233-5248 recordOutgoingDeliveryFailure。"""

        def task() -> None:
            now = now_utc()
            if message.get('scriptEvent'):
                self.update_script_delivery_outcome(story.get('id'), message['scriptEvent'], 'failed', now, clip(reason, _REASON_LIMIT))
            self.append_entry(story.get('id'), {
                'kind': 'outgoing-delivery-failed', 'actor': 'system',
                'content': '未投递的主角消息（仍未发送，不能视为用户已收到）：%s'
                           % clip(message.get('content'), _slice_limit((self.config.get('runtime') or {}).get('maxMessageCharacters'))),
                'occurredAt': iso(now),
                'metadata': {
                    'status': 'failed', 'participantId': participant_id, 'reason': clip(reason, _REASON_LIMIT),
                    **(message.get('scriptEvent') or {}),
                    **({'automaticDelivery': message['automaticDelivery']} if message.get('automaticDelivery') else {}),
                },
            }, now, participant_id)

        self.serial(story.get('id'), task)

    # ================= 上游 5253-5298：updateScriptDeliveryOutcome =================

    def update_script_delivery_outcome(self, story_id: str, reference: Dict[str, Any], status: str,
                                       at: datetime, reason: Optional[str] = None) -> None:
        """上游 5253-5298 updateScriptDeliveryOutcome。

        Update the M6.1 ledger stored beside the authoritative script. Callers
        already hold the story queue, so this helper never opens a nested serial
        section and cannot reorder platform delivery.
        """
        if not _is_safe_integer(reference.get('scriptEntryId')):
            return
        try:
            rows = self.db_get('interlude_script_entry', {'id': reference.get('scriptEntryId')})
            entry = rows[0] if rows else None
            if not entry or entry.get('storyId') != story_id or (entry.get('metadata') or {}).get('commitId') != reference.get('commitId'):
                return
            normalized = {
                'commitId': reference.get('commitId'),
                'eventId': reference.get('eventId'),
                'scriptEntryId': int(reference.get('scriptEntryId')),
                'segmentIndex': _nullish(reference.get('segmentIndex'), _nullish(reference.get('bubbleIndex'), 0)),
            }
            entry_metadata = entry.get('metadata') or {}
            actions = update_script_delivery_actions(entry_metadata.get('deliveryActions'), normalized, status, at, reason)
            metadata: Dict[str, Any] = {**entry_metadata, **({'deliveryActions': actions} if actions else {})}
            if actions:
                self.db_set('interlude_script_entry', {'id': entry.get('id')}, {'metadata': metadata})
            delivery_actions = metadata.get('deliveryActions')
            delivered = None
            if isinstance(delivery_actions, (list, tuple)):
                delivered = next((
                    action for action in delivery_actions
                    if is_record(action)
                    and action.get('commitId') == reference.get('commitId')
                    and action.get('eventId') == reference.get('eventId')
                    and action.get('status') == 'delivered'
                ), None)
            events = metadata.get('scriptEvents') if isinstance(metadata.get('scriptEvents'), (list, tuple)) else []
            event = next((
                item for item in events
                if is_record(item)
                and item.get('commitId') == reference.get('commitId')
                and item.get('eventId') == reference.get('eventId')
                and item.get('kind') == 'outgoing-message'
            ), None)
            if (
                delivered
                and event is not None
                and event.get('participantId')
                and metadata.get('followUpResolutionEventId') != event.get('eventId')
            ):
                resolutions = normalize_follow_up_resolutions((event.get('metadata') or {}).get('followUpResolutions'))
                occurred_at = to_date(entry.get('occurredAt'))
                commitment = normalize_follow_up_commitment(
                    (event.get('metadata') or {}).get('followUpCommitment'),
                    occurred_at if occurred_at is not None else at,
                )
                if resolutions or commitment:
                    self.apply_follow_up_resolutions(
                        story_id, event.get('participantId'), resolutions,
                        {'seen': False, 'reply': {'mode': 'immediate', 'content': event.get('content')}}, at, event.get('eventId'),
                    )
                    if commitment:
                        self.append_follow_up_commitment(
                            self.get_story(story_id), event.get('participantId'), commitment, entry.get('id'), at, event.get('eventId'),
                        )
                    self.db_set('interlude_script_entry', {'id': entry.get('id')}, {
                        'metadata': {**metadata, 'followUpResolutionEventId': event.get('eventId')},
                    })
        except Exception as error:  # noqa: BLE001 - M6.1 只是观测，不能阻断投递链
            # M6.1 is observational: a failed ledger write must not prevent the
            # caller from scheduling remaining bubbles or executing platform actions.
            self.report_standalone(
                'warn', '投递账本记录失败 故事=%s 事件=%s；保留平台结果并继续原投递链 错误=%s',
                story_id, reference.get('eventId'), error,
            )

    # ================= 上游 5300-5311：recordPlatformDeliveryOutcome =================

    def record_platform_delivery_outcome(self, story_id: str, reference: Dict[str, Any], status: str,
                                         reason: Optional[str] = None) -> None:
        """上游 5300-5311 recordPlatformDeliveryOutcome。"""
        try:
            self.serial(story_id, lambda: self.update_script_delivery_outcome(story_id, reference, status, now_utc(), reason))
        except Exception as error:  # noqa: BLE001 - 平台结果记账失败只上报
            self.report_standalone(
                'warn', '平台行动结果记录失败 故事=%s 事件=%s segment=%d 错误=%s',
                story_id, reference.get('eventId'), reference.get('segmentIndex'), error,
            )

    # ================= 上游 5313-5321：resolveLiteralQuoteMessageId =================

    def resolve_literal_quote_message_id(self, story_id: str, participant_id: str, content: Any) -> Optional[str]:
        """上游 5313-5321 resolveLiteralQuoteMessageId。"""
        quoted = literal_quote_text(content)
        if not quoted:
            return None
        entries = self.db_get('interlude_script_entry', {'storyId': story_id, 'participantId': participant_id}, {
            'limit': 120, 'sort': {'occurredAt': 'desc'},
        })
        for entry in entries:
            if (entry.get('content') or '').strip() != quoted:
                continue
            message_id = targetable_message_id((entry.get('metadata') or {}).get('messageId'))
            if message_id:
                return message_id
        return None

    # ================= 上游 5325-5370：recordAutomaticDelivery =================

    def record_automatic_delivery(self, story_id: str, participant_id: str,
                                  delivery: Dict[str, Any], now: datetime) -> None:
        """上游 5325-5370 recordAutomaticDelivery。

        Records only completed background deliveries. It is intentionally a
        bounded action ledger, rather than a duplicate conversation transcript.
        """
        try:
            if self.urge_config.get('enabled') and delivery.get('sourceEntryId'):
                current = self.get_story(story_id)
                state = decode_story_state(current.get('state'))
                prior = normalize_urge_state((state.get('extensions') or {}).get('urge'), _epoch_ms(now) or 0)
                urge = acknowledge_urge(prior, participant_id, delivery.get('sourceEntryId'), _epoch_ms(now) or 0)
                if urge is not prior:
                    self.db_set('interlude_story', {'id': story_id}, {
                        'state': encode_story_state({**state, 'extensions': {**(state.get('extensions') or {}), 'urge': urge}}),
                        'updatedAt': now,
                    })
                    self.schedule_urge_advance(self.get_story(story_id), now)
            if delivery.get('sourceEntryId'):
                rows = self.db_get('interlude_script_entry', {'storyId': story_id, 'id': delivery.get('sourceEntryId')})
                entry = rows[0] if rows else None
                actions = (entry.get('metadata') or {}).get('deliveryActions') if entry else None
                if isinstance(actions, (list, tuple)):
                    speech = [
                        action for action in actions
                        if is_record(action) and action.get('participantId') == participant_id
                        and action.get('eventKind') == 'outgoing-message'
                    ]
                    if speech and any(action.get('status') != 'delivered' for action in speech):
                        return
            story = self.get_story(story_id)
            state = decode_story_state(story.get('state'))
            summary = clip(delivery.get('summary'), 240).strip()
            if not summary:
                return
            prior_summaries = state.get('automaticDeliverySummaries') or []
            same = next((
                item for item in prior_summaries
                if item.get('participantId') == participant_id and item.get('sourceEntryId') == delivery.get('sourceEntryId')
            ), None)
            next_summary = {
                'participantId': participant_id,
                'summary': merge_delivery_summary(same.get('summary'), summary) if same is not None else summary,
                **({'sourceEntryId': delivery.get('sourceEntryId')} if delivery.get('sourceEntryId') else {}),
                'deliveredAt': iso(now),
            }
            retained = [item for item in prior_summaries if item is not same]
            retained.append(next_summary)
            self.db_set('interlude_story', {'id': story.get('id')}, {
                'state': encode_story_state({**state, 'automaticDeliverySummaries': retained[-6:]}), 'updatedAt': now,
            })
        except Exception as error:  # noqa: BLE001 - 摘要只是投影，不能影响剩余气泡
            # A summary is a projection, never a prerequisite for remaining bubbles.
            self.report_standalone('warn', '自动通信摘要记录失败，保留原文与投递主链 错误=%s', error)

    # ================= 上游 5372-5377：splitOutgoingMessage =================

    def split_outgoing_message(self, content: str) -> List[str]:
        """上游 5372-5377 splitOutgoingMessage。"""
        if (self.config.get('runtime') or {}).get('splitReplyMessages') is False:
            return [content]
        separator = (((self.config.get('runtime') or {}).get('messageSeparator') or '').strip()) or '<sep/>'
        if not separator or separator not in content:
            return [content]
        return [part.strip() for part in content.split(separator) if part.strip()]

    # ================= 上游 5379-5387：typingDelayMilliseconds =================

    def typing_delay_milliseconds(self, next_segment: str) -> int:
        """上游 5379-5387 typingDelayMilliseconds。"""
        runtime = self.config.get('runtime') or {}
        base_seconds = max(0, _nullish_number(runtime.get('typingBaseDelaySeconds'), 1))
        characters_per_second = max(1, _nullish_number(runtime.get('typingCharactersPerSecond'), 8))
        maximum_seconds = max(base_seconds, _nullish_number(runtime.get('typingMaxDelaySeconds'), 12))
        nominal = min(maximum_seconds, base_seconds + math.ceil(_js_string_length(next_segment) / characters_per_second))
        jitter = max(0, min(0.5, _number_or(_nullish(runtime.get('typingJitterRatio'), 0.3), 0)))
        factor = 1 + (random.random() * 2 - 1) * jitter if jitter else 1
        return max(250, min(int(maximum_seconds * _SECOND_MS), _js_round(nominal * factor * _SECOND_MS)))

    # ================= 上游 5389-5393：findBotForParticipant =================

    def find_bot_for_participant(self, participant: Dict[str, Any]) -> Any:
        """上游 5389-5393 findBotForParticipant（ctx.bots → self.platform.list_bots()）。

        说明：本移植的投递路径直接走 PlatformAdapter.send_private / send_group
        （见 sendOutgoingMessages），本方法按上游语义保留供平台层/桌面端复用。
        """
        for bot in self.platform.list_bots() or []:
            if str(_object_field(bot, 'selfId')) == str(participant.get('selfId')) and (
                _object_field(bot, 'platform') == participant.get('platform')
                or (
                    is_one_bot_platform(_object_field(bot, 'platform'))
                    and is_one_bot_platform(participant.get('platform'))
                )
            ):
                return bot
        # list_bots 为空的适配器（如微信侧按会话名投递）用平台账号查找兜底。
        find_bot = getattr(self.platform, 'find_bot', None)
        if callable(find_bot):
            return find_bot(participant.get('platform'), participant.get('selfId'))
        return None

    # ================= 上游 5419-5440：scheduleUrgeAdvance =================

    def schedule_urge_advance(self, story: InterludeStory, anchor: datetime, incoming: bool = False) -> None:
        """上游 5419-5440 scheduleUrgeAdvance。"""
        config = self.urge_config
        state = decode_story_state(story.get('state'))
        anchor_ms = _epoch_ms(anchor) or 0
        urge = normalize_urge_state((state.get('extensions') or {}).get('urge'), anchor_ms)
        if incoming:
            urge = urge_user_event(urge, anchor_ms)
        timezone_name = (story.get('setting') or {}).get('timezone')
        rest = active_rest_window(self.auto_advance_config.get('restWindows') or [], timezone_name, anchor)
        window = active_agency_window(state.get('agencyWindow'), anchor)
        planned = plan_urge(
            urge, anchor_ms, config,
            automatic_interval_minutes(story, anchor, self.auto_advance_config) if rest else 0,
            bool(window) and (window.get('deviceAccess') != 'available' or window.get('activityLoad') == 'overloaded'),
        )
        ordinary = to_date(planned.get('nextAdvanceAt'))
        next_time = (
            self.schedule_preplan_anchored_time(story, anchor, ordinary)
            if planned.get('reason') == 'conversation-density-decay' else ordinary
        )
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({
                **state,
                'extensions': {
                    **(state.get('extensions') or {}),
                    'urge': {**planned.get('state'), 'mode': json.dumps(config, ensure_ascii=False, separators=(',', ':'))},
                },
                'automation': {
                    **(state.get('automation') or {}),
                    'quietUntil': None,
                    'conversationFollowUpAt': [],
                    'conversationFollowUpParticipantId': None,
                    **({'lastUserMessageAt': iso(anchor)} if incoming else {}),
                    'nextAdvanceAt': iso(next_time),
                },
            }),
            'updatedAt': anchor,
        })
        burst = (planned.get('state') or {}).get('burst') or {}
        self.report_operation(
            'standard', 'info', story, 'advance', 'Urge 调度 档位=%s 原因=%s 下次=%s 加速已用=%d/%d',
            config.get('frequency'), planned.get('reason'), format_log_time(next_time, timezone_name),
            _nullish(burst.get('used'), 0), config.get('budget'),
        )

    # ================= 上游 5442-5445：isAutomaticAdvancePaused =================

    def is_automatic_advance_paused(self, story: InterludeStory, now: datetime) -> bool:
        """上游 5442-5445 isAutomaticAdvancePaused。"""
        quiet_until = to_date(_story_automation(story).get('quietUntil'))
        return bool(quiet_until and quiet_until > now)

    # ================= 上游 5447-5454：dueConversationFollowUps =================

    def due_conversation_follow_ups(self, story: InterludeStory, now: datetime) -> List[datetime]:
        """上游 5447-5454 dueConversationFollowUps。"""
        if self.urge_config.get('enabled'):
            return []
        planned = [
            parsed for parsed in (
                to_date(value) for value in (_story_automation(story).get('conversationFollowUpAt') or [])
            )
            if parsed is not None
        ]
        planned.sort()
        return [value for value in planned if value <= now]

    # ================= 上游 5459-5473：completeConversationFollowUps =================

    def complete_conversation_follow_ups(self, story_id: str, now: datetime) -> bool:
        """上游 5459-5473 completeConversationFollowUps。

        Remove elapsed short passes after their single writing turn. The next
        remaining pass stays persisted, so reloads never restart the 10/20-minute
        sequence or accidentally run both passes at once.
        """
        story = self.get_story(story_id)
        remaining = sorted(
            parsed for parsed in (
                to_date(value) for value in (_story_automation(story).get('conversationFollowUpAt') or [])
            )
            if parsed is not None and parsed > now
        )
        automation = {
            **_story_automation(story),
            'conversationFollowUpAt': [iso(value) for value in remaining],
            **({} if remaining else {'conversationFollowUpParticipantId': None}),
            'nextAdvanceAt': iso(remaining[0]) if remaining else None,
        }
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({**decode_story_state(story.get('state')), 'automation': automation}),
            'updatedAt': now,
        })
        return len(remaining) > 0

    # ================= 上游 5475-5483：isAutomaticAdvanceDue =================

    def is_automatic_advance_due(self, story: InterludeStory, now: datetime) -> bool:
        """上游 5475-5483 isAutomaticAdvanceDue。"""
        config = self.auto_advance_config
        if not config.get('enabled'):
            return False
        scheduled = to_date(_story_automation(story).get('nextAdvanceAt'))
        if scheduled is not None:
            return scheduled <= now
        # Stories created before this scheduler existed have no persisted next time.
        # Use the normal cadence once, then persist a randomized schedule afterwards.
        cursor_at = to_date(story.get('cursorAt'))
        if cursor_at is None:
            return False
        return (_epoch_ms(now) or 0) - (_epoch_ms(cursor_at) or 0) >= (_to_number(config.get('intervalMinutes')) or 0) * _MINUTE_MS

    # ================= 上游 5485-5503：pauseAutomaticAdvanceAfterUserMessage =================

    def pause_automatic_advance_after_user_message(self, story_id: str, now: datetime) -> None:
        """上游 5485-5503 pauseAutomaticAdvanceAfterUserMessage。"""
        # Cancel the old post-conversation cadence as soon as a new message
        # arrives. The new cadence is set after this turn has actually decided
        # whether it replies now, later, or not at all.
        story = self.get_story(story_id)
        if self.urge_config.get('enabled'):
            return self.schedule_urge_advance(story, now, True)
        fallback_next = self.schedule_preplan_anchored_time(
            story, now, now + timedelta(minutes=automatic_interval_minutes(story, now, self.auto_advance_config)),
        )
        automation = {
            **_story_automation(story),
            'conversationFollowUpAt': [],
            'conversationFollowUpParticipantId': None,
            'quietUntil': None,
            'lastUserMessageAt': iso(now),
            # Covers group-gate silence and provider failures: no old short timer
            # may fire while this fresh conversation event is still unresolved.
            'nextAdvanceAt': iso(fallback_next),
        }
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({**decode_story_state(story.get('state')), 'automation': automation}),
            'updatedAt': now,
        })

    # ================= 上游 5505-5507：pauseAutomaticAdvanceAfterDelayedReply =================

    def pause_automatic_advance_after_delayed_reply(self, story_id: str, now: datetime,
                                                    participant_id: str = '') -> None:
        """上游 5505-5507 pauseAutomaticAdvanceAfterDelayedReply。"""
        self.schedule_conversation_follow_ups_after_turn(story_id, now, None, participant_id)

    # ================= 上游 5511-5541：scheduleConversationFollowUpsAfterTurn =================

    def schedule_conversation_follow_ups_after_turn(self, story_id: str, now: datetime,
                                                    raw_interaction: Optional[Dict[str, Any]] = None,
                                                    participant_id: str = '') -> None:
        """上游 5511-5541 scheduleConversationFollowUpsAfterTurn。

        Schedule the 10/20-minute continuity passes from the actual endpoint of
        a conversation. A delayed reply anchors them after its planned send time.
        """
        config = self.auto_advance_config
        if not config.get('enabled'):
            return
        story = self.get_story(story_id)
        interaction = (
            normalize_interaction(raw_interaction, now, self.config.get('runtime') or {})
            if raw_interaction else None
        )
        reply = (interaction.get('reply') or {}) if is_record(interaction) else {}
        delayed_until = to_date(reply.get('sendAt')) if reply.get('mode') == 'delayed' else None
        anchor = delayed_until if (delayed_until is not None and delayed_until > now) else now
        if self.urge_config.get('enabled'):
            return self.schedule_urge_advance(story, anchor)
        timezone_name = (story.get('setting') or {}).get('timezone')
        # Sleep/rest windows keep their low-frequency cadence: do not wake the
        # story twice in twenty minutes merely because a conversation ended near
        # bedtime.
        follow_ups = (
            []
            if active_rest_window(config.get('restWindows') or [], timezone_name, anchor)
            else schedule_conversation_follow_ups(anchor, config)
        )
        ordinary_next = (
            follow_ups[-1] if follow_ups
            else anchor + timedelta(minutes=automatic_interval_minutes(story, anchor, config))
        )
        normal_next = ordinary_next if follow_ups else self.schedule_preplan_anchored_time(story, anchor, ordinary_next)
        automation = {
            **_story_automation(story),
            # Follow-ups are the only special post-conversation schedule. Regular
            # 40-minute cadence resumes after the final short pass, not from every
            # incoming message.
            'quietUntil': None,
            'conversationFollowUpAt': [iso(value) for value in follow_ups],
            'conversationFollowUpParticipantId': (participant_id or None) if follow_ups else None,
            'nextAdvanceAt': iso(normal_next),
        }
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({**decode_story_state(story.get('state')), 'automation': automation}),
            'updatedAt': now,
        })
        self.report_operation(
            'standard', 'info', story, 'conversation-follow-up', '已更新对话后续计划 短期补写=%s 常规推进=%s',
            '、'.join(format_log_time(value, timezone_name) for value in follow_ups) if follow_ups else '无',
            format_log_time(normal_next, timezone_name),
        )

    # ================= 上游 5543-5561：scheduleNextAutomaticAdvance =================

    def schedule_next_automatic_advance(self, story_id: str, now: datetime) -> None:
        """上游 5543-5561 scheduleNextAutomaticAdvance。"""
        config = self.auto_advance_config
        if not config.get('enabled'):
            return
        story = self.get_story(story_id)
        if self.urge_config.get('enabled'):
            return self.schedule_urge_advance(story, now)
        interval_minutes = automatic_interval_minutes(story, now, config)
        ordinary_next = now + timedelta(minutes=interval_minutes)
        next_advance_at = self.schedule_preplan_anchored_time(story, now, ordinary_next)
        automation = {
            **_story_automation(story),
            'quietUntil': None,
            'conversationFollowUpAt': [],
            'conversationFollowUpParticipantId': None,
            'lastAutoAdvanceAt': iso(now),
            'nextAdvanceAt': iso(next_advance_at),
        }
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({**decode_story_state(story.get('state')), 'automation': automation}),
            'updatedAt': now,
        })
        self.report_operation(
            'standard', 'info', story, 'advance', '已设置下次自动推进 时间=%s 间隔=%d分钟%s',
            format_log_time(next_advance_at, (story.get('setting') or {}).get('timezone')),
            int(max(1, _js_round(((_epoch_ms(next_advance_at) or 0) - (_epoch_ms(now) or 0)) / _MINUTE_MS))),
            '（Schedule Preplan 锚点）' if next_advance_at < ordinary_next else '',
        )

    # ================= 上游 5563-5568：schedulePreplanAnchoredTime =================

    def schedule_preplan_anchored_time(self, story: InterludeStory, now: datetime,
                                       ordinary_next: datetime) -> datetime:
        """上游 5563-5568 schedulePreplanAnchoredTime。"""
        if not self.schedule_preplan_config.get('enabled') or not self.schedule_preplan_config.get('anchorAutoAdvance'):
            return ordinary_next
        schedule = self.get_schedule_preplan(story.get('id'))
        transition = next_schedule_preplan_transition(schedule, now, (story.get('setting') or {}).get('timezone'), 12)
        return transition if (transition is not None and transition > now and transition < ordinary_next) else ordinary_next
