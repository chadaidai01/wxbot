# -*- coding: utf-8 -*-
"""HDS-Interlude 类型契约，与上游 src/types.ts 一一对应（1.0.1-beta6-rebuild）。

运行时数据结构一律是 dict；这里的 TypedDict 只用于标注与 IDE 提示。
字段名与上游 JSON key 完全一致（camelCase），不要改名。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict, Union

StoryStatus = str  # 'active' | 'paused' | 'archived'
SceneFrameField = str
ScenePresenceStatus = str  # 'present' | 'off-scene' | 'expected'
SchedulePreplanBlockKind = str  # 'fixed' | 'routine' | 'flexible' | 'open'
SchedulePreplanWeekday = str
AgencyActivityLoad = str  # 'free' | 'occupied' | 'overloaded'
AgencyPrivacy = str  # 'private' | 'shared' | 'public'
AgencyDeviceAccess = str  # 'available' | 'limited' | 'unavailable'
ProactiveContactOrigin = str
ProactiveDisclosure = str
ProactiveOutcome = str
SceneStatus = str  # 'active' | 'closed'
StatePatchTarget = str  # 'character' | 'perspective' | 'world' | 'relationship'
StatePatchStatus = str
IntentStatus = str  # 'pending' | 'completed' | 'cancelled'
InteractionReplyMode = str  # 'none' | 'immediate' | 'delayed'
ChatReactionName = str
NativeFaceSemantic = str
FollowUpCommitmentKind = str  # 'thinking' | 'checking' | 'decision' | 'emotional-settle'
NarrativePhase = str  # 'advance' | 'conversation-follow-up' | 'user-message' | 'intent-due'
TimelineBeatKind = str  # 'activity' | 'thought' | 'state'

SCHEMA_VERSION = 1


class CharacterSetting(TypedDict):
    name: str
    profile: str


class StorySetting(TypedDict):
    character: CharacterSetting
    user: Dict[str, str]  # displayName / profile
    relationship: str
    world: str
    perspective: str
    supportingCast: str
    location: str
    style: str
    timezone: str


class ContinuitySnapshot(TypedDict, total=False):
    current: str
    next: List[str]
    recent: List[str]
    salient: List[str]


class SceneFrame(TypedDict, total=False):
    id: str
    localBoundaryEntryId: int
    sceneId: int
    place: str
    presentPeople: List[str]
    ongoingActivity: str
    postureOrMotion: str
    attention: str
    deviceAccess: str
    privacy: str
    affectiveBaseline: str
    openMotions: List[str]
    openTopics: List[str]
    narrativeFocus: str
    sourceEntryIds: List[int]
    sources: Dict[str, List[int]]
    updatedAt: str


class DialogueBurstState(TypedDict, total=False):
    id: str
    frameId: str
    startedAt: str
    sourceEntryIds: List[int]
    lastEventId: str
    scopeKey: str
    topicKeys: List[str]


class WorkingDetail(TypedDict, total=False):
    participantId: str
    knowledge: Dict[str, Any]
    label: str
    value: str
    expiresAt: str
    createdAt: str
    sourceEntryIds: List[int]


class ScenePresenceState(TypedDict, total=False):
    name: str
    status: str
    basis: str
    sourceEntryIds: List[int]
    updatedAt: str


class AutomaticDeliverySummary(TypedDict, total=False):
    participantId: str
    summary: str
    sourceEntryId: int
    deliveredAt: str


class SchedulePreplanBlock(TypedDict, total=False):
    id: str
    start: str
    end: str
    label: str
    kind: str
    location: str
    tentative: bool
    sourceEntryIds: List[int]


class SchedulePreplanRegime(TypedDict, total=False):
    id: str
    label: str
    from_: str  # 上游字段名 from（Python 保留字，见 schedule_preplan.py 的映射）
    to: str
    weekly: Dict[str, List[SchedulePreplanBlock]]
    sourceEntryIds: List[int]


class SchedulePreplanException(TypedDict, total=False):
    date: str
    mode: str  # 'replace' | 'patch'
    reason: str
    removeBlockIds: List[str]
    blocks: List[SchedulePreplanBlock]
    sourceEntryIds: List[int]


class SchedulePreplanDay(TypedDict):
    date: str
    blocks: List[SchedulePreplanBlock]


class SchedulePreplanRecord(TypedDict, total=False):
    storyId: str
    revision: int
    timezone: str
    validFrom: str
    validThrough: str
    lastReviewedLocalDate: str
    lastEvidenceEntryId: int
    reviewReason: str
    regimes: List[SchedulePreplanRegime]
    exceptions: List[SchedulePreplanException]
    materializedDays: List[SchedulePreplanDay]
    createdAt: datetime
    updatedAt: datetime


class SchedulePreplanProposal(TypedDict, total=False):
    outcome: str  # 'unchanged' | 'extend' | 'patch' | 'replace'
    reason: str
    confidence: float
    sourceEntryIds: List[int]
    regimes: List[SchedulePreplanRegime]
    exceptions: List[SchedulePreplanException]


class SchedulePreplanReviewRequest(TypedDict, total=False):
    localDate: str
    horizonDays: int
    variationLevel: str
    current: Optional[SchedulePreplanRecord]
    evidenceEntries: List['ScriptEntry']


class SchedulePreplanWindow(TypedDict, total=False):
    name: str
    timezone: str
    from_: str
    to: str
    plannedNotObserved: bool
    revision: int
    blocks: List[Dict[str, Any]]


class ParticipantState(TypedDict, total=False):
    openThreads: List[str]
    relationshipNotes: List[str]
    relationshipOverlay: str
    unreadMessageCount: int
    pendingReplyCount: int
    lastUserMessageAt: str
    lastCharacterMessageAt: str


class StoryAutomationState(TypedDict, total=False):
    quietUntil: str
    nextAdvanceAt: str
    timelineRetryAt: str
    timelineRetryFrom: str
    lastAutoAdvanceAt: str
    lastUserMessageAt: str
    conversationFollowUpAt: List[str]
    conversationFollowUpParticipantId: str


class AgencyWindowState(TypedDict, total=False):
    activityLoad: str
    privacy: str
    deviceAccess: str
    nextOpportunityAt: str
    validUntil: str
    basis: str
    sourceEntryIds: List[int]
    updatedAt: str


class ProactiveContactDraft(TypedDict, total=False):
    participantId: str
    origin: str
    motive: str
    disclosure: str
    sourceEntryIds: List[int]
    willingness: float
    outcome: str
    notBefore: str
    expiresAt: str


class AgencyConfig(TypedDict, total=False):
    enabled: bool
    maxWindowMinutes: int
    minimumProactiveIntervalMinutes: int
    maxCandidateHours: int


class StorySettingOverlay(TypedDict, total=False):
    characterProfile: str
    perspective: str
    relationship: str
    world: str
    supportingCast: str
    location: str
    characterTraits: List[str]


class StoryState(TypedDict, total=False):
    schemaVersion: int
    extensions: Dict[str, Any]
    settingOverlay: StorySettingOverlay
    activeSceneId: int
    activeArcId: int
    continuitySnapshot: ContinuitySnapshot
    narrativeUpdateCount: int
    lastContinuityUpdateAt: str
    continuityDirty: bool
    automation: StoryAutomationState
    alterSystem: Dict[str, Any]
    agencyWindow: AgencyWindowState
    scenePresence: List[ScenePresenceState]
    automaticDeliverySummaries: List[AutomaticDeliverySummary]
    workingDetails: List[WorkingDetail]
    workingDetailResolutions: Dict[str, int]
    timelineCarry: List[str]
    chatRhythm: Dict[str, Any]
    sceneFrame: SceneFrame
    dialogueBurst: DialogueBurstState


class InterludeStory(TypedDict, total=False):
    id: str
    platform: str
    selfId: str
    userId: str
    channelId: str
    status: str
    setting: StorySetting
    state: StoryState
    cursorAt: datetime
    createdAt: datetime
    updatedAt: datetime


class InterludeParticipant(TypedDict, total=False):
    id: str
    storyId: str
    platform: str
    selfId: str
    userId: str
    channelId: str
    personId: str
    displayName: str
    profile: str
    relationship: str
    state: ParticipantState
    status: str
    createdAt: datetime
    updatedAt: datetime


class ScriptEntry(TypedDict, total=False):
    id: int
    storyId: str
    participantId: str
    kind: str
    actor: str
    content: str
    occurredAt: datetime
    metadata: Dict[str, Any]
    embedding: List[float]
    createdAt: datetime


class NarrativeMemory(TypedDict, total=False):
    id: int
    storyId: str
    participantId: str
    category: str
    content: str
    importance: float
    status: str
    sourceEntryId: Optional[int]
    createdAt: datetime
    updatedAt: datetime


class InterludeScene(TypedDict, total=False):
    id: int
    storyId: str
    status: str
    startedAt: datetime
    endedAt: Optional[datetime]
    hook: str
    summary: str
    entryCount: int
    lastEntryId: Optional[int]
    createdAt: datetime
    updatedAt: datetime


class InterludeArc(TypedDict, total=False):
    id: int
    storyId: str
    status: str
    title: str
    summary: str
    sceneCount: int
    createdAt: datetime
    updatedAt: datetime


class StatePatchProposal(TypedDict, total=False):
    id: int
    storyId: str
    participantId: str
    target: str
    path: str
    proposedValue: str
    evidence: str
    confidence: float
    impact: str
    status: str
    sourceEntryIds: List[int]
    createdAt: datetime
    appliedAt: Optional[datetime]


class OverlaySnapshot(TypedDict, total=False):
    id: int
    storyId: str
    participantId: str
    target: str
    tier: str
    periodStart: datetime
    periodEnd: datetime
    summary: str
    majorEvents: List[str]
    sourcePatchIds: List[int]
    status: str
    createdAt: datetime
    updatedAt: datetime


class NarrativeFact(TypedDict, total=False):
    knowledge: Dict[str, Any]
    id: int
    storyId: str
    participantId: str
    scope: str
    content: str
    importance: float
    confidence: float
    unresolved: bool
    embedding: List[float]
    status: str
    sourceEntryIds: List[int]
    lastSeenAt: datetime
    createdAt: datetime
    updatedAt: datetime


class NarrativeIntent(TypedDict, total=False):
    id: int
    storyId: str
    participantId: str
    type: str
    summary: str
    notBefore: datetime
    status: str
    payload: Dict[str, Any]
    createdAt: datetime
    updatedAt: datetime


class WebObservation(TypedDict, total=False):
    id: int
    storyId: str
    participantId: str
    intentId: Optional[int]
    mode: str
    query: str
    url: str
    title: str
    excerpt: str
    summary: str
    status: str
    accessedAt: datetime
    createdAt: datetime


class ScriptEntryDraft(TypedDict, total=False):
    kind: str
    actor: str
    content: str
    occurredAt: str
    metadata: Dict[str, Any]


class MemoryDraft(TypedDict, total=False):
    category: str
    content: str
    importance: float
    participantId: str


class IntentDraft(TypedDict, total=False):
    type: str
    summary: str
    notBefore: str
    payload: Dict[str, Any]
    participantId: str


class IntentUpdateDraft(TypedDict, total=False):
    id: int
    status: str
    resolution: str


class ScriptEventReference(TypedDict, total=False):
    commitId: str
    eventId: str
    scriptEntryId: int
    eventKind: str
    causedByEventIds: List[str]
    fullContent: str
    bubbleIndex: int
    bubbleCount: int


class OutgoingMessageDraft(TypedDict, total=False):
    participantId: str
    content: str
    automaticDelivery: Dict[str, Any]
    interaction: Optional[Dict[str, Any]]
    laterSegments: List[str]
    userInitiated: bool
    quoteMessageId: str
    scriptEvent: ScriptEventReference


class BrowserIntentDraft(TypedDict, total=False):
    mode: str
    query: str
    url: str
    purpose: str
    timing: str
    participantId: str


class ConversationActionDraft(TypedDict, total=False):
    actionId: str
    participantId: str
    mode: str
    content: str
    sendAt: str
    willingness: float
    reason: str


class NarrativeInteraction(TypedDict, total=False):
    seen: bool
    reply: Dict[str, Any]


class EarlyNarrativeReply(TypedDict, total=False):
    kind: str
    content: str
    interaction: NarrativeInteraction
    groupReply: Dict[str, Any]


class ChatActionCapabilities(TypedDict, total=False):
    platform: str
    quoteReply: bool
    reactions: List[str]
    nativeFaces: List[str]
    expressionThreshold: float


class MessageReactionDraft(TypedDict, total=False):
    messageRef: str
    reaction: str


class StickerCatalogEntry(TypedDict, total=False):
    assetId: str
    group: str
    description: str
    aliases: List[str]
    animated: bool


class LocalMediaDraft(TypedDict, total=False):
    assetId: str
    placement: str
    willingness: float


class NativeFaceDraft(TypedDict, total=False):
    semantic: str
    willingness: float


class StickerAsset(TypedDict, total=False):
    id: int
    assetId: str
    filePath: str
    group: str
    mimeType: str
    animated: bool
    size: int
    hash: str
    description: str
    aliases: List[str]
    status: str
    embedding: List[float]
    createdAt: datetime
    updatedAt: datetime


class QuotedMessageContext(TypedDict, total=False):
    senderId: str
    senderName: str
    speaker: str
    content: str


class IndexedQuotedMessageContext(QuotedMessageContext, total=False):
    messageIndex: int


class FollowUpCommitmentDraft(TypedDict, total=False):
    kind: str
    summary: str
    notBefore: str
    expiresAt: str
    sourceEntryIds: List[int]


class FollowUpResolutionDraft(TypedDict, total=False):
    id: int
    outcome: str
    notBefore: str


class NarrativeDecision(TypedDict, total=False):
    urge: Any
    script: str
    authoredActions: List[Dict[str, Any]]
    lifeHandoff: Dict[str, Any]
    alter: float
    agencyWindow: Dict[str, Any]
    proactiveContact: ProactiveContactDraft
    continuity: ContinuitySnapshot
    interaction: NarrativeInteraction
    followUpCommitment: FollowUpCommitmentDraft
    followUpResolutions: List[FollowUpResolutionDraft]
    automaticDeliverySummary: str
    memories: List[MemoryDraft]
    intents: List[IntentDraft]
    intentUpdates: List[IntentUpdateDraft]
    browserIntents: List[BrowserIntentDraft]
    statePatch: Dict[str, Any]
    crossConversationActions: List[ConversationActionDraft]
    groupReply: Dict[str, Any]
    messageReactions: List[MessageReactionDraft]
    localMedia: LocalMediaDraft
    nativeFace: NativeFaceDraft


class NarrativeImage(TypedDict, total=False):
    id: str
    mimeType: str
    dataUri: str


class NarrativeAudio(TypedDict, total=False):
    id: str
    format: str
    base64: str


class NarrativeRequest(TypedDict, total=False):
    urgeEnabled: bool
    contactThreads: List[Dict[str, Any]]
    phase: str
    refreshContinuity: bool
    outputRecovery: bool
    story: InterludeStory
    from_: datetime
    now: datetime
    userMessage: str
    userReportedTimes: List[Dict[str, Any]]
    images: List[NarrativeImage]
    audio: List[NarrativeAudio]
    visualObservations: List[str]
    timelinePlan: Dict[str, Any]
    timelineCarry: List[str]
    participant: Optional[InterludeParticipant]
    participants: List[InterludeParticipant]
    shareParticipantDetails: bool
    dueIntents: List[NarrativeIntent]
    upcomingIntents: List[NarrativeIntent]
    activeConsequences: List[NarrativeIntent]
    supersededIntents: List[NarrativeIntent]
    recentEntries: List[ScriptEntry]
    writingOptions: Dict[str, Any]
    recentProtectionSince: datetime
    memories: List[NarrativeMemory]
    sceneContext: Dict[str, Any]
    facts: List[NarrativeFact]
    overlaySnapshots: List[OverlaySnapshot]
    developmentTendencies: List[Dict[str, Any]]
    webContext: List[WebObservation]
    groupContext: Dict[str, Any]
    quotedMessages: List[IndexedQuotedMessageContext]
    chatCapabilities: ChatActionCapabilities
    stickerCatalog: List[StickerCatalogEntry]
    alterEnabled: bool
    emotionalOffset: Optional[Dict[str, Any]]
    agencyEnabled: bool
    agencyWindow: Optional[AgencyWindowState]
    automaticDeliverySummaries: List[AutomaticDeliverySummary]
    followUpCommitments: List[NarrativeIntent]
    schedulePreplan: Optional[SchedulePreplanWindow]
    onEarlyReply: Any
    workingDetails: List[WorkingDetail]
    recalledHistory: List[Dict[str, Any]]
    sceneFrame: SceneFrame
    dialogueBurst: DialogueBurstState


class UserReportedTime(TypedDict, total=False):
    localTime: str
    relation: str
    statement: str


class TimelineBeat(TypedDict, total=False):
    at: float
    kind: str
    summary: str


class TimelinePlan(TypedDict, total=False):
    beats: List[TimelineBeat]
    carry: List[str]


class TimelinePlanRequest(TypedDict, total=False):
    recalledHistory: List[Dict[str, Any]]
    contactThreads: List[Dict[str, Any]]
    story: InterludeStory
    participant: Optional[InterludeParticipant]
    phase: str
    from_: datetime
    now: datetime
    scene: Optional[InterludeScene]
    facts: List[NarrativeFact]
    recentEntries: List[ScriptEntry]
    recentScriptContinuation: Optional[Dict[str, Any]]
    dueIntents: List[NarrativeIntent]
    schedulePreplan: Optional[SchedulePreplanWindow]


class RecalledMoment(TypedDict, total=False):
    id: int
    occurredAt: str
    content: str
    sourceEntryIds: List[int]


class GroupMessageContext(TypedDict, total=False):
    senderId: str
    senderName: str
    speaker: str
    messageRef: str
    messageId: str
    quote: QuotedMessageContext
    content: str
    occurredAt: datetime
    direction: str


class GroupContext(TypedDict, total=False):
    groupId: str
    channelId: str
    label: str
    purpose: str
    characterRole: str
    messages: List[GroupMessageContext]


class PreviousSceneSummary(TypedDict, total=False):
    startedAt: str
    endedAt: str
    summary: str


class SceneContext(TypedDict, total=False):
    scene: Optional[InterludeScene]
    arc: Optional[InterludeArc]
    previousScenes: List[PreviousSceneSummary]


class CompactionRequest(TypedDict, total=False):
    story: InterludeStory
    from_: datetime
    now: datetime
    entries: List[ScriptEntry]
    scene: Optional[InterludeScene]
    arc: Optional[InterludeArc]
    precedingEntries: List[ScriptEntry]
    developmentCandidates: List[StatePatchProposal]
    participants: List[InterludeParticipant]
    facts: List[NarrativeFact]
    schedulePreplan: SchedulePreplanReviewRequest


class FactDraft(TypedDict, total=False):
    knowledge: Dict[str, Any]
    scope: str
    participantId: str
    content: str
    importance: float
    confidence: float
    unresolved: bool
    sourceEntryIds: List[int]
    resolvesFactIds: List[int]


class StatePatchDraft(TypedDict, total=False):
    interactionReview: Dict[str, Any]
    target: str
    participantId: str
    path: str
    proposedValue: str
    evidence: str
    confidence: float
    impact: str
    sourceEntryIds: List[int]
    contradictsProposalIds: List[int]


class ScenePresenceDraft(TypedDict, total=False):
    name: str
    status: str
    basis: str
    sourceEntryIds: List[int]


class WorkingDetailDraft(TypedDict, total=False):
    replacesLabel: str
    knowledge: Dict[str, Any]
    label: str
    value: str
    resolved: bool
    expiresAt: str
    sourceEntryIds: List[int]


class CompactionDecision(TypedDict, total=False):
    episodeTags: List[Dict[str, Any]]
    scene: Dict[str, Any]
    arc: Dict[str, Any]
    facts: List[FactDraft]
    statePatches: List[StatePatchDraft]
    workingDetails: List[WorkingDetailDraft]
    schedulePreplan: SchedulePreplanProposal


class OverlayCompactionRequest(TypedDict, total=False):
    story: InterludeStory
    participant: InterludeParticipant
    target: str
    tier: str
    from_: datetime
    to: datetime
    patches: List[StatePatchProposal]
    snapshots: List[OverlaySnapshot]


class OverlayCompactionDecision(TypedDict, total=False):
    summary: str
    majorEvents: List[str]


class NarrativeProvider:
    """上游 interface NarrativeProvider。"""

    def decide(self, request: NarrativeRequest) -> NarrativeDecision:  # pragma: no cover - 接口
        raise NotImplementedError

    def analyze_alter(self, request: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - 接口
        raise NotImplementedError


class NarrativeCompactor:
    def compact(self, request: CompactionRequest) -> CompactionDecision:  # pragma: no cover - 接口
        raise NotImplementedError

    def compact_overlay(self, request: OverlayCompactionRequest) -> OverlayCompactionDecision:  # pragma: no cover - 接口
        raise NotImplementedError


class NarrativeEmbedder:
    def identity(self) -> str:  # pragma: no cover - 接口
        return ''

    def embed(self, input_text: str) -> List[float]:  # pragma: no cover - 接口
        raise NotImplementedError


# ========== Alter System Types ==========

class EmotionalOffset(TypedDict, total=False):
    direction: str
    description: str
    intensity: float
    generatedAt: str


class EmotionalOffsetPrompt(EmotionalOffset, total=False):
    weight: float


class AlterHistoryEntry(TypedDict, total=False):
    turn: int
    phase: str
    alter: float
    alterValue: float
    timestamp: str
    participantId: str


class AlterPendingScope(TypedDict, total=False):
    participantId: str
    alterValue: float
    lastAnalysisAttemptAt: str


class AlterSystemState(TypedDict, total=False):
    alterValue: float
    alterWeight: float
    lastTriggerDirection: int
    emotionalOffset: Optional[EmotionalOffset]
    history: List[AlterHistoryEntry]
    pendingScopes: List[AlterPendingScope]
    lastUpdatedAt: str
    lastAnalysisAttemptAt: str


class RhythmSignature(TypedDict, total=False):
    bubbles: int
    shape: List[str]
    tail: str
    totalChars: int
    occurredAt: str


class CollapsedRhythm(TypedDict, total=False):
    templateKey: str
    reason: str
    streak: int


class ChatRhythmState(TypedDict, total=False):
    recent: List[RhythmSignature]
    collapsed: Optional[CollapsedRhythm]
    exhausted: bool
    lastDirectiveLevel: int
    updatedAt: str


class ChatRhythmConfig(TypedDict, total=False):
    enabled: bool
    mode: str
    historyLimit: int
    collapseMinSamples: int
    exhaustLimit: int


class AlterAnalysisRequest(TypedDict, total=False):
    characterName: str
    triggerValue: float
    threshold: float
    direction: str
    recentScripts: List[Dict[str, str]]
    history: List[AlterHistoryEntry]
    settingOverlay: StorySettingOverlay
    currentOffset: Optional[EmotionalOffsetPrompt]


class AlterAnalysisDecision(TypedDict, total=False):
    description: str


class AlterSystemConfig(TypedDict, total=False):
    enabled: bool
    baseThreshold: float
    densityFactor: float
    sameDirectionBoost: float
    oppositeDecay: float
    minWeight: float
    maxIntensity: float
    modelId: str
    providerId: str
    model: str
    temperature: float
    topP: float
    maxTokens: int
    timeout: int
    prompt: str


def empty_story_setting() -> StorySetting:
    return {
        'character': {'name': 'Unnamed character', 'profile': ''},
        'user': {'displayName': '', 'profile': ''},
        'relationship': '', 'world': '', 'perspective': '', 'supportingCast': '', 'location': '',
        'style': 'Realistic, restrained, and centered on ordinary life.',
        'timezone': 'Asia/Shanghai',
    }


def empty_story_state() -> StoryState:
    return {'schemaVersion': 1, 'settingOverlay': {'characterTraits': []}, 'automation': {}, 'narrativeUpdateCount': 0}


def empty_participant_state() -> ParticipantState:
    return {'openThreads': [], 'relationshipNotes': [], 'unreadMessageCount': 0, 'pendingReplyCount': 0}
