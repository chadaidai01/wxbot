# -*- coding: utf-8 -*-
"""HDS-Interlude 服务层配置类型契约。

上游：src/service.ts 行号 65-620（Config 及各子配置 interface / type）。
本文件只做类型声明，不包含任何逻辑：
`export interface X` → `class X(TypedDict, total=False)`；
TS 可选字段（`?:`）由 total=False 覆盖（Python 3.9 没有 NotRequired）。
字段名与上游 JSON key 完全一致（camelCase），不要改名；
TS 保留字字段（如 `from`）在 TypedDict 里写成 `from_` 仅作占位注释，
真实数据 key 仍按上游是 `from`（见 hdsi/types.py 的同款处理）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Set, TypedDict, Union

from .group_willingness import GroupWillingnessConfig
from .narrator_types import ModelConfig
from .platform.session import InboundSession  # 上游 Koishi Session 的项目对应物
from .schedule_preplan import SchedulePreplanConfig
from .script.recall_navigation import RecallSpan
from .types import (
    AgencyConfig,
    AlterSystemConfig,
    ChatReactionName,
    ChatRhythmConfig,
    CompactionRequest,
    GroupMessageContext,
    InterludeParticipant,
    InterludeScene,
    InterludeStory,
    MessageReactionDraft,
    NarrativeFact,
    NarrativeIntent,
    NativeFaceSemantic,
    QuotedMessageContext,
    ScriptEntry,
)
from .urge import UrgeConfig

# 上游 65：export type DesktopTimelineTrack = 'script' | 'messages' | 'system' | 'scenes' | 'facts' | 'preplan'
DesktopTimelineTrack = str


class DesktopTimelineRangeRequest(TypedDict, total=False):
    """上游 66-74。"""

    from_: str  # 上游字段名 from（Python 保留字；数据 key 仍是 'from'）
    to: str
    tracks: List[DesktopTimelineTrack]
    # Opaque oldest-entry cursor returned by the preceding response.
    cursor: str
    detailLevel: str  # 'summary' | 'full'
    limit: int


class HistoryVectorCheckpoint(TypedDict, total=False):
    """上游 180 HistoryVectorEntry.checkpoint 内联类型。"""

    sceneId: int
    firstEntryId: int
    lastEntryId: int


class HistoryVectorEntry(TypedDict, total=False):
    """上游 175-186（type HistoryVectorEntry）。"""

    spans: List[RecallSpan]  # 上游 RecallSpan[]（script/recall-navigation.ts）
    embeddingIdentity: str
    tags: List[str]
    frameId: str
    checkpoint: HistoryVectorCheckpoint
    vector: List[float]
    content: str
    occurredAt: str
    participantId: str
    kind: str


# ===== 上游 225-250：压缩准备的内部结构 =====

class PreparedCompactionSkip(TypedDict, total=False):
    """上游 225-228。"""

    phase: str  # 'skip'
    overlayCompacted: bool


class PreparedCompactionRun(TypedDict, total=False):
    """上游 230-243。"""

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


class CompactionBackoff(TypedDict, total=False):
    """上游 245-248。"""

    fingerprint: str
    until: float


# 上游 250：type PreparedCompaction = PreparedCompactionSkip | PreparedCompactionRun
PreparedCompaction = Union[PreparedCompactionSkip, PreparedCompactionRun]


# ===== 上游 260-620：Config 及各子配置 =====

class Config(TypedDict, total=False):
    """上游 260-285。"""

    blindMode: BlindModeConfig
    blackBox: BlindModeConfig  # @deprecated Renamed to blindMode; retained for existing Console YAML.
    model: ModelConfig
    runtime: RuntimeConfig
    storyDefaults: StoryDefaults
    logging: LoggingConfig
    memory: MemoryConfig
    sharedStory: SharedStoryConfig
    browser: BrowserConfig
    onebot: OneBotNapCatConfig
    chatActions: ChatActionsConfig
    stickers: StickerLibraryConfig
    alterSystem: AlterSystemConfig  # 上游 AlterSystemConfig（alter.ts → types.ts）
    chatRhythm: ChatRhythmConfig
    timelineDirector: TimelineDirectorConfig
    agency: AgencyConfig
    urge: UrgeConfig
    schedulePreplan: SchedulePreplanConfig


class TimelineDirectorConfig(TypedDict, total=False):
    """上游 281 Config.timelineDirector 内联类型 `{ enabled: boolean }`。"""

    enabled: bool


class BlindModeConfig(TypedDict, total=False):
    """上游 287-291。"""

    enabled: bool
    # Periodic, intentionally minimal health signal while all other HDSI logs stay hidden.
    healthReportMinutes: int


class OneBotAccountRule(TypedDict, total=False):
    """上游 295-303。QQ ids 用字符串（可能超出 JS safe integer）。"""

    qq: str
    label: str
    enabled: bool
    # Optional identity fields for a whitelisted private-message user.
    personId: str
    profile: str
    relationship: str


class OneBotNapCatConfig(TypedDict, total=False):
    """上游 305-317。"""

    # When false (or omitted for old configurations), OneBot access is unchanged.
    enabled: bool
    # NapCat accounts that are allowed to send the character's messages.
    botAccounts: List[OneBotAccountRule]
    userMode: str  # 'allowlist' | 'blocklist' @deprecated
    userAccounts: List[OneBotAccountRule]
    # Explicit OneBot group allowlist. Group members do not need DM whitelist access.
    groupChats: List[GroupChatRule]
    # Prevent an echoed self-message from entering the narrative.
    ignoreSelfMessages: bool


class ChatActionsConfig(TypedDict, total=False):
    """上游 319-328。"""

    enabled: bool
    platforms: List[str]  # 'qq' | 'wechat'
    quoteReply: bool
    messageReactions: bool
    allowedReactions: List[ChatReactionName]
    nativeFaces: bool
    expressionThreshold: float
    allowedNativeFaces: List[NativeFaceSemantic]


class StickerLibraryConfig(TypedDict, total=False):
    """上游 330-338。"""

    enabled: bool
    directory: str
    maxFileSizeMB: float
    catalogLimit: int
    descriptionMaxTokens: int
    # API JSON mode is optional; prompt-only still asks for the compact JSON contract.
    descriptionResponseFormat: str  # 'json-object' | 'prompt-only'


class GroupChatRule(TypedDict, total=False):
    """上游 340-351。"""

    groupId: str
    label: str
    enabled: bool
    purpose: str
    characterRole: str
    responseMode: str  # 'mention-only' | 'always'
    contextLimit: int
    debounceSeconds: int
    cooldownSeconds: int
    willingness: GroupWillingnessConfig  # 上游 Partial<GroupWillingnessConfig>（group-willingness.ts）


class MemoryConfig(TypedDict, total=False):
    """上游 353-401。"""

    enabled: bool
    backgroundIntervalMinutes: int
    maxStoriesPerCompactionRun: int
    sceneEntryThreshold: int
    sceneCharacterThreshold: int
    compactionEntryLimit: int
    compactionCharacterLimit: int
    sceneHookCharacters: int
    sceneSummaryCharacters: int
    arcSummaryCharacters: int
    # How many immediately-preceding closed scene summaries join the prompt.
    previousSceneSummaries: int
    recentEntryLimit: int
    factLimit: int
    factContentCharacters: int
    factImportanceWeight: float
    factConfidenceWeight: float
    factRecencyWeight: float
    semanticWeight: float
    unresolvedWeight: float
    statePatchConfidenceThreshold: float
    majorStatePatchConfidenceThreshold: float
    statePatchMinEvidence: int
    # Minimum independent narrative turns for a minor overlay change.
    statePatchMinTurns: int
    # Minimum distinct calendar days represented by minor-patch evidence.
    statePatchMinDays: int
    # Cooldown between stable overlay changes on the same target/path.
    statePatchCooldownHours: int
    autoApplyStatePatches: bool
    allowMajorStateChanges: bool
    maxFactsPerStory: int
    # Keep short-lived dramatic aftereffects as context for later writing.
    activeConsequencesEnabled: bool
    # Maximum active consequences carried into one main-narrative prompt.
    activeConsequencePromptLimit: int
    # Longest permitted lifetime of one consequence; protects canon from drift.
    activeConsequenceMaxDays: int
    # Used when the narrator omits a precise strength for a valid consequence.
    activeConsequenceDefaultStrength: float
    overlayCompressionEnabled: bool
    overlayRecentDays: int
    overlayMonthlyAfterDays: int
    overlayWeeklyWindowDays: int
    overlayMonthlyWindowDays: int
    overlayWeeklySummaryCharacters: int
    overlayMonthlySummaryCharacters: int


class RuntimeConfig(TypedDict, total=False):
    """上游 403-447。"""

    captureDirectMessages: bool
    autoCreate: bool
    ignoreCommandMessages: bool
    allowProactiveMessages: bool
    # Minimum narrator-declared willingness for a background-initiated contact.
    proactiveWillingnessThreshold: float
    sweepIntervalMinutes: int
    minimumAdvanceMinutes: int
    maxStoriesPerSweep: int
    contextEntryLimit: int
    # Preserve raw entries from this recent time window in addition to the count floor.
    contextTimeWindowMinutes: float
    memoryLimit: int
    maxScriptCharacters: int
    maxMessageCharacters: int
    minimumDelayedReplySeconds: int
    maximumDelayedReplyMinutes: int
    cancelDelayedRepliesOnUserMessage: bool
    # Retry a user turn after a transient narrative-provider failure.
    narrativeRetryDelaySeconds: float
    # Maximum automatic retries per failed user turn; 0 disables retry.
    narrativeRetryMaxAttempts: int
    # Split model reply.content into multiple QQ messages at the configured separator.
    splitReplyMessages: bool
    messageSeparator: str
    typingBaseDelaySeconds: float
    typingCharactersPerSecond: float
    typingMaxDelaySeconds: float
    # Random variation applied to simulated typing delays; 0 keeps deterministic timing.
    typingJitterRatio: float
    # Wait after the newest user message before starting a writing request.
    userMessageDebounceSeconds: float
    # @deprecated Ignored since 0.1.2; requests remain replaceable until the first reply is committed.
    staleNarrativeRequestWindowSeconds: float
    # 新版自动推进调度；旧版 minimumAdvanceMinutes 仍保留兼容。
    autoAdvanceEnabled: bool
    autoAdvanceIntervalMinutes: float
    autoAdvanceJitterMinutes: float
    # Short life-writing passes after a conversation, in minutes.
    conversationFollowUpMinutes: List[int]
    # Small random offset applied to each short conversation follow-up.
    conversationFollowUpJitterMinutes: float
    restWindows: List[RestWindow]


class BrowserConfig(TypedDict, total=False):
    """上游 449-469。"""

    enabled: bool
    # Immediate browsing is opt-in because it intentionally adds one more model/browser round trip.
    mode: str  # 'deferred-only' | 'allow-immediate'
    allowSearch: bool
    allowVisit: bool
    searchUrlTemplate: str
    allowedDomains: List[str]
    blockedDomains: List[str]
    maxConcurrentPages: float
    # Bound work per background sweep so a backlog cannot hold the story queue for minutes.
    maxResearchPerSweep: float
    navigationTimeout: float
    waitUntil: str  # 'domcontentloaded' | 'networkidle2'
    maxTextCharacters: float
    maxExcerptCharacters: float
    maxObservationsInPrompt: float
    cacheMinutes: float
    allowGroupTriggeredResearch: bool
    logObservationPreview: bool


class ParticipantPreset(TypedDict, total=False):
    """上游 471-479：Console presets that turn QQ accounts into named relationship branches。"""

    qq: str
    personId: str
    label: str
    profile: str
    relationship: str
    enabled: bool


class SharedStoryConfig(TypedDict, total=False):
    """上游 481-499。"""

    # One main story per bot account. Kept configurable for a safe rollback.
    enabled: bool
    # Enroll an allowed account into an existing main story on its first DM.
    autoEnrollParticipants: bool
    # Allow one incoming message to cause an explicitly justified message to another account.
    allowCrossConversationMessages: bool
    # Send other participants' relationship/profile details to the model provider.
    shareParticipantDetails: bool
    # Hard cap for cross-account messages produced by one narrative turn.
    maxCrossConversationActions: int
    # Number of other relationship summaries sent to the main narrator.
    participantContextLimit: int
    # Empty keeps legacy behaviour; otherwise only these QQs may run global management commands.
    managerAccounts: List[str]
    # Optional QQ-to-person presets; accounts with the same personId share identity notes.
    # @deprecated Use onebot.userAccounts identity fields in new configs.
    participantPresets: List[ParticipantPreset]


class RestWindow(TypedDict, total=False):
    """上游 501-508。"""

    enabled: bool
    label: str
    start: str
    end: str
    minIntervalMinutes: int
    maxIntervalMinutes: int


class AutoAdvanceConfig(TypedDict, total=False):
    """上游 510-517（上游未 export，本包内复用）。"""

    enabled: bool
    intervalMinutes: float
    jitterMinutes: float
    followUpMinutes: List[int]
    followUpJitterMinutes: float
    restWindows: List[RestWindow]


class BufferedUserMessage(TypedDict, total=False):
    """上游 519-528。"""

    content: str
    occurredAt: datetime
    supersededIntents: List[NarrativeIntent]
    quote: QuotedMessageContext
    # Short-lived source links only; never written to HDSI storage.
    imageSources: List[str]
    # Short-lived voice record tokens/URLs only; never written to HDSI storage.
    audioSources: List[str]


class BufferedNarrativeTurn(TypedDict, total=False):
    """上游 530-543：A per-relationship input buffer。"""

    storyId: str
    participantId: str
    messages: List[BufferedUserMessage]
    latestSession: InboundSession  # 上游 Koishi Session
    # Context timers return a disposer rather than Node's native Timeout.
    timer: Callable[[], Any]
    nextRevision: int
    inFlightRequestId: int
    firstMessageCommittedRequestId: int
    obsoleteRequestIds: Set[int]


class BufferedGroupTurn(TypedDict, total=False):
    """上游 545-556。"""

    storyId: str
    groupId: str
    rule: GroupChatRule
    channelId: str
    latestSession: InboundSession  # 上游 Koishi Session
    messages: List[GroupMessageContext]
    timer: Callable[[], Any]
    revision: int
    mentionedBot: bool
    quotedBot: bool


class ExecutableMessageReaction(MessageReactionDraft, total=False):
    """上游 558-560：export interface ExecutableMessageReaction extends MessageReactionDraft。"""

    messageId: str


class ExecutableGroupChatActions(TypedDict, total=False):
    """上游 562-565。"""

    replyTo: Dict[str, str]  # { messageRef: string, messageId: string }
    reactions: List[ExecutableMessageReaction]


class GroupDeliverySegmentOutcome(TypedDict, total=False):
    """上游 570 segmentOutcomes 内联类型。"""

    index: int
    content: str
    status: str  # 'delivered' | 'failed'
    reason: str


class GroupDeliveryResult(TypedDict, total=False):
    """上游 567-571。"""

    deliveredSegments: List[str]
    complete: bool
    segmentOutcomes: List[GroupDeliverySegmentOutcome]


class DueIntentWake(TypedDict, total=False):
    """上游 573-576。取消句柄 + 到期毫秒时间戳。"""

    cancel: Callable[[], Any]
    dueAt: float


class StoryDefaults(TypedDict, total=False):
    """上游 578-589。"""

    characterName: str
    characterProfile: str
    perspective: str
    userProfile: str
    relationship: str
    world: str
    supportingCast: str
    location: str
    style: str
    timezone: str


class LoggingConfig(TypedDict, total=False):
    """上游 591-606。"""

    level: str  # 'silent' | 'error' | 'warn' | 'info' | 'debug'
    # Controls how much normal operational activity is written at info level.
    verbosity: str  # 'summary' | 'standard' | 'diagnostic'
    format: str  # 'compact' | 'detailed' | 'layered'
    # Apply semantic ANSI colors; Koishi Console and normal terminals render them.
    colors: bool
    # Select a high-contrast ANSI palette for dark or light Console themes.
    colorTheme: str  # 'dark' | 'light'
    # Show fixed action kaomoji; false uses compact symbols instead.
    kaomoji: bool
    logScriptPreview: bool
    # Emit user-visible incoming/outgoing message bodies to the plugin log.
    logMessageContent: bool
    previewLength: int


class SessionFileFact(TypedDict, total=False):
    name: str
    url: str
    size: int
    audio: bool


class StoryStartReadinessPreview(TypedDict, total=False):
    """上游 613-621 StoryStartReadiness.preview 内联类型。"""

    characterName: str
    characterProfile: bool
    perspective: bool
    world: bool
    timezone: str
    model: str
    autoCreate: bool


class StoryStartReadiness(TypedDict, total=False):
    """上游 608-622。"""

    ready: bool
    existing: InterludeStory
    blockers: List[str]
    warnings: List[str]
    preview: StoryStartReadinessPreview
