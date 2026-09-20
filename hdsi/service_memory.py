# -*- coding: utf-8 -*-
"""HDS-Interlude 移植：上游 `.hdsi_reference/src/service.ts` 行号 5866-6720（1.0.1-beta6-rebuild）。

ServiceMemoryMixin —— 记忆层：
- 压缩指纹 / in-memory 冷却 / 排队与后台扫描（compactionFingerprint、scheduleCompaction、
  compactStories）；
- Schedule Preplan 后台审查、证据投影与流式脚本恢复（getSchedulePreplan、
  prepareSchedulePreplanReview、persistSchedulePreplanReview、scheduleStreamScriptRecovery 等）；
- 压缩准备与落库（prepareCompaction / applyCompaction / persistCompaction /
  persistFact / persistStatePatch）；
- Overlay 分层归档与 live overlay 重建（compactOverlayUnlocked、overlaySnapshotsForPrompt、
  rebuildLiveOverlayState）；
- Embedding 文本请求与长期事实向量回填（embedText、backfillFactEmbeddings）；
- development 提示词候选（developmentForPrompt）。

约定（见 SERVICE_PORTING_SPEC.md / PORTING_GUIDE.md）：
- 本 mixin 不定义 __init__；共享状态与能力由 hdsi/service_base.py 提供：
  self.config / self.ctx / self.platform / self.compactor / self.embedder /
  self.db_get / self.db_create / self.db_set / self.serial / self.get_story /
  self.get_participant / self.participants / self.facts / self.active_scene / self.active_arc /
  self.ensure_continuity / self.append_entry / self.append_intent /
  self.schedule_due_intent_wake / self.has_pending_narrative / self.get_canonical_story /
  self.can_handle_story / self.invalidate_history_vectors / self.report / self.report_operation /
  self.report_standalone_operation / self.memory_config / self.schedule_preplan_config /
  self.shared_story_config / self.compaction_backoff / self.schedule_preplan_backoff /
  self.scheduled_compactions / self.fact_backfills。
- 上游 async/Promise 同步化；`void (async () => {})()` 这类后台work用
  self.ctx.set_timeout 起步（与上游一致不阻塞调用方），错误按上游 .catch 文案记录。
- 压缩调用：self.compactor.compact(request) / compact_overlay(request) /
  plan_schedule_preplan(request)；Embedding：self.embedder.embed(text)。
- 持久化字段名 / JSON key / prompt 与日志文案逐字保留（camelCase 原样）。

附带移植了区间内引用、但上游定位于 service.ts 顶层的私有小 helper
（mergeNote / patchClaimsMatch / statePatchEvidence / groupOverlayPatches /
groupOverlaySnapshots / normalizeMajorEvents / limitEntriesByCharacters /
hasCompactionEvidence / resolveParticipantId / normalizeFact / startOfUtcWindow /
lexicalRecallKeys / normalizeScenePresenceDrafts / timelineEntryPromptProjection /
historyLexicalScore / normalizeTimelinePlan）与 PreparedCompaction* / CompactionBackoff 结构；
其中上游 export 的三个（timelineEntryPromptProjection 7521 / normalizeScenePresenceDrafts 7807 /
historyLexicalScore 8247）优先从 hdsi/service_helpers.py 导入。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, TypedDict, Union

from .script.continuity_checkpoint import assert_continuity_review, compaction_prefix
from .script.development import (
    development_dimension,
    development_scenes,
    prompt_ready_development,
    reviewed_development_support,
)
from .script.episode_index import grounded_episode_tags
from .script.knowledge_evidence import normalize_knowledge_evidence, supports_recorded_outcome
from .script.life_handoff import entry_life_handoff
from .schedule_preplan import (
    apply_schedule_preplan_proposal,
    normalize_schedule_preplan_record,
    refresh_schedule_preplan,
    schedule_preplan_needs_model,
    schedule_preplan_review_due,
)
from .story_state import (
    decode_story_state,
    encode_story_state,
    normalize_participant_state,
    normalize_scene_presence_state,
)
from .time_utils import calendar_day_key, iso, now_utc, parse_time
from .types import (
    CompactionDecision,
    CompactionRequest,
    InterludeParticipant,
    InterludeScene,
    InterludeStory,
    NarrativeFact,
    OverlaySnapshot,
    SchedulePreplanRecord,
    SchedulePreplanReviewRequest,
    ScriptEntry,
    StatePatchDraft,
    StatePatchProposal,
)
from .utils import clamp_number, clip, is_record

# ===================== 上游 service.ts 顶层常量（逐字保留） =====================

_TIME_SECOND = 1_000
_TIME_MINUTE = 60_000
_TIME_HOUR = 3_600_000
_TIME_DAY = 86_400_000

# 上游 211：Schedule Preplan 失败后的每日重试冷却（2 小时）。
SCHEDULE_PREPLAN_RETRY_BACKOFF = 2 * _TIME_HOUR
# 上游 215：压缩失败后对同一指纹的冷却（2 小时，刻意只保存在内存里）。
COMPACTION_RETRY_BACKOFF = 2 * _TIME_HOUR

_NAN = float('nan')

# 上游 mergeNote / patchClaimsMatch / normalizeFact 的字符类（逐字）。
_RE_CLAIM_PUNCT = re.compile(r'[，。！？、,.!?；;:：]')
_RE_WHITESPACE = re.compile(r'\s+')
# 上游 historyLexicalScore 的 lexicalRecallKeys：英文词 + 中文二元组。
_RE_WORD_KEY = re.compile(r'[a-z0-9]{3,}')
_RE_CJK_RUN = re.compile(r'[\u3400-\u9fff]{2,}')
# 对应 JS /[\p{P}\p{S}\s]+/gu（近似：\W 含标点/符号/空白，再显式去掉下划线）。
_RE_PUNCT_SYMBOL_SPACE = re.compile(r'[\W_]+')

# 上游 normalizeScenePresenceDrafts 的 hasExplicitPresenceEvidence 正则（逐字）。
_RE_PRESENCE_OFF_SCENE = re.compile(r'告别|道别|分别|先走|离开|离去|回家|回去了|独自|分开|告辞')
_RE_PRESENCE_EXPECTED = re.compile(r'约好|约在|等会|稍后|会来|准备来|约见')
_RE_PRESENCE_PRESENT = re.compile(r'一起|同行|身边|来到|抵达|进入|走进|拉着|坐在|站在|陪着')

# 上游 7454-7461 TIMELINE_KIND_ALIASES（逐字，含中文标签）。
_TIMELINE_KIND_ALIASES: Dict[str, str] = {
    'activity': 'activity', 'action': 'activity', 'event': 'activity', 'scene': 'activity', 'behavior': 'activity',
    'thought': 'thought', 'think': 'thought', 'feeling': 'thought', 'mood': 'thought', 'inner': 'thought',
    'state': 'state', 'status': 'state', 'condition': 'state',
    '活动': 'activity', '行动': 'activity', '事件': 'activity', '场景': 'activity',
    '想法': 'thought', '心情': 'thought', '思绪': 'thought',
    '状态': 'state',
}


# ===================== 上游顶层导出 helper（hdsi/service_helpers.py） =====================
# 上游 7521 / 7807 / 8247 的纯函数由并行移植的 service_helpers.py 提供；
# 尚未落地时回退到本文件内联副本（与上游逐行等价，便于本 mixin 独立可用）。

try:  # pragma: no cover - 取决于并行移植进度
    from .service_helpers import (
        history_lexical_score,
        normalize_scene_presence_drafts,
        timeline_entry_prompt_projection,
    )
except ImportError:  # TODO(并行移植): service_helpers 就绪后自动切换
    def timeline_entry_prompt_projection(entry: ScriptEntry) -> ScriptEntry:
        """上游 7521-7529 timelineEntryPromptProjection。"""
        metadata = entry.get('metadata') or {}
        if metadata.get('narrativeAuthority') == 'original-v2':
            return entry
        if entry.get('kind') != 'script':
            return entry
        plan = normalize_timeline_plan(metadata.get('timelinePlan'))
        if not plan:
            return entry
        beats = ' | '.join(
            '%d%% %s: %s' % (_js_round((beat.get('at') or 0) * 100), beat.get('kind'), beat.get('summary'))
            for beat in plan.get('beats') or []
        )
        carry = (' Carry: ' + ' | '.join(plan.get('carry') or [])) if plan.get('carry') else ''
        return {**entry, 'content': '[Host timeline ledger for this completed automatic window: %s.%s]' % (beats, carry)}

    def normalize_scene_presence_drafts(value: Any, entries: List[ScriptEntry],
                                        now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """上游 7807-7826 normalizeScenePresenceDrafts。"""
        if now is None:
            now = now_utc()
        if not isinstance(value, list):
            return []
        by_id: Dict[Any, ScriptEntry] = {entry.get('id'): entry for entry in entries}
        next_items: List[Dict[str, Any]] = []
        for item in value:
            if not is_record(item):
                continue
            name = clip(item.get('name'), 80)
            status = item.get('status') if item.get('status') in ('present', 'off-scene', 'expected') else None
            basis = clip(item.get('basis'), 300)
            raw_ids = item.get('sourceEntryIds')
            source_entry_ids = [
                id_ for id_ in raw_ids if _is_number(id_) and id_ in by_id
            ][:8] if isinstance(raw_ids, list) else []
            evidence = [
                by_id[id_] for id_ in source_entry_ids
                if name and name in (by_id[id_].get('content') or '')
            ]
            if not name or not status or not basis or not evidence:
                continue
            if not has_explicit_presence_evidence(status, evidence):
                continue
            next_items.append({
                'name': name, 'status': status, 'basis': basis,
                'sourceEntryIds': source_entry_ids, 'updatedAt': iso(now),
            })
        return normalize_scene_presence_state(next_items)

    def history_lexical_score(query: str, content: str) -> float:
        """上游 8247-8258 historyLexicalScore。"""
        query_keys = lexical_recall_keys(query)
        if not query_keys:
            return 0.0
        content_keys = set(lexical_recall_keys(content))
        overlap = sum(1 for key in query_keys if key in content_keys)
        normalized_query = _RE_PUNCT_SYMBOL_SPACE.sub('', (query or '').lower())
        normalized_content = _RE_PUNCT_SYMBOL_SPACE.sub('', (content or '').lower())
        phrase = 0.5 if len(normalized_query) >= 3 and normalized_query in normalized_content else 0.0
        return min(1.0, overlap / len(query_keys) + phrase)


# ===================== 区间内引用的私有小 helper =====================

def _now_ms() -> int:
    """复刻 Date.now()：epoch 毫秒（整数）。"""
    return int(now_utc().timestamp() * 1000)


def _timestamp_ms(value: Any) -> int:
    """把 datetime / ISO 字符串 / epoch 毫秒统一成毫秒时间戳（无效值按 0）。"""
    parsed = parse_time(value)
    if parsed is None:
        return 0
    return int(parsed.timestamp() * 1000)


def _to_number(value: Any) -> Optional[float]:
    """复刻 JS Number(value)；不可转换返回 None（等价 NaN）。"""
    if value is None:
        return None
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
            return None
    return None


def _nullish(value: Any, fallback: Any) -> Any:
    """复刻 TS 的 `value ?? fallback`（None 视作 null/undefined）。"""
    return fallback if value is None else value


def _js_string(value: Any) -> str:
    """复刻 JS String(value)（仅覆盖模板串/日志里会出现的类型）。"""
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def _to_integer_or_zero(value: Any) -> int:
    """复刻 JS ToIntegerOrInfinity（用于 Array.slice 的上界）。"""
    number = _to_number(value)
    if number is None or math.isnan(number):
        return 0
    if math.isinf(number):
        return (2 ** 63 - 1) if number > 0 else -(2 ** 63)
    return max(0, int(math.floor(number)))


def _content_length(entry: Any) -> int:
    """上游 `entry.content.length`（与 script/continuity_checkpoint 的口径一致）。"""
    content = entry.get('content') if isinstance(entry, dict) else None
    return len(content) if isinstance(content, str) else 0


def _is_number(value: Any) -> bool:
    """上游 `typeof value === 'number'`（排除 bool）。"""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _date_after(value: Any, now: datetime) -> bool:
    """复刻 `new Date(value) > now`：无效日期一律 False。"""
    parsed = parse_time(value)
    return parsed is not None and parsed > now


def _date_at_or_before(value: Any, cutoff: datetime) -> bool:
    """复刻 `new Date(value) <= cutoff`：无效日期一律 False。"""
    parsed = parse_time(value)
    return parsed is not None and parsed <= cutoff


def _js_round(value: float) -> int:
    """JS Math.round：正值 .5 向上取整。"""
    return int(math.floor(value + 0.5))


def normalize_fact(value: Any) -> str:
    """上游 8212 normalizeFact。"""
    text = value if isinstance(value, str) else ''
    return _RE_WHITESPACE.sub(' ', text.strip().lower())


def merge_note(existing: Any, next_value: Any) -> Optional[str]:
    """上游 8460-8466 mergeNote。"""
    value = clip(next_value, 2_000)
    if not value:
        return existing
    if not existing:
        return value
    if normalize_fact(value) in normalize_fact(existing):
        return existing
    return ('%s\n%s' % (existing, value))[-6_000:]


def patch_claims_match(left: Any, right: Any) -> bool:
    """上游 8468-8476 patchClaimsMatch。"""
    a = _RE_CLAIM_PUNCT.sub('', normalize_fact(left))
    b = _RE_CLAIM_PUNCT.sub('', normalize_fact(right))
    if not a or not b:
        return False
    if a == b:
        return True
    # Allow small wording variations, while avoiding very short claims that
    # could incorrectly merge contradictory changes.
    return min(len(a), len(b)) >= 8 and (b in a or a in b)


def state_patch_evidence(entries: List[ScriptEntry], timezone: str) -> Dict[str, int]:
    """上游 8478-8486 statePatchEvidence。"""
    narrative = [entry for entry in entries if entry.get('kind') == 'script' or entry.get('actor') == 'narrator']
    # Use the narrative timestamp as the turn key. Duplicate rows created at
    # the same instant must not count as independent evidence.
    turns = len({_timestamp_ms(entry.get('occurredAt')) for entry in narrative})
    days = len({calendar_day_key(entry.get('occurredAt'), timezone) for entry in narrative})
    return {'turns': turns, 'days': days, 'scenes': development_scenes(narrative)}


def start_of_utc_window(value: Any, window_days: Any) -> datetime:
    """上游 8489-8493 startOfUtcWindow。"""
    number = _to_number(window_days)
    size = max(1, math.floor(number)) if number is not None and math.isfinite(number) else 1
    epoch_day = math.floor(_timestamp_ms(value) / _TIME_DAY)
    start_ms = math.floor(epoch_day / size) * size * _TIME_DAY
    return datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)


def group_overlay_patches(patches: List[StatePatchProposal], window_days: Any = 5) -> List[Dict[str, Any]]:
    """上游 8493-8503 groupOverlayPatches。"""
    raw_days = _to_number(window_days)
    if raw_days is None or math.isnan(raw_days):
        raw_days = 5.0
    groups: Dict[str, Dict[str, Any]] = {}
    for patch in patches:
        applied_at = patch.get('appliedAt')
        start = start_of_utc_window(applied_at if applied_at is not None else patch.get('createdAt'), window_days)
        key = '%s|%s|%s' % (_js_string(patch.get('participantId')), _js_string(patch.get('target')), iso(start))
        if key not in groups:
            groups[key] = {
                'participantId': patch.get('participantId'), 'target': patch.get('target'),
                'from': start, 'to': start + timedelta(milliseconds=raw_days * _TIME_DAY), 'patches': [],
            }
        groups[key]['patches'].append(patch)
    return list(groups.values())


def group_overlay_snapshots(snapshots: List[OverlaySnapshot], window_days: Any = 10) -> List[Dict[str, Any]]:
    """上游 8505-8515 groupOverlaySnapshots。"""
    raw_days = _to_number(window_days)
    if raw_days is None or math.isnan(raw_days):
        raw_days = 10.0
    groups: Dict[str, Dict[str, Any]] = {}
    for snapshot in snapshots:
        start = start_of_utc_window(snapshot.get('periodEnd'), window_days)
        key = '%s|%s|%s' % (_js_string(snapshot.get('participantId')), _js_string(snapshot.get('target')), iso(start))
        if key not in groups:
            groups[key] = {
                'participantId': snapshot.get('participantId'), 'target': snapshot.get('target'),
                'from': start, 'to': start + timedelta(milliseconds=raw_days * _TIME_DAY), 'snapshots': [],
            }
        groups[key]['snapshots'].append(snapshot)
    return list(groups.values())


def normalize_major_events(value: Any, patches: List[StatePatchProposal],
                           snapshots: Optional[List[OverlaySnapshot]] = None) -> List[str]:
    """上游 8517-8524 normalizeMajorEvents。"""
    snapshot_list = snapshots or []
    model_events = [clip(item, 600) for item in value if isinstance(item, str)] if isinstance(value, list) else []
    retained: List[str] = []
    for snapshot in snapshot_list:
        retained.extend(snapshot.get('majorEvents') or [])
    for patch in patches:
        if patch.get('impact') == 'major':
            retained.append(clip(patch.get('proposedValue') or patch.get('evidence'), 600))
    return list(dict.fromkeys([item for item in [*retained, *model_events] if item]))[-20:]


def has_compaction_evidence(source_entry_ids: Any, entries: List[ScriptEntry]) -> bool:
    """上游 8039-8043 hasCompactionEvidence。"""
    if not isinstance(source_entry_ids, (list, tuple)) or not source_entry_ids:
        return False
    ids = {entry.get('id') for entry in entries}
    return any(id_ in ids for id_ in source_entry_ids)


def resolve_participant_id(explicit: Any, source_entry_ids: Any, entries: List[ScriptEntry]) -> str:
    """上游 8125-8129 resolveParticipantId。"""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    ids: List[str] = []
    for entry_id in source_entry_ids or []:
        match = next((entry for entry in entries if entry.get('id') == entry_id), None)
        participant_id = match.get('participantId') if match else None
        if participant_id:
            ids.append(participant_id)
    return ids[0] if ids else ''


def limit_entries_by_characters(entries: List[ScriptEntry], limit: int) -> List[ScriptEntry]:
    """上游 8214-8227 limitEntriesByCharacters。"""
    if limit <= 0:
        return []
    used = 0
    selected: List[ScriptEntry] = []
    # 从最新条目向前保留，保证压缩请求优先看到场景接续点。
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if selected and used + _content_length(entry) > limit:
            break
        selected.insert(0, entry)
        used += _content_length(entry)
    return selected


def has_explicit_presence_evidence(status: str, entries: List[ScriptEntry]) -> bool:
    """上游 7828-7833 hasExplicitPresenceEvidence。"""
    text = '\n'.join(entry.get('content') for entry in entries)
    if status == 'off-scene':
        return bool(_RE_PRESENCE_OFF_SCENE.search(text))
    if status == 'expected':
        return bool(_RE_PRESENCE_EXPECTED.search(text))
    return bool(_RE_PRESENCE_PRESENT.search(text))


def coerce_timeline_kind(value: Any) -> str:
    """上游 7463-7467 coerceTimelineKind。"""
    if not isinstance(value, str):
        return ''
    normalized = value.strip().lower()
    return _TIMELINE_KIND_ALIASES.get(normalized, 'activity')


def coerce_timeline_position(value: Any) -> float:
    """上游 7469-7481 coerceTimelinePosition。"""
    # Accept numeric strings ("0.5") and percentage strings ("50%") before
    # falling back to NaN so one sloppy field doesn't discard the whole beat.
    if isinstance(value, bool):
        return _NAN
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        text = value.strip()
        if text.endswith('%'):
            text = text[:-1]
        if text == '':
            parsed = 0.0
        else:
            try:
                parsed = float(text)
            except ValueError:
                return _NAN
        if not math.isfinite(parsed):
            return _NAN
        return max(0.0, min(1.0, parsed if parsed <= 1 else parsed / 100))
    return _NAN


def normalize_timeline_plan(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7483-7501 normalizeTimelinePlan。"""
    if not is_record(value) or not isinstance(value.get('beats'), list):
        return None
    beats: List[Dict[str, Any]] = []
    for item in value.get('beats') or []:
        if not is_record(item):
            continue
        at = coerce_timeline_position(item.get('at'))
        kind = coerce_timeline_kind(item.get('kind'))
        summary = clip(item.get('summary'), 240)
        if not (math.isfinite(at) and kind and summary):
            continue
        beats.append({'at': at, 'kind': kind, 'summary': summary})
    beats = sorted(beats, key=lambda beat: beat['at'])[:4]
    if not beats:
        return None
    raw_carry = value.get('carry')
    carry = [clip(item, 180) for item in raw_carry if isinstance(item, str)][:4] if isinstance(raw_carry, list) else []
    carry = [item for item in carry if item]
    plan: Dict[str, Any] = {'beats': beats}
    if carry:
        plan['carry'] = carry
    return plan


def lexical_recall_keys(text: str) -> List[str]:
    """上游 8263-8272 lexicalRecallKeys。"""
    normalized = (text or '').lower()
    words = _RE_WORD_KEY.findall(normalized)
    chinese_runs = _RE_CJK_RUN.findall(normalized)
    bigrams: List[str] = []
    for run in chinese_runs:
        for index in range(max(0, len(run) - 1)):
            bigrams.append(run[index:index + 2])
    return list(dict.fromkeys([*words, *bigrams]))[:80]


# ===================== PreparedCompaction* / CompactionBackoff 结构 =====================

class PreparedCompactionSkip(TypedDict, total=False):
    """上游 225-228 PreparedCompactionSkip。"""

    phase: str  # 'skip'
    overlayCompacted: bool


class PreparedCompactionRun(TypedDict, total=False):
    """上游 230-243 PreparedCompactionRun。"""

    phase: str  # 'run'
    overlayCompacted: bool
    scene: InterludeScene
    sceneEntries: List[ScriptEntry]
    chars: int
    sceneCompactionDue: bool
    current: InterludeStory
    participants: List[InterludeParticipant]
    visibleCompactionEntries: List[ScriptEntry]
    visibleCompactionFacts: List[NarrativeFact]
    compactRequest: CompactionRequest
    fingerprint: str


class CompactionBackoff(TypedDict):
    """上游 245-248 CompactionBackoff。"""

    fingerprint: str
    until: int


PreparedCompaction = Union[PreparedCompactionSkip, PreparedCompactionRun]


class ServiceMemoryMixin:
    """上游 InterludeService 的记忆层（service.ts 5866-6720）。"""

    # ================= 上游 5866-5896：压缩指纹 / 冷却 / 检查点 =================

    def compaction_fingerprint(self, scene: InterludeScene, entries: List[ScriptEntry], chars: int) -> str:
        """上游 5866-5870 compactionFingerprint。"""
        first = entries[0].get('id') if entries else None
        last = entries[-1].get('id') if entries else None
        scene_last = scene.get('lastEntryId')
        return '%s:%s:%s-%s:%d:%s' % (
            _js_string(scene.get('id')),
            _js_string(0 if scene_last is None else scene_last),
            _js_string(0 if first is None else first),
            _js_string(0 if last is None else last),
            len(entries),
            _js_string(chars),
        )

    def compaction_is_backed_off(self, story_id: str, fingerprint: str, now: Optional[int] = None) -> bool:
        """上游 5872-5879 compactionIsBackedOff。"""
        if now is None:
            now = _now_ms()
        backoff = self.compaction_backoff.get(story_id)
        until = (backoff or {}).get('until') or 0
        if not backoff or backoff.get('fingerprint') != fingerprint or now >= until:
            if backoff and now >= until:
                self.compaction_backoff.pop(story_id, None)
            return False
        return True

    def note_compaction_failure(self, story_id: str, fingerprint: str, error: Any) -> None:
        """上游 5881-5886 noteCompactionFailure。"""
        until = _now_ms() + COMPACTION_RETRY_BACKOFF
        self.compaction_backoff[story_id] = {'fingerprint': fingerprint, 'until': until}
        self.report_standalone_operation(
            'diagnostic', 'debug', '记忆整理进入冷却 故事=%s 冷却至=%s 错误=%s',
            story_id, iso(parse_time(until)), error,
        )

    def compaction_checkpoint_advanced(self, context: PreparedCompactionRun) -> bool:
        """上游 5891-5896 compactionCheckpointAdvanced。

        Confirm the database checkpoint moved after a successful compactor call.
        A provider response alone is not enough: if the write was lost or
        interrupted, retrying the same range on every turn would recreate the
        token-burning loop this guard is meant to stop.
        """
        entries = context.get('sceneEntries') or []
        expected_last_entry_id = entries[-1].get('id') if entries else None
        if not expected_last_entry_id:
            return True
        rows = self.db_get('interlude_scene', {'id': (context.get('scene') or {}).get('id')})
        persisted = rows[0] if rows else None
        return bool(persisted) and (
            (persisted.get('lastEntryId') or 0) >= expected_last_entry_id
            or persisted.get('status') == 'closed'
        )

    # ================= 上游 5898-6001：排队 / 后台扫描 =================

    def schedule_compaction(self, story_id: str) -> None:
        """上游 5898-5984 scheduleCompaction。"""
        if (not self.memory_config.get('enabled') and not self.schedule_preplan_config.get('enabled')) \
                or story_id in self.scheduled_compactions:
            return
        self.scheduled_compactions.add(story_id)
        self.report_standalone_operation('diagnostic', 'debug', '记忆整理已排队 故事=%s', story_id)

        def run() -> None:
            if self.desktop_runtime_phase == 'paused' or self.database_resetting:
                self.scheduled_compactions.discard(story_id)
                return
            # Let an active or debounced user turn go first. This keeps compaction
            # fully off the latency-sensitive path even during a busy conversation.
            if self.has_pending_narrative(story_id):
                self.report_standalone_operation('diagnostic', 'debug', '记忆整理等待前台回合结束 故事=%s', story_id)
                self.ctx.set_timeout(run, 500)
                return

            def work() -> None:
                try:
                    # Phase 1 (serial): cheap reads and due checks. The queue MUST be
                    # released before the compactor call — a promise-chain serial cannot
                    # be re-entered from inside one of its own running tasks (that
                    # deadlocks the whole story queue), so the expensive model call runs
                    # unqueued and only the cheap DB writes re-acquire it afterwards.
                    def prepare() -> Optional[Dict[str, Any]]:
                        if self.has_pending_narrative(story_id):
                            return None
                        story = self.get_story(story_id)
                        review = self.prepare_schedule_preplan_review(story, now_utc())
                        context = self.prepare_compaction(story, now_utc(), False)
                        return {'story': story, 'review': review, 'context': context}

                    prepared = self.serial(story_id, prepare)
                    if not prepared:
                        return
                    review = prepared.get('review')
                    needs_model = bool(review and review.get('needsModel'))
                    prepared_context = prepared.get('context')
                    if not needs_model and (not prepared_context or prepared_context.get('phase') == 'skip'):
                        return
                    # Phase 2: the expensive model call, deliberately outside the queue.
                    started_at = _now_ms()
                    context = prepared_context if (prepared_context or {}).get('phase') == 'run' else None
                    self.report_operation(
                        'standard', 'info', prepared.get('story'), 'advance',
                        '后台整理开始 条目=%d 字符=%d 场景压缩=%s SchedulePreplan=%s',
                        len((context or {}).get('sceneEntries') or []),
                        (context or {}).get('chars') or 0,
                        bool((context or {}).get('sceneCompactionDue')),
                        needs_model,
                    )
                    schedule_proposal = None
                    if review and review.get('needsModel') and review.get('request'):
                        try:
                            schedule_proposal = self.request_schedule_preplan(prepared.get('story'), review['request'])
                        except Exception as error:  # noqa: BLE001 - 上游 catch 后仅告警
                            # Keep the review checkpoint moving even when a provider throws
                            # before returning a proposal. The persistence phase will retain
                            # the existing plan (or create an empty first review), preventing
                            # the same daily request from firing on every maintenance sweep.
                            self.report(
                                'warn', prepared.get('story'), 'advance',
                                'Schedule Preplan 调用失败，将保存本日审查状态：%s', error,
                            )
                    decision: Dict[str, Any] = {}
                    compaction_error: Optional[BaseException] = None
                    if context:
                        try:
                            decision = self.compactor.compact(context['compactRequest'])
                        except Exception as error:  # noqa: BLE001 - 上游 catch 后进入冷却
                            compaction_error = error
                            self.note_compaction_failure(story_id, context['fingerprint'], error)
                            self.report('warn', context.get('current'), 'advance', '记忆压缩失败：%s', error)

                    # Phase 3 (serial): cheap DB writes, re-queued after the model call.
                    def persist() -> None:
                        if self.database_resetting:
                            return
                        if review and review.get('needsModel'):
                            persisted = self.persist_schedule_preplan_review(
                                prepared.get('story'), review, schedule_proposal, now_utc(),
                            )
                            if persisted:
                                self.schedule_preplan_backoff.pop(story_id, None)
                            else:
                                self.schedule_preplan_backoff[story_id] = _now_ms() + SCHEDULE_PREPLAN_RETRY_BACKOFF
                        # Schedule Preplan is independent background work. A scene
                        # compaction failure must not discard its already completed
                        # review; simply leave the scene for its own retry cooldown.
                        if context and not compaction_error:
                            self.apply_compaction(context.get('current'), context, decision, now_utc(), started_at)
                            if not self.compaction_checkpoint_advanced(context):
                                checkpoint_entries = context.get('sceneEntries') or []
                                expected = checkpoint_entries[-1].get('id') if checkpoint_entries else None
                                raise RuntimeError(
                                    'Compaction checkpoint did not advance (scene=%s, expected=%s)' % (
                                        _js_string((context.get('scene') or {}).get('id')),
                                        _js_string(0 if expected is None else expected),
                                    )
                                )

                    try:
                        self.serial(story_id, persist)
                        if context and not compaction_error:
                            self.compaction_backoff.pop(story_id, None)
                    except Exception as error:  # noqa: BLE001 - 上游 rethrow 后统一记录
                        if context and not compaction_error:
                            self.note_compaction_failure(story_id, context['fingerprint'], error)
                        raise
                except Exception as error:  # noqa: BLE001 - 上游 .catch(...)
                    self.report_standalone_operation('diagnostic', 'debug', '记忆压缩跳过 错误=%s', error)
                finally:
                    # 上游 .finally(() => this.scheduledCompactions.delete(storyId))
                    self.scheduled_compactions.discard(story_id)

            self.ctx.set_timeout(work, 0)

        run()

    def compact_stories(self) -> None:
        """上游 5986-6001 compactStories。"""
        if self.desktop_runtime_phase == 'paused' \
                or (not self.memory_config.get('enabled') and not self.schedule_preplan_config.get('enabled')) \
                or self.compaction_sweep_running:
            return
        self.compaction_sweep_running = True
        try:
            story = self.get_canonical_story()
            if not story or not self.can_handle_story(story):
                return
            if self.memory_config.get('enabled'):
                self.schedule_fact_embedding_backfill(story.get('id'))
            if ((self.config.get('model') or {}).get('embedding') or {}).get('semanticHistory'):
                def run_backfill() -> None:
                    try:
                        self.backfill_history_embeddings(story.get('id'))
                    except Exception as error:  # noqa: BLE001 - 上游 .catch(...)
                        self.report_standalone_operation('diagnostic', 'debug', '历史向量补齐跳过 错误=%s', error)
                self.ctx.set_timeout(run_backfill, 0)
            self.schedule_compaction(story.get('id'))
        finally:
            self.compaction_sweep_running = False

    # ================= 上游 6003-6129：Schedule Preplan 后台审查 =================

    def get_schedule_preplan(self, story_id: str) -> Optional[SchedulePreplanRecord]:
        """上游 6003-6006 getSchedulePreplan。"""
        rows = self.db_get('interlude_schedule_preplan', {'storyId': story_id})
        row = rows[0] if rows else None
        return normalize_schedule_preplan_record(row)

    def schedule_preplan_evidence(self, story_id: str, after_entry_id: int) -> List[ScriptEntry]:
        """上游 6008-6032 schedulePreplanEvidence。"""
        entry_filter: Dict[str, Any] = {'storyId': story_id, 'kind': 'script'}
        if after_entry_id > 0:
            entry_filter['id'] = {'$gt': after_entry_id}
        entries = self.db_get(
            'interlude_script_entry', entry_filter,
            {'sort': {'occurredAt': 'asc'}, 'limit': 60},
        )
        if self.shared_story_config.get('shareParticipantDetails'):
            return entries
        # A recurring schedule belongs to the protagonist, but raw private prose
        # must not be sent to the background schedule model. Automatic scripts
        # already carry a host-validated timelinePlan: project that safe ledger
        # instead of dropping every participant-owned life event.
        projected_entries: List[ScriptEntry] = []
        for entry in entries:
            if not entry.get('participantId'):
                projected_entries.append(entry)
                continue
            metadata = entry.get('metadata') or {}
            if metadata.get('narrativeAuthority') == 'original-v2':
                handoff = entry_life_handoff(entry)
                if not handoff or (not handoff.get('activity') and not handoff.get('place')):
                    continue
                # Only the protagonist's small concrete local fields cross into the
                # schedule reader, never private prose, dialogue, presence or quotes.
                content: Dict[str, Any] = {
                    'sourceEntryId': entry.get('id'),
                    'observedAt': iso(entry.get('occurredAt')),
                }
                place = handoff.get('place')
                activity = handoff.get('activity')
                if place is not None and place.get('value') is not None:
                    content['place'] = place.get('value')
                if activity is not None and activity.get('value') is not None:
                    content['activity'] = activity.get('value')
                projected_entries.append({
                    **entry, 'participantId': '',
                    'content': json.dumps(content, ensure_ascii=False, separators=(',', ':')),
                    'metadata': {'narrativeAuthority': 'original-v2'},
                })
                continue
            projected = timeline_entry_prompt_projection(entry)
            if projected is entry:
                continue
            projected_entries.append({**projected, 'participantId': ''})
        return projected_entries

    def save_schedule_preplan(self, record: SchedulePreplanRecord) -> None:
        """上游 6034-6045 saveSchedulePreplan。"""
        rows = self.db_get('interlude_schedule_preplan', {'storyId': record.get('storyId')})
        existing = rows[0] if rows else None
        if existing:
            # `storyId` is the table primary key. Minato rejects updates that include
            # a primary-key field, even when the value is unchanged. Keep the key in
            # the lookup only and send a key-free patch to make the review checkpoint
            # actually advance after a successful/empty preplan review.
            update = {key: value for key, value in record.items() if key != 'storyId'}
            self.db_set('interlude_schedule_preplan', {'storyId': record.get('storyId')}, update)
        else:
            self.db_create('interlude_schedule_preplan', record)

    def prepare_schedule_preplan_review(self, story: InterludeStory, now: datetime) -> Optional[Dict[str, Any]]:
        """上游 6047-6081 prepareSchedulePreplanReview。"""
        config = self.schedule_preplan_config
        if not config.get('enabled'):
            return None
        backoff_until = self.schedule_preplan_backoff.get(story.get('id'))
        if backoff_until and _now_ms() < backoff_until:
            return None
        timezone = (story.get('setting') or {}).get('timezone')
        current = self.get_schedule_preplan(story.get('id'))
        if not schedule_preplan_review_due(current, now, timezone, config):
            return None
        local_date = calendar_day_key(now, timezone)
        evidence_entries = self.schedule_preplan_evidence(
            story.get('id'), (current.get('lastEvidenceEntryId') if current else None) or 0,
        )
        if not current and not evidence_entries:
            empty = apply_schedule_preplan_proposal(
                None,
                {'outcome': 'replace', 'reason': 'No concrete recurring schedule evidence yet.',
                 'regimes': [], 'exceptions': []},
                [], local_date, timezone, config, now, config.get('variationLevel'),
            )
            if empty:
                empty['storyId'] = story.get('id')
                self.save_schedule_preplan(empty)
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    'Schedule Preplan 已建立空记录：等待可验证的生活日程证据',
                )
                return {'current': empty, 'evidenceEntries': evidence_entries, 'localDate': local_date,
                        'needsModel': False, 'request': None}
        needs_model = schedule_preplan_needs_model(current, evidence_entries, local_date, timezone, config)
        if not needs_model and current:
            self.save_schedule_preplan(refresh_schedule_preplan(current, local_date, timezone, config, now))
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                'Schedule Preplan 今日检查完成：没有新证据，日程保持不变',
            )
        request: Optional[SchedulePreplanReviewRequest] = {
            'localDate': local_date,
            'horizonDays': config.get('horizonDays'),
            'variationLevel': config.get('variationLevel'),
            'current': current if current else None,
            'evidenceEntries': evidence_entries,
        } if needs_model else None
        return {'current': current, 'evidenceEntries': evidence_entries, 'localDate': local_date,
                'needsModel': needs_model, 'request': request}

    def request_schedule_preplan(self, story: InterludeStory,
                                 request: SchedulePreplanReviewRequest) -> Optional[Dict[str, Any]]:
        """上游 6083-6090 requestSchedulePreplan。"""
        if not getattr(self.compactor, 'plan_schedule_preplan', None):
            return None
        proposal = self.compactor.plan_schedule_preplan(request)
        if proposal:
            return proposal
        self.report_operation(
            'diagnostic', 'warn', story, 'advance',
            'Schedule Preplan 返回为空，正在进行一次轻量恢复重试',
        )
        proposal = self.compactor.plan_schedule_preplan(request)
        return proposal

    def persist_schedule_preplan_review(self, story: InterludeStory, review: Dict[str, Any],
                                        proposal: Any, now: datetime) -> bool:
        """上游 6092-6129 persistSchedulePreplanReview。"""
        timezone = (story.get('setting') or {}).get('timezone')
        config = self.schedule_preplan_config
        next_record = apply_schedule_preplan_proposal(
            review.get('current'), proposal, review.get('evidenceEntries') or [], review.get('localDate'),
            timezone, config, now, config.get('variationLevel'),
        )
        if not next_record:
            # A narrow review can still fail on an unstable provider. On first use,
            # persist an explicit empty review with the inspected evidence cursor:
            # this is truthful, prevents a full retry loop, and new evidence will
            # naturally make the next review due again.
            if not review.get('current'):
                empty = apply_schedule_preplan_proposal(
                    None,
                    {'outcome': 'replace',
                     'reason': 'Schedule review returned no valid structure; waiting for new concrete evidence.',
                     'regimes': [], 'exceptions': []},
                    review.get('evidenceEntries') or [], review.get('localDate'),
                    timezone, config, now, config.get('variationLevel'),
                )
                if empty:
                    empty['storyId'] = story.get('id')
                    self.save_schedule_preplan(empty)
                    self.report_operation(
                        'standard', 'warn', story, 'advance',
                        'Schedule Preplan 未形成有效日程，已保存空审查记录并等待新证据',
                    )
                    return True
            self.report_operation(
                'standard', 'warn', story, 'advance',
                'Schedule Preplan 未更新：模型没有返回可用日程，保留现有版本',
            )
            return False
        next_record['storyId'] = story.get('id')
        self.save_schedule_preplan(next_record)
        self.report_operation(
            'standard', 'info', story, 'advance',
            'Schedule Preplan 已审查 版本=%d 覆盖=%s→%s 原因=%s',
            next_record.get('revision'), next_record.get('validFrom'),
            next_record.get('validThrough'), next_record.get('reviewReason'),
        )
        return True

    # ================= 上游 6131-6161：流式脚本恢复 =================

    def schedule_stream_script_recovery(self, story_id: str, participant_id: str,
                                        now: datetime, previous_attempts: int = 0) -> bool:
        """上游 6131-6146 scheduleStreamScriptRecovery。

        A visible reply already reached the user, so this retry may write only
        the missing life script. It must never create a second transport message.
        """
        runtime = self.config.get('runtime') or {}
        delay_seconds = max(5, _nullish(runtime.get('narrativeRetryDelaySeconds'), 60))
        max_attempts = min(2, max(0, _nullish(runtime.get('narrativeRetryMaxAttempts'), 6)))
        if not participant_id or previous_attempts >= max_attempts:
            return False
        pending = self.db_get(
            'interlude_intent',
            {'storyId': story_id, 'participantId': participant_id, 'status': 'pending', 'type': 'narrative-retry'},
        )
        existing = [intent for intent in pending if (intent.get('payload') or {}).get('streamRecovery') is True]
        if existing:
            self.db_set(
                'interlude_intent', {'id': {'$in': [intent.get('id') for intent in existing]}},
                {'status': 'cancelled', 'updatedAt': now},
            )
        attempt = previous_attempts + 1
        not_before = now + timedelta(seconds=delay_seconds)
        self.append_intent(story_id, {
            'type': 'narrative-retry',
            'summary': 'Recover only the missing script after a streamed reply (attempt %s/%s).' % (
                _js_string(attempt), _js_string(max_attempts),
            ),
            'notBefore': iso(not_before),
            'payload': {'narrativeRetry': True, 'streamRecovery': True, 'userInitiated': True, 'attempt': attempt},
        }, now, participant_id)
        self.schedule_due_intent_wake(story_id, not_before)
        return True

    def persist_stream_script_recovery(self, story: InterludeStory, participant: Optional[InterludeParticipant],
                                       decision: CompactionDecision, now: datetime) -> bool:
        """上游 6148-6161 persistStreamScriptRecovery。"""
        raw_script = decision.get('script')
        script = raw_script.strip() if isinstance(raw_script, str) else ''
        if not script:
            return False
        self.append_entry(story.get('id'), {
            'kind': 'script', 'actor': 'narrator', 'content': script, 'occurredAt': iso(now),
            'metadata': {'phase': 'stream-script-recovery', 'interaction': None},
        }, now, (participant.get('id') if participant else None) or '')
        state = decode_story_state(story.get('state'))
        next_state = {**state, 'narrativeUpdateCount': state.get('narrativeUpdateCount', 0) + 1}
        # Stream recovery only restores the missing original, never a second
        # communication decision or an independent continuity summary.
        self.db_set('interlude_story', {'id': story.get('id')},
                    {'state': encode_story_state(next_state), 'updatedAt': now})
        return True

    # ================= 上游 6163-6271：压缩主流程 =================

    def compact_unlocked(self, story: InterludeStory, now: datetime, force: bool) -> bool:
        """上游 6163-6187 compactUnlocked。"""
        review = self.prepare_schedule_preplan_review(story, now)
        context = self.prepare_compaction(story, now, force)
        if review and review.get('needsModel') and review.get('request'):
            proposal = self.request_schedule_preplan(story, review['request'])
            persisted = self.persist_schedule_preplan_review(story, review, proposal, now_utc())
            if persisted:
                self.schedule_preplan_backoff.pop(story.get('id'), None)
            else:
                self.schedule_preplan_backoff[story.get('id')] = _now_ms() + SCHEDULE_PREPLAN_RETRY_BACKOFF
        if not context or context.get('phase') == 'skip':
            return bool(context.get('overlayCompacted')) if context else False
        started_at = _now_ms()
        self.report_operation(
            'standard', 'info', story, 'advance',
            '后台整理开始 条目=%d 字符=%d 场景压缩=%s SchedulePreplan=%s',
            len(context.get('sceneEntries') or []), context.get('chars') or 0,
            bool(context.get('sceneCompactionDue')), bool(review and review.get('needsModel')),
        )
        decision: Dict[str, Any] = {}
        try:
            decision = self.compactor.compact(context['compactRequest'])
        except Exception as error:  # noqa: BLE001 - 上游 catch 后进入冷却并返回 false
            self.note_compaction_failure(story.get('id'), context['fingerprint'], error)
            self.report('warn', story, 'advance', '记忆压缩失败：%s', error)
            return False
        try:
            result = self.apply_compaction(story, context, decision, now, started_at)
            if not self.compaction_checkpoint_advanced(context):
                checkpoint_entries = context.get('sceneEntries') or []
                expected = checkpoint_entries[-1].get('id') if checkpoint_entries else None
                raise RuntimeError(
                    'Compaction checkpoint did not advance (scene=%s, expected=%s)' % (
                        _js_string((context.get('scene') or {}).get('id')),
                        _js_string(0 if expected is None else expected),
                    )
                )
            self.compaction_backoff.pop(story.get('id'), None)
            return result
        except Exception as error:  # noqa: BLE001 - 上游 rethrow 前记录冷却
            self.note_compaction_failure(story.get('id'), context['fingerprint'], error)
            raise

    def prepare_compaction(self, story: InterludeStory, now: datetime,
                           force: bool) -> Optional[PreparedCompaction]:
        """上游 6199-6261 prepareCompaction。

        Everything up to the expensive compactor call: cheap reads plus the due
        checks. Runs inside the story serial queue, but the model call itself must
        not — a queued compactor request would delay the next live turn.
        """
        self.ensure_continuity(story, now)
        overlay_compacted = self.compact_overlay_unlocked(story, now) if self.memory_config.get('enabled') else False
        scene = self.active_scene(story.get('id'))
        if not scene:
            return {'phase': 'skip', 'overlayCompacted': overlay_compacted}
        # lastEntryId 将场景摘要变成增量检查点：已经压缩过的原文不再重复传给模型。
        entry_filter: Dict[str, Any] = {'storyId': story.get('id'), 'occurredAt': {'$gte': scene.get('startedAt')}}
        if scene.get('lastEntryId') is not None:
            entry_filter['id'] = {'$gt': scene.get('lastEntryId')}
            del entry_filter['occurredAt']
        entries = self.db_get('interlude_script_entry', entry_filter, {
            'limit': int(max(self.memory_config.get('compactionEntryLimit', 0) * 2,
                             self.memory_config.get('compactionEntryLimit', 0))),
            'sort': {'id': 'asc'},
        })
        scene_entries = compaction_prefix(entries, self.memory_config.get('compactionCharacterLimit', 0))
        chars = sum(_content_length(entry) for entry in scene_entries)
        entries_chars = sum(_content_length(entry) for entry in entries)
        scene_compaction_due = bool(self.memory_config.get('enabled')) and len(scene_entries) > 0 and (
            force
            or len(entries) >= self.memory_config.get('sceneEntryThreshold', 0)
            or entries_chars >= self.memory_config.get('sceneCharacterThreshold', 0)
        )
        if not scene_compaction_due:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '记忆整理跳过：未达到阈值 条目=%d/%d 字符=%d/%d',
                len(scene_entries), self.memory_config.get('sceneEntryThreshold', 0),
                chars, self.memory_config.get('sceneCharacterThreshold', 0),
            )
            return {'phase': 'skip', 'overlayCompacted': overlay_compacted}
        fingerprint = self.compaction_fingerprint(scene, scene_entries, chars)
        if not force and self.compaction_is_backed_off(story.get('id'), fingerprint):
            return {'phase': 'skip', 'overlayCompacted': overlay_compacted}
        current = self.get_story(story.get('id'))
        participants = self.participants(story.get('id'))
        if scene.get('lastEntryId') is None:
            preceding: List[ScriptEntry] = []
        else:
            preceding = self.db_get('interlude_script_entry', {
                'storyId': story.get('id'), 'id': {'$lte': scene.get('lastEntryId')},
            }, {'limit': 12, 'sort': {'id': 'desc'}})
        preceding.reverse()
        if not self.shared_story_config.get('shareParticipantDetails'):
            preceding = [entry for entry in preceding if not entry.get('participantId')]
        preceding_entries = limit_entries_by_characters(preceding, 8_000)
        if self.shared_story_config.get('shareParticipantDetails'):
            visible_compaction_entries = list(scene_entries)
        else:
            visible_compaction_entries = [
                ({**entry, 'participantId': '', 'content': '[participant-specific conversation omitted by privacy setting]',
                  'metadata': {}} if entry.get('participantId') else entry)
                for entry in scene_entries
            ]
        visible_compaction_entries = [
            entry for entry in visible_compaction_entries if (entry.get('content') or '').strip()
        ]
        if self.memory_config.get('enabled') and self.shared_story_config.get('shareParticipantDetails'):
            visible_compaction_facts = self.facts(story.get('id'), self.memory_config.get('maxFactsPerStory'))
        elif self.memory_config.get('enabled'):
            visible_compaction_facts = [
                fact for fact in self.facts(story.get('id'), self.memory_config.get('maxFactsPerStory'))
                if not fact.get('participantId')
            ]
        else:
            visible_compaction_facts = []
        raw_development_candidates = [
            item for item in self.db_get(
                'interlude_state_patch',
                {'storyId': story.get('id'), 'status': {'$in': ['proposed', 'applied']}},
                {'limit': 50, 'sort': {'createdAt': 'desc'}},
            )
            if str(item.get('path') or '').startswith('development.')
            and (self.shared_story_config.get('shareParticipantDetails') or not item.get('participantId'))
        ]
        candidate_source_ids = list(dict.fromkeys(
            id_ for item in raw_development_candidates for id_ in (item.get('sourceEntryIds') or [])
        ))[:1_000]
        candidate_sources = self.db_get(
            'interlude_script_entry', {'storyId': story.get('id'), 'id': {'$in': candidate_source_ids}},
        ) if candidate_source_ids else []
        development_candidates = [
            item for item in raw_development_candidates if prompt_ready_development(item, candidate_sources)
        ]
        return {
            'phase': 'run', 'overlayCompacted': overlay_compacted, 'scene': scene, 'sceneEntries': scene_entries,
            'chars': chars, 'sceneCompactionDue': scene_compaction_due, 'current': current,
            'participants': participants, 'visibleCompactionEntries': visible_compaction_entries,
            'visibleCompactionFacts': visible_compaction_facts, 'fingerprint': fingerprint,
            'compactRequest': {
                'story': current, 'from': scene.get('startedAt'), 'now': now, 'entries': visible_compaction_entries,
                'scene': scene, 'arc': self.active_arc(story.get('id')), 'participants': participants,
                'precedingEntries': preceding_entries, 'developmentCandidates': development_candidates,
                'facts': visible_compaction_facts,
            },
        }

    def apply_compaction(self, story: InterludeStory, context: PreparedCompactionRun,
                         decision: CompactionDecision, now: datetime, started_at: int) -> bool:
        """上游 6263-6271 applyCompaction。

        Cheap DB persistence for one compaction decision. Re-acquires the story
        serial queue in the caller so writes stay ordered with narrative turns.
        """
        due = bool(context.get('sceneCompactionDue'))
        if due:
            self.persist_compaction(
                context.get('current'), context.get('scene'), decision, context.get('sceneEntries') or [],
                now, {(fact.get('id')) for fact in (context.get('visibleCompactionFacts') or [])},
            )
        self.report_operation(
            'standard', 'info', story, 'advance',
            '后台整理完成 耗时=%dms 剧本条目=%d 长期事实=%d 状态变更=%d',
            _now_ms() - started_at,
            len(context.get('sceneEntries') or []) if due else 0,
            len(decision.get('facts') or []) if due else 0,
            len(decision.get('statePatches') or []) if due else 0,
        )
        return True

    # ================= 上游 6273-6392：Overlay 分层归档 =================

    def compact_overlay_unlocked(self, story: InterludeStory, now: datetime) -> bool:
        """上游 6273-6327 compactOverlayUnlocked。

        Older state patches are compacted only by the background maintenance
        lane. Live turns always retain the last few days as raw detail.
        """
        config = self.memory_config
        if not config.get('overlayCompressionEnabled'):
            return False
        try:
            recent_cutoff = now - timedelta(days=_nullish(config.get('overlayRecentDays'), 2))
            monthly_cutoff = now - timedelta(days=_nullish(config.get('overlayMonthlyAfterDays'), 10))
            applied = self.db_get(
                'interlude_state_patch', {'storyId': story.get('id'), 'status': 'applied'},
                {'sort': {'appliedAt': 'asc'}},
            )
            weekly = [
                patch for patch in applied
                if not str(patch.get('path') or '').startswith('development.')
                and _date_at_or_before(
                    patch.get('appliedAt') if patch.get('appliedAt') is not None else patch.get('createdAt'),
                    recent_cutoff,
                )
            ]
            changed = False
            for group in group_overlay_patches(weekly, _nullish(config.get('overlayWeeklyWindowDays'), 5)):
                existing = self.db_get('interlude_overlay_snapshot', {
                    'storyId': story.get('id'), 'participantId': group.get('participantId'),
                    'target': group.get('target'), 'tier': 'weekly', 'periodStart': group.get('from'),
                })
                if existing:
                    continue
                participant = self.get_participant(group.get('participantId')) if group.get('participantId') else None
                decision = self.compactor.compact_overlay({
                    'story': story, 'participant': participant, 'target': group.get('target'), 'tier': 'weekly',
                    'from': group.get('from'), 'to': group.get('to'), 'patches': group.get('patches') or [],
                })
                summary = clip((decision or {}).get('summary'),
                               _nullish(config.get('overlayWeeklySummaryCharacters'), 1_600))
                if not summary:
                    continue
                self.db_create('interlude_overlay_snapshot', {
                    'storyId': story.get('id'), 'participantId': group.get('participantId'),
                    'target': group.get('target'), 'tier': 'weekly',
                    'periodStart': group.get('from'), 'periodEnd': group.get('to'),
                    'summary': summary,
                    'majorEvents': normalize_major_events((decision or {}).get('majorEvents'), group.get('patches') or []),
                    'sourcePatchIds': [patch.get('id') for patch in (group.get('patches') or [])],
                    'status': 'active', 'createdAt': now, 'updatedAt': now,
                })
                for patch in group.get('patches') or []:
                    self.db_set('interlude_state_patch', {'id': patch.get('id')}, {'status': 'compacted'})
                changed = True

            snapshots = self.db_get(
                'interlude_overlay_snapshot',
                {'storyId': story.get('id'), 'tier': 'weekly', 'status': 'active'},
                {'sort': {'periodEnd': 'asc'}},
            )
            monthly_candidates = [
                snapshot for snapshot in snapshots
                if _date_at_or_before(snapshot.get('periodEnd'), monthly_cutoff)
            ]
            for group in group_overlay_snapshots(monthly_candidates, _nullish(config.get('overlayMonthlyWindowDays'), 10)):
                existing = self.db_get('interlude_overlay_snapshot', {
                    'storyId': story.get('id'), 'participantId': group.get('participantId'),
                    'target': group.get('target'), 'tier': 'monthly', 'periodStart': group.get('from'),
                })
                if existing:
                    continue
                participant = self.get_participant(group.get('participantId')) if group.get('participantId') else None
                decision = self.compactor.compact_overlay({
                    'story': story, 'participant': participant, 'target': group.get('target'), 'tier': 'monthly',
                    'from': group.get('from'), 'to': group.get('to'), 'patches': [],
                    'snapshots': group.get('snapshots') or [],
                })
                summary = clip((decision or {}).get('summary'),
                               _nullish(config.get('overlayMonthlySummaryCharacters'), 2_400))
                if not summary:
                    continue
                source_patch_ids: List[int] = []
                for snapshot in group.get('snapshots') or []:
                    source_patch_ids.extend(snapshot.get('sourcePatchIds') or [])
                self.db_create('interlude_overlay_snapshot', {
                    'storyId': story.get('id'), 'participantId': group.get('participantId'),
                    'target': group.get('target'), 'tier': 'monthly',
                    'periodStart': group.get('from'), 'periodEnd': group.get('to'),
                    'summary': summary,
                    'majorEvents': normalize_major_events((decision or {}).get('majorEvents'), [],
                                                          group.get('snapshots') or []),
                    'sourcePatchIds': source_patch_ids,
                    'status': 'active', 'createdAt': now, 'updatedAt': now,
                })
                for snapshot in group.get('snapshots') or []:
                    self.db_set('interlude_overlay_snapshot', {'id': snapshot.get('id')},
                                {'status': 'superseded', 'updatedAt': now})
                changed = True
            if changed:
                self.rebuild_live_overlay_state(story, now)
                self.report_operation(
                    'standard', 'info', story, 'advance',
                    'Overlay 分层归档完成：最近 %d 天保留原始补丁，短期窗口=%d天，长期窗口=%d天',
                    _nullish(config.get('overlayRecentDays'), 2),
                    _nullish(config.get('overlayWeeklyWindowDays'), 5),
                    _nullish(config.get('overlayMonthlyWindowDays'), 10),
                )
            return changed
        except Exception as error:  # noqa: BLE001 - 上游 catch：可选后台工作失败不阻断叙事
            # Overlay maintenance is optional background work. A bad compression
            # response must leave raw patches untouched and never block narration.
            self.report_operation('standard', 'warn', story, 'advance', 'Overlay 分层归档跳过：%s', error)
            return False

    def overlay_snapshots_for_prompt(self, story_id: str, participant_id: Optional[str] = None,
                                     background: bool = False) -> List[OverlaySnapshot]:
        """上游 6329-6346 overlaySnapshotsForPrompt。"""
        if not self.memory_config.get('overlayCompressionEnabled'):
            return []
        rows = self.db_get(
            'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'},
            {'sort': {'periodEnd': 'desc'}},
        )
        share = self.shared_story_config.get('shareParticipantDetails')
        visible = [
            snapshot for snapshot in rows
            if not snapshot.get('participantId')
            or (share if background else snapshot.get('participantId') == participant_id)
        ]
        # Current long-term state plus recent short-window deltas is sufficient; older
        # snapshots remain searchable/auditable without permanently taxing prompts.
        result: List[OverlaySnapshot] = []
        for target in ('character', 'perspective', 'world', 'relationship'):
            matches = [snapshot for snapshot in visible if snapshot.get('target') == target]
            monthly = next((snapshot for snapshot in matches if snapshot.get('tier') == 'monthly'), None)
            if monthly:
                result.append(monthly)
            result.extend([snapshot for snapshot in matches if snapshot.get('tier') == 'weekly'][:4])
        return result

    def rebuild_live_overlay_state(self, story: InterludeStory, now: datetime) -> None:
        """上游 6348-6392 rebuildLiveOverlayState。

        Once a snapshot safely represents older changes, keep state.overlay as
        the live (uncompacted) delta only. This is what actually reduces prompt
        size; snapshots carry the older evolution separately.
        """
        story_id = story.get('id')
        applied = [
            item for item in self.db_get('interlude_state_patch', {'storyId': story_id, 'status': 'applied'})
            if not str(item.get('path') or '').startswith('development.')
        ]
        snapshots = self.db_get('interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'})

        def has_global_history(target: str) -> bool:
            return any(
                snapshot.get('target') == target and not snapshot.get('participantId')
                for snapshot in snapshots
            )

        overlay = {**((story.get('state') or {}).get('settingOverlay') or {})}
        if has_global_history('character'):
            overlay.pop('characterProfile', None)
            overlay['characterTraits'] = []
            for patch in applied:
                if patch.get('participantId') or patch.get('target') != 'character':
                    continue
                if 'trait' in str(patch.get('path') or ''):
                    overlay['characterTraits'].append(clip(patch.get('proposedValue'), 500))
                else:
                    overlay['characterProfile'] = merge_note(
                        overlay.get('characterProfile'), patch.get('proposedValue'),
                    )
            overlay['characterTraits'] = list(dict.fromkeys(overlay['characterTraits']))[-30:]
        if has_global_history('perspective'):
            overlay.pop('perspective', None)
            for patch in applied:
                if patch.get('participantId') or patch.get('target') != 'perspective':
                    continue
                overlay['perspective'] = merge_note(overlay.get('perspective'), patch.get('proposedValue'))
        if has_global_history('world'):
            overlay.pop('world', None)
            for patch in applied:
                if patch.get('participantId') or patch.get('target') != 'world':
                    continue
                overlay['world'] = merge_note(overlay.get('world'), patch.get('proposedValue'))
        if has_global_history('relationship'):
            overlay.pop('relationship', None)
            for patch in applied:
                if patch.get('participantId') or patch.get('target') != 'relationship':
                    continue
                overlay['relationship'] = merge_note(overlay.get('relationship'), patch.get('proposedValue'))
        self.db_set(
            'interlude_story', {'id': story_id},
            {'state': encode_story_state({**decode_story_state(story.get('state')), 'settingOverlay': overlay}),
             'updatedAt': now},
        )

        participant_ids = list(dict.fromkeys(
            snapshot.get('participantId') for snapshot in snapshots
            if snapshot.get('target') == 'relationship' and snapshot.get('participantId')
        ))
        for participant_id in participant_ids:
            participant = self.get_participant(participant_id)
            if not participant:
                continue
            state = normalize_participant_state(participant.get('state'))
            state.pop('relationshipOverlay', None)
            for patch in applied:
                if patch.get('target') == 'relationship' and patch.get('participantId') == participant_id:
                    state['relationshipOverlay'] = merge_note(state.get('relationshipOverlay'), patch.get('proposedValue'))
            self.db_set('interlude_participant', {'id': participant.get('id')}, {'state': state, 'updatedAt': now})

    # ================= 上游 6394-6533：压缩落库 =================

    def persist_compaction(self, story: InterludeStory, scene: InterludeScene, decision: CompactionDecision,
                           entries: List[ScriptEntry], now: datetime,
                           visible_fact_ids: Optional[Set[int]] = None) -> None:
        """上游 6394-6533 persistCompaction，逐分支复刻。"""
        if visible_fact_ids is None:
            visible_fact_ids = set()
        assert_continuity_review(decision)
        if not entries:
            return
        for draft in decision.get('episodeTags') or []:
            source = next((entry for entry in entries if entry.get('id') == draft.get('sourceEntryId')), None)
            if not source:
                continue
            tags = grounded_episode_tags(source.get('content'), draft)
            if not tags:
                continue
            rows = self.db_get('interlude_script_entry', {'storyId': story.get('id'), 'id': source.get('id')})
            current_entry = rows[0] if rows else None
            if not current_entry:
                continue
            self.db_set('interlude_script_entry', {'id': source.get('id')},
                        {'metadata': {**(current_entry.get('metadata') or {}), 'episodeTags': tags}})
            self.invalidate_history_vectors(story.get('id'))
        # Commit the arc before acknowledging the incremental evidence. A failed
        # arc write must not silently discard the material it was to learn from.
        arc = self.active_arc(story.get('id'))
        if not arc:
            raise RuntimeError('Continuity review has no active arc')
        arc_decision = decision.get('arc') or {}
        raw_title = arc_decision.get('title')
        title = raw_title.strip() if isinstance(raw_title, str) else ''
        self.db_set('interlude_arc', {'id': arc.get('id')}, {
            'title': clip(title or arc.get('title'), 255),
            'summary': clip(arc_decision.get('summary'), self.memory_config.get('arcSummaryCharacters', 0)),
            'updatedAt': now,
        })
        # 摘要更新成功后才移动 lastEntryId，确保失败时原始条目仍会在下次被重新处理。
        scene_patch = decision.get('scene') or {}
        boundary = scene_patch.get('boundary') or {}
        boundary_reason = boundary.get('reason')
        explicit_boundary = (
            scene_patch.get('close') is True
            and isinstance(boundary_reason, str)
            and bool(boundary_reason.strip())
            and has_compaction_evidence(boundary.get('sourceEntryIds'), entries)
        )
        raw_hook = scene_patch.get('hook')
        raw_summary = scene_patch.get('summary')
        last_entry_id = entries[-1].get('id') if entries else None
        if last_entry_id is None:
            last_entry_id = scene.get('lastEntryId')
        self.db_set('interlude_scene', {'id': scene.get('id')}, {
            'hook': clip(raw_hook if raw_hook is not None else scene.get('hook'),
                         self.memory_config.get('sceneHookCharacters', 0)),
            'summary': clip(raw_summary if raw_summary is not None else scene.get('summary'),
                            self.memory_config.get('sceneSummaryCharacters', 0)),
            'entryCount': max(0, scene.get('entryCount') or 0) + len(entries),
            'lastEntryId': last_entry_id,
            'updatedAt': now,
        })
        if explicit_boundary:
            # Preserve boundary provenance beside its original evidence. This
            # checkpoint is navigation only, with no generated summary or prose.
            source_ids = [
                id_ for id_ in (boundary.get('sourceEntryIds') or [])
                if any(entry.get('id') == id_ for entry in entries)
            ]
            boundary_entry = None
            if source_ids:
                boundary_id = max(source_ids)
                boundary_entry = next((entry for entry in entries if entry.get('id') == boundary_id), None)
            if boundary_entry:
                rows = self.db_get('interlude_script_entry', {'id': boundary_entry.get('id')})
                source = rows[0] if rows else None
                if source and source.get('storyId') == story.get('id'):
                    first_rows = self.db_get('interlude_script_entry', {
                        'storyId': story.get('id'),
                        'occurredAt': {'$gte': scene.get('startedAt')},
                        'id': {'$lte': boundary_entry.get('id')},
                    }, {'sort': {'id': 'asc'}, 'limit': 1})
                    first = first_rows[0] if first_rows else None
                    first_entry_id = first.get('id') if first else None
                    if first_entry_id is None:
                        first_entry_id = boundary_entry.get('id')
                    checkpoint_last = entries[-1].get('id')
                    if checkpoint_last is None:
                        checkpoint_last = scene.get('lastEntryId')
                    self.db_set('interlude_script_entry', {'id': source.get('id')}, {
                        'metadata': {
                            **(source.get('metadata') or {}),
                            'sceneCheckpoint': {
                                'sceneId': scene.get('id'),
                                'startedAt': iso(scene.get('startedAt')),
                                'endedAt': iso(entries[-1].get('occurredAt')),
                                'boundarySourceEntryIds': source_ids,
                                'reason': boundary.get('reason'),
                                'firstEntryId': first_entry_id,
                                'lastEntryId': checkpoint_last,
                            },
                        },
                    })
                    self.invalidate_history_vectors(story.get('id'))
            # Close at the processed frontier, not at model completion. Entries
            # arriving during the background request belong to the next review.
            frontier = entries[-1].get('occurredAt')
            self.db_set('interlude_scene', {'id': scene.get('id')},
                        {'status': 'closed', 'endedAt': frontier, 'updatedAt': now})
            self.ensure_continuity(story, frontier)
            next_scene = self.active_scene(story.get('id'))
            if next_scene:
                self.db_set('interlude_scene', {'id': next_scene.get('id')},
                            {'lastEntryId': entries[-1].get('id')})
        presence_updates = normalize_scene_presence_drafts(scene_patch.get('presence'), entries, now)
        if presence_updates:
            current = self.get_story(story.get('id'))
            state = decode_story_state(current.get('state'))
            by_name: Dict[str, Dict[str, Any]] = {
                item.get('name'): item for item in (state.get('scenePresence') or [])
            }
            for update in presence_updates:
                update_max = max(update.get('sourceEntryIds') or [])
                frame = state.get('sceneFrame') or {}
                if update_max < (frame.get('localBoundaryEntryId') or 0):
                    continue
                previous = by_name.get(update.get('name'))
                if previous and max(previous.get('sourceEntryIds') or []) > update_max:
                    continue
                by_name[update.get('name')] = update
            self.db_set('interlude_story', {'id': current.get('id')}, {
                'state': encode_story_state({**state, 'scenePresence': list(by_name.values())[-8:]}),
                'updatedAt': now,
            })
        if decision.get('workingDetails'):
            current = self.get_story(story.get('id'))
            state = decode_story_state(current.get('state'))
            merged: Dict[str, Dict[str, Any]] = {}
            resolutions = {**(state.get('workingDetailResolutions') or {})}
            for item in state.get('workingDetails') or []:
                merged[item.get('label')] = item
            for draft in decision.get('workingDetails') or []:
                if not has_compaction_evidence(draft.get('sourceEntryIds') or [], entries):
                    continue
                label = clip(draft.get('label'), 80)
                valid_revisions = [
                    id_ for id_ in (draft.get('sourceEntryIds') or [])
                    if any(entry.get('id') == id_ for entry in entries)
                ]
                revision = max(valid_revisions) if valid_revisions else 0
                if revision <= (resolutions.get(label) or 0):
                    continue
                previous = merged.get(label)
                if previous and revision <= max([0, *(previous.get('sourceEntryIds') or [])]):
                    continue
                sources = [entry for entry in entries if entry.get('id') in (draft.get('sourceEntryIds') or [])]
                owners = {entry.get('participantId') for entry in sources}
                participant_id = sources[0].get('participantId') if len(owners) == 1 and sources else None
                if previous is not None and previous.get('participantId') is not None \
                        and previous.get('participantId') != participant_id:
                    continue
                source_entry_ids = [
                    id_ for id_ in (draft.get('sourceEntryIds') or [])
                    if any(entry.get('id') == id_ for entry in entries)
                ][:8]
                knowledge = normalize_knowledge_evidence(draft.get('knowledge'), entries, source_entry_ids)
                previous_knowledge = previous.get('knowledge') if previous else None
                if previous_knowledge and supports_recorded_outcome(previous_knowledge) \
                        and not supports_recorded_outcome(knowledge):
                    continue
                if label and draft.get('resolved') is True:
                    merged.pop(label, None)
                    resolutions[label] = revision
                    continue
                value = clip(draft.get('value'), 300)
                if not label or not value:
                    continue
                raw_expires = draft.get('expiresAt')
                expires_at = raw_expires if raw_expires and parse_time(raw_expires) is not None else None
                replaces_label = clip(draft.get('replacesLabel'), 80)
                replaced = merged.get(replaces_label) if replaces_label and replaces_label != label else None
                if replaces_label and replaces_label != label \
                        and (not replaced or previous or revision <= (resolutions.get(replaces_label) or 0)):
                    continue
                if replaced:
                    if replaced.get('participantId') is not None:
                        same_owner = replaced.get('participantId') == participant_id
                    else:
                        holder = (replaced.get('knowledge') or {}).get('holder')
                        same_owner = bool(holder) and holder == knowledge.get('holder')
                    replaced_source_ids = replaced.get('sourceEntryIds')
                    replaced_source_ids = replaced_source_ids if isinstance(replaced_source_ids, list) else [0]
                    if not same_owner or not supports_recorded_outcome(knowledge) \
                            or revision <= max([(resolutions.get(replaces_label) or 0), *replaced_source_ids]):
                        continue
                    merged.pop(replaces_label, None)
                    resolutions[replaces_label] = revision
                created_at = previous.get('createdAt') if previous else None
                if created_at is None and replaced:
                    created_at = replaced.get('createdAt')
                if created_at is None:
                    created_at = iso(now)
                item: Dict[str, Any] = {'label': label, 'value': value, 'knowledge': knowledge}
                if participant_id is not None:
                    item['participantId'] = participant_id
                if expires_at:
                    item['expiresAt'] = expires_at
                item['createdAt'] = created_at
                if source_entry_ids:
                    item['sourceEntryIds'] = source_entry_ids
                merged[label] = item
            live = [
                item for item in merged.values()
                if not item.get('expiresAt') or _date_after(item.get('expiresAt'), now)
            ][-10:]
            self.db_set('interlude_story', {'id': current.get('id')}, {
                'state': encode_story_state({**state, 'workingDetails': live, 'workingDetailResolutions': resolutions}),
                'updatedAt': now,
            })
        resolved_facts = False
        for fact in decision.get('facts') or []:
            if not has_compaction_evidence(fact.get('sourceEntryIds'), entries):
                continue
            knowledge = normalize_knowledge_evidence(
                fact.get('knowledge'), entries, fact.get('sourceEntryIds') or [],
            )
            resolved = supports_recorded_outcome(knowledge) and self.resolve_compaction_facts(
                story.get('id'), fact.get('resolvesFactIds'), visible_fact_ids, now,
            )
            resolved_facts = resolved_facts or bool(resolved)
            merged_resolution = self.persist_fact(story.get('id'), fact, entries, now)
            resolved_facts = resolved_facts or bool(merged_resolution)
        for patch in decision.get('statePatches') or []:
            if not has_compaction_evidence(patch.get('sourceEntryIds'), entries):
                continue
            self.persist_state_patch(story, patch, entries, now)
        if resolved_facts:
            self.mark_continuity_dirty(story.get('id'), now)

    def persist_fact(self, story_id: str, draft: Dict[str, Any], entries: List[ScriptEntry], now: datetime) -> bool:
        """上游 6535-6580 persistFact。"""
        content = clip(draft.get('content'), self.memory_config.get('factContentCharacters', 0))
        if not content:
            return False
        participant_id = resolve_participant_id(draft.get('participantId'), draft.get('sourceEntryIds'), entries)
        existing = self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'})
        # 当前先做完全规范化匹配的去重；更复杂的语义去重可在检索层升级时替换。
        content_normalized = normalize_fact(content)
        matching = [
            fact for fact in existing
            if normalize_fact(fact.get('content')) == content_normalized
            and fact.get('participantId') == participant_id
        ]
        source_entry_ids = [
            id_ for id_ in (draft.get('sourceEntryIds') or [])
            if any(entry.get('id') == id_ for entry in entries)
        ][:20]
        raw_related = (draft.get('knowledge') or {}).get('relatedFactIds')
        related_fact_ids = [
            id_ for id_ in raw_related
            if any(fact.get('id') == id_ and fact.get('participantId') == participant_id for fact in existing)
        ] if isinstance(raw_related, list) else []
        knowledge = normalize_knowledge_evidence(draft.get('knowledge'), entries, source_entry_ids, related_fact_ids)

        def knowledge_mode(fact: Dict[str, Any]) -> str:
            fact_knowledge = fact.get('knowledge')
            if isinstance(fact_knowledge, dict):
                mode = fact_knowledge.get('mode')
                return 'unclassified' if mode is None else mode
            return 'unclassified'

        # Promise facts are unresolved by default, unless the compactor explicitly
        # says that the promise has already been fulfilled or closed.
        can_close = supports_recorded_outcome(knowledge)
        same = next((fact for fact in matching if knowledge_mode(fact) == knowledge.get('mode')), None)
        if same is None:
            same = next((fact for fact in matching if not fact.get('knowledge') and can_close), None)
        unresolved = draft.get('unresolved') is True or (
            draft.get('scope') == 'promise' and (draft.get('unresolved') is not False or not can_close)
        )
        if same and (knowledge_mode(same) == knowledge.get('mode') or (not same.get('knowledge') and can_close)):
            resolved = bool(same.get('unresolved')) and draft.get('unresolved') is False and can_close
            same_embedding = same.get('embedding')
            embedding = same_embedding if same_embedding else self.embed_text(content)
            same_source_ids = same.get('sourceEntryIds') or []
            fresh_support = any(
                clause.get('sourceEntryId') not in same_source_ids
                and clause.get('role') in ('observation', 'confirmation')
                and any(
                    entry.get('id') == clause.get('sourceEntryId')
                    and entry.get('kind') in ('user-message', 'character-message')
                    for entry in entries
                )
                for clause in (knowledge.get('clauses') or [])
            )
            knowledge_patch = {
                **knowledge,
                'clauses': [
                    *((same.get('knowledge') or {}).get('clauses') or []),
                    *(knowledge.get('clauses') or []),
                ][-12:],
                'relatedFactIds': list(dict.fromkeys([
                    *((same.get('knowledge') or {}).get('relatedFactIds') or []),
                    *(knowledge.get('relatedFactIds') or []),
                ]))[-12:],
            }
            patch: Dict[str, Any] = {
                'importance': max(same.get('importance') or 0,
                                  clamp_number(draft.get('importance'), same.get('importance'), 0, 1)),
                'confidence': max(same.get('confidence') or 0,
                                  clamp_number(draft.get('confidence'), same.get('confidence'), 0, 1))
                if fresh_support else same.get('confidence'),
                'unresolved': False if resolved else (bool(same.get('unresolved')) or unresolved),
                'sourceEntryIds': list(dict.fromkeys([*same_source_ids, *source_entry_ids])),
                'lastSeenAt': now,
                'updatedAt': now,
                'knowledge': knowledge_patch,
            }
            if embedding:
                patch['embedding'] = embedding
            self.db_set('interlude_fact', {'id': same.get('id')}, patch)
            return resolved
        if len(existing) >= self.memory_config.get('maxFactsPerStory', 0):
            ordered = sorted(
                existing,
                key=lambda fact: (fact.get('importance') or 0) * (fact.get('confidence') or 0),
            )
            oldest = ordered[0] if ordered else None
            if oldest:
                self.db_set('interlude_fact', {'id': oldest.get('id')}, {'status': 'superseded', 'updatedAt': now})
        self.db_create('interlude_fact', {
            'storyId': story_id, 'participantId': participant_id, 'scope': draft.get('scope'),
            'content': content, 'knowledge': knowledge,
            'importance': clamp_number(draft.get('importance'), 0.5, 0, 1),
            'confidence': clamp_number(draft.get('confidence'), 0.5, 0, 1),
            'unresolved': unresolved, 'embedding': self.embed_text(content), 'status': 'active',
            'sourceEntryIds': source_entry_ids, 'lastSeenAt': now, 'createdAt': now, 'updatedAt': now,
        })
        return False

    # ================= 上游 6582-6603：Embedding =================

    def embed_text(self, value: str) -> List[float]:
        """上游 6582-6590 embedText。"""
        try:
            return self.embedder.embed(value)
        except Exception as error:  # noqa: BLE001 - Embeddings 永不阻断私聊回合
            # Embeddings improve recall but must never make a private-message turn fail.
            self.report_standalone_operation('diagnostic', 'debug', 'Embedding 请求跳过 错误=%s', error)
            return []

    def schedule_fact_embedding_backfill(self, story_id: str) -> None:
        """上游 6592-6603 scheduleFactEmbeddingBackfill。"""
        embedding = (self.config.get('model') or {}).get('embedding')
        raw_batch_size = embedding.get('backfillBatchSize') if isinstance(embedding, dict) else None
        batch_size = 5 if raw_batch_size is None else raw_batch_size
        batch_number = _to_number(batch_size)
        if (
            not isinstance(embedding, dict)
            or not embedding.get('enabled')
            or not str(embedding.get('model') or '').strip()
            or (batch_number is not None and batch_number <= 0)
        ):
            return
        if story_id in self.fact_backfills:
            return
        self.fact_backfills.add(story_id)

        # This maintenance task deliberately stays out of the narrative serial queue:
        # it only fills an optional index column and must not delay a new user event.
        def run_backfill() -> None:
            try:
                self.backfill_fact_embeddings(story_id, batch_size)
            except Exception as error:  # noqa: BLE001 - 上游 .catch(...)
                self.report_standalone_operation('diagnostic', 'debug', '长期事实向量补齐跳过 错误=%s', error)
            finally:
                # 上游 .finally(() => this.factBackfills.delete(storyId))
                self.fact_backfills.discard(story_id)

        self.ctx.set_timeout(run_backfill, 0)

    def backfill_fact_embeddings(self, story_id: str, batch_size: Any) -> None:
        """上游 6605-6615 backfillFactEmbeddings。"""
        facts = self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'})
        missing = [fact for fact in facts if not fact.get('embedding')]
        missing.sort(key=lambda fact: _timestamp_ms(fact.get('updatedAt')), reverse=True)
        missing = missing[:max(0, _to_integer_or_zero(batch_size))]
        for fact in missing:
            embedding = self.embed_text(fact.get('content'))
            if embedding:
                self.db_set('interlude_fact', {'id': fact.get('id')},
                            {'embedding': embedding, 'updatedAt': now_utc()})

    # ================= 上游 6617-6720：状态补丁 / development =================

    def persist_state_patch(self, story: InterludeStory, draft: StatePatchDraft,
                            entries: List[ScriptEntry], now: datetime) -> None:
        """上游 6617-6712 persistStatePatch。"""
        confidence = clamp_number(draft.get('confidence'), 0, 0, 1)
        participant_id = '' if draft.get('target') == 'perspective' \
            else resolve_participant_id(draft.get('participantId'), draft.get('sourceEntryIds'), entries)
        dimension = development_dimension(draft.get('target'), draft.get('path') or '')
        path = 'development.%s' % dimension if dimension else ''
        source_entry_ids = [
            id_ for id_ in (draft.get('sourceEntryIds') or [])
            if any(entry.get('id') == id_ for entry in entries)
        ][:20]
        proposed_value = clip(draft.get('proposedValue'), 4_000)
        impact = 'major' if draft.get('impact') == 'major' else 'minor'
        if not path or not proposed_value or not source_entry_ids:
            return
        # Relationship observations must remain in their named branch; private
        # material cannot become a global personality or world change.
        if draft.get('target') == 'relationship' and not participant_id:
            return
        if draft.get('target') == 'relationship' and any(
            entry.get('id') in source_entry_ids and entry.get('participantId')
            and entry.get('participantId') != participant_id
            for entry in entries
        ):
            return
        if draft.get('target') != 'relationship' and any(
            entry.get('id') in source_entry_ids and entry.get('participantId')
            for entry in entries
        ):
            return
        # A relationship tendency becomes usable only when actual user feedback
        # and the protagonist's subsequent delivered answer support the same
        # reading. This prevents one scene's prose rhythm from becoming a claim
        # about the relationship before the other person has participated in it.
        reception_supported = draft.get('target') != 'relationship' \
            or reviewed_development_support(draft, entries, participant_id)
        if not reception_supported:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '关系发展提案暂不保留：缺少已接收反馈的交互证据 目标=%s/%s', draft.get('target'), path,
            )
            return

        # Merge repeated proposals for one setting path before evaluating them.
        candidates = self.db_get('interlude_state_patch', {
            'storyId': story.get('id'), 'participantId': participant_id,
            'target': draft.get('target'), 'path': path,
        })
        matching = [candidate for candidate in candidates if patch_claims_match(candidate.get('proposedValue'), proposed_value)]
        contradicts_ids = draft.get('contradictsProposalIds') or []
        for contradicted in [
            candidate for candidate in candidates
            if candidate.get('status') in ('proposed', 'applied') and candidate.get('id') in contradicts_ids
        ]:
            contradicted_source_ids = contradicted.get('sourceEntryIds') or []
            fresh = [id_ for id_ in source_entry_ids if id_ not in contradicted_source_ids]
            if not fresh:
                continue
            self.db_set('interlude_state_patch', {'id': contradicted.get('id')}, {
                'confidence': max(0, (contradicted.get('confidence') or 0) - 0.2),
                'status': 'rejected',
                'sourceEntryIds': list(dict.fromkeys([*contradicted_source_ids, *fresh])),
                'evidence': clip(merge_note(
                    contradicted.get('evidence'),
                    'Counter-evidence (%s): %s' % (','.join(_js_string(id_) for id_ in fresh), draft.get('evidence')),
                ), 4_000),
            })
        if isinstance(contradicts_ids, list) and contradicts_ids:
            return
        if any(candidate.get('status') in ('applied', 'compacted') for candidate in matching):
            return
        candidate = next((item for item in matching if item.get('status') == 'proposed'), None)
        merged_source_entry_ids = list(dict.fromkeys([
            *((candidate or {}).get('sourceEntryIds') or []),
            *source_entry_ids,
        ]))[:80]
        source_rows = self.db_get('interlude_script_entry', {
            'storyId': story.get('id'), 'id': {'$in': merged_source_entry_ids},
        })
        evidence = state_patch_evidence(source_rows, (story.get('setting') or {}).get('timezone'))
        minimum_turns = max(3, _nullish(
            self.memory_config.get('statePatchMinTurns'), self.memory_config.get('statePatchMinEvidence'),
        ))
        minimum_days = max(1, _nullish(self.memory_config.get('statePatchMinDays'), 2))
        minimum = self.memory_config.get('majorStatePatchConfidenceThreshold') if impact == 'major' \
            else self.memory_config.get('statePatchConfidenceThreshold')
        candidate_source_ids = (candidate or {}).get('sourceEntryIds') or []
        old_scenes = development_scenes([
            entry for entry in source_rows if entry.get('id') in candidate_source_ids
        ])
        merged_confidence = min(confidence, (candidate.get('confidence') or 0)
                                + (0.05 if reception_supported and evidence.get('scenes', 0) > old_scenes else 0)) \
            if candidate else confidence
        merged_evidence_text = merge_note(candidate.get('evidence') if candidate else None, draft.get('evidence'))
        proposal = candidate
        if proposal is None:
            proposal = self.db_create('interlude_state_patch', {
                'storyId': story.get('id'), 'participantId': participant_id, 'target': draft.get('target'),
                'path': path, 'proposedValue': proposed_value,
                'evidence': clip(merged_evidence_text, 4_000), 'confidence': merged_confidence, 'impact': impact,
                'status': 'proposed', 'sourceEntryIds': merged_source_entry_ids, 'createdAt': now,
                'appliedAt': None,
            })
        if candidate and candidate.get('id'):
            self.db_set('interlude_state_patch', {'id': candidate.get('id')}, {
                'evidence': clip(merged_evidence_text, 4_000), 'confidence': merged_confidence,
                'impact': 'major' if candidate.get('impact') == 'major' or impact == 'major' else 'minor',
                'sourceEntryIds': merged_source_entry_ids,
            })

        # Ordinary changes require independent narrative turns on different days.
        if not reception_supported:
            return
        if not candidate or evidence.get('scenes', 0) <= old_scenes \
                or not self.memory_config.get('autoApplyStatePatches') \
                or (impact == 'major' and not self.memory_config.get('allowMajorStateChanges')):
            return
        stable_evidence = (
            merged_confidence >= minimum and evidence.get('scenes', 0) >= 2
            if impact == 'major'
            else merged_confidence >= minimum and evidence.get('scenes', 0) >= minimum_turns
            and evidence.get('days', 0) >= minimum_days
        )
        if not stable_evidence:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                'Overlay 候选继续累计 目标=%s/%s 回合=%d/%d 日期=%d/%d',
                draft.get('target'), path, evidence.get('turns', 0), minimum_turns,
                evidence.get('days', 0), minimum_days,
            )
            return

        cooldown_hours = max(1, _nullish(self.memory_config.get('statePatchCooldownHours'), 72))
        recent_applied = [
            parse_time(item.get('appliedAt') if item.get('appliedAt') is not None else item.get('createdAt'))
            for item in candidates
            if item.get('status') in ('applied', 'compacted')
        ]
        recent_applied = [item for item in recent_applied if item is not None]
        most_recent = max(recent_applied) if recent_applied else None
        if most_recent and now - most_recent < timedelta(hours=cooldown_hours):
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                'Overlay 冷却中，候选保留 目标=%s/%s 冷却=%d小时', draft.get('target'), path, cooldown_hours,
            )
            return

        # New tendencies remain sourced records, rather than accumulating in the
        # permanent Canon overlay. The live reader selects only relevant ones.
        if proposal and proposal.get('id'):
            self.db_set('interlude_state_patch', {'id': proposal.get('id')}, {'status': 'applied', 'appliedAt': now})

    def development_for_prompt(self, story_id: str, participant_id: Optional[str],
                               query: str) -> List[Dict[str, Any]]:
        """上游 6714-6720 developmentForPrompt。"""
        if not isinstance(query, str) or not query.strip():
            return []
        rows = self.db_get(
            'interlude_state_patch', {'storyId': story_id, 'status': 'applied'},
            {'limit': 100, 'sort': {'appliedAt': 'desc'}},
        )
        scored: List[Dict[str, Any]] = []
        for item in rows:
            if not str(item.get('path') or '').startswith('development.'):
                continue
            if not (item.get('sourceEntryIds') or []):
                continue
            item_participant = item.get('participantId')
            if item_participant and item_participant != participant_id:
                continue
            scored.append({'item': item, 'score': history_lexical_score(query, item.get('proposedValue'))})
        scored = [entry for entry in scored if entry.get('score') >= 0.12]
        scored.sort(key=lambda entry: entry.get('score'), reverse=True)
        return [
            {'target': entry['item'].get('target'),
             'tendency': clip(entry['item'].get('proposedValue'), 300),
             'sourceEntryIds': entry['item'].get('sourceEntryIds')}
            for entry in scored[:2]
        ]
