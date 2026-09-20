# -*- coding: utf-8 -*-
"""HDS-Interlude 移植：上游 src/service.ts 行号 624-760、2240-2270、4211-4230、
5395-5420、5570-5590、5755-5838、6724-7010。

ServiceBaseMixin —— InterludeService 的基础层：
字段初始化 / 构造函数 / 后台定时器 / 配置 getter / serial 队列 /
日志（report 系列）/ 数据库封装（db_get 等）。

说明：
- 上游 5755-5830 在 `browserConfig` getter 中间截断，这里把 5802-5838 的
  `browserConfig` 整体移植，保证 getter 返回结构完整。
- 上游 769-918 的 desktopRuntimePhase 快照/desktopTimeline 系列不属于本文件
  分配区间（见 SERVICE_PORTING_SPEC.md 的区间表），此处只保留字段与 758-768
  的 setter/getter；`set_desktop_runtime_phase` 等留给后续任务。
- 上游 async/Promise 全部同步化：serial 用每故事一把 threading.RLock；
  databaseWriteQueue 用全局写锁；重试分支保留（sleep 用 time.sleep）。
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from .agency import resolve_agency_config
from .alter import emotional_offset_for_prompt, normalize_alter_system_state, resolve_alter_system_config
from .logging import format_layered_log, phase_label, render_log_message
from .model_routing import format_model_routing, resolve_model_routing
from .narrator import (
    create_compactor,
    create_embedder,
    create_narrator,
    create_sticker_describer,
    create_vision_describer,
    format_token_usage_line,
)
from .schedule_preplan import resolve_schedule_preplan_config
from .service_types import (
    AutoAdvanceConfig,
    BlindModeConfig,
    BrowserConfig,
    Config,
    MemoryConfig,
    RuntimeConfig,
    SharedStoryConfig,
    StickerLibraryConfig,
)
from .story_state import decode_story_state, encode_story_state, inspect_story_state_migration
from .time_utils import now_utc, same_timestamp
from .types import (
    AgencyConfig,
    AlterSystemConfig,
    EmotionalOffsetPrompt,
    InterludeStory,
)
from .urge import resolve_urge_config
from .utils import is_record, json_equal

# 上游 service.ts 7084-8524 的顶层 helper 由 hdsi/service_helpers.py 提供（并行移植）。
# 其中 isOneBotPlatform / normalizeFollowUpMinutes / resolveBlindModeConfig 在本模块也要用；
# helpers 缺失这三个名字时回退到文件内联副本（语义与上游一致，默认值逐字相同）。
try:
    from .service_helpers import (
        is_one_bot_platform,
        normalize_follow_up_minutes,
        resolve_blind_mode_config,
    )
except ImportError:  # TODO(并行移植): service_helpers 暂缺这些名字时的本地副本
    def is_one_bot_platform(platform: Any) -> bool:
        """上游 service.ts 7026-7034 isOneBotPlatform。"""
        value = str(platform if platform is not None else '').lower()
        return (
            value == 'onebot'
            or value.startswith('onebot:')
            or value == 'napcat'
            or value.startswith('napcat:')
            or value == 'qq:onebot'
            or value.startswith('qq:onebot:')
        )

    def normalize_follow_up_minutes(values: Any) -> List[int]:
        """上游 service.ts 8331-8337 normalizeFollowUpMinutes（未导出）。"""
        defaults = [10, 20]
        source = values if isinstance(values, (list, tuple)) else defaults
        normalized: List[int] = []
        for value in source:
            number = _to_number(value)
            if number is None or math.isnan(number):
                continue
            floored = math.floor(number)
            if math.isfinite(floored) and 1 <= floored <= 240:
                normalized.append(int(floored))
        return sorted(set(normalized))[:6]

    def resolve_blind_mode_config(value: Any = None) -> Dict[str, Any]:
        """上游 service.ts 7715-7720 resolveBlindModeConfig。"""
        record = value if isinstance(value, dict) else {}
        return {
            'enabled': record.get('enabled') is True,
            'healthReportMinutes': max(1, min(1_440, math.floor(_nullish_number(record.get('healthReportMinutes'), 10)))),
        }

# 规格/任务里写作 is_onebot_platform（helpers 里的名字是 is_one_bot_platform），这里给别名。
is_onebot_platform = is_one_bot_platform

# 上游 service.ts 7700-7703 isTransientDatabaseError（未导出，仅本文件/DB 封装使用）。
_RE_TRANSIENT_DB_ERROR = re.compile(r'disk\s*i/o|database is locked|busy|unable to open', re.IGNORECASE)


def is_transient_database_error(error: Any) -> bool:
    """上游 service.ts 7700-7703：可重试的 SQLite/sql.js 瞬时错误。"""
    return bool(_RE_TRANSIENT_DB_ERROR.search(str(error)))


def _nullish(value: Any, fallback: Any) -> Any:
    """复刻 TS 的 `value ?? fallback`（None 视作 null/undefined）。"""
    return fallback if value is None else value


def _to_number(value: Any) -> Optional[float]:
    """复刻 JS `Number(value)`；不可转换返回 None（等价 NaN）。"""
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


def _number_or(value: Any, fallback: float) -> float:
    """复刻 `Number(value) || fallback`（NaN 与 0 都取 fallback）。"""
    number = _to_number(value)
    if number is None or math.isnan(number) or number == 0:
        return fallback
    return number


def _nullish_number(value: Any, fallback: float) -> float:
    """复刻 `(value ?? fallback)` 并做 JS 数值化；不可转换时用 fallback。"""
    resolved = fallback if value is None else value
    number = _to_number(resolved)
    if number is None or math.isnan(number):
        return fallback
    return number


def _is_safe_integer(value: Any) -> bool:
    """复刻 JS Number.isSafeInteger（用于 resolve_compaction_facts 的 id 过滤）。"""
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 9_007_199_254_740_991


class ServiceBaseMixin:
    """上游 InterludeService 的基础层（字段/构造/定时器/配置/日志/数据库）。"""

    def __init__(self, ctx: Any, config: Config) -> None:
        self.ctx = ctx
        self.config = config
        self.store = ctx.store
        self.platform = ctx.platform
        self.logger = ctx.logger
        # 上游 `private readonly serviceLogger: Logger` = ctx.logger('hds-interlude')。
        # 本项目 RuntimeContext 只有一个 logger；emit_log / reportBlindModeHealth 使用它。
        self.service_logger = ctx.logger

        # ===== 上游 624-716：InterludeService 字段 =====
        self.narrator: Any = None
        self.compactor: Any = None
        self.embedder: Any = None
        self.sticker_describer: Any = None
        self.vision_describer: Any = None
        self.sticker_catalog: List[Dict[str, Any]] = []
        # 每个故事一张 map：entry id → {vector, content, ...}（懒加载，不持久化）。
        self.history_vectors: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self.history_vectors_ready: set = set()
        self.history_vector_loads: Dict[str, Any] = {}
        self.history_backfills: set = set()
        self.history_backoff: Dict[str, float] = {}
        self.automatic_recall_cache: Dict[str, Dict[str, Any]] = {}
        self.schedule_preplan_backoff: Dict[str, float] = {}
        self.timeline_backoff: Dict[str, Dict[str, float]] = {}
        self.timeline_director_failures: Dict[str, int] = {}
        self.compaction_backoff: Dict[str, Dict[str, Any]] = {}
        self.sticker_by_id: Dict[str, Dict[str, Any]] = {}
        self.sticker_scan_running = False
        # 同一故事的用户消息、到期意图和后台压缩必须串行（上游 queues: Map<string, Promise>）。
        # Python 用每故事一把 RLock；见 serial()。
        self.queues: Dict[str, threading.RLock] = {}
        self.buffered_narrative_turns: Dict[str, Dict[str, Any]] = {}
        self.buffered_group_turns: Dict[str, Dict[str, Any]] = {}
        self.group_member_name_cache: Dict[str, Dict[str, Any]] = {}
        self.group_member_name_lookups: Dict[str, Any] = {}
        self.group_willingness: Dict[str, Dict[str, Any]] = {}
        self.due_intent_wake_timers: Dict[str, Dict[str, Any]] = {}
        self.interrupted_typing_participants: set = set()
        self.narrating_stories: set = set()
        self.fact_backfills: set = set()
        self.scheduled_compactions: set = set()
        self.scheduled_alter_analyses: set = set()
        # 上游 databaseWriteQueue: Promise.resolve()：SQLite 单写连接，全局串行写。
        self.database_write_queue: threading.RLock = threading.RLock()
        self.browser_active = 0
        self.browser_waiters: List[Callable[[], None]] = []
        self.background_started = False
        self.database_resetting = False
        self.sweep_running = False
        self.compaction_sweep_running = False
        self.blind_mode_health_issue = False
        self.cached_audio_config: Optional[Dict[str, Any]] = None
        self.cached_sticker_config: Optional[StickerLibraryConfig] = None
        self.cached_alter_system_config: Optional[AlterSystemConfig] = None
        self.cached_agency_config: Optional[AgencyConfig] = None
        self.cached_schedule_preplan_config: Optional[Dict[str, Any]] = None
        self.cached_blind_mode_config: Optional[BlindModeConfig] = None
        self.cached_auto_advance_config: Optional[AutoAdvanceConfig] = None
        self.cached_shared_story_config: Optional[SharedStoryConfig] = None
        self.cached_memory_config: Optional[MemoryConfig] = None
        self.cached_browser_config: Optional[BrowserConfig] = None
        self.model_routing: Dict[str, Any] = {}
        # Migration diagnostics are emitted once per story without changing Canon.
        self.reported_state_migrations: set = set()
        # typ-0 uses this optional gate only inside a dedicated worker process.
        self.desktop_runtime_phase = 'running'
        self.desktop_event_sink: Optional[Callable[[str, Any], None]] = None
        # typ-0 后台投递出口；普通 Koishi 永远为空。
        self.desktop_delivery_handler: Optional[Callable[..., Any]] = None

        # ===== 上游 717-739：constructor =====
        # registerTables(ctx) 不移植：store 建表已含。
        # 共享的 token 用量出口：主叙事/压缩/alter/贴纸描述都从这里写日志。
        on_usage = lambda record: self.report_token_usage(record)  # noqa: E731
        model_config = config.get('model') or {}
        self.model_routing = resolve_model_routing(model_config, config.get('alterSystem'))
        blind_mode_enabled = self.blind_mode_config['enabled']
        self.narrator = create_narrator(ctx, model_config, blind_mode_enabled, on_usage, self.model_routing)
        self.compactor = create_compactor(ctx, model_config, blind_mode_enabled, on_usage, self.model_routing)
        self.embedder = create_embedder(ctx, model_config, self.model_routing)
        self.sticker_describer = create_sticker_describer(ctx, model_config, blind_mode_enabled, on_usage, self.model_routing)
        self.vision_describer = create_vision_describer(ctx, model_config, blind_mode_enabled, on_usage, self.model_routing)
        # Defer timer registration by one event-loop turn（同步实现：threading.Timer(0)）。
        ctx.set_timeout(self.start_background_tasks, 0)
        # 上游 `ctx.on('ready', () => this.reportStandaloneOperation('summary', 'info', '服务已就绪'))`：
        # 本项目没有 Koishi 生命周期，构造时直接调用一次对应日志。
        self.report_standalone_operation('summary', 'info', '服务已就绪')
        self.report_standalone_operation(
            'summary', 'info',
            '服务初始化完成 主叙事路由=%s 共享主剧本=%s 自动推进=%s',
            '已配置' if self.model_routing['main']['available'] else '未配置',
            self.shared_story_config['enabled'],
            self.auto_advance_config['enabled'],
        )
        self.report_standalone_operation('diagnostic', 'debug', '模型任务路由 %s', format_model_routing(self.model_routing))

    # ================= 上游 741-756：startBackgroundTasks =================

    def start_background_tasks(self) -> None:
        """上游 741-756 startBackgroundTasks。"""
        if self.background_started:
            return
        self.background_started = True

        def guard(task: Callable[[], Any], failure_message: str) -> Callable[[], None]:
            """复刻 `() => void task().catch(error => reportStandalone('warn', ...))`。"""
            def run() -> None:
                try:
                    task()
                except Exception as error:  # noqa: BLE001 - 后台任务异常只记录，不上抛
                    self.report_standalone('warn', failure_message, error)
            return run

        # Life advancement and memory compaction are both serialized per story.
        sweep_interval = max(1, _nullish_number((self.config.get('runtime') or {}).get('sweepIntervalMinutes'), 1))
        self.ctx.set_interval(guard(self.sweep, '后台推进失败 错误=%s'), sweep_interval * 60_000)
        if self.memory_config['enabled'] or self.schedule_preplan_config['enabled']:
            self.ctx.set_interval(
                guard(self.compact_stories, '后台整理失败 错误=%s'),
                max(1, self.memory_config['backgroundIntervalMinutes']) * 60_000,
            )
        if self.blind_mode_config['enabled']:
            self.ctx.set_interval(self.report_blind_mode_health, self.blind_mode_config['healthReportMinutes'] * 60_000)
        if self.sticker_config['enabled']:
            self.ctx.set_timeout(self.scan_sticker_library, 0)
            self.ctx.set_interval(self.scan_sticker_library, 5 * 60_000)
        self.report_standalone_operation(
            'standard', 'info', '后台调度已启动 剧本扫描=%d分钟 记忆扫描=%d分钟',
            sweep_interval, self.memory_config['backgroundIntervalMinutes'],
        )

    # ================= 上游 758-768：provider / desktop setter =================

    def set_narrator(self, provider: Any) -> None:
        """上游 758 setNarrator。"""
        self.narrator = provider

    def get_narrator(self) -> Any:
        """上游 759 getNarrator。"""
        return self.narrator

    def set_compactor(self, provider: Any) -> None:
        """上游 760 setCompactor。"""
        self.compactor = provider

    def set_embedder(self, provider: Any) -> None:
        """上游 762 setEmbedder（Allows a custom/local vector service without replacing the main narrator）。"""
        self.embedder = provider

    def set_desktop_event_sink(self, sink: Optional[Callable[[str, Any], None]] = None) -> None:
        """上游 765 setDesktopEventSink（普通 Koishi 不安装 sink）。"""
        self.desktop_event_sink = sink

    def set_desktop_delivery_handler(self, handler: Optional[Callable[..., Any]] = None) -> None:
        """上游 767 setDesktopDeliveryHandler（typ-0 bridge 安装/卸载后台投递通道）。"""
        self.desktop_delivery_handler = handler

    def get_desktop_runtime_phase(self) -> str:
        """上游 768 getDesktopRuntimePhase。"""
        return self.desktop_runtime_phase

    # ================= 上游 2246-2271：audioConfig / stickerConfig =================

    @property
    def audio_config(self) -> Dict[str, Any]:
        """上游 2246-2258 audioConfig（Normalized native-audio understanding config）。"""
        if self.cached_audio_config is not None:
            return self.cached_audio_config
        configured = (self.config.get('model') or {}).get('audio') or {}
        formats = ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr']
        out_format = configured.get('outFormat') if configured.get('outFormat') in formats else 'mp3'
        self.cached_audio_config = {
            'enabled': configured.get('enabled') is True,
            'outFormat': out_format,
            'maxFileSizeMB': max(1, min(25, math.floor(_number_or(configured.get('maxFileSizeMB'), 10)))),
            'maxPerMessage': max(1, min(3, math.floor(_number_or(configured.get('maxPerMessage'), 1)))),
        }
        return self.cached_audio_config

    @property
    def sticker_config(self) -> StickerLibraryConfig:
        """上游 2260-2271 stickerConfig。"""
        if self.cached_sticker_config is not None:
            return self.cached_sticker_config
        configured = self.config.get('stickers') or {}
        self.cached_sticker_config = {
            'enabled': configured.get('enabled') is True,
            'directory': str(configured.get('directory') or 'data/hds-interlude/stickers').strip(),
            'maxFileSizeMB': max(1, min(30, _number_or(configured.get('maxFileSizeMB'), 10))),
            'catalogLimit': max(1, min(80, math.floor(_number_or(configured.get('catalogLimit'), 40)))),
            'descriptionMaxTokens': max(256, min(4_096, math.floor(_number_or(configured.get('descriptionMaxTokens'), 768)))),
            'descriptionResponseFormat': 'prompt-only' if configured.get('descriptionResponseFormat') == 'prompt-only' else 'json-object',
        }
        return self.cached_sticker_config

    # ================= 上游 4211-4229：alter / agency / preplan / blind mode =================

    @property
    def alter_system_config(self) -> AlterSystemConfig:
        """上游 4211-4213 alterSystemConfig。"""
        if self.cached_alter_system_config is None:
            self.cached_alter_system_config = resolve_alter_system_config(self.config.get('alterSystem'))
        return self.cached_alter_system_config

    @property
    def agency_config(self) -> AgencyConfig:
        """上游 4215-4217 agencyConfig。"""
        if self.cached_agency_config is None:
            self.cached_agency_config = resolve_agency_config(self.config.get('agency'))
        return self.cached_agency_config

    @property
    def schedule_preplan_config(self) -> Dict[str, Any]:
        """上游 4219-4221 schedulePreplanConfig。"""
        if self.cached_schedule_preplan_config is None:
            self.cached_schedule_preplan_config = resolve_schedule_preplan_config(self.config.get('schedulePreplan'))
        return self.cached_schedule_preplan_config

    @property
    def blind_mode_config(self) -> BlindModeConfig:
        """上游 4223-4225 blindModeConfig（blindMode ?? blackBox）。"""
        if self.cached_blind_mode_config is None:
            configured = self.config.get('blindMode')
            if configured is None:
                configured = self.config.get('blackBox')
            self.cached_blind_mode_config = resolve_blind_mode_config(configured)
        return self.cached_blind_mode_config

    def emotional_offset_for_prompt(self, story: InterludeStory) -> Optional[EmotionalOffsetPrompt]:
        """上游 4227-4229 emotionalOffsetForPrompt。"""
        state = (story.get('state') or {}).get('alterSystem')
        return emotional_offset_for_prompt(normalize_alter_system_state(state), self.alter_system_config)

    # ================= 上游 5395-5417：autoAdvance / urge =================

    @property
    def auto_advance_config(self) -> AutoAdvanceConfig:
        """上游 5395-5409 autoAdvanceConfig。"""
        if self.cached_auto_advance_config is not None:
            return self.cached_auto_advance_config
        runtime = self.config.get('runtime') or {}
        self.cached_auto_advance_config = {
            'enabled': _nullish(runtime.get('autoAdvanceEnabled'), True),
            'intervalMinutes': max(1, _nullish_number(runtime.get('autoAdvanceIntervalMinutes'), 40)),
            'jitterMinutes': max(0, _nullish_number(runtime.get('autoAdvanceJitterMinutes'), 5)),
            'followUpMinutes': normalize_follow_up_minutes(runtime.get('conversationFollowUpMinutes')),
            'followUpJitterMinutes': max(0, min(10, _nullish_number(runtime.get('conversationFollowUpJitterMinutes'), 1))),
            'restWindows': _nullish(runtime.get('restWindows'), [{
                'enabled': True, 'label': 'night sleep', 'start': '23:00', 'end': '07:00',
                'minIntervalMinutes': 120, 'maxIntervalMinutes': 240,
            }]),
        }
        return self.cached_auto_advance_config

    @property
    def urge_config(self) -> Dict[str, Any]:
        """上游 5411 urgeConfig（上游不缓存）。"""
        return resolve_urge_config(self.config.get('urge'))

    @property
    def effective_urge_runtime(self) -> RuntimeConfig:
        """上游 5413-5417 effectiveUrgeRuntime。"""
        runtime = self.config.get('runtime') or {}
        if self.urge_config['enabled']:
            return {**runtime, 'proactiveWillingnessThreshold': self.urge_config['willingness']}
        return runtime

    # ================= 上游 5570-5587：sharedStoryConfig =================

    @property
    def shared_story_config(self) -> SharedStoryConfig:
        """上游 5570-5587 sharedStoryConfig。"""
        if self.cached_shared_story_config is not None:
            return self.cached_shared_story_config
        configured = _nullish(self.config.get('sharedStory'), {})
        # const { enabled: _legacyEnabled, ...overrides } = this.config.sharedStory ?? {}
        overrides = {key: value for key, value in configured.items() if key != 'enabled'}
        self.cached_shared_story_config = {
            # Beta2 deliberately keeps the single-story guard hard-enabled. Older
            # builds exposed a rollback switch here, but turning it off could create
            # fresh per-account stories that a later background sweep would revive.
            'enabled': True,
            'autoEnrollParticipants': True,
            'allowCrossConversationMessages': True,
            'shareParticipantDetails': False,
            'maxCrossConversationActions': 1,
            'participantContextLimit': 6,
            'managerAccounts': [],
            'participantPresets': [],
            **overrides,
        }
        return self.cached_shared_story_config

    # ================= 上游 5755-5838：memoryConfig / browserConfig =================

    @property
    def memory_config(self) -> MemoryConfig:
        """上游 5755-5800 memoryConfig。"""
        if self.cached_memory_config is not None:
            return self.cached_memory_config
        runtime = self.config.get('runtime') or {}
        # 保持 memory 为可选配置，方便从旧版本配置平滑升级；未填写时使用保守默认值。
        self.cached_memory_config = {
            'enabled': True,
            'backgroundIntervalMinutes': 10,
            'maxStoriesPerCompactionRun': runtime.get('maxStoriesPerSweep'),
            'sceneEntryThreshold': 16,
            'sceneCharacterThreshold': 10_000,
            'compactionEntryLimit': 80,
            'compactionCharacterLimit': 32_000,
            'sceneHookCharacters': 2_000,
            'sceneSummaryCharacters': 8_000,
            'arcSummaryCharacters': 12_000,
            'previousSceneSummaries': 2,
            'recentEntryLimit': runtime.get('contextEntryLimit'),
            'factLimit': runtime.get('memoryLimit'),
            'factContentCharacters': 4_000,
            'factImportanceWeight': 0.5,
            'factConfidenceWeight': 0.35,
            'factRecencyWeight': 0.15,
            'semanticWeight': 0.55,
            'unresolvedWeight': 0.2,
            'statePatchConfidenceThreshold': 0.82,
            'majorStatePatchConfidenceThreshold': 0.95,
            'statePatchMinEvidence': 3,
            'statePatchMinTurns': 3,
            'statePatchMinDays': 2,
            'statePatchCooldownHours': 72,
            'autoApplyStatePatches': True,
            'allowMajorStateChanges': True,
            'maxFactsPerStory': 200,
            'activeConsequencesEnabled': True,
            'activeConsequencePromptLimit': 6,
            'activeConsequenceMaxDays': 7,
            'activeConsequenceDefaultStrength': 0.55,
            'overlayCompressionEnabled': True,
            'overlayRecentDays': 2,
            'overlayMonthlyAfterDays': 10,
            'overlayWeeklyWindowDays': 5,
            'overlayMonthlyWindowDays': 10,
            'overlayWeeklySummaryCharacters': 1_600,
            'overlayMonthlySummaryCharacters': 2_400,
            **(_nullish(self.config.get('memory'), {})),
        }
        return self.cached_memory_config

    @property
    def browser_config(self) -> BrowserConfig:
        """上游 5802-5838 browserConfig（区间 5755-5830 在函数中间截断，这里补全）。"""
        if self.cached_browser_config is not None:
            return self.cached_browser_config
        merged: Dict[str, Any] = {
            'enabled': False,
            'mode': 'deferred-only',
            'allowSearch': True,
            'allowVisit': True,
            'searchUrlTemplate': 'https://html.duckduckgo.com/html/?q={query}',
            'allowedDomains': [],
            'blockedDomains': [],
            'maxConcurrentPages': 1,
            'maxResearchPerSweep': 1,
            'navigationTimeout': 15_000,
            'waitUntil': 'domcontentloaded',
            'maxTextCharacters': 12_000,
            'maxExcerptCharacters': 3_000,
            'maxObservationsInPrompt': 4,
            'cacheMinutes': 30,
            'allowGroupTriggeredResearch': False,
            'logObservationPreview': False,
            **(_nullish(self.config.get('browser'), {})),
        }
        # Schema defaults cover Console input, but old YAML and programmatic
        # callers can still provide undefined/invalid numeric fields. Normalise
        # here so one malformed browser option cannot silently disable all due
        # research or break the page semaphore.
        self.cached_browser_config = {
            **merged,
            'maxConcurrentPages': max(1, min(4, _number_or(merged.get('maxConcurrentPages'), 1))),
            'maxResearchPerSweep': max(1, min(20, _number_or(merged.get('maxResearchPerSweep'), 1))),
            'navigationTimeout': max(1_000, _number_or(merged.get('navigationTimeout'), 15_000)),
            'maxTextCharacters': max(500, _number_or(merged.get('maxTextCharacters'), 12_000)),
            'maxExcerptCharacters': max(200, _number_or(merged.get('maxExcerptCharacters'), 3_000)),
            'maxObservationsInPrompt': max(1, min(20, _number_or(merged.get('maxObservationsInPrompt'), 4))),
            'cacheMinutes': max(0, _number_or(merged.get('cacheMinutes'), 0)),
        }
        return self.cached_browser_config

    # ================= 上游 6724-6790：日志 =================

    def report(self, level: str, story: InterludeStory, phase: str, message: str, *args: Any) -> None:
        """上游 6724-6726 report。"""
        self.write_report(level, story, phase, message, args)

    def report_operation(self, verbosity: str, level: str, story: InterludeStory, phase: str, message: str, *args: Any) -> None:
        """上游 6731-6734 reportOperation（Summary 结果 / standard 调度 / diagnostic 内部原因）。"""
        if not self.allows_verbosity(verbosity):
            return
        self.write_report(level, story, phase, message, args)

    def write_report(self, level: str, story: InterludeStory, phase: str, message: str, args: Sequence[Any]) -> None:
        """上游 6736-6755 writeReport。"""
        if self.blind_mode_config['enabled']:
            if level == 'error' or level == 'warn':
                self.blind_mode_health_issue = True
            return
        rank = {'silent': 0, 'error': 1, 'warn': 2, 'info': 3, 'debug': 4}
        logging_config = _nullish(self.config.get('logging'), {
            'level': 'info', 'format': 'layered', 'colors': True, 'colorTheme': 'dark',
            'kaomoji': True, 'logScriptPreview': False, 'previewLength': 500,
        })
        if rank.get(logging_config.get('level'), 0) < rank.get(level, 0):
            return
        rendered = render_log_message(message, list(args))
        character_name = (((story or {}).get('setting') or {}).get('character') or {}).get('name')
        story_detail = (' 故事=%s' % story.get('id')) if _nullish(logging_config.get('verbosity'), 'standard') == 'diagnostic' else ''
        log_format = logging_config.get('format')
        if log_format == 'layered':
            output = format_layered_log({
                'level': level, 'phase': phase, 'protagonist': character_name, 'message': message, 'args': list(args),
                'colors': logging_config.get('colors') is not False,
                'colorTheme': _nullish(logging_config.get('colorTheme'), 'dark'),
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        elif log_format == 'compact':
            output = '[%s] %s %s%s' % (phase_label(phase), character_name, rendered, story_detail)
        else:
            output = '[%s] %s\n事件：%s%s' % (phase_label(phase), character_name, rendered, story_detail)
        self.emit_log(level, output)

    def report_standalone(self, level: str, message: str, *args: Any) -> None:
        """上游 6757-6759 reportStandalone。"""
        self.write_standalone(level, message, args)

    def report_token_usage(self, record: Dict[str, Any]) -> None:
        """上游 6763-6768 reportTokenUsage（TokenUsageRecord 类型缺失，用 dict 标注）。"""
        sink = self.desktop_event_sink
        if sink is not None:
            sink('token', record)
        line = format_token_usage_line(record)
        if not line:
            return
        self.report_standalone('info', 'Token 用量[%s] 模型=%s %s', record.get('task'), record.get('model'), line)

    def report_standalone_operation(self, verbosity: str, level: str, message: str, *args: Any) -> None:
        """上游 6770-6773 reportStandaloneOperation。"""
        if not self.allows_verbosity(verbosity):
            return
        self.write_standalone(level, message, args)

    def write_standalone(self, level: str, message: str, args: Sequence[Any]) -> None:
        """上游 6775-6790 writeStandalone。"""
        if self.blind_mode_config['enabled']:
            if level == 'error' or level == 'warn':
                self.blind_mode_health_issue = True
            return
        rank = {'silent': 0, 'error': 1, 'warn': 2, 'info': 3, 'debug': 4}
        logging_config = _nullish(self.config.get('logging'), {
            'level': 'info', 'format': 'layered', 'colors': True, 'colorTheme': 'dark', 'kaomoji': True,
        })
        if rank.get(logging_config.get('level'), 0) < rank.get(level, 0):
            return
        if logging_config.get('format') == 'layered':
            output = format_layered_log({
                'level': level, 'protagonist': 'HDSI', 'message': message, 'args': list(args), 'standalone': True,
                'colors': logging_config.get('colors') is not False,
                'colorTheme': _nullish(logging_config.get('colorTheme'), 'dark'),
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        else:
            output = '[系统] %s' % render_log_message(message, list(args))
        self.emit_log(level, output)

    def emit_log(self, level: str, output: str) -> None:
        """上游 6811-6816 emitLog（serviceLogger 直写）。"""
        if level == 'error':
            self.service_logger.error(output)
        elif level == 'warn':
            self.service_logger.warning(output)
        elif level == 'info':
            self.service_logger.info(output)
        else:
            self.service_logger.debug(output)

    def report_blind_mode_health(self) -> None:
        """上游 6818-6825 reportBlindModeHealth（失明模式唯一的 HDSI 记录）。"""
        status = '需关注' if (self.blind_mode_health_issue or self.database_resetting) else '正常'
        scheduler = '运行中' if self.background_started else '未就绪'
        # This is the sole HDSI record emitted in Blind Mode. It deliberately
        # carries no story, account, model, message, or failure-detail content.
        self.service_logger.info('[失明模式] 运行状态=%s 后台任务=%s', status, scheduler)
        self.blind_mode_health_issue = False

    def allows_verbosity(self, required: str) -> bool:
        """上游 6827-6831 allowsVerbosity。"""
        rank = {'summary': 1, 'standard': 2, 'diagnostic': 3}
        configured = _nullish((self.config.get('logging') or {}).get('verbosity'), 'standard')
        return rank.get(configured, 0) >= rank.get(required, 0)

    # ================= 上游 6792-6809：continuity / 压缩 facts =================

    def resolve_compaction_facts(self, story_id: str, value: Any, allowed_ids: Any, now: datetime) -> bool:
        """上游 6792-6802 resolveCompactionFacts（`allowedIds: ReadonlySet<number>`）。"""
        ids: List[int] = []
        if isinstance(value, (list, tuple)):
            seen: set = set()
            for item in value:
                if _is_safe_integer(item) and item > 0 and item in allowed_ids and item not in seen:
                    seen.add(item)
                    ids.append(item)
            ids = ids[:20]
        if not ids:
            return False
        facts = self.db_get('interlude_fact', {'storyId': story_id, 'id': {'$in': ids}, 'status': 'active'})
        unresolved = [fact for fact in facts if fact.get('unresolved')]
        if not unresolved:
            return False
        self.db_set(
            'interlude_fact',
            {'id': {'$in': [fact.get('id') for fact in unresolved]}},
            {'unresolved': False, 'lastSeenAt': now, 'updatedAt': now},
        )
        return True

    def mark_continuity_dirty(self, story_id: str, now: datetime) -> None:
        """上游 6804-6809 markContinuityDirty。"""
        story = self.get_story(story_id)
        state = decode_story_state(story.get('state'))
        if state.get('continuityDirty'):
            return
        self.db_set(
            'interlude_story',
            {'id': story_id},
            {'state': encode_story_state({**state, 'continuityDirty': True}), 'updatedAt': now},
        )

    # ================= 上游 6833-6861：getStory / serial =================

    def get_story(self, id: str) -> InterludeStory:
        """上游 6833-6849 getStory。"""
        rows = self.db_get('interlude_story', {'id': id})
        story = rows[0] if rows else None
        if not story:
            raise Exception('Interlude story not found: %s' % id)
        if story.get('id') not in self.reported_state_migrations:
            self.reported_state_migrations.add(story.get('id'))
            inspection = inspect_story_state_migration(
                story.get('state'),
                (self.config.get('storyDefaults') or {}).get('perspective', ''),
            )
            if inspection.get('perspectiveDefaultAvailable') and not (((story.get('setting') or {}).get('perspective') or '').strip()):
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    '状态迁移提示：当前故事未持久化 Perspective，但 Console 默认值可用；本阶段只报告，不自动改写 Canon',
                )
            if inspection.get('unknownKeys'):
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    '状态迁移保留未知扩展字段 数量=%d', len(inspection['unknownKeys']),
                )
        return story

    def serial(self, id: str, task: Callable[[], Any]) -> Any:
        """上游 6851-6861 serial：同一故事串行执行。

        上游用 Promise 队列（previous.catch(...).then(task)，finally 删除队列）。
        这里用 `self.queues: Dict[str, threading.RLock]` + 直接同步执行：
        - 同一 id 的调用严格串行；
        - `with` 在 task 抛异常时同样释放锁，保证前一次失败不会堵住队列
          （对应上游 `.catch(() => undefined)` 与两个 then 分支的释放语义）。
        """
        lock = self.queues.get(id)
        if lock is None:
            lock = threading.RLock()
            self.queues[id] = lock
        with lock:
            return task()

    # ================= 上游 6863-7006：数据库封装 =================

    def _db_write(self, task: Callable[[], Any]) -> Any:
        """上游 6863-6867 dbWrite：全局写队列（SQLite 单写连接）。"""
        with self.database_write_queue:
            return self._retry_db_write(task)

    def _db_read(self, task: Callable[[], Any]) -> Any:
        """上游 6874-6891 dbRead：读取允许并发，仅对瞬时驱动错误做有界重试。"""
        delays = [50, 125, 250]
        attempt = 0
        while True:
            try:
                return task()
            except Exception as error:  # noqa: BLE001 - 需要识别瞬时错误
                if attempt >= len(delays) or not is_transient_database_error(error):
                    if is_transient_database_error(error):
                        self.report_standalone('warn', 'SQLite 读取连续失败，已停止重试 错误=%s', error)
                    raise
                delay = delays[attempt] + int(random.random() * 25)
                self.report_standalone_operation(
                    'diagnostic', 'debug',
                    'SQLite 读取暂时失败，准备重试 等待=%dms 次数=%d 错误=%s', delay, attempt + 1, error,
                )
                time.sleep(delay / 1000.0)
                attempt += 1

    def db_get(self, table: str, query: Any, options: Any = None) -> List[Dict[str, Any]]:
        """上游 6893-6898 dbGet。

        normalizeDatabaseRow 已由 hdsi/store.py 在读出时统一处理，这里不再实现。
        """
        return self._db_read(lambda: self.store.get(table, query, options))

    def repair_canonical_one_bot_story_transport(self, story: InterludeStory, session: Any) -> InterludeStory:
        """上游 6900-6912 repairCanonicalOneBotStoryTransport。

        Repair only a stale canonical story whose configured bot is no longer
        online. A live OneBot session is stronger evidence than historical story
        metadata, while a still-online story bot remains untouched.
        """
        if not is_onebot_platform(getattr(session, 'platform', None)) or not getattr(session, 'selfId', ''):
            return story
        # 上游 `this.ctx.bots.some(...)` → self.platform.list_bots()。
        has_live_story_bot = any(
            str(getattr(bot, 'selfId', '')) == str(story.get('selfId'))
            and (
                getattr(bot, 'platform', None) == story.get('platform')
                or (is_onebot_platform(getattr(bot, 'platform', None)) and is_onebot_platform(story.get('platform')))
            )
            for bot in self.platform.list_bots()
        )
        if has_live_story_bot or (
            story.get('platform') == getattr(session, 'platform', None)
            and str(story.get('selfId')) == str(getattr(session, 'selfId', ''))
        ):
            return story
        now = now_utc()
        self.db_set(
            'interlude_story', {'id': story.get('id')},
            {'platform': session.platform, 'selfId': session.selfId, 'updatedAt': now},
        )
        self.report_standalone('warn', '主剧本投递账号已自愈 故事=%s 平台=%s 账号=%s', story.get('id'), session.platform, session.selfId)
        return {**story, 'platform': session.platform, 'selfId': session.selfId, 'updatedAt': now}

    def _retry_db_write(self, task: Callable[[], Any]) -> Any:
        """上游 6914-6937 retryDbWrite：有界重试 + 抖动，最后一次失败只警告一次。"""
        delays = [100, 250, 500, 1_000, 2_000, 3_000, 5_000]
        attempt = 0
        while True:
            try:
                return task()
            except Exception as error:  # noqa: BLE001 - 需要识别瞬时错误
                # sql.js/SQLite may briefly report disk I/O or locking errors while
                # Koishi flushes its in-memory database. A short retry is useful, but
                # logging every transient attempt as a warning makes normal file
                # flush contention look like a fatal HDSI failure.
                if attempt >= 7 or not is_transient_database_error(error):
                    if is_transient_database_error(error):
                        self.report_standalone('warn', 'SQLite 写入连续失败，已停止重试 错误=%s', error)
                    raise
                base_delay = delays[attempt] if attempt < len(delays) else 5_000
                delay = base_delay + int(random.random() * min(250, base_delay // 4))
                self.report_standalone_operation(
                    'diagnostic', 'debug',
                    'SQLite 写入暂时失败，准备重试 等待=%dms 次数=%d 错误=%s', delay, attempt + 1, error,
                )
                time.sleep(delay / 1000.0)
                attempt += 1

    def db_create(self, table: str, data: Any) -> Any:
        """上游 6939-6954 dbCreate。"""
        def task() -> Any:
            try:
                return self.store.create(table, data)
            except Exception as error:  # noqa: BLE001 - 需要识别瞬时错误
                # sql.js can report disk I/O after SQLite has already committed an
                # INSERT. Before retrying, look for the same logical row; this keeps a
                # transient flush error from creating duplicate split-message intents,
                # script entries, or memories.
                if not is_transient_database_error(error):
                    raise
                existing = self.find_possibly_committed_create(table, data)
                if existing:
                    return existing
                raise
        return self._db_write(task)

    def find_possibly_committed_create(self, table: str, data: Any) -> Optional[Dict[str, Any]]:
        """上游 6956-6984 findPossiblyCommittedCreate。"""
        if not is_record(data):
            return None
        story_id = data.get('storyId') if isinstance(data.get('storyId'), str) else ''
        if not story_id:
            return None
        rows = self.db_get(table, {'storyId': story_id}, {'limit': 100})
        for row in rows:
            if table == 'interlude_intent':
                if (
                    row.get('participantId') == data.get('participantId')
                    and row.get('type') == data.get('type')
                    and row.get('summary') == data.get('summary')
                    and same_timestamp(row.get('notBefore'), data.get('notBefore'))
                    and json_equal(row.get('payload') or {}, data.get('payload') or {})
                ):
                    return row
            elif table == 'interlude_script_entry':
                if (
                    row.get('participantId') == data.get('participantId')
                    and row.get('kind') == data.get('kind')
                    and row.get('actor') == data.get('actor')
                    and row.get('content') == data.get('content')
                    and same_timestamp(row.get('occurredAt'), data.get('occurredAt'))
                ):
                    return row
            elif table == 'interlude_memory':
                if (
                    row.get('participantId') == data.get('participantId')
                    and row.get('category') == data.get('category')
                    and row.get('content') == data.get('content')
                    and same_timestamp(row.get('createdAt'), data.get('createdAt'))
                ):
                    return row
            elif isinstance(data.get('id'), str) and row.get('id') == data.get('id'):
                return row
        return None

    def db_set(self, table: str, query: Any, data: Any) -> None:
        """上游 6986-6988 dbSet。"""
        self._db_write(lambda: self.store.set(table, query, data))

    def db_remove(self, table: str, query: Any) -> None:
        """上游 6990-6992 dbRemove。"""
        self._db_write(lambda: self.store.remove(table, query))

    def purge_table(self, table: str, query: Any, fallback: Any) -> None:
        """上游 6994-7006 purgeTable：物理 DELETE 失败时退回逻辑删除（redaction）。"""
        try:
            self.db_remove(table, query)
        except Exception as error:  # noqa: BLE001 - 物理删除失败本身就是降级分支
            self.report_standalone('warn', 'SQLite 物理删除失败，改用逻辑删除 表=%s 错误=%s', table, error)
            self.db_set(table, query, fallback)
