# -*- coding: utf-8 -*-
"""HDS-Interlude 配置模型（Python 移植）。

上游文件：`.hdsi_reference/src/index.ts`（HDS-Interlude 1.0.1-beta6-rebuild，919 行）
  · 配置 Schema 与默认值：第 18-490 行
    （defaultProvider / ProviderIdentity / ProviderAssignments / Provider / Failover / Embedding /
     Vision / AudioUnderstanding / Model / RestWindowSchema / Runtime / BlindMode / SchedulePreplan /
     Agency / ChatRhythm / AlterSystem / Browser / Memory / StoryDefaults / Logging /
     OneBotBotAccount / OneBotUserAccount / GroupWillingness / GroupChatRuleSchema / OneBot /
     ChatActions / Stickers / SharedStory / UrgeAdvanced / Config）。
  · 顶层 Config 的段列表与顺序：第 461-489 行（共 17 段）。
interface 逐字抄自：
  · `.hdsi_reference/src/service.ts`：Config 260、BlindModeConfig 287、OneBotAccountRule 295、
    OneBotNapCatConfig 305、ChatActionsConfig 319、StickerLibraryConfig 330、GroupChatRule 340、
    MemoryConfig 353、RuntimeConfig 403、BrowserConfig 449、ParticipantPreset 472、
    SharedStoryConfig 481、RestWindow 501、AutoAdvanceConfig 510、StoryDefaults 578、LoggingConfig 591。
  · `.hdsi_reference/src/narrator.ts`：ProviderConfig 57、FailoverConfig 92、ModelConfig 99、
    VisionConfig 129、AudioConfig 142、ModelProfile 154、CompactionConfig 166、EmbeddingConfig 186。
  · `.hdsi_reference/src/types.ts`：AgencyConfig / AlterSystemConfig / ChatRhythmConfig /
    ChatReactionName / NativeFaceSemantic（已在 hdsi/types.py 定义，这里直接复用）。
  · `.hdsi_reference/src/group-willingness.ts`：GroupWillingnessConfig（7 行）。
  · `.hdsi_reference/src/urge.ts`：UrgeConfig（2 行）。
  · `.hdsi_reference/src/schedule-preplan.ts`：SchedulePreplanConfig（8 行）。

移植约定（与 PORTING_GUIDE.md 一致）：
- 字段名严格保持上游 camelCase（JSON key），不改名、不翻译；
- 每个默认值逐字抄录上游 `.default(...)`（含长英文/中文 prompt、数组默认值与嵌套对象默认值）；
- 数组整段替换，不做元素级合并；`resolve_config` 只对顶层段做 `{**default, **override}`；
- 上游 index.ts 中没有 `.default(...)` 的字段（例如 urge.advanced.hotMin/hotMax/idleMin/idleMax/
  burstMin/burstMax/slowMin/slowMax）不写进 DEFAULT_CONFIG，由运行时按 Urge 频率档位补全
  （见 hdsi/urge.py 的 resolve_urge_config，对应上游 urge.ts resolveUrgeConfig）；
- 上游没有任何 `desktop` 配置段：`installDesktopBridge(service)`（index.ts 499）是环境门控的
  worker 集成，既不注册 Console 字段也不读 config；因此本文件不提供 desktop 默认值。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, TypedDict

from .types import AgencyConfig, AlterSystemConfig, ChatReactionName, ChatRhythmConfig, NativeFaceSemantic

# 上游 TS 的字符串联合类型在 Python 里退化为 str（取值见右侧注释）。
ProviderResponseFormat = str  # 'json-object' | 'prompt-only'
ProviderStrategy = str        # 'priority' | 'round-robin'
ZhipuReasoningEffort = str    # 'low' | 'high' | 'max'
DeepSeekThinkingMode = str    # 'disabled' | 'enabled'
ProviderMode = str            # 'openai-compatible' | 'zhipu-official' | 'openai-official' |
                              # 'deepseek-official' | 'moonshot-official' | 'dashscope-official' |
                              # 'siliconflow-official' | 'openrouter' | 'gemini-openai'
VisionDetail = str            # 'low' | 'high' | 'auto'
ChatPlatform = str            # 'qq' | 'wechat'
StoryLogLevel = str           # 'silent' | 'error' | 'warn' | 'info' | 'debug'
StoryLogVerbosity = str       # 'summary' | 'standard' | 'diagnostic'
StoryLogFormat = str          # 'compact' | 'detailed' | 'layered'


# ==================== interface（上游 service.ts / narrator.ts / types.ts） ====================

class ProviderConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface ProviderConfig（57-90 行）。"""

    id: str
    label: str
    enabled: bool
    endpoint: str
    apiKey: str
    model: str
    temperature: float
    topP: float
    maxTokens: int
    timeout: int
    responseFormat: ProviderResponseFormat
    extraHeaders: str
    extraBody: str
    mode: ProviderMode
    useForMain: bool
    useForCompaction: bool
    useForAlter: bool
    useForEmbedding: bool
    useForStickers: bool
    useForVision: bool
    zhipuOfficial: bool
    reasoningEffort: ZhipuReasoningEffort
    deepseekOfficial: bool
    deepseekThinking: DeepSeekThinkingMode
    deepseekReasoningEffort: ZhipuReasoningEffort
    dashscopeRegion: str  # 'beijing' | 'singapore' | 'us'
    priceInput: float
    priceOutput: float
    priceCachedInput: float


class ModelProfile(TypedDict, total=False):
    """上游 src/narrator.ts interface ModelProfile（154 行）：model.models 的条目。"""

    id: str
    label: str
    enabled: bool
    providerId: str
    model: str
    maxTokens: int
    timeout: int
    responseFormat: ProviderResponseFormat


class FailoverConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface FailoverConfig（92 行）。"""

    enabled: bool
    strategy: ProviderStrategy
    maxAttemptsPerProvider: int
    cooldownMinutes: int


class VisionConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface VisionConfig（129 行）。"""

    enabled: bool
    mode: str  # 'native' | 'sidecar'
    detail: VisionDetail
    maxImageDimension: int  # 0 | 512 | 768 | 1024


class AudioConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface AudioConfig（142 行）。"""

    enabled: bool
    outFormat: str  # 'mp3' | 'wav' | 'ogg' | 'm4a' | 'flac' | 'amr'
    maxFileSizeMB: int
    maxPerMessage: int


class EmbeddingConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface EmbeddingConfig（186 行）。"""

    enabled: bool
    liveQuery: bool
    semanticStickerFilter: bool
    semanticHistory: bool
    providerId: str
    modelId: str
    endpoint: str
    model: str
    dimensions: int
    timeout: int
    maxInputCharacters: int
    backfillBatchSize: int


class CompactionConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface CompactionConfig（166 行）。"""

    enabled: bool
    modelId: str
    providerId: str
    model: str
    temperature: float
    topP: float
    maxTokens: int
    timeout: int
    responseFormat: ProviderResponseFormat
    mainPrompt: str
    fixedPrompt: str
    stylePrompt: str


class ModelConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface ModelConfig（99 行）。"""

    mode: str  # 'fallback' | 'openai-compatible'（@deprecated）
    providers: List[ProviderConfig]
    failover: FailoverConfig
    mainPrompt: str
    formatPrompt: str
    fixedPrompt: str
    stylePrompt: str
    models: List[ModelProfile]
    mainModelId: str
    mainTemperature: float
    mainTopP: float
    mainMaxTokens: int
    mainTimeout: int
    mainResponseFormat: ProviderResponseFormat
    mainStreamingMode: str  # 'off' | 'experimental'
    mainPayloadOrder: str   # 'legacy' | 'cache-first'
    compaction: CompactionConfig
    embedding: EmbeddingConfig
    vision: VisionConfig
    audio: AudioConfig


class RestWindow(TypedDict, total=False):
    """上游 src/service.ts interface RestWindow（501 行）。"""

    enabled: bool
    label: str
    start: str
    end: str
    minIntervalMinutes: int
    maxIntervalMinutes: int


class RuntimeConfig(TypedDict, total=False):
    """上游 src/service.ts interface RuntimeConfig（403 行）。"""

    captureDirectMessages: bool
    autoCreate: bool
    ignoreCommandMessages: bool
    allowProactiveMessages: bool
    proactiveWillingnessThreshold: float
    sweepIntervalMinutes: int
    minimumAdvanceMinutes: int
    maxStoriesPerSweep: int
    contextEntryLimit: int
    contextTimeWindowMinutes: int
    memoryLimit: int
    maxScriptCharacters: int
    maxMessageCharacters: int
    minimumDelayedReplySeconds: int
    maximumDelayedReplyMinutes: int
    cancelDelayedRepliesOnUserMessage: bool
    narrativeRetryDelaySeconds: int
    narrativeRetryMaxAttempts: int
    splitReplyMessages: bool
    messageSeparator: str
    typingBaseDelaySeconds: float
    typingCharactersPerSecond: float
    typingMaxDelaySeconds: float
    typingJitterRatio: float
    userMessageDebounceSeconds: float
    staleNarrativeRequestWindowSeconds: float  # @deprecated：0.1.2 起忽略
    autoAdvanceEnabled: bool
    autoAdvanceIntervalMinutes: int
    autoAdvanceJitterMinutes: int
    conversationFollowUpMinutes: List[int]
    conversationFollowUpJitterMinutes: int
    restWindows: List[RestWindow]


class AutoAdvanceConfig(TypedDict, total=False):
    """上游 src/service.ts（非导出）interface AutoAdvanceConfig（510 行）。"""

    enabled: bool
    intervalMinutes: int
    jitterMinutes: int
    followUpMinutes: List[int]
    followUpJitterMinutes: int
    restWindows: List[RestWindow]


class BlindModeConfig(TypedDict, total=False):
    """上游 src/service.ts interface BlindModeConfig（287 行）。"""

    enabled: bool
    healthReportMinutes: int


class OneBotAccountRule(TypedDict, total=False):
    """上游 src/service.ts interface OneBotAccountRule（295 行）。

    qq / label / enabled 必填；personId / profile / relationship 只用于私聊白名单。
    """

    qq: str
    label: str
    enabled: bool
    personId: str
    profile: str
    relationship: str


class GroupWillingnessConfig(TypedDict, total=False):
    """上游 src/group-willingness.ts interface GroupWillingnessConfig（7 行）。"""

    enabled: bool
    maxScore: float
    threshold: float
    probabilityAmplifier: float
    decayHalfLifeSeconds: int
    replyCost: float
    baseGain: float
    quoteGain: float
    keywordGain: float
    keywords: List[str]


class GroupChatRule(TypedDict, total=False):
    """上游 src/service.ts interface GroupChatRule（340 行）。"""

    groupId: str
    label: str
    enabled: bool
    purpose: str
    characterRole: str
    responseMode: str  # 'mention-only' | 'always'
    contextLimit: int
    debounceSeconds: float
    cooldownSeconds: int
    willingness: GroupWillingnessConfig


class OneBotNapCatConfig(TypedDict, total=False):
    """上游 src/service.ts interface OneBotNapCatConfig（305 行）。"""

    enabled: bool
    botAccounts: List[OneBotAccountRule]
    userMode: str  # 'allowlist' | 'blocklist'（@deprecated，运行时只认白名单）
    userAccounts: List[OneBotAccountRule]
    groupChats: List[GroupChatRule]
    ignoreSelfMessages: bool


class ChatActionsConfig(TypedDict, total=False):
    """上游 src/service.ts interface ChatActionsConfig（319 行）。"""

    enabled: bool
    platforms: List[ChatPlatform]
    quoteReply: bool
    messageReactions: bool
    allowedReactions: List[ChatReactionName]
    nativeFaces: bool
    expressionThreshold: float
    allowedNativeFaces: List[NativeFaceSemantic]


class StickerLibraryConfig(TypedDict, total=False):
    """上游 src/service.ts interface StickerLibraryConfig（330 行）。"""

    enabled: bool
    directory: str
    maxFileSizeMB: int
    catalogLimit: int
    descriptionMaxTokens: int
    descriptionResponseFormat: ProviderResponseFormat


class MemoryConfig(TypedDict, total=False):
    """上游 src/service.ts interface MemoryConfig（353 行）。"""

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
    statePatchMinTurns: int
    statePatchMinDays: int
    statePatchCooldownHours: int
    autoApplyStatePatches: bool
    allowMajorStateChanges: bool
    maxFactsPerStory: int
    activeConsequencesEnabled: bool
    activeConsequencePromptLimit: int
    activeConsequenceMaxDays: int
    activeConsequenceDefaultStrength: float
    overlayCompressionEnabled: bool
    overlayRecentDays: int
    overlayMonthlyAfterDays: int
    overlayWeeklyWindowDays: int
    overlayMonthlyWindowDays: int
    overlayWeeklySummaryCharacters: int
    overlayMonthlySummaryCharacters: int


class BrowserConfig(TypedDict, total=False):
    """上游 src/service.ts interface BrowserConfig（449 行）。"""

    enabled: bool
    mode: str  # 'deferred-only' | 'allow-immediate'
    allowSearch: bool
    allowVisit: bool
    searchUrlTemplate: str
    allowedDomains: List[str]
    blockedDomains: List[str]
    maxConcurrentPages: int
    maxResearchPerSweep: int
    navigationTimeout: int
    waitUntil: str  # 'domcontentloaded' | 'networkidle2'
    maxTextCharacters: int
    maxExcerptCharacters: int
    maxObservationsInPrompt: int
    cacheMinutes: int
    allowGroupTriggeredResearch: bool
    logObservationPreview: bool


class ParticipantPreset(TypedDict, total=False):
    """上游 src/service.ts interface ParticipantPreset（472 行）。"""

    qq: str
    personId: str
    label: str
    profile: str
    relationship: str
    enabled: bool


class SharedStoryConfig(TypedDict, total=False):
    """上游 src/service.ts interface SharedStoryConfig（481 行）。"""

    enabled: bool
    autoEnrollParticipants: bool
    allowCrossConversationMessages: bool
    shareParticipantDetails: bool
    maxCrossConversationActions: int
    participantContextLimit: int
    managerAccounts: List[str]
    participantPresets: List[ParticipantPreset]  # @deprecated


class UrgeAdvancedConfig(TypedDict, total=False):
    """上游 src/urge.ts UrgeConfig.advanced（2 行）。"""

    hotMin: float
    hotMax: float
    idleMin: float
    idleMax: float
    burstMin: float
    burstMax: float
    slowMin: float
    slowMax: float
    halfLifeMinutes: float
    burstThreshold: float
    jitter: float
    extremeChance: float
    burstTtlMinutes: float
    burstBudget: int
    burstContactMinMinutes: float


class UrgeConfig(TypedDict, total=False):
    """上游 src/urge.ts interface UrgeConfig（2 行）。"""

    enabled: bool
    frequency: str  # 'low' | 'medium' | 'high' | 'custom'
    proactiveWillingnessThreshold: float
    advanced: UrgeAdvancedConfig


class SchedulePreplanConfig(TypedDict, total=False):
    """上游 src/schedule-preplan.ts interface SchedulePreplanConfig（8 行）。"""

    enabled: bool
    horizonDays: int
    reviewAfterLocalHour: int
    anchorAutoAdvance: bool
    variationLevel: str  # 'stable' | 'contextual' | 'granular'
    candidateActivationProbability: float
    candidateRevealMinutes: int


class TimelineDirectorConfig(TypedDict, total=False):
    """index.ts 第 474 行 Config.timelineDirector 的内联类型 `{ enabled: boolean }`。"""

    enabled: bool


class StoryDefaults(TypedDict, total=False):
    """上游 src/service.ts interface StoryDefaults（578 行）。"""

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
    """上游 src/service.ts interface LoggingConfig（591 行）。"""

    level: StoryLogLevel
    verbosity: StoryLogVerbosity
    format: StoryLogFormat
    colors: bool
    colorTheme: str  # 'dark' | 'light'
    kaomoji: bool
    logScriptPreview: bool
    logMessageContent: bool
    previewLength: int


class Config(TypedDict, total=False):
    """上游 src/service.ts interface Config（260 行）；index.ts 里别名为 InterludeConfig。"""

    blindMode: BlindModeConfig
    blackBox: BlindModeConfig  # @deprecated：blindMode 的旧名字，service.ts 仍然读取
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
    alterSystem: AlterSystemConfig
    chatRhythm: ChatRhythmConfig
    timelineDirector: TimelineDirectorConfig
    agency: AgencyConfig
    urge: UrgeConfig
    schedulePreplan: SchedulePreplanConfig


# index.ts 的写法：`import { Config as InterludeConfig } from './service'`。
InterludeConfig = Config


# ==================== 各 Schema 的默认值（index.ts 逐字段抄录） ====================

# index.ts 20-39：const defaultProvider: ProviderConfig
DEFAULT_PROVIDER: ProviderConfig = {
    'id': 'primary',
    'label': 'Primary provider',
    'enabled': True,
    'endpoint': '',
    'apiKey': '',
    'model': '',
    'temperature': 0.8,
    'topP': 1,
    'maxTokens': 4096,
    'timeout': 60000,
    'responseFormat': 'json-object',
    'extraHeaders': '',
    'extraBody': '',
    'useForMain': True,
    'useForCompaction': True,
    'useForAlter': True,
    'useForEmbedding': False,
    'useForStickers': False,
    'useForVision': False,
    'mode': 'openai-compatible',
}

# index.ts 107-112：const Failover（.collapse(true)）
DEFAULT_FAILOVER: FailoverConfig = {
    'enabled': True,
    'strategy': 'priority',
    'maxAttemptsPerProvider': 1,
    'cooldownMinutes': 5,
}

# index.ts 114-125：const Embedding（Model.embedding 的默认对象见 168 行）
DEFAULT_EMBEDDING: EmbeddingConfig = {
    'enabled': False,
    'liveQuery': False,
    'semanticHistory': False,
    'modelId': '',
    'providerId': '',
    'endpoint': '',
    'model': '',
    'dimensions': 0,
    'timeout': 10000,
    'maxInputCharacters': 4000,
    'backfillBatchSize': 5,
    'semanticStickerFilter': True,
}

# index.ts 169-180：Model.compaction 的默认对象（179 行）
DEFAULT_COMPACTION: CompactionConfig = {
    'enabled': True,
    'modelId': '',
    'providerId': '',
    'model': '',
    'temperature': 0.3,
    'topP': 1,
    'maxTokens': 2048,
    'timeout': 60000,
    'responseFormat': 'json-object',
    'mainPrompt': 'Compress completed scenes into concise continuity notes while preserving causality, promises, unresolved matters, and gradual character change.',
    'fixedPrompt': '',
    'stylePrompt': 'Concise, factual, chronological, and concrete.',
}

# index.ts 128-133：const Vision（143 行 Model.vision 的默认对象）
DEFAULT_VISION: VisionConfig = {
    'enabled': False,
    'mode': 'native',
    'detail': 'auto',
    'maxImageDimension': 1024,
}

# index.ts 135-140：const AudioUnderstanding（144 行 Model.audio 的默认对象）
DEFAULT_AUDIO: AudioConfig = {
    'enabled': False,
    'outFormat': 'mp3',
    'maxFileSizeMB': 10,
    'maxPerMessage': 1,
}

# index.ts 142-180：const Model
DEFAULT_MODEL: ModelConfig = {
    'vision': DEFAULT_VISION,
    'audio': DEFAULT_AUDIO,
    'providers': [DEFAULT_PROVIDER],
    'mainTemperature': 0.8,
    'mainTopP': 1,
    'mainMaxTokens': 4096,
    'mainTimeout': 60000,
    'mainResponseFormat': 'json-object',
    'mainStreamingMode': 'off',
    'mainPayloadOrder': 'legacy',
    'failover': DEFAULT_FAILOVER,
    'mainPrompt': 'Continue the character-centered life script with grounded actions, motives, relationships, and ordinary time passing.',
    'formatPrompt': '',
    'fixedPrompt': '',
    'stylePrompt': 'Use restrained, realistic prose with concrete daily details, natural pauses, and no forced drama.',
    'embedding': DEFAULT_EMBEDDING,
    'compaction': DEFAULT_COMPACTION,
}

# index.ts 182-189：const RestWindowSchema（222-224 行 restWindows 默认数组的元素）
DEFAULT_REST_WINDOW: RestWindow = {
    'enabled': True,
    'label': 'night sleep',
    'start': '23:00',
    'end': '07:00',
    'minIntervalMinutes': 120,
    'maxIntervalMinutes': 240,
}

# index.ts 191-226：const Runtime
DEFAULT_RUNTIME: RuntimeConfig = {
    'splitReplyMessages': True,
    'messageSeparator': '<sep/>',
    'typingBaseDelaySeconds': 1,
    'typingCharactersPerSecond': 8,
    'typingMaxDelaySeconds': 12,
    'typingJitterRatio': 0.3,
    'userMessageDebounceSeconds': 2,
    'narrativeRetryDelaySeconds': 60,
    'narrativeRetryMaxAttempts': 6,
    'captureDirectMessages': True,
    'autoCreate': False,
    'ignoreCommandMessages': True,
    'allowProactiveMessages': False,
    'proactiveWillingnessThreshold': 0.65,
    'sweepIntervalMinutes': 5,
    'minimumAdvanceMinutes': 30,
    'maxStoriesPerSweep': 20,
    'contextEntryLimit': 50,
    'contextTimeWindowMinutes': 60,
    'memoryLimit': 20,
    'maxScriptCharacters': 8000,
    'maxMessageCharacters': 2000,
    'minimumDelayedReplySeconds': 10,
    'maximumDelayedReplyMinutes': 1440,
    'cancelDelayedRepliesOnUserMessage': True,
    'autoAdvanceEnabled': True,
    'autoAdvanceIntervalMinutes': 40,
    'autoAdvanceJitterMinutes': 5,
    'conversationFollowUpMinutes': [10, 20],
    'conversationFollowUpJitterMinutes': 1,
    'restWindows': [DEFAULT_REST_WINDOW],
}

# index.ts 227-230：const BlindMode
DEFAULT_BLIND_MODE: BlindModeConfig = {
    'enabled': False,
    'healthReportMinutes': 10,
}

# index.ts 232-240：const SchedulePreplan
DEFAULT_SCHEDULE_PREPLAN: SchedulePreplanConfig = {
    'enabled': True,
    'horizonDays': 14,
    'variationLevel': 'stable',
    'candidateActivationProbability': 0.25,
    'candidateRevealMinutes': 120,
    'reviewAfterLocalHour': 3,
    'anchorAutoAdvance': True,
}

# index.ts 242-247：const Agency（.collapse(true)）
DEFAULT_AGENCY: AgencyConfig = {
    'enabled': True,
    'maxWindowMinutes': 240,
    'minimumProactiveIntervalMinutes': 60,
    'maxCandidateHours': 24,
}

# index.ts 249-255：const ChatRhythm（已弃用段）
DEFAULT_CHAT_RHYTHM: ChatRhythmConfig = {
    'enabled': True,
    'mode': 'balanced',
    'historyLimit': 12,
    'collapseMinSamples': 5,
    'exhaustLimit': 6,
}

# index.ts 257-270：const AlterSystem（.collapse(true)）
DEFAULT_ALTER_SYSTEM: AlterSystemConfig = {
    'enabled': True,
    'baseThreshold': 10,
    'densityFactor': 0.3,
    'sameDirectionBoost': 0.05,
    'oppositeDecay': 0.15,
    'minWeight': 0.2,
    'maxIntensity': 2,
    'temperature': 0.3,
    'topP': 1,
    'maxTokens': 400,
    'timeout': 30000,
    'prompt': '',
}

# index.ts 272-290：const Browser（.collapse(true)）
DEFAULT_BROWSER: BrowserConfig = {
    'enabled': False,
    'mode': 'deferred-only',
    'allowSearch': True,
    'allowVisit': True,
    'searchUrlTemplate': 'https://html.duckduckgo.com/html/?q={query}',
    'allowedDomains': [],
    'blockedDomains': [],
    'maxConcurrentPages': 1,
    'maxResearchPerSweep': 1,
    'navigationTimeout': 15000,
    'waitUntil': 'domcontentloaded',
    'maxTextCharacters': 12000,
    'maxExcerptCharacters': 3000,
    'maxObservationsInPrompt': 4,
    'cacheMinutes': 30,
    'allowGroupTriggeredResearch': False,
    'logObservationPreview': False,
}

# index.ts 292-332：const Memory（.collapse(true)）
DEFAULT_MEMORY: MemoryConfig = {
    'enabled': True,
    'backgroundIntervalMinutes': 10,
    'sceneEntryThreshold': 16,
    'sceneCharacterThreshold': 10000,
    'recentEntryLimit': 30,
    'factLimit': 20,
    'statePatchConfidenceThreshold': 0.82,
    'majorStatePatchConfidenceThreshold': 0.95,
    'statePatchMinEvidence': 3,
    'statePatchMinTurns': 3,
    'statePatchMinDays': 2,
    'statePatchCooldownHours': 72,
    'maxFactsPerStory': 200,
    'maxStoriesPerCompactionRun': 20,
    'compactionEntryLimit': 80,
    'compactionCharacterLimit': 32000,
    'sceneHookCharacters': 2000,
    'sceneSummaryCharacters': 8000,
    'arcSummaryCharacters': 12000,
    'previousSceneSummaries': 2,
    'factContentCharacters': 4000,
    'factImportanceWeight': 0.5,
    'factConfidenceWeight': 0.35,
    'factRecencyWeight': 0.15,
    'semanticWeight': 0.55,
    'unresolvedWeight': 0.2,
    'autoApplyStatePatches': True,
    'allowMajorStateChanges': True,
    'activeConsequencesEnabled': True,
    'activeConsequencePromptLimit': 6,
    'activeConsequenceMaxDays': 7,
    'activeConsequenceDefaultStrength': 0.55,
    'overlayCompressionEnabled': True,
    'overlayRecentDays': 2,
    'overlayMonthlyAfterDays': 10,
    'overlayWeeklyWindowDays': 5,
    'overlayMonthlyWindowDays': 10,
    'overlayWeeklySummaryCharacters': 1600,
    'overlayMonthlySummaryCharacters': 2400,
}

# index.ts 334-345：const StoryDefaults
DEFAULT_STORY_DEFAULTS: StoryDefaults = {
    'characterName': 'Unnamed character',
    'characterProfile': '',
    'perspective': '',
    'userProfile': '',
    'relationship': '',
    'world': '',
    'supportingCast': '',
    'location': '',
    'style': '现实主义日常叙事，情绪克制，关系变化缓慢而具体。',
    'timezone': 'Asia/Shanghai',
}

# index.ts 347-357：const Logging（.collapse(true)）
DEFAULT_LOGGING: LoggingConfig = {
    'level': 'info',
    'verbosity': 'standard',
    'format': 'layered',
    'colors': True,
    'colorTheme': 'dark',
    'kaomoji': True,
    'logScriptPreview': False,
    'logMessageContent': False,
    'previewLength': 500,
}

# index.ts 359-363：const OneBotBotAccount（机器人账号白名单行）
DEFAULT_ONE_BOT_BOT_ACCOUNT: OneBotAccountRule = {
    'qq': '',
    'label': '',
    'enabled': True,
}

# index.ts 366-374：const OneBotUserAccount（用户白名单行，.collapse(true)）
DEFAULT_ONE_BOT_USER_ACCOUNT: OneBotAccountRule = {
    'qq': '',
    'label': '',
    'personId': '',
    'profile': '',
    'relationship': '',
    'enabled': True,
}

# index.ts 376-387：const GroupWillingness（.collapse(true)）；与
# src/group-willingness.ts DEFAULT_GROUP_WILLINGNESS（34-45 行）逐字段一致。
DEFAULT_GROUP_WILLINGNESS: GroupWillingnessConfig = {
    'enabled': False,
    'maxScore': 1,
    'threshold': 0.24,
    'probabilityAmplifier': 1.3,
    'decayHalfLifeSeconds': 180,
    'replyCost': 0.55,
    'baseGain': 0.12,
    'quoteGain': 0.12,
    'keywordGain': 0.18,
    'keywords': [],
}

# index.ts 389-400：const GroupChatRuleSchema（.collapse(true)）
DEFAULT_GROUP_CHAT_RULE: GroupChatRule = {
    'groupId': '',
    'label': '',
    'enabled': True,
    'purpose': '',
    'characterRole': '',
    'responseMode': 'mention-only',
    'contextLimit': 20,
    'debounceSeconds': 1,
    'cooldownSeconds': 60,
    'willingness': DEFAULT_GROUP_WILLINGNESS,
}

# index.ts 401-408：const OneBot（三个列表默认都是空表）
DEFAULT_ONE_BOT: OneBotNapCatConfig = {
    'enabled': False,
    'botAccounts': [],
    'userAccounts': [],
    'groupChats': [],
    'ignoreSelfMessages': True,
}

# index.ts 410-423：const ChatActions（.collapse(true)）
DEFAULT_CHAT_ACTIONS: ChatActionsConfig = {
    'enabled': False,
    'platforms': ['qq'],
    'quoteReply': True,
    'messageReactions': True,
    'allowedReactions': ['like', 'smile', 'laugh', 'heart'],
    'nativeFaces': True,
    'expressionThreshold': 0.7,
    'allowedNativeFaces': ['smile', 'laugh', 'sweat', 'awkward'],
}

# index.ts 425-432：const Stickers（.collapse(true)）。
# 注意：463 行 Config 里的段默认只写了 4 个字段，但字段级 .default 还有
# descriptionMaxTokens=768 与 descriptionResponseFormat='json-object'，Koishi 解析后同样生效。
DEFAULT_STICKERS: StickerLibraryConfig = {
    'enabled': False,
    'directory': 'data/hds-interlude/stickers',
    'maxFileSizeMB': 10,
    'catalogLimit': 40,
    'descriptionMaxTokens': 768,
    'descriptionResponseFormat': 'json-object',
}

# index.ts 434-441：const SharedStory（.collapse(true)）
DEFAULT_SHARED_STORY: SharedStoryConfig = {
    'autoEnrollParticipants': True,
    'allowCrossConversationMessages': True,
    'shareParticipantDetails': False,
    'maxCrossConversationActions': 1,
    'participantContextLimit': 6,
    'managerAccounts': [],
}

# index.ts 443-459：const UrgeAdvanced（.collapse(true)）。
# hotMin/hotMax/idleMin/idleMax/burstMin/burstMax/slowMin/slowMax 在上游没有 .default(...)，
# 因此不进入默认值；运行时由 hdsi/urge.py resolve_urge_config 按 frequency 档位补全。
DEFAULT_URGE_ADVANCED: UrgeAdvancedConfig = {
    'halfLifeMinutes': 45,
    'burstThreshold': 0.75,
    'jitter': 0.15,
    'extremeChance': 0.03,
    'burstTtlMinutes': 35,
    'burstBudget': 3,
    'burstContactMinMinutes': 5,
}

# index.ts 466-472：Config.urge 内联 Schema
DEFAULT_URGE: UrgeConfig = {
    'enabled': False,
    'frequency': 'medium',
    'proactiveWillingnessThreshold': 0.4,
    'advanced': DEFAULT_URGE_ADVANCED,
}

# index.ts 474：Config.timelineDirector 内联 Schema
DEFAULT_TIMELINE_DIRECTOR: TimelineDirectorConfig = {
    'enabled': True,
}

# index.ts 461-489：export const Config: Schema<InterludeConfig>
# 顶层段顺序与上游完全一致：storyDefaults / model / onebot / sharedStory / runtime / urge /
# schedulePreplan / timelineDirector / agency / chatActions / stickers / memory / alterSystem /
# browser / blindMode / logging / chatRhythm（17 段）。
DEFAULT_CONFIG: Config = {
    'storyDefaults': DEFAULT_STORY_DEFAULTS,
    'model': DEFAULT_MODEL,
    'onebot': DEFAULT_ONE_BOT,
    'sharedStory': DEFAULT_SHARED_STORY,
    'runtime': DEFAULT_RUNTIME,
    'urge': DEFAULT_URGE,
    'schedulePreplan': DEFAULT_SCHEDULE_PREPLAN,
    'timelineDirector': DEFAULT_TIMELINE_DIRECTOR,
    'agency': DEFAULT_AGENCY,
    'chatActions': DEFAULT_CHAT_ACTIONS,
    'stickers': DEFAULT_STICKERS,
    'memory': DEFAULT_MEMORY,
    'alterSystem': DEFAULT_ALTER_SYSTEM,
    'browser': DEFAULT_BROWSER,
    'blindMode': DEFAULT_BLIND_MODE,
    'logging': DEFAULT_LOGGING,
    'chatRhythm': DEFAULT_CHAT_RHYTHM,
}


def resolve_config(overrides: Optional[Dict[str, Any]] = None) -> Config:
    """与 Koishi Schema 一样，把用户配置浅合并到各顶层段。

    上游 Koishi 的解析语义：
    - 顶层每个段：`{ ...段默认值, ...用户段 }`（`{**default, **override}`）；
    - 数组整段替换（providers / botAccounts / userAccounts / groupChats / restWindows /
      keywords / managerAccounts 等都不做元素级合并）；
    - `undefined`/`None` 不覆盖默认值；
    - 段默认值里没有的字段（例如 `urge.advanced.hotMin`）保持用户给的值或不存在。
    注意：这里与上游一样只做一层浅合并；override 里的嵌套对象（如 `model.embedding`）是
    整段替换，不会与默认值再深合并。

    返回值是全新对象（deepcopy），调用方可以自由修改而不影响 DEFAULT_CONFIG。
    """
    resolved: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(overrides, dict):
        return resolved  # type: ignore[return-value]
    for key, value in overrides.items():
        if value is None:
            continue  # None 不覆盖
        default_segment = resolved.get(key)
        if isinstance(default_segment, dict) and isinstance(value, dict):
            merged = dict(default_segment)
            merged.update(value)
            resolved[key] = merged
        else:
            # 数组 / 标量整段替换；DEFAULT_CONFIG 里没有的键（例如已弃用的 blackBox）
            # 原样透传，service.ts 仍会读取 config.blackBox。
            resolved[key] = value
    return resolved  # type: ignore[return-value]
