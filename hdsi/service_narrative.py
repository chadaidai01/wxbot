# -*- coding: utf-8 -*-
"""HDS-Interlude 移植：上游 `.hdsi_reference/src/service.ts` 行号 3140-3833（1.0.1-beta6-rebuild）。

ServiceNarrativeMixin —— 叙事推进层：
- 推进入口 advanceStory / deliverMessages / compactStory / compactOverlay / adminOverlayStatus；
- 后台扫描 sweep 与真正的推进实现 advanceUnlocked（分段投递、网页研究、时间导演重试门闩、
  自动推进与到期计划批次、短期跟进收尾）；
- 主模型上下文唯一入口 decide（可见性过滤、场景帧、计划/后果投影、史官检索）；
- 时间导演 planAutomaticTimeline / isTimelineDirectorFused / persistTimelineRetry 与调用封装
  tryDecide（可见回复恢复、实时时间越界重写、熔断降级）。

约定（与 SERVICE_PORTING_SPEC.md / PORTING_GUIDE.md 一致）：
- 同步方法；上游 async/Promise 一律同步化。上游 `Promise.all` 在这里按数组字面量顺序顺序
  执行（保持每一项的返回语义与调用顺序；没有线程池，避免并发副作用）。
- 本 mixin 不定义 __init__；共享状态/能力由 hdsi/service_base.py 提供：
  self.config / self.ctx / self.platform / self.store / self.narrator / self.compactor /
  self.embedder / self.model_routing / self.db_get / self.db_create / self.db_set /
  self.db_remove / self.serial / self.report / self.report_operation / self.report_standalone /
  self.report_standalone_operation / self.allows_verbosity / self.get_story /
  self.timeline_backoff / self.timeline_director_failures / self.interrupted_typing_participants /
  self.sweep_running / self.database_resetting / self.desktop_runtime_phase /
  self.memory_config / self.browser_config / self.shared_story_config / self.auto_advance_config /
  self.urge_config / self.agency_config / self.alter_system_config / self.schedule_preplan_config /
  self.emotional_offset_for_prompt，以及跨 mixin 的 snake_case 方法（见对照表）。
- 日志/文案/阈值逐字保留（含中文标点）；`Date.now()` → `_now_ms()`，`new Date()` → `now_utc()`，
  `Time.second/minute/hour` → 毫秒常量。上游 `story.setting.timezone` 读失败时由
  hdsi/time_utils.py 的 format_log_time / story_local_time_context 回退 UTC。
- 顶层 helper（含 narrativeCursor / createFactQuery / safeJsonPreview /
  isAutomaticNarrativePhase / automaticDeliveryFromPayload / normalizeBrowserIntentDraft /
  requiresVisibleReplyRecovery 等上游私有函数）统一从 `hdsi/service_helpers.py` 引入；
  该文件缺失时按既有约定（service_base.py / service_media.py / service_story.py）
  回退到文件内联副本，就绪后 import 自动切走。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from .agency import active_agency_window
from .delivery import delivery_entry_metadata, restore_message_event
from .logging import phase_label
from .narrator import compact_prompt_entries, normalize_decoded_decision
from .schedule_preplan import schedule_preplan_window
from .script.authored_actions import resolve_authored_actions
from .script.development import development_context_query
from .script.recall_navigation import recall_focus
from .script.scene_frame import project_scene_frame, resolve_dialogue_burst
from .script.timeline_routing import needs_timeline_director
from .story_state import (
    decode_story_state,
    encode_story_state,
    normalize_participant_state,
    participant_relevance,
)
from .time_utils import (
    calendar_day_key,
    format_log_time,
    iso,
    local_clock_minutes,
    now_utc,
    parse_time,
    story_local_time_context,
)
from .types import (
    ChatActionCapabilities,
    EarlyNarrativeReply,
    GroupContext,
    IndexedQuotedMessageContext,
    InterludeParticipant,
    InterludeStory,
    NarrativeAudio,
    NarrativeDecision,
    NarrativeImage,
    NarrativeIntent,
    NarrativePhase,
    NarrativeRequest,
    OutgoingMessageDraft,
    StickerCatalogEntry,
    TimelinePlan,
    WebObservation,
)
from .urge import normalize_urge_state
from .utils import clip, is_record

# ===================== 上游 service.ts 顶层常量（218-222、8381-8391 等） =====================
# 上游 `namespace Time`：Time.second / Time.minute / Time.hour（毫秒）。
_TIME_SECOND = 1_000
_TIME_MINUTE = 60_000
_TIME_HOUR = 3_600_000

# 上游 218：TIMELINE_RETRY_BACKOFF_BASE = 10 * Time.minute
TIMELINE_RETRY_BACKOFF_BASE = 10 * _TIME_MINUTE
# 上游 220：TIMELINE_DIRECTOR_FUSE = 6
TIMELINE_DIRECTOR_FUSE = 6
# 上游 222：TIMELINE_DIRECTOR_FUSE_COOLDOWN = 2 * Time.hour
TIMELINE_DIRECTOR_FUSE_COOLDOWN = 2 * _TIME_HOUR

# 上游 8381：TIME_OVERFLOW_GRACE_MINUTES = 5
TIME_OVERFLOW_GRACE_MINUTES = 5
# 上游 8385：TIME_FORWARD_HORIZON_MINUTES = 6 * 60
TIME_FORWARD_HORIZON_MINUTES = 6 * 60
# 上游 8389：LIVE_SCRIPT_HEADLINE_CHARS = 30
LIVE_SCRIPT_HEADLINE_CHARS = 30
# 上游 8391：PLAN_SEMANTICS（计划/约定语义，钟点被引用时不构成越界）
PLAN_SEMANTICS = re.compile('赶到|约定|答应|要在|得在|之前|以前|打算|计划|准备|约好|说好|出发|来不及|赶不上|预计|大概|左右|还没|尚未')
# 上游 8394：CONTEXT_WINDOW = 8
CONTEXT_WINDOW = 8

# index.ts 191-226 DEFAULT_RUNTIME 里本文件用到的键。上游 `this.config.runtime.x` 读的是
# Schema 解析后的值；本包 config 未注入默认值时（见 hdsi/config_model.py DEFAULT_RUNTIME）
# 用同一份默认值兜底，语义与上游一致。
_RUNTIME_DEFAULTS: Dict[str, Any] = {
    'allowProactiveMessages': False,
    'sweepIntervalMinutes': 5,
    'minimumAdvanceMinutes': 30,
    'contextTimeWindowMinutes': 60,
    'memoryLimit': 20,
    'maxMessageCharacters': 2_000,
    'messageSeparator': '<sep/>',
    'splitReplyMessages': True,
}


class _UndefinedType:
    """TS `undefined` 的显式占位。

    只在 undefined / null 语义不同、且会进入日志或 JSON 预览的地方使用
    （`JSON.stringify(undefined)` 是 undefined，`JSON.stringify(null)` 是 'null'）。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return 'undefined'


UNDEFINED = _UndefinedType()


def _get(value: Any, key: str) -> Any:
    """对象属性读取：缺失返回 UNDEFINED（对应 TS 的 undefined）。"""
    if isinstance(value, dict):
        return value[key] if key in value else UNDEFINED
    return UNDEFINED


def _now_ms() -> int:
    """Date.now()。"""
    return int(now_utc().timestamp() * 1000)


def _time_ms(value: Any) -> int:
    """`toDate(value)?.getTime()`：不可解析返回 0（JS 是 NaN，比较/排序语义等价）。"""
    parsed = parse_time(value)
    return int(parsed.timestamp() * 1000) if parsed is not None else 0


def _timezone_of(story: Any) -> str:
    """上游 `story.setting.timezone`。"""
    setting = story.get('setting') if isinstance(story, dict) else None
    value = (setting or {}).get('timezone') if isinstance(setting, dict) else None
    return value if isinstance(value, str) else ''


def _js_number(value: Any) -> float:
    """JS `Number(value)`：解析失败返回 NaN。"""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return float('nan')
    return float('nan')


def _number_or(value: Any, fallback: float) -> float:
    """JS `Number(value) || fallback`（NaN 与 0 都取 fallback）。"""
    number = _js_number(value)
    if math.isnan(number) or number == 0:
        return fallback
    return number


def _js_round(value: float) -> int:
    """JS Math.round：.5 向上取整（Python round 是银行家舍入）。"""
    if math.isnan(value) or math.isinf(value):
        return value  # type: ignore[return-value]
    return int(math.floor(value + 0.5))


def _js_number_text(value: float) -> str:
    """JS `String(number)` 的常用形态。"""
    if math.isnan(value):
        return 'NaN'
    if math.isinf(value):
        return 'Infinity' if value > 0 else '-Infinity'
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def _ts_text(value: Any) -> str:
    """TS 模板字符串 `${value}` 的文本化（undefined→'undefined'，null→'null'）。"""
    if value is UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float):
        return _js_number_text(value)
    if isinstance(value, int):
        return str(value)
    return str(value)


def _string_or_empty(value: Any) -> str:
    """TS `String(value ?? '')`。"""
    if value is None or value is UNDEFINED:
        return ''
    return str(value)


def _slice_end(value: Any) -> int:
    """`String.slice(0, n)` 里的 n（ToIntegerOrInfinity；负值由调用方按常量语义处理）。"""
    number = _js_number(value)
    if math.isnan(number) or number <= 0:
        return 0
    if math.isinf(number):
        return 2 ** 31 - 1
    return int(number)


def _slice_head(values: Any, end: Any) -> Any:
    """JS `Array.prototype.slice(0, end)`（end 可能是配置值：小数 / 负数 / 缺省）。"""
    raw = list(values)
    if end is None or end is UNDEFINED:
        return raw
    number = _js_number(end)
    if math.isnan(number):
        return []
    if math.isinf(number):
        return raw if number > 0 else []
    index = int(number)
    if index < 0:
        index = max(len(raw) + index, 0)
    return raw[:min(index, len(raw))]


def _slice_tail(values: Any, count: Any) -> Any:
    """JS `Array.prototype.slice(-count)`（count 是正数上限表达式的结果）。"""
    raw = list(values)
    number = _js_number(count)
    if math.isnan(number):
        return raw
    index = int(number)
    if index >= 0:
        return raw[index:]
    return raw[max(0, len(raw) + index):]


def _has_trimmed_content(entry: Any) -> bool:
    """上游 `!!entry.content.trim()`。"""
    content = entry.get('content') if isinstance(entry, dict) else None
    return isinstance(content, str) and bool(content.strip())


def _fact_topic(fact: Any) -> str:
    """上游 `fact.knowledge?.topic ?? ''`（`.filter(Boolean)` 由调用方负责）。"""
    if not isinstance(fact, dict):
        return ''
    knowledge = fact.get('knowledge')
    if not isinstance(knowledge, dict):
        return ''
    topic = knowledge.get('topic')
    return topic if isinstance(topic, str) else ''


def _runtime_value(config: Any, key: str) -> Any:
    """上游 `this.config.runtime.<key>`（Schema 默认值见 DEFAULT_RUNTIME）。"""
    runtime = config.get('runtime') if isinstance(config, dict) else None
    value = (runtime or {}).get(key)
    return _RUNTIME_DEFAULTS[key] if value is None else value


def _automation(story: Any) -> Dict[str, Any]:
    """上游 `story.state.automation ?? {}`（不做 decode，直接读行内 JSON）。"""
    state = story.get('state') if isinstance(story, dict) else None
    automation = state.get('automation') if isinstance(state, dict) else None
    return automation if isinstance(automation, dict) else {}


def _automation_value(story: Any, key: str) -> Any:
    """上游 `story.state.automation?.<key>`。"""
    return _automation(story).get(key)


def _nested_get(value: Any, *keys: str) -> Any:
    """上游可选链读取（任意一级缺失或不是对象都返回 UNDEFINED）。"""
    current = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return UNDEFINED
        current = current[key]
    return current


def _endorsed_clock_value(local_time: Any) -> Optional[float]:
    """上游 `Number(fact.localTime.slice(-5, -3)) * 60 + Number(fact.localTime.slice(-2))`。

    结果再过 `Number.isFinite`（不可解析返回 None，调用方据此丢弃）。
    """
    if not isinstance(local_time, str):
        return None
    length = len(local_time)
    hour = _js_number(local_time[max(length - 5, 0):max(length - 3, 0)])
    minute = _js_number(local_time[max(length - 2, 0):])
    value = hour * 60 + minute
    return value if math.isfinite(value) else None


def _unique_js(values: Any) -> List[str]:
    """`Array.from(new Set(values)).join(',')` 的元素列表（元素按 JS String 文本去重）。"""
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        text = _ts_text(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _js_json_stringify(value: Any) -> Optional[str]:
    """JS `JSON.stringify`；undefined（含函数/符号语义）返回 None。"""
    if value is UNDEFINED:
        return None
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return 'null'
        return _js_number_text(value)
    if isinstance(value, (list, tuple)):
        encoded = [_js_json_stringify(item) for item in value]
        return '[' + ','.join('null' if item is None else item for item in encoded) + ']'
    if isinstance(value, datetime):
        return json.dumps(iso(value), ensure_ascii=False)
    if isinstance(value, dict):
        parts: List[str] = []
        for key, item in value.items():
            item_text = _js_json_stringify(item)
            if item_text is None:
                continue
            parts.append(json.dumps(str(key), ensure_ascii=False) + ':' + item_text)
        return '{' + ','.join(parts) + '}'
    # JS 对普通对象/Error/Set 之类一律序列化成 '{}'（没有可枚举自有属性）。
    return '{}'


def _json_text(value: Any) -> str:
    """模板字符串里 `${JSON.stringify(value)}`：undefined 渲染成 'undefined'。"""
    text = _js_json_stringify(value)
    return 'undefined' if text is None else text


def _preview(value: Any) -> str:
    """`safeJsonPreview(...)`：本模块的 UNDEFINED 哨兵走缺省参数（等价 JS undefined）。"""
    if value is UNDEFINED:
        return safe_json_preview()
    return safe_json_preview(value)


# ===================== 上游 service.ts 顶层 helper（hdsi/service_helpers.py 提供） =====================
# 上游 7084-8524 的顶层纯函数归并行移植的 hdsi/service_helpers.py；该文件尚未落地时，
# 按下述内联副本执行（与上游逐行等价），落地后 import 自动切走。

try:  # pragma: no cover - 取决于并行移植进度
    from .service_helpers import (
        automatic_delivery_from_payload,
        create_fact_query,
        describe_timeline_plan_rejection,
        detect_live_script_time_overflow,
        extract_user_reported_times,
        group_due_intents,
        has_required_narrative_script,
        is_automatic_narrative_phase,
        narrative_cursor,
        normalize_browser_intent_draft,
        normalize_timeline_plan,
        requires_visible_reply_recovery,
        safe_json_preview,
        timeline_entry_prompt_projection,
        visible_reply_mode,
    )
except ImportError:  # TODO(并行移植): service_helpers 就绪后自动切换
    def normalize_timeline_plan(value: Any) -> Optional[TimelinePlan]:
        """上游 7483-7501 normalizeTimelinePlan。"""
        if not is_record(value) or not isinstance(value.get('beats'), list):
            return None
        beats: List[Dict[str, Any]] = []
        for item in value.get('beats'):
            if not is_record(item):
                continue
            at = coerce_timeline_position(item.get('at'))
            kind = coerce_timeline_kind(item.get('kind'))
            summary = clip(item.get('summary'), 240) if isinstance(item.get('summary'), str) else ''
            if not (math.isfinite(at) and kind and summary):
                continue
            beats.append({'at': at, 'kind': kind, 'summary': summary})
        beats.sort(key=lambda beat: beat.get('at'))
        beats = beats[:4]
        if not beats:
            return None
        carry: List[str] = []
        if isinstance(value.get('carry'), list):
            for item in value.get('carry'):
                if not isinstance(item, str):
                    continue
                text = clip(item, 180)
                if text:
                    carry.append(text)
            carry = carry[:4]
        plan: TimelinePlan = {'beats': beats}
        if carry:
            plan['carry'] = carry
        return plan

    def describe_timeline_plan_rejection(value: Any) -> str:
        """上游 7503-7516 describeTimelinePlanRejection。"""
        if not is_record(value):
            return '返回不是 JSON 对象'
        if not isinstance(value.get('beats'), list):
            return '缺少 beats 数组'
        if not value.get('beats'):
            return 'beats 为空数组（模型未产出任何节点）'
        details: List[str] = []
        for item in value.get('beats'):
            if not is_record(item):
                continue
            problems: List[str] = []
            at = coerce_timeline_position(item.get('at'))
            if not math.isfinite(at):
                problems.append('at=%s 无法解析' % _json_text(_get(item, 'at')))
            if not coerce_timeline_kind(_get(item, 'kind')):
                problems.append('kind=%s 非法' % _json_text(_get(item, 'kind')))
            raw_summary = item.get('summary')
            if not isinstance(raw_summary, str) or not raw_summary.strip():
                problems.append('summary 为空')
            details.append('，'.join(problems) if problems else '通过')
        return '节点校验详情：%s' % '；'.join(details)

    def timeline_entry_prompt_projection(entry: Any) -> Any:
        """上游 7521-7529 timelineEntryPromptProjection。"""
        metadata = entry.get('metadata') if isinstance(entry, dict) else None
        if is_record(metadata) and metadata.get('narrativeAuthority') == 'original-v2':
            return entry
        if not isinstance(entry, dict) or entry.get('kind') != 'script':
            return entry
        plan = normalize_timeline_plan(metadata.get('timelinePlan') if is_record(metadata) else None)
        if not plan:
            return entry
        beats = ' | '.join(
            '%d%% %s: %s' % (_js_round(beat.get('at') * 100), beat.get('kind'), beat.get('summary'))
            for beat in plan.get('beats') or []
        )
        carry = ' Carry: %s' % ' | '.join(plan.get('carry') or []) if plan.get('carry') else ''
        return {
            **entry,
            'content': '[Host timeline ledger for this completed automatic window: %s.%s]' % (beats, carry),
        }

    def visible_reply_mode(decision: Any, phase: NarrativePhase, group_context: Any = None) -> str:
        """上游 7594-7608 visibleReplyMode。"""
        cross = decision.get('crossConversationActions') if isinstance(decision, dict) else None
        if phase == 'advance':
            if any(is_record(action) and action.get('mode') == 'immediate' for action in (cross or [])):
                return '主动联系'
            if any(is_record(action) and action.get('mode') == 'delayed' for action in (cross or [])):
                return '计划联系'
            return '无可见投递'
        if phase in ('conversation-follow-up', 'intent-due'):
            if has_structured_interaction(decision.get('interaction') if isinstance(decision, dict) else None):
                return decision['interaction']['reply']['mode']
            if any(is_record(action) and action.get('mode') == 'immediate' for action in (cross or [])):
                return '主动联系'
            return '无可见投递'
        interaction = decision.get('interaction') if isinstance(decision, dict) else None
        group_reply = decision.get('groupReply') if isinstance(decision, dict) else None
        if not group_context:
            return interaction['reply']['mode'] if has_structured_interaction(interaction) else '未提供或无效'
        if has_structured_group_reply_field(group_reply):
            return 'group:%s' % group_reply['mode']
        if has_structured_interaction(interaction):
            return 'group-fallback:%s' % interaction['reply']['mode']
        return '未提供或无效'

    def has_required_narrative_script(value: Any) -> bool:
        """上游 7711-7713 hasRequiredNarrativeScript。"""
        script = value.get('script') if isinstance(value, dict) else None
        return isinstance(script, str) and len(script.strip()) > 0

    def extract_user_reported_times(content: Any, now: datetime, timezone: str) -> List[Dict[str, Any]]:
        """上游 7351-7397 extractUserReportedTimes。"""
        text = _string_or_empty(content)
        current_minutes = local_clock_minutes(now, timezone)
        date = calendar_day_key(now, timezone)
        facts: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        def add(hour: Any, minute: Any, statement: str) -> None:
            if not _is_js_integer(hour) or not _is_js_integer(minute):
                return
            hour = int(hour)
            minute = int(minute)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                return
            clock = '%02d:%02d' % (hour, minute)
            value = hour * 60 + minute
            relation = 'past' if value < current_minutes else 'future' if value > current_minutes else 'current'
            key = '%s:%s' % (clock, statement)
            if key in seen:
                return
            seen.add(key)
            facts.append({
                'localTime': '%s %s' % (date, clock),
                'relation': relation,
                'statement': clip(statement, 240).strip(),
            })

        for match in re.finditer(r'(?:今天|今晚|下午|晚上|早上|上午)?\s*(\d{1,2})\s*(?:[:：.]|点)\s*(\d{2}|半)?', text):
            hour = _js_number(match.group(1))
            raw_minute = match.group(2)
            minute = 30.0 if raw_minute == '半' else (_js_number(raw_minute) if raw_minute else 0.0)
            prefix = match.group(0)
            if ('下午' in prefix or '晚上' in prefix) and hour < 12:
                hour += 12
            elif not re.search('早上|上午|下午|晚上|中午', prefix) and 0 < hour < 12:
                # Bare “6.30” is ambiguous. Prefer the 12-hour interpretation nearest
                # to the current story clock, so an evening report normally means 18:30
                # rather than an implausibly distant 06:30.
                morning = hour * 60 + minute
                evening = (hour + 12) * 60 + minute
                if abs(evening - current_minutes) < abs(morning - current_minutes):
                    hour += 12
            add(
                hour,
                minute,
                text[max(0, match.start() - 48):min(len(text), match.start() + len(match.group(0)) + 96)],
            )
        # 时段词（中午/下午/晚上等）作为该时段代表性的钟点锚点，供守卫与 prompt
        # 理解“中午一起吃饭”这类不含数字的时间约定。
        for match in re.finditer(r'(早上|上午|中午|下午|傍晚|晚上)', text):
            hour = {'早上': 8, '上午': 9, '中午': 12, '下午': 15, '傍晚': 18, '晚上': 20}[match.group(1)]
            add(
                hour,
                0,
                text[max(0, match.start() - 48):min(len(text), match.start() + len(match.group(0)) + 96)],
            )
        # 中文数字钟点（“八点”“八点半”“九点一刻”）：口语消息最常用的写法。
        for match in re.finditer(r'([零一二三四五六七八九十两]+)点(?:(?:零|([一二三四五六七八九十两]+))分?|半)?', text):
            hour = chinese_clock_number(match.group(1))
            raw_minute = match.group(2)
            minute = 30 if raw_minute == '半' else (chinese_clock_number(raw_minute) if raw_minute else 0)
            if hour is None or minute is None:
                continue
            add(
                hour,
                minute,
                text[max(0, match.start() - 48):min(len(text), match.start() + len(match.group(0)) + 96)],
            )
        return facts[:4]

    def group_due_intents(intents: Any) -> List[List[NarrativeIntent]]:
        """上游 8113-8125 groupDueIntents。"""
        batches: Dict[str, List[NarrativeIntent]] = {}
        ordered = sorted(
            list(intents or []),
            key=lambda intent: (_time_ms(intent.get('notBefore')), _number_or(intent.get('id'), 0)),
        )
        for intent in ordered:
            family = 'agency' if intent.get('type') == 'proactive-check' else 'normal'
            key = '%s|%s' % (intent.get('participantId') or '__global__', family)
            batches.setdefault(key, []).append(intent)
        return list(batches.values())

    def detect_live_script_time_overflow(
        script: Any,
        phase: NarrativePhase,
        from_: datetime,
        now: datetime,
        timezone: str,
        endorsed_clocks: Optional[Set[int]] = None,
    ) -> Optional[str]:
        """上游 8417-8441 detectLiveScriptTimeOverflow。"""
        text = _string_or_empty(script).strip()
        elapsed_minutes = max(0.0, (_time_ms(now) - _time_ms(from_)) / float(_TIME_MINUTE))
        if phase != 'user-message' or not text or elapsed_minutes > 60:
            return None
        endpoint = story_local_time_context(now, timezone)
        now_minutes = endpoint['hour'] * 60 + _js_number(endpoint['time'][3:5])

        def overflow(value: float) -> bool:
            return (
                value > now_minutes + TIME_OVERFLOW_GRACE_MINUTES
                and value - now_minutes <= TIME_FORWARD_HORIZON_MINUTES
            )

        # 用户给出了未来期限（"九点前赶到"）时，其之间的叙事推进被授权：落在
        # (now, maxEndorsed] 区间的钟点一并豁免；endorsed 为空时行为不变。
        endorsed_deadline = max(endorsed_clocks) if endorsed_clocks else None
        # 只把剧本开头（叙事宣告位）里、且没有计划语义的钟点视为"把叙事时间写过头"。
        for clock in clocks_in(text[:LIVE_SCRIPT_HEADLINE_CHARS]):
            value = clock['hour'] * 60 + clock['minute']
            if endorsed_clocks and value in endorsed_clocks:
                continue
            if endorsed_deadline is not None and value > now_minutes and value <= endorsed_deadline:
                continue
            if PLAN_SEMANTICS.search(clock['around']):
                continue
            if overflow(value):
                return 'explicit clock %s:%s exceeds %s' % (
                    '%02d' % clock['hour'], '%02d' % clock['minute'], endpoint['time'][:5],
                )
        # 全文（含开头）的课程阶段推进不变：多个已完成的课程节 = 时间被写飞。
        # JS `new Set(...)` 保序，这里用 dict.fromkeys 复刻（集合序列会影响日志文本）。
        lesson_stages = list(dict.fromkeys(
            match.group(1) for match in re.finditer(r'第\s*([一二三四五六七八九十\d]+)\s*节', text)
        ))
        if len(lesson_stages) >= 2:
            return 'multiple lesson stages (%s) inside a %d minute live window' % (
                '→'.join(lesson_stages), _js_round(elapsed_minutes),
            )
        return None

    def chinese_clock_number(value: str) -> Optional[int]:
        """上游 8443-8453 chineseClockNumber。"""
        digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
        if value == '十':
            return 10
        if '十' in value:
            left, _, right = value.partition('十')
            tens = digits.get(left) if left else 1
            ones = digits.get(right) if right else 0
            if tens is None or ones is None:
                return None
            return tens * 10 + ones
        return digits.get(value) if len(value) == 1 else None

    def context_around(text: str, index: int, length: int) -> str:
        """上游 8396-8398 contextAround。"""
        return text[max(0, index - CONTEXT_WINDOW):min(len(text), index + length + CONTEXT_WINDOW)]

    def clocks_in(text: str) -> List[Dict[str, Any]]:
        """上游 8400-8415 clocksIn。"""
        found: List[Dict[str, Any]] = []
        for match in re.finditer(r'(?:^|[^\d])(?:(\d{1,2})[:：](\d{2})|(\d{1,2})点(?:(\d{1,2})分?)?)', text):
            raw_hour = match.group(1) if match.group(1) is not None else match.group(3)
            raw_minute = match.group(2) if match.group(2) is not None else (match.group(4) if match.group(4) is not None else 0)
            hour = _js_number(raw_hour)
            minute = _js_number(raw_minute)
            if hour > 23 or minute > 59:
                continue
            found.append({'hour': hour, 'minute': minute, 'around': context_around(text, match.start(), len(match.group(0)))})
        for match in re.finditer(r'([零一二三四五六七八九十两]+)点(?:(?:零|([一二三四五六七八九十两]+))分?|半)?', text):
            hour = chinese_clock_number(match.group(1))
            minute = chinese_clock_number(match.group(2)) if match.group(2) else 0
            if hour is None or minute is None or hour > 23 or minute > 59:
                continue
            found.append({'hour': hour, 'minute': minute, 'around': context_around(text, match.start(), len(match.group(0)))})
        return found

    def coerce_timeline_kind(value: Any) -> str:
        """上游 7463-7467 coerceTimelineKind。"""
        if not isinstance(value, str):
            return ''
        return TIMELINE_KIND_ALIASES.get(value.strip().lower(), 'activity')

    def coerce_timeline_position(value: Any) -> float:
        """上游 7469-7479 coerceTimelinePosition。"""
        if isinstance(value, bool):
            return float('nan')
        if isinstance(value, (int, float)):
            number = float(value)
            if math.isnan(number):
                return number
            return max(0.0, min(1.0, number))
        if isinstance(value, str):
            text = value.strip()
            if text.endswith('%'):
                text = text[:-1]
            parsed = _js_number(text)
            if not math.isnan(parsed) and math.isfinite(parsed):
                return max(0.0, min(1.0, parsed if parsed <= 1 else parsed / 100))
        return float('nan')


    def narrative_cursor(story: InterludeStory, now: datetime) -> datetime:
        """上游 8200-8203 narrativeCursor。

        A corrupted/future cursor must never make the narrator "fill in" time
        backwards. Clamp only the prompt interval; normal successful persistence
        still advances the stored cursor to the actual wall-clock time.
        """
        cursor = parse_time(story.get('cursorAt'))
        if cursor is None:
            cursor = now
        return now if cursor > now else cursor


    def has_structured_group_reply_field(value: Any) -> bool:
        """上游 7613-7616 hasStructuredGroupReplyField。"""
        if not is_record(value) or value.get('mode') not in ('none', 'immediate'):
            return False
        return value.get('mode') == 'none' or (isinstance(value.get('content'), str) and bool(value.get('content').strip()))


    def has_structured_interaction(value: Any) -> bool:
        """上游 7618-7626 hasStructuredInteraction。"""
        if not is_record(value) or not isinstance(value.get('seen'), bool) or not is_record(value.get('reply')):
            return False
        reply = value.get('reply')
        mode = reply.get('mode')
        if mode not in ('none', 'immediate', 'delayed'):
            return False
        if mode == 'none':
            return True
        if not isinstance(reply.get('content'), str) or not reply.get('content').strip():
            return False
        return mode == 'immediate' or (isinstance(reply.get('sendAt'), str) and bool(reply.get('sendAt').strip()))


    def has_structured_group_reply(decision: Any) -> bool:
        """上游 7610-7612 hasStructuredGroupReply。"""
        if not isinstance(decision, dict):
            return False
        return has_structured_group_reply_field(decision.get('groupReply')) or has_structured_interaction(decision.get('interaction'))


    def requires_visible_reply_recovery(phase: NarrativePhase, group_context: Any, decision: Any) -> bool:
        """上游 7589-7592 requiresVisibleReplyRecovery。"""
        if phase != 'user-message':
            return False
        record = decision if isinstance(decision, dict) else {}
        if group_context:
            return not has_structured_group_reply(record)
        return not has_structured_interaction(record.get('interaction'))


    def safe_json_preview(value: Any = UNDEFINED) -> str:
        """上游 7630-7636 safeJsonPreview：诊断日志用的紧凑 JSON，永不抛错、长度有界。"""
        try:
            text = 'undefined' if value is UNDEFINED else _js_json_stringify(value)
            if text is None:
                text = 'undefined'
            return str(text)[:300]
        except Exception:  # noqa: BLE001 - 诊断辅助函数不允许影响主流程
            return '(unserializable)'


    def is_automatic_narrative_phase(phase: NarrativePhase) -> bool:
        """上游 7725-7727 isAutomaticNarrativePhase。"""
        return phase == 'advance' or phase == 'conversation-follow-up'


    def normalize_automatic_delivery_summary(value: Any) -> str:
        """上游 7729-7731 normalizeAutomaticDeliverySummary。"""
        return clip(value, 240).strip() if isinstance(value, str) else ''


    def automatic_delivery_from_payload(value: Any) -> Optional[Dict[str, Any]]:
        """上游 7789-7794 automaticDeliveryFromPayload。"""
        record = value.get('automaticDelivery') if is_record(value) and is_record(value.get('automaticDelivery')) else None
        summary = normalize_automatic_delivery_summary(record.get('summary') if record else None)
        raw_source_entry_id = record.get('sourceEntryId') if record else None
        source_entry_id = None
        if isinstance(raw_source_entry_id, (int, float)) and not isinstance(raw_source_entry_id, bool):
            if float(raw_source_entry_id).is_integer() and abs(raw_source_entry_id) <= 2 ** 53 - 1:
                source_entry_id = int(raw_source_entry_id)
        if not summary:
            return None
        delivery: Dict[str, Any] = {'summary': summary}
        if source_entry_id:
            delivery['sourceEntryId'] = source_entry_id
        return delivery


    def normalize_browser_intent_draft_loose(value: Any) -> Optional[Dict[str, Any]]:
        """上游 7881-7893 normalizeBrowserIntentDraftLoose。"""
        if not is_record(value) or value.get('mode') not in ('search', 'visit') or not isinstance(value.get('purpose'), str):
            return None
        query = clip(value.get('query'), 500) if isinstance(value.get('query'), str) else ''
        url = clip(value.get('url'), 2_000) if isinstance(value.get('url'), str) else ''
        if value.get('mode') == 'search' and not query:
            return None
        if value.get('mode') == 'visit' and not url:
            return None
        normalized: Dict[str, Any] = {'mode': value.get('mode')}
        if query:
            normalized['query'] = query
        if url:
            normalized['url'] = url
        normalized['purpose'] = clip(value.get('purpose'), 500)
        normalized['timing'] = 'immediate' if value.get('timing') == 'immediate' else 'deferred'
        if isinstance(value.get('participantId'), str):
            normalized['participantId'] = value.get('participantId').strip()
        return normalized


    def normalize_browser_intent_draft(draft: Any, config: Any) -> Optional[Dict[str, Any]]:
        """上游 7896-7901 normalizeBrowserIntentDraft。"""
        normalized = normalize_browser_intent_draft_loose(draft)
        if not normalized:
            return None
        if normalized.get('mode') == 'search' and not (config or {}).get('allowSearch'):
            return None
        if normalized.get('mode') == 'visit' and not (config or {}).get('allowVisit'):
            return None
        return normalized


    def create_fact_query(
        participant: Optional[InterludeParticipant],
        user_message: Optional[str],
        due_intents: List[NarrativeIntent],
        superseded_intents: List[NarrativeIntent],
    ) -> str:
        """上游 8306-8314 createFactQuery。"""
        state = normalize_participant_state(participant.get('state')) if participant else None
        parts: List[str] = []
        if user_message:
            parts.append('Current user message: %s' % _ts_text(user_message))
        for thread in (state.get('openThreads') or []) if state else []:
            parts.append('Open thread: %s' % _ts_text(thread))
        for note in (state.get('relationshipNotes') or []) if state else []:
            parts.append('Relationship note: %s' % _ts_text(note))
        for intent in due_intents:
            parts.append('Due intent: %s' % _ts_text(intent.get('summary')))
        for intent in superseded_intents:
            parts.append('Superseded plan: %s' % _ts_text(intent.get('summary')))
        return '\n'.join(part for part in parts if part)


# 上游 7454-7461 TIMELINE_KIND_ALIASES（宽松的 kind 映射）。
TIMELINE_KIND_ALIASES: Dict[str, str] = {
    'activity': 'activity', 'action': 'activity', 'event': 'activity', 'scene': 'activity', 'behavior': 'activity',
    'thought': 'thought', 'think': 'thought', 'feeling': 'thought', 'mood': 'thought', 'inner': 'thought',
    'state': 'state', 'status': 'state', 'condition': 'state',
    '活动': 'activity', '行动': 'activity', '事件': 'activity', '场景': 'activity',
    '想法': 'thought', '心情': 'thought', '思绪': 'thought',
    '状态': 'state',
}


# ===================== 上游 service.ts 小工具（供上面的回退副本使用） =====================

def _is_js_integer(value: Any) -> bool:
    """JS Number.isInteger。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer()
    return False


class ServiceNarrativeMixin:
    """上游 3140-3833：叙事推进入口 / 后台扫描 / 主叙事上下文 / 时间导演。"""

    # ================= 上游 3140-3148：advanceStory =================

    def advance_story(self, story: InterludeStory, force: bool = True) -> List[OutgoingMessageDraft]:
        """上游 3140-3148 advanceStory。"""
        if self.desktop_runtime_phase == 'paused':
            return []
        if not self.can_handle_story(story):
            return []
        messages = self.serial(
            story.get('id'),
            lambda: self.advance_unlocked(self.get_story(story.get('id')), now_utc(), force),
        )
        if force or messages:
            self.report_operation('summary', 'info', story, 'advance', '剧本推进完成 可见消息=%d', len(messages))
        self.schedule_compaction(story.get('id'))
        return messages

    # ================= 上游 3150-3155：deliverMessages =================

    def deliver_messages(
        self,
        story: InterludeStory,
        messages: List[OutgoingMessageDraft],
        session: Any = None,
    ) -> List[OutgoingMessageDraft]:
        """上游 3150-3155 deliverMessages（Used by commands/tests to deliver a mixed set of account-targeted actions safely）。"""
        participant = self.find_participant(session, story) if session is not None else None
        delivered = self.send_outgoing_messages(story, messages, participant, session)
        self.confirm_outgoing_deliveries(story, delivered)
        return delivered

    # ================= 上游 3157-3169：compactStory / compactOverlay =================

    def compact_story(self, story: InterludeStory, force: bool = True) -> Any:
        """上游 3157-3161 compactStory。"""
        if self.desktop_runtime_phase == 'paused':
            return False
        if not self.can_handle_story(story):
            return False
        return self.serial(
            story.get('id'),
            lambda: self.compact_unlocked(self.get_story(story.get('id')), now_utc(), force),
        )

    def compact_overlay(self, story: InterludeStory) -> Any:
        """上游 3163-3169 compactOverlay。

        Merge and compress already-applied overlay patches without running the
        full scene/fact compaction pass. This is safe for manual maintenance.
        """
        if not self.can_handle_story(story):
            return False
        return self.serial(
            story.get('id'),
            lambda: self.compact_overlay_unlocked(self.get_story(story.get('id')), now_utc()),
        )

    # ================= 上游 3171-3186：adminOverlayStatus =================

    def admin_overlay_status(self, story_id: str) -> Dict[str, Any]:
        """上游 3171-3186 adminOverlayStatus（Administrative overlay view used by the Console command）。"""
        # Promise.all → 顺序执行（每项结果语义不变）。
        story = self.get_story(story_id)
        patches = self.db_get('interlude_state_patch', {'storyId': story_id}, {'sort': {'createdAt': 'desc'}})
        snapshots = self.db_get(
            'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'}, {'sort': {'periodEnd': 'desc'}},
        )
        participants = self.participants(story_id, True)
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        overlay = state.get('settingOverlay')
        return {
            'state': overlay if overlay is not None else {},
            'proposed': [patch for patch in patches if patch.get('status') == 'proposed'],
            'applied': [patch for patch in patches if patch.get('status') in ('applied', 'compacted')],
            'cleared': [patch for patch in patches if patch.get('status') == 'cleared'],
            'snapshots': snapshots,
            'participantOverlays': [
                participant for participant in participants
                if normalize_participant_state(participant.get('state')).get('relationshipOverlay')
            ],
        }

    # ================= 上游 3188-3215：sweep =================

    def sweep(self) -> None:
        """上游 3188-3215 sweep。"""
        if self.desktop_runtime_phase == 'paused' or self.database_resetting or self.sweep_running:
            return
        self.sweep_running = True
        started_at = _now_ms()
        try:
            story = self.get_canonical_story()
            if not story or not self.can_handle_story(story):
                self.report_standalone_operation('diagnostic', 'debug', '后台扫描跳过：没有可处理的活动主剧本')
                return
            if self.has_pending_narrative(story.get('id')):
                # A split-message is an already-decided typing fragment. It may start
                # transport while another relationship is waiting, but an incoming
                # message from its own participant still interrupts it first.
                pending_due = self.due_intents(story.get('id'), now_utc())
                if any(intent.get('type') == 'split-message' for intent in pending_due):
                    self.deliver_due_split_segments(story.get('id'))
                self.report_operation('diagnostic', 'debug', story, 'advance', '后台写作跳过：前台回合处理中；已独立检查到期分段投递')
                return
            timezone = _timezone_of(story)
            self.report_operation(
                'diagnostic', 'debug', story, 'advance', '后台扫描开始 游标=%s 下次自动推进=%s',
                format_log_time(story.get('cursorAt'), timezone),
                format_log_time(parse_time(_automation_value(story, 'nextAdvanceAt')), timezone),
            )
            messages = self.advance_story(story, False)
            delivered = self.send_scheduled_messages(story, messages) if messages else []
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '后台扫描完成 耗时=%dms 已投递=%d', _now_ms() - started_at, len(delivered),
            )
        finally:
            self.sweep_running = False

    # ================= 上游 3217-3435：advanceUnlocked =================

    def advance_unlocked(self, story: InterludeStory, now: datetime, force: bool) -> List[OutgoingMessageDraft]:
        """上游 3217-3435 advanceUnlocked。"""
        urge_mode = (
            json.dumps(self.urge_config, ensure_ascii=False, separators=(',', ':'))
            if self.urge_config.get('enabled') else 'off'
        )
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        extensions = state.get('extensions') if isinstance(state.get('extensions'), dict) else {}
        previous_urge = normalize_urge_state(extensions.get('urge'), now.timestamp() * 1000)
        if previous_urge.get('mode') != urge_mode and (
            self.urge_config.get('enabled') or (previous_urge.get('mode') and previous_urge.get('mode') != 'off')
        ):
            decoded = decode_story_state(story.get('state'))
            # 上游 `{ ...previousUrge, mode, armed: undefined, burst: undefined, pace: 'normal' }`：
            # JSON.stringify 会丢掉值为 undefined 的键，这里显式剔除 armed/burst。
            next_urge = {
                key: value
                for key, value in {**previous_urge, 'mode': urge_mode, 'pace': 'normal'}.items()
                if key not in ('armed', 'burst')
            }
            self.db_set(
                'interlude_story', {'id': story.get('id')},
                {'state': encode_story_state({
                    **decoded,
                    'extensions': {**(decoded.get('extensions') or {}), 'urge': next_urge},
                })},
            )
            self.schedule_next_automatic_advance(story.get('id'), now)
            story = self.get_story(story.get('id'))
        from_ = narrative_cursor(story, now)
        elapsed = max(0, _time_ms(now) - _time_ms(from_))
        due = self.due_intents(story.get('id'), now)
        messages: List[OutgoingMessageDraft] = []
        # Later <sep/> bubbles are delivery events, not new writing turns.  They
        # are persisted only at their actual send time, which also lets a newer
        # incoming message cancel them before the character "finishes typing".
        # Deliver at most one split segment per wake-up. If the scheduler was
        # blocked for a while, sending every overdue segment together would skip
        # the configured typing-time simulation.
        split_segments = sorted(
            [intent for intent in due if intent.get('type') == 'split-message'],
            key=lambda intent: _time_ms(intent.get('notBefore')),
        )[:1]
        split_handled = False
        for intent in split_segments:
            payload = intent.get('payload') if isinstance(intent.get('payload'), dict) else {}
            content = clip(payload.get('content'), _slice_end(_runtime_value(self.config, 'maxMessageCharacters')))
            automatic_delivery = automatic_delivery_from_payload(intent.get('payload'))
            participant = self.get_participant(intent.get('participantId')) if intent.get('participantId') else None
            if intent.get('participantId') and intent.get('participantId') in self.interrupted_typing_participants:
                continue
            split_handled = True
            if not content or not participant or participant.get('status') != 'active':
                self.db_set('interlude_intent', {'id': intent.get('id')}, {'status': 'cancelled', 'updatedAt': now})
                reference = restore_message_event(intent.get('payload'), content or '')
                if reference:
                    self.update_script_delivery_outcome(
                        story.get('id'), reference, 'cancelled', now, 'delivery-target-unavailable',
                    )
                continue
            message: OutgoingMessageDraft = {'participantId': participant.get('id'), 'content': content}
            if automatic_delivery is not None:
                message['automaticDelivery'] = automatic_delivery
            script_event = restore_message_event(intent.get('payload'), content)
            if script_event is not None:
                message['scriptEvent'] = script_event
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
                    continue
                if message.get('scriptEvent'):
                    self.update_script_delivery_outcome(
                        story.get('id'), message.get('scriptEvent'), 'pending', now,
                        'delivery-unconfirmed-retry-scheduled',
                    )
                retry_at = parse_time(_time_ms(now) + 30 * _TIME_SECOND)
                self.db_set('interlude_intent', {'id': intent.get('id')}, {'notBefore': retry_at, 'updatedAt': now})
                self.schedule_due_intent_wake(story.get('id'), retry_at)
                continue
            self.append_entry(story.get('id'), {
                'kind': 'character-message', 'actor': 'character', 'content': content,
                'occurredAt': iso(now), 'metadata': delivery_entry_metadata(message, {'splitSegment': True}),
            }, now, participant.get('id'))
            if message.get('scriptEvent'):
                self.update_script_delivery_outcome(story.get('id'), message.get('scriptEvent'), 'delivered', now)
            if message.get('automaticDelivery'):
                self.record_automatic_delivery(
                    story.get('id'), participant.get('id'), message.get('automaticDelivery'), now,
                )
            self.record_character_message(participant, now)
            self.db_set('interlude_intent', {'id': intent.get('id')}, {'status': 'completed', 'updatedAt': now})
        if split_handled:
            self.schedule_next_split_wake(story.get('id'))
        due = [intent for intent in due if intent.get('type') != 'split-message']
        # Browser research is an external, already-happened observation once it
        # completes. It must never be handed to the narrator as an ordinary
        # future plan, otherwise the model could write as if it had read a page
        # before Puppeteer actually did so.
        # Keep a backlog of optional research from turning one background sweep
        # into several serial page loads. The remaining intents stay pending for
        # the next sweep and never block a live user turn for an unbounded time.
        browser_intents = _slice_head(
            [intent for intent in due if intent.get('type') == 'browser-research'],
            max(1, self.browser_config.get('maxResearchPerSweep')),
        )
        for intent in browser_intents:
            self.execute_deferred_browser_intent(story, intent, now)
        # Browser intents always complete (successfully or as a recorded failure)
        # in executeDeferredBrowserIntent(), so re-reading the whole pending list
        # here only adds a SQLite round trip to every background sweep.
        due = [intent for intent in due if intent.get('type') != 'browser-research']
        # Paces director retries only: a persisted retry gate with an unchanged
        # window start means the previous sweep ended without consuming the
        # cursor (e.g. another failure), so wait for the backoff instead of
        # re-entering tryDecide every sweep. Successful director-less degradation
        # moves the cursor and clears this gate naturally. Manual advancement
        # remains an explicit escape hatch for operators.
        timeline_retry_at = parse_time(_automation_value(story, 'timelineRetryAt'))
        timeline_retry_from = _automation_value(story, 'timelineRetryFrom')
        from_iso = iso(from_)
        if not force and timeline_retry_at and timeline_retry_from == from_iso and timeline_retry_at > now:
            timezone = _timezone_of(story)
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '自动推进等待时间导演重试 冷却至=%s 时间窗口起点=%s',
                format_log_time(timeline_retry_at, timezone), format_log_time(from_, timezone),
            )
            self.schedule_due_intent_wake(story.get('id'), timeline_retry_at)
            return messages
        if timeline_retry_at and (timeline_retry_at <= now or timeline_retry_from != from_iso):
            automation = {**(_automation(story))}
            automation.pop('timelineRetryAt', None)
            automation.pop('timelineRetryFrom', None)
            story['state'] = {**(story.get('state') if isinstance(story.get('state'), dict) else {}), 'automation': automation}
        # Turning off automatic advancement must suppress *every* background
        # writing path, including short plans that were persisted before the
        # owner disabled the feature. Manual `interlude.advance` still passes
        # `force` and remains available.
        auto_advance_enabled = self.auto_advance_config.get('enabled')
        due_follow_ups = self.due_conversation_follow_ups(story, now) if auto_advance_enabled else []
        automatic_due = bool(auto_advance_enabled) and (
            len(due_follow_ups) > 0 or self.is_automatic_advance_due(story, now)
        )
        paused_for_conversation = self.is_automatic_advance_paused(story, now)
        self.report_operation(
            'diagnostic', 'debug', story, 'advance',
            '后台状态 到期计划=%d 分段消息=%d 网页任务=%d 短期跟进=%d 自动推进到期=%s 对话暂停=%s',
            len(due), len(split_segments), len(browser_intents), len(due_follow_ups),
            automatic_due, paused_for_conversation,
        )
        # A due typing segment can be delivered during the conversation pause;
        # it is already a committed message, not an automatic life update.
        if not force and not due and (not automatic_due or paused_for_conversation):
            return messages

        # A manual advance may be queued behind a background pass. Do not open a
        # second narrator turn merely for a few seconds of empty time.
        minimum_advance_minutes = _js_number(_runtime_value(self.config, 'minimumAdvanceMinutes'))
        # Math.max(1, NaN) 在 JS 里是 NaN（比较恒为 false），Python 的 max 会退回 1，这里显式区分。
        minimum_manual_advance_ms = (
            float('nan') if math.isnan(minimum_advance_minutes)
            else max(1, minimum_advance_minutes) * _TIME_MINUTE
        )
        manual_advance_too_soon = bool(force) and not due and not due_follow_ups and elapsed < minimum_manual_advance_ms
        if manual_advance_too_soon:
            self.report_operation(
                'standard', 'info', story, 'advance',
                '手动推进跳过：游标距离现在不足 %d 分钟，且没有到期计划或对话后续任务', minimum_advance_minutes,
            )
            return messages

        advanced = False
        delayed_reply_processed = False
        # A due plan is itself a complete writing turn: it fills the old cursor→now
        # gap and then decides the plan. Avoid a preceding ordinary advance, which
        # would make the next request write the same now→now moment again.
        has_narrative_due = len(due) > 0
        if elapsed > 0 and not has_narrative_due and (force or (automatic_due and not paused_for_conversation)):
            follow_up_participant_id = _automation_value(story, 'conversationFollowUpParticipantId') if due_follow_ups else ''
            follow_up_participant = self.get_participant(follow_up_participant_id) if follow_up_participant_id else None
            phase = (
                'conversation-follow-up'
                if follow_up_participant and follow_up_participant.get('status') == 'active'
                else 'advance'
            )
            timezone = _timezone_of(story)
            self.report_operation(
                'standard', 'info', story, phase,
                '即将执行自动写作 类型=%s 时间段=%s→%s',
                phase_label(phase), format_log_time(from_, timezone), format_log_time(now, timezone),
            )
            decided = self.try_decide(story, follow_up_participant, phase, from_, now, None, [])
            decision = decided.get('decision')
            if decided.get('succeeded'):
                permit_messages = phase == 'conversation-follow-up' or bool(_runtime_value(self.config, 'allowProactiveMessages'))
                persisted = self.persist_decision(
                    story, follow_up_participant, decision, from_, now, permit_messages, phase, [], False,
                    decided.get('timelinePlan'),
                )
                messages.extend(persisted.get('messages') or [])
                self.db_set('interlude_story', {'id': story.get('id')}, {'cursorAt': now, 'updatedAt': now})
                advanced = True

        due_batches = group_due_intents(due)
        # One shared story has one clock. Process one relationship branch per
        # sweep so another branch cannot trigger a duplicate now→now scene.
        due_batch = due_batches[0] if due_batches else None
        if due_batch:
            current = self.get_story(story.get('id'))
            # 如果本轮没有先做 automatic advance，到期意图也必须从故事游标
            # 补写到现在；否则“延迟回复到点”会漏掉中间这段角色生活。
            due_from = narrative_cursor(current, now)
            # Each batch is one relationship branch. This keeps prompts private
            # while still draining every plan that was already due this sweep.
            first_intent = due_batch[0] if isinstance(due_batch[0], dict) else {}
            due_participant_id = first_intent.get('participantId') or ''
            due_participant = self.get_participant(due_participant_id) if due_participant_id else None
            self.report_operation(
                'standard', 'info', current, 'intent-due',
                '即将处理到期计划 数量=%d 类型=%s 参与者=%s',
                len(due_batch),
                ','.join(_unique_js(intent.get('type') for intent in due_batch)),
                (due_participant or {}).get('id') or '全局',
            )
            decided = self.try_decide(current, due_participant, 'intent-due', due_from, now, None, due_batch)
            decision = decided.get('decision')
            succeeded = bool(decided.get('succeeded'))
            stream_recovery = all(
                intent.get('type') == 'narrative-retry'
                and isinstance(intent.get('payload'), dict)
                and intent.get('payload').get('streamRecovery') is True
                for intent in due_batch
            )
            recovered = (
                bool(self.persist_stream_script_recovery(current, due_participant, decision, now))
                if stream_recovery and succeeded else False
            )
            turn_succeeded = recovered if stream_recovery else succeeded
            if not stream_recovery:
                permit_messages = bool(_runtime_value(self.config, 'allowProactiveMessages')) or any(
                    isinstance(intent.get('payload'), dict) and intent.get('payload').get('userInitiated') is True
                    for intent in due_batch
                )
                persisted = self.persist_decision(
                    current, due_participant, decision, due_from, now, permit_messages, 'intent-due', due_batch,
                    False, decided.get('timelinePlan'),
                )
                messages.extend(persisted.get('messages') or [])
            if turn_succeeded:
                self.db_set('interlude_story', {'id': current.get('id')}, {'cursorAt': now, 'updatedAt': now})
                ordinary_due_ids = [intent.get('id') for intent in due_batch if intent.get('type') != 'follow-up-commitment']
                if ordinary_due_ids:
                    self.db_set(
                        'interlude_intent', {'id': {'$in': ordinary_due_ids}},
                        {'status': 'completed', 'updatedAt': now},
                    )
                if any(intent.get('type') == 'delayed-reply' for intent in due_batch):
                    delayed_reply_processed = True
                    self.pause_automatic_advance_after_delayed_reply(
                        story.get('id'), now, (due_participant or {}).get('id') or '',
                    )
                elif not advanced and not delayed_reply_processed:
                    self.schedule_next_automatic_advance(story.get('id'), now)
            else:
                # A failed user turn gets a persisted retry. Otherwise a transient
                # 403/5xx would leave its already-recorded incoming message waiting
                # forever for somebody to send another DM.
                retries = [intent for intent in due_batch if intent.get('type') == 'narrative-retry']
                if retries:
                    attempts = max(
                        _number_or(
                            (intent.get('payload') or {}).get('attempt') if isinstance(intent.get('payload'), dict) else None,
                            0,
                        )
                        for intent in retries
                    )
                    self.db_set(
                        'interlude_intent', {'id': {'$in': [intent.get('id') for intent in retries]}},
                        {'status': 'cancelled', 'updatedAt': now},
                    )
                    if stream_recovery:
                        self.schedule_stream_script_recovery(
                            current.get('id'), (due_participant or {}).get('id') or '', now, attempts,
                        )
                    else:
                        self.schedule_narrative_retry(
                            current.get('id'), (due_participant or {}).get('id') or '', now, attempts,
                        )
                # Keep ordinary delayed plans pending until the provider recovers.
        if len(due_batches) > 1:
            current = self.get_story(story.get('id'))
            self.report_operation(
                'standard', 'info', current, 'intent-due',
                '其余 %d 组到期计划已保留，下一次扫描将按新的时间段继续处理', len(due_batches) - 1,
            )
            wake_at = parse_time(_time_ms(now) + max(
                _TIME_SECOND, _js_number(_runtime_value(self.config, 'sweepIntervalMinutes')) * _TIME_MINUTE,
            ))
            self.schedule_due_intent_wake(story.get('id'), wake_at)
        if advanced and not delayed_reply_processed:
            has_more_follow_ups = bool(due_follow_ups) and bool(self.complete_conversation_follow_ups(story.get('id'), now))
            if not has_more_follow_ups:
                self.schedule_next_automatic_advance(story.get('id'), now)
        return messages

    # ================= 上游 3437-3600：decide（主模型上下文的唯一入口） =================

    def decide(
        self,
        story: InterludeStory,
        participant: Optional[InterludeParticipant],
        phase: NarrativePhase,
        from_: datetime,
        now: datetime,
        user_message: Optional[str],
        due_intents: List[NarrativeIntent],
        superseded_intents: Optional[List[NarrativeIntent]] = None,
        group_context: Optional[GroupContext] = None,
        images: Optional[List[NarrativeImage]] = None,
        audio: Optional[List[NarrativeAudio]] = None,
        extra_web_context: Optional[List[WebObservation]] = None,
        output_recovery: bool = False,
        chat_capabilities: Optional[ChatActionCapabilities] = None,
        quoted_messages: Optional[List[IndexedQuotedMessageContext]] = None,
        sticker_catalog: Optional[List[StickerCatalogEntry]] = None,
        turn_query_embedding: Optional[List[float]] = None,
        visual_observations: Optional[List[str]] = None,
        timeline_plan: Optional[TimelinePlan] = None,
        on_early_reply: Optional[Callable[[EarlyNarrativeReply], bool]] = None,
    ) -> NarrativeDecision:
        """上游 3437-3600 decide。"""
        # 这里是主模型上下文的唯一入口。recentEntries 保留近距离质感，场景、弧线和
        # facts 负责把很长的过去压缩成连续性线索。参与者摘要让模型知道角色
        # 同时还在与谁维系关系，而不是把每个 QQ 当成独立世界。
        superseded_intents = superseded_intents or []
        images = images or []
        audio = audio or []
        extra_web_context = extra_web_context or []
        quoted_messages = quoted_messages or []
        sticker_catalog = sticker_catalog or []
        story_id = story.get('id')
        story_state = story.get('state') if isinstance(story.get('state'), dict) else {}
        participant_id = (participant or {}).get('id')
        timezone = _timezone_of(story)
        shared = self.shared_story_config
        memory_enabled = bool(self.memory_config.get('enabled'))
        model_config = self.config.get('model') if isinstance(self.config.get('model'), dict) else {}
        embedding_config = model_config.get('embedding') if isinstance(model_config.get('embedding'), dict) else {}
        # User and due-intent turns may arrive before the next background sweep.
        # Retire expired consequences here too, while keeping this a cheap local
        # database operation rather than a separate model request.
        fact_query = create_fact_query(participant, user_message, due_intents, superseded_intents)
        # One turn-level query vector serves every semantic consumer (sticker
        # filter, fact ranking, history recall); undefined when none needs it.
        if turn_query_embedding is not None:
            resolved_turn_embedding = turn_query_embedding
        elif participant and isinstance(user_message, str) and user_message.strip() and self.semantic_turn_embedding_enabled():
            max_input = embedding_config.get('maxInputCharacters')
            max_input = 4_000 if max_input is None else _slice_end(max_input)
            resolved_turn_embedding = self.embed_text(user_message.strip()[:max_input])
        else:
            resolved_turn_embedding = None
        live_fact_embedding = resolved_turn_embedding if embedding_config.get('liveQuery') else None
        # 上游 Promise.all → 顺序执行（保持每一项的调用顺序与结果语义）。
        # Use the runtime limits on the live path.  They are the options shown
        # to testers as “上下文条目/长期事实”，and should be authoritative.
        recent_entries = self.recent_entries_for_prompt(story_id, now)
        memories = (
            self.memories(story_id, _runtime_value(self.config, 'memoryLimit'), participant_id)
            if memory_enabled else []
        )
        scene = self.active_scene(story_id)
        arc = self.active_arc(story_id)
        previous_scenes = self.previous_scene_summaries(story_id)
        facts = (
            self.facts(
                story_id, _runtime_value(self.config, 'memoryLimit'), fact_query, participant_id, live_fact_embedding,
            ) if memory_enabled else []
        )
        all_participants = self.participants(story_id)
        web_context = self.web_observations(story_id, participant_id)
        active_consequences = self.active_consequences_and_expire(
            story_id,
            now,
            None if (phase == 'advance' or shared.get('shareParticipantDetails')) else participant_id,
        )
        overlay_snapshots = (
            self.overlay_snapshots_for_prompt(story_id, participant_id, phase == 'advance') if memory_enabled else []
        )
        follow_up_commitments = (
            self.pending_follow_up_commitments(story_id, participant_id)
            if participant and phase in ('user-message', 'intent-due') else []
        )
        schedule_record = self.get_schedule_preplan(story_id) if self.schedule_preplan_config.get('enabled') else None
        upcoming_intents = self.upcoming_narrative_intents(story_id, now)
        visible_entries = recent_entries if shared.get('shareParticipantDetails') else [
            entry for entry in recent_entries
            # Group transcripts are part of the shared life, but do not expose
            # their raw text to a private relationship unless the owner opts in.
            if not (group_context is None and entry.get('kind') in ('group-message', 'character-group-message'))
            and (not entry.get('participantId') or entry.get('participantId') == participant_id)
        ]
        # Background advancement is not a chat turn. It receives the ongoing
        # life script, scene and facts, but not raw private/group transcript rows
        # that a model could mistake for a message arriving right now.
        turn_entries = (
            [
                entry for entry in visible_entries
                if entry.get('kind') not in ('user-message', 'character-message', 'group-message', 'character-group-message')
            ]
            if phase == 'advance' else visible_entries
        )
        # The turn's explicit event decides what is happening now. Historical
        # rows are context only and are never reinterpreted as a fresh message.
        prompt_entries = [entry for entry in turn_entries if _has_trimmed_content(entry)]
        if phase == 'user-message' and any(_has_trimmed_content(entry) for entry in visible_entries) and not prompt_entries:
            raise Exception('Narrative context integrity failure: visible raw history did not reach recentScript.')
        decoded_state = decode_story_state(story.get('state'))
        scene_frame = project_scene_frame({
            'storyId': story_id, 'now': now, 'scene': scene, 'state': decoded_state, 'recentEntries': prompt_entries,
            'workingDetails': self.prune_working_details(decoded_state.get('workingDetails'), now),
            'scenePresence': decoded_state.get('scenePresence'),
            'agencyWindow': active_agency_window(decoded_state.get('agencyWindow'), now),
        })
        if user_message is not None:
            topic_text = user_message
        elif group_context is not None:
            topic_text = '\n'.join(_ts_text(message.get('content')) for message in (group_context.get('messages') or []))
        else:
            topic_text = ''
        dialogue_burst = resolve_dialogue_burst(
            scene_frame,
            decoded_state.get('dialogueBurst'),
            now,
            {
                'scope': participant_id or 'protagonist-life',
                'topicText': topic_text,
                'boundary': phase == 'advance',
            },
        )
        participants = sorted(
            [item for item in all_participants if item.get('id') != participant_id and self.can_handle_participant(item)],
            key=participant_relevance,
            reverse=True,
        )
        participants = _slice_head(participants, shared.get('participantContextLimit'))
        agency_enabled = bool(self.agency_config.get('enabled')) \
            and bool(_runtime_value(self.config, 'allowProactiveMessages')) \
            and (phase == 'advance' or (
                phase == 'intent-due' and any(intent.get('type') == 'proactive-check' for intent in due_intents)
            ))
        advance_can_contact = phase == 'advance' and bool(_runtime_value(self.config, 'allowProactiveMessages'))
        visible_due_intents = due_intents if shared.get('shareParticipantDetails') else [
            intent for intent in due_intents
            if not intent.get('participantId') or intent.get('participantId') == participant_id
        ]
        visible_upcoming_intents = upcoming_intents if (phase == 'advance' or shared.get('shareParticipantDetails')) else [
            intent for intent in upcoming_intents
            if not intent.get('participantId') or intent.get('participantId') == participant_id
        ]
        # A relationship consequence belongs to the protagonist's actual life.
        # Background writing therefore sees its compact effect even when raw
        # cross-participant chat history remains private. Live turns still see
        # only their own (and global) consequences unless sharing is enabled.
        visible_consequences = active_consequences if (phase == 'advance' or shared.get('shareParticipantDetails')) else [
            intent for intent in active_consequences
            if not intent.get('participantId') or intent.get('participantId') == participant_id
        ]
        merged_web_context = sorted(
            [observation for observation in (list(web_context) + list(extra_web_context)) if observation.get('status') != 'deleted'],
            key=lambda observation: _time_ms(observation.get('accessedAt')),
        )
        merged_web_context = _slice_tail(merged_web_context, -max(1, self.browser_config.get('maxObservationsInPrompt')))
        refresh_continuity = self.should_refresh_continuity(story, phase)
        user_reported_times = (
            extract_user_reported_times(user_message, now, timezone)
            if phase == 'user-message' and isinstance(user_message, str) and user_message.strip() else None
        )
        development_tendencies = (
            self.development_for_prompt(
                story_id,
                participant_id,
                development_context_query(
                    user_message,
                    [intent.get('summary') for intent in visible_due_intents],
                    prompt_entries,
                ),
            ) if memory_enabled else []
        )
        recall_topics = [_fact_topic(fact) for fact in facts if fact.get('unresolved')]
        recall_topics = [topic for topic in recall_topics if topic]
        recall_query = recall_focus(
            user_message, recall_topics, [intent.get('summary') for intent in visible_due_intents],
        )
        context_time_window = _js_number(_runtime_value(self.config, 'contextTimeWindowMinutes'))
        recent_protection_since = (
            parse_time(_time_ms(now) - min(context_time_window, 1_440) * _TIME_MINUTE)
            if context_time_window > 0 else None
        )
        configured_separator = (self.config.get('runtime') or {}).get('messageSeparator')
        message_separator = configured_separator.strip() if isinstance(configured_separator, str) else ''
        message_separator = message_separator or '<sep/>'
        request: NarrativeRequest = {
            'urgeEnabled': bool(self.urge_config.get('enabled')) and not any(
                intent.get('type') == 'narrative-retry' for intent in due_intents
            ),
            'phase': phase, 'refreshContinuity': refresh_continuity, 'outputRecovery': output_recovery,
            'story': story, 'from': from_, 'now': now, 'userMessage': user_message,
            'userReportedTimes': user_reported_times, 'images': images, 'audio': audio,
            'visualObservations': visual_observations, 'timelinePlan': timeline_plan,
            'developmentTendencies': development_tendencies,
            'writingOptions': {
                'messageSeparator': message_separator,
                'splitReplyMessages': _runtime_value(self.config, 'splitReplyMessages') is not False,
                'browserMode': 'disabled'
                if (not self.browser_config.get('enabled')
                    or (group_context is not None and not self.browser_config.get('allowGroupTriggeredResearch')))
                else (self.browser_config.get('mode')
                      if (phase == 'user-message' and participant and group_context is None)
                      else 'deferred-only'),
            },
            'participant': None if phase == 'advance' else participant,
            # A background turn may see relationship state through these opaque
            # participant summaries and may proactively contact one account only
            # when the owner explicitly enables proactive messages.
            'participants': [] if (phase == 'advance' and not advance_can_contact) else participants,
            'dueIntents': visible_due_intents, 'upcomingIntents': visible_upcoming_intents,
            'activeConsequences': visible_consequences, 'supersededIntents': superseded_intents,
            'shareParticipantDetails': shared.get('shareParticipantDetails'),
            'recentEntries': prompt_entries, 'memories': memories,
            'sceneContext': {
                'scene': scene, 'arc': arc,
                **({'previousScenes': previous_scenes} if previous_scenes else {}),
            },
            'facts': facts, 'groupContext': group_context, 'chatCapabilities': chat_capabilities,
            'contactThreads': self.contact_threads(story_id, facts, participant_id) if memory_enabled else [],
            'sceneFrame': scene_frame, 'dialogueBurst': dialogue_burst,
            'workingDetails': self.prune_working_details(decoded_state.get('workingDetails'), now),
            'timelineCarry': decoded_state.get('timelineCarry'),
            'recalledHistory': (
                self.recall_history(
                    story_id,
                    participant_id or '',
                    recall_query,
                    resolved_turn_embedding or [],
                    set(entry.get('id') for entry in compact_prompt_entries(
                        prompt_entries, 24_000, recent_protection_since,
                    )),
                    set(
                        source_id
                        for fact in facts
                        if (isinstance(user_message, str) and user_message.strip())
                        or (fact.get('unresolved') and _fact_topic(fact))
                        for source_id in (fact.get('sourceEntryIds') or [])
                    ),
                    3 if (isinstance(user_message, str) and user_message.strip()) else 1,
                ) if (memory_enabled and recall_query.strip()) else None
            ),
            'recentProtectionSince': recent_protection_since,
            'webContext': merged_web_context, 'overlaySnapshots': overlay_snapshots,
            'alterEnabled': bool(self.alter_system_config.get('enabled')),
            'emotionalOffset': self.emotional_offset_for_prompt(story),
            'agencyEnabled': agency_enabled,
            'agencyWindow': (active_agency_window(story_state.get('agencyWindow'), now) or None) if agency_enabled else None,
            'automaticDeliverySummaries': decoded_state.get('automaticDeliverySummaries')
            if is_automatic_narrative_phase(phase) else [],
            'followUpCommitments': follow_up_commitments,
            'schedulePreplan': schedule_preplan_window(
                schedule_record, now, timezone, 12, self.schedule_preplan_config,
            ),
            'onEarlyReply': on_early_reply,
        }
        if quoted_messages:
            request['quotedMessages'] = quoted_messages
        if sticker_catalog and phase == 'user-message':
            request['stickerCatalog'] = sticker_catalog
        # 注入的 provider 也可能返回非对象 JSON 根：统一归一化，避免后续 .get 抛 AttributeError。
        return normalize_decoded_decision(resolve_authored_actions(
            self.narrator.decide(request), False, _runtime_value(self.config, 'messageSeparator'),
        ))

    # ================= 上游 3604-3608：shouldRefreshContinuity =================

    def should_refresh_continuity(self, _story: InterludeStory, _phase: NarrativePhase) -> bool:
        """上游 3604-3608 shouldRefreshContinuity。

        Refresh continuity only on the first automatic pass or every fifteenth
        successful narrative write. Ordinary turns reuse the last snapshot.
        """
        # Background scene/arc compaction remains active. A second real-time
        # summary adds neither original evidence nor execution confirmation.
        return False

    # ================= 上游 3613-3687：planAutomaticTimeline =================

    def plan_automatic_timeline(
        self,
        story: InterludeStory,
        participant: Optional[InterludeParticipant],
        phase: NarrativePhase,
        from_: datetime,
        now: datetime,
        due_intents: List[NarrativeIntent],
    ) -> Optional[TimelinePlan]:
        """上游 3613-3687 planAutomaticTimeline。

        Automatic prose no longer invents the world timeline by itself. The
        compaction route first returns a tiny relative-time ledger; if it cannot,
        preserving the current cursor is safer than writing an ungrounded future.
        """
        if (self.config.get('timelineDirector') or {}).get('enabled') is False:
            return None
        if getattr(self.compactor, 'plan_timeline', None) is None:
            return None
        story_id = story.get('id')
        timezone = _timezone_of(story)
        from_ms = _time_ms(from_)
        now_ms = _time_ms(now)
        backoff = self.timeline_backoff.get(story_id)
        if backoff and backoff.get('from') == from_ms and now_ms < backoff.get('until'):
            self.report_operation(
                'diagnostic', 'debug', story, phase,
                '时间导演调用冷却中，保留当前时间窗口至 %s', format_log_time(parse_time(backoff.get('until')), timezone),
            )
            return None
        if backoff and (backoff.get('from') != from_ms or now_ms >= backoff.get('until')):
            self.timeline_backoff.pop(story_id, None)
        # 熔断冷却：连续失败达阈值后，冷却期内不再调用时间导演（降级路径接管），到期重试一次完整路径。
        failures = self.timeline_director_failures.get(story_id, 0)
        if failures >= TIMELINE_DIRECTOR_FUSE and backoff and now_ms < backoff.get('until'):
            return None
        participant_id = (participant or {}).get('id')
        shared = self.shared_story_config
        memory_enabled = bool(self.memory_config.get('enabled'))
        # 上游 Promise.all → 顺序执行（保持每一项的调用顺序与结果语义）。
        scene = self.active_scene(story_id)
        recent_entries = self.recent_entries_for_prompt(story_id, now)
        facts = (
            self.facts(
                story_id,
                _slice_end(min(16, _js_number(_runtime_value(self.config, 'memoryLimit')))),
                '', participant_id,
            ) if memory_enabled else []
        )
        schedule_record = self.get_schedule_preplan(story_id) if self.schedule_preplan_config.get('enabled') else None
        visible_entries = recent_entries if shared.get('shareParticipantDetails') else [
            entry for entry in recent_entries
            if not entry.get('participantId') or entry.get('participantId') == participant_id
        ]
        continuation: Optional[Dict[str, Any]] = None
        for entry in reversed(visible_entries):
            if entry.get('kind') == 'script' and _has_trimmed_content(entry):
                continuation = entry
                break
        continuation_ledger = timeline_entry_prompt_projection(continuation) if continuation else None
        visible_due_intents = [
            intent for intent in due_intents
            if not intent.get('participantId') or intent.get('participantId') == participant_id
        ]
        recall_topics = [_fact_topic(fact) for fact in facts if fact.get('unresolved')]
        recall_query = recall_focus(
            None, recall_topics, [intent.get('summary') for intent in visible_due_intents],
        )
        request = {
            'story': story, 'participant': participant, 'phase': phase, 'from': from_, 'now': now,
            'scene': scene, 'facts': facts,
            'recentEntries': [timeline_entry_prompt_projection(entry) for entry in visible_entries],
            'contactThreads': self.contact_threads(story_id, facts, participant_id) if memory_enabled else [],
            'recalledHistory': (
                self.recall_history(
                    story_id,
                    participant_id or '',
                    recall_query,
                    [],
                    set(entry.get('id') for entry in visible_entries),
                    set(
                        source_id
                        for fact in facts
                        if fact.get('unresolved') and _fact_topic(fact)
                        for source_id in (fact.get('sourceEntryIds') or [])
                    ),
                    1,
                ) if (memory_enabled and recall_query) else None
            ),
            'recentScriptContinuation': ({
                'content': continuation.get('content'),
                'occurredAt': continuation.get('occurredAt'),
                **({'hostTimelineLedger': continuation_ledger.get('content')}
                   if (continuation_ledger and continuation_ledger.get('content') != continuation.get('content'))
                   else {}),
            } if continuation else None),
            'dueIntents': due_intents,
            'schedulePreplan': schedule_preplan_window(
                schedule_record, now, timezone, 12, self.schedule_preplan_config,
            ),
        }
        try:
            raw_plan = self.compactor.plan_timeline(request)
            plan = normalize_timeline_plan(raw_plan)
            if plan:
                self.timeline_director_failures.pop(story_id, None)
                self.timeline_backoff.pop(story_id, None)
                automation = {**_automation(story)}
                automation.pop('timelineRetryAt', None)
                automation.pop('timelineRetryFrom', None)
                story['state'] = {
                    **(story.get('state') if isinstance(story.get('state'), dict) else {}),
                    'automation': automation,
                }
                self.report_operation(
                    'diagnostic', 'debug', story, phase, '时间导演已生成事件账本 节点=%d', len(plan.get('beats') or []),
                )
            else:
                # 可诊断性：把模型原始返回暴露出来，避免"永远失效但不知道为什么"。
                if isinstance(raw_plan, str):
                    raw_preview = raw_plan[:400]
                else:
                    stringified = _js_json_stringify(None if raw_plan is None else raw_plan)
                    raw_preview = stringified[:400] if stringified is not None else None
                failures = self.timeline_director_failures.get(story_id, 0) + 1
                self.timeline_director_failures[story_id] = failures
                self.report_operation(
                    'standard', 'warn', story, phase,
                    '时间导演返回被拒绝（连续第 %d 次）原始返回=%s 拒绝原因=%s',
                    failures,
                    raw_preview if raw_preview is not None else 'undefined',
                    describe_timeline_plan_rejection(raw_plan),
                )
                self.persist_timeline_retry(story, from_, phase, failures)
            return plan
        except Exception as error:  # noqa: BLE001 - 与上游 catch 一致：失败走退避重试
            failures = self.timeline_director_failures.get(story_id, 0) + 1
            self.timeline_director_failures[story_id] = failures
            self.report_operation('diagnostic', 'warn', story, phase, '时间导演调用失败 错误=%s', error)
            self.persist_timeline_retry(story, from_, phase, failures)
            return None

    # ================= 上游 3689-3693：isTimelineDirectorFused =================

    def is_timeline_director_fused(self, story_id: str) -> Optional[int]:
        """上游 3689-3693 isTimelineDirectorFused（熔断判定：连续失败达到阈值即熔断）。"""
        failures = self.timeline_director_failures.get(story_id)
        if not failures or failures < TIMELINE_DIRECTOR_FUSE:
            return None
        return failures

    # ================= 上游 3698-3718：persistTimelineRetry =================

    def persist_timeline_retry(
        self,
        story: InterludeStory,
        from_: datetime,
        phase: NarrativePhase,
        failures: int = 1,
    ) -> None:
        """上游 3698-3718 persistTimelineRetry。

        Persist the retry gate once per unchanged cursor. The in-memory map is
        retained for fast checks inside a live turn, while the story state makes
        the guard survive a plugin reload/restart.
        """
        # 指数退避：10min → 20min → 40min → 80min → 160min → 封顶 2h。
        # 退避只决定重试节奏；真正终止循环的是熔断降级（planAutomaticTimeline）。
        # `min(..., 60)` 只为拦住无界大整数（2**n 早已超过 2h 封顶），不改变结果。
        failure_count = _js_number(failures)
        exponent = 60 if not math.isfinite(failure_count) else min(max(0, int(failure_count) - 1), 60)
        backoff = min(TIMELINE_RETRY_BACKOFF_BASE * (2 ** exponent), TIMELINE_DIRECTOR_FUSE_COOLDOWN)
        until = parse_time(_now_ms() + backoff)
        self.timeline_backoff[story.get('id')] = {'from': _time_ms(from_), 'until': _time_ms(until)}
        automation = {
            **_automation(story),
            'timelineRetryAt': iso(until),
            'timelineRetryFrom': iso(from_),
            # Move the automatic wake-up out of the failed window. This is a hint
            # for legacy schedulers; the entry guard above remains authoritative.
            'nextAdvanceAt': iso(until),
        }
        story['state'] = encode_story_state({**decode_story_state(story.get('state')), 'automation': automation})
        try:
            self.db_set('interlude_story', {'id': story.get('id')}, {'state': story.get('state'), 'updatedAt': now_utc()})
        except Exception as error:  # noqa: BLE001 - 冷却只是加速器，持久化失败继续用内存冷却
            self.report_operation(
                'diagnostic', 'debug', story, phase,
                '时间导演冷却状态持久化失败，将继续使用内存冷却 错误=%s', error,
            )

    # ================= 上游 3720-3831：tryDecide =================

    def try_decide(
        self,
        story: InterludeStory,
        participant: Optional[InterludeParticipant],
        phase: NarrativePhase,
        from_: datetime,
        now: datetime,
        user_message: Optional[str],
        due_intents: List[NarrativeIntent],
        superseded_intents: Optional[List[NarrativeIntent]] = None,
        group_context: Optional[GroupContext] = None,
        images: Optional[List[NarrativeImage]] = None,
        audio: Optional[List[NarrativeAudio]] = None,
        chat_capabilities: Optional[ChatActionCapabilities] = None,
        quoted_messages: Optional[List[IndexedQuotedMessageContext]] = None,
        sticker_catalog: Optional[List[StickerCatalogEntry]] = None,
        turn_query_embedding: Optional[List[float]] = None,
        visual_observations: Optional[List[str]] = None,
        on_early_reply: Optional[Callable[[EarlyNarrativeReply], bool]] = None,
    ) -> Dict[str, Any]:
        """上游 3720-3831 tryDecide。"""
        immediate_observations: List[WebObservation] = []
        effective_now = now
        timezone = _timezone_of(story)
        automatic_phase = phase in ('advance', 'conversation-follow-up', 'intent-due')
        short_schedule = (
            schedule_preplan_window(
                self.get_schedule_preplan(story.get('id')), from_, timezone, 12, self.schedule_preplan_config,
            )
            if (automatic_phase and phase != 'advance' and self.schedule_preplan_config.get('enabled')) else None
        )
        director_required = (
            automatic_phase
            and (self.config.get('timelineDirector') or {}).get('enabled') is not False
            and needs_timeline_director(phase, from_, now, timezone, short_schedule)
        )
        timeline_plan = (
            self.plan_automatic_timeline(story, participant, phase, from_, now, due_intents)
            if director_required else None
        )
        if director_required and not timeline_plan:
            # 降级而非冻结：账本缺失只损失时间结构辅助，自动推进照常进行
            # （熔断计数仍抑制导演调用本身，冷却后自动重试完整路径）。
            # 旧实现在这里丢弃整个回合，0 命中率曾让自动生活流实质瘫痪。
            fused = self.is_timeline_director_fused(story.get('id'))
            if fused:
                self.report_operation(
                    'standard', 'warn', story, phase,
                    '时间导演已熔断（连续失败 %d 次），本次自动回合降级为无账本推进', fused,
                )
            else:
                self.report_operation(
                    'standard', 'warn', story, phase, '时间导演未生成有效事件账本，本次自动回合降级为无账本推进',
                )
        started_at = _now_ms()
        self.report_operation(
            'standard', 'info', story, phase,
            '模型调用开始 任务=主叙事 模型=%s 参与者=%s 时间段=%s→%s 到期计划=%d',
            self.main_model_label(), (participant or {}).get('id') or '全局',
            format_log_time(from_, timezone), format_log_time(now, timezone), len(due_intents),
        )
        try:
            early_reply_committed = False

            def wrapped_early_reply(reply: EarlyNarrativeReply) -> Any:
                """上游 `async reply => { const committed = await onEarlyReply(reply); ... }`。"""
                nonlocal early_reply_committed
                committed = on_early_reply(reply)
                if committed:
                    early_reply_committed = True
                return committed

            can_early_reply = on_early_reply is not None and not (
                phase == 'user-message' and participant and group_context is None
                and self.browser_config.get('enabled') and self.browser_config.get('mode') == 'allow-immediate'
            )
            early_reply = wrapped_early_reply if can_early_reply else None
            decision = self.decide(
                story, participant, phase, from_, effective_now, user_message, due_intents,
                superseded_intents, group_context, images, audio, [], False, chat_capabilities,
                quoted_messages, sticker_catalog, turn_query_embedding, visual_observations,
                timeline_plan, early_reply,
            )
            immediate = None
            if (
                phase == 'user-message' and participant and group_context is None
                and self.browser_config.get('enabled') and self.browser_config.get('mode') == 'allow-immediate'
            ):
                immediate = next(
                    (
                        normalized for normalized in (
                            normalize_browser_intent_draft(intent, self.browser_config)
                            for intent in (decision.get('browserIntents') or [])
                        )
                        if normalized and normalized.get('timing') == 'immediate'
                    ),
                    None,
                )
            if immediate:
                # The first pass merely proposes the action. Do not persist its prose
                # or chat decision: after the real page read, ask the narrator once
                # more with the observation so the final script stays a single,
                # coherent piece of writing rather than two stitched tool calls.
                self.report_operation('standard', 'info', story, phase, '即时网页观察开始 模式=%s', immediate.get('mode'))
                observation = self.collect_web_observation(
                    story, immediate, participant.get('id'), None, now_utc(), False,
                )
                immediate_observations = [observation]
                effective_now = now_utc()
                decision = self.decide(
                    story, participant, phase, from_, effective_now, user_message, due_intents,
                    superseded_intents, group_context, images, audio, immediate_observations, False,
                    chat_capabilities, quoted_messages, sticker_catalog, turn_query_embedding,
                    visual_observations, timeline_plan,
                )
            # 用户自报的钟点（“八点赶到”）对守卫背书：模型复述它们不是时间越界。
            # 提取是 O(消息长度) 的本地正则，只在实况用户回合发生一次。
            endorsed_clocks: Optional[Set[float]] = None
            if phase == 'user-message' and isinstance(user_message, str) and user_message.strip():
                endorsed_clocks = set()
                for fact in extract_user_reported_times(user_message, effective_now, timezone):
                    value = _endorsed_clock_value(fact.get('localTime'))
                    if value is not None:
                        endorsed_clocks.add(value)
            initial_time_overflow = detect_live_script_time_overflow(
                decision.get('script'), phase, from_, effective_now, timezone, endorsed_clocks,
            )
            main_available = bool((self.model_routing.get('main') or {}).get('available'))
            initial_visible_recovery = (
                main_available and not early_reply_committed
                and requires_visible_reply_recovery(phase, group_context, decision)
            )
            if initial_time_overflow or initial_visible_recovery:
                # 诊断：记录被抛弃草稿里模型实际返回的 interaction（缺失/为空/形状错误），
                # 让下一次"结构化可见回复缺失"可以直接从日志定位是模型行为还是解析问题。
                if initial_visible_recovery:
                    self.report_operation(
                        'standard', 'warn', story, phase, '被抛弃草稿的结构化回复字段 interaction=%s groupReply=%s',
                        _preview(_get(decision, 'interaction')), _preview(_get(decision, 'groupReply')),
                    )
                if initial_time_overflow:
                    self.report_operation(
                        'standard', 'warn', story, phase,
                        '剧本越过当前时间终点，已抛弃本次未落库剧本并重新写作 原因=%s', initial_time_overflow,
                    )
                else:
                    self.report_operation(
                        'standard', 'warn', story, phase, '结构化可见回复缺失，已抛弃本次未落库剧本并重新写作',
                    )
                decision = self.decide(
                    story, participant, phase, from_, effective_now, user_message, due_intents,
                    superseded_intents, group_context, images, audio, immediate_observations, True,
                    chat_capabilities, quoted_messages, sticker_catalog, turn_query_embedding,
                    visual_observations, timeline_plan,
                )
                recovered_time_overflow = detect_live_script_time_overflow(
                    decision.get('script'), phase, from_, effective_now, timezone, endorsed_clocks,
                )
                if recovered_time_overflow:
                    raise Exception(
                        'Narrative provider crossed the live time boundary after one recovery attempt: %s'
                        % recovered_time_overflow
                    )
                if main_available and not early_reply_committed and requires_visible_reply_recovery(phase, group_context, decision):
                    self.report_operation(
                        'standard', 'warn', story, phase,
                        '恢复尝试仍缺失结构化回复 interaction=%s', _preview(_get(decision, 'interaction')),
                    )
                    raise Exception('Narrative provider omitted the required visible-reply structure after one recovery attempt.')
            # The fixed narrative contract requires prose for every real model turn.
            # A syntactically valid object with an omitted/blank script used to be
            # treated as success, advancing the cursor while leaving a gap in the
            # life record. Treat it like a provider failure so live user turns use
            # the existing persisted retry path and background turns retain time for
            # their next attempt. Fallback is intentionally a no-network smoke mode.
            if main_available and not has_required_narrative_script(decision):
                raise Exception('Narrative provider returned no usable script.')
            result: Dict[str, Any] = {
                'decision': decision,
                'succeeded': True,
                'effectiveNow': effective_now,
                'immediateObservations': immediate_observations,
                'timelinePlan': timeline_plan,
            }
            logging_config = self.config.get('logging') if isinstance(self.config.get('logging'), dict) else {}
            script_text = result['decision'].get('script')
            if logging_config.get('logScriptPreview') and script_text:
                preview_length = logging_config.get('previewLength')
                preview_text = script_text if preview_length is None else script_text[:_slice_end(preview_length)]
                self.report('info', story, phase, '当前剧本内容：\n%s', preview_text)
            self.report_operation(
                'standard', 'info', story, phase,
                '模型调用完成 任务=主叙事 耗时=%dms 剧本文字=%d 回复模式=%s',
                _now_ms() - started_at,
                len(script_text) if isinstance(script_text, str) else 0,
                visible_reply_mode(result['decision'], phase, group_context),
            )
            # 无可见回复的私聊回合打出最终 interaction，用于区分：模型主动 none、
            # 未读沉默（seen=false）、引用失配被兜底前丢弃（actionId 残留）三种链路。
            if phase == 'user-message' and group_context is None and _nested_get(result['decision'], 'interaction', 'reply', 'mode') == 'none':
                self.report_operation(
                    'diagnostic', 'info', story, phase, '本回合无可见回复 interaction=%s',
                    _preview(_get(result['decision'], 'interaction')),
                )
            return result
        except Exception as error:  # noqa: BLE001 - 与上游 catch 一致：失败返回可重试结果
            self.report('warn', story, phase, '模型调用失败 任务=主叙事 耗时=%dms 错误=%s', _now_ms() - started_at, error)
            return {
                'decision': {}, 'succeeded': False, 'effectiveNow': effective_now,
                'immediateObservations': immediate_observations, 'timelinePlan': timeline_plan,
            }
