# -*- coding: utf-8 -*-
"""narrator 类型与配置接口，对应上游 src/narrator.ts 第 29-234 行
（HDS-Interlude 1.0.1-beta6-rebuild）。

导出与上游 export 一一对应：
    ProviderResponseFormat / ProviderStrategy / ZhipuReasoningEffort / DeepSeekThinkingMode /
    ProviderMode / ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT / StickerDescription / StickerDescriber /
    VisionDescriber / ProviderConfig / FailoverConfig / ModelConfig / VisionConfig /
    VisionDetail / AudioConfig / ModelProfile / CompactionConfig / EmbeddingConfig

移植约定：
- TS 字符串联合类型 → `typing.Literal`（py3.9 可用；运行时不参与，仅标注）。
- TS interface → `TypedDict`；带 `?` 的字段用 `total=False`（可选字段读取一律 `.get()`）。
  字段名 / JSON key 与上游逐字一致；`from` 这类保留字只在注解里写作 `from_` 占位，
  真实数据里的 key 永远是 `'from'`（上游 TS 里就不是这个形状，这里仅作说明）。
- `StickerDescriber` / `VisionDescriber` 是带方法的行为接口（上游 interface 里只有方法），
  按 hdsi/types.py 里 NarrativeProvider/NarrativeCompactor/NarrativeEmbedder 的既有风格
  落成普通基类，而不是 TypedDict。
- `ChatCompletionResponse` / `EmbeddingResponse` / `ChatRequestOverrides` 上游没有 export，
  只是同一区间的文件私有 interface；这里照抄定义（供 narrator_providers 标注），
  不进入 __all__。
- `ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT` 原样保留 45_000。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict

from .types import NarrativeImage  # noqa: F401 - 仅用于 VisionDescriber 的注解

# TS: export type ProviderResponseFormat = 'json-object' | 'prompt-only'
ProviderResponseFormat = Literal['json-object', 'prompt-only']
# TS: export type ProviderStrategy = 'priority' | 'round-robin'
ProviderStrategy = Literal['priority', 'round-robin']
# TS: export type ZhipuReasoningEffort = 'low' | 'high' | 'max'
ZhipuReasoningEffort = Literal['low', 'high', 'max']
# TS: export type DeepSeekThinkingMode = 'disabled' | 'enabled'
DeepSeekThinkingMode = Literal['disabled', 'enabled']
# TS: export type ProviderMode = 'openai-compatible' | 'zhipu-official' | 'openai-official'
#   | 'deepseek-official' | 'moonshot-official' | 'dashscope-official'
#   | 'siliconflow-official' | 'openrouter' | 'gemini-openai'
ProviderMode = Literal[
    'openai-compatible', 'zhipu-official', 'openai-official',
    'deepseek-official', 'moonshot-official', 'dashscope-official',
    'siliconflow-official', 'openrouter', 'gemini-openai',
]
# TS: export const ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT = 45_000
ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT = 45_000


class StickerDescription(TypedDict):
    """上游 export interface StickerDescription。"""

    description: str
    aliases: List[str]


class StickerDescriber:
    """上游 export interface StickerDescriber。

    上游是结构化 TS 接口；Python 里用同名基类表达，方法与上游一一对应
    （`describeSticker` → `describe_sticker`，默认值与上游一致）。
    """

    def available(self) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    def describe_sticker(
        self,
        data_uri: str,
        mime_type: str,
        file_name: str,
        animated: bool,
        response_format: ProviderResponseFormat = 'json-object',
        max_tokens: int = 768,
    ) -> Optional[StickerDescription]:  # pragma: no cover - 接口
        raise NotImplementedError


class VisionDescriber:
    """上游 export interface VisionDescriber。

    Converts current user images into factual text for a text-only main narrator.
    Results are transient and deliberately have no memory API.
    """

    def available(self) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    def describe_images(
        self,
        images: List['NarrativeImage'],
        user_text: str = '',
        detail: VisionDetail = 'auto',
    ) -> Optional[List[str]]:  # pragma: no cover - 接口
        raise NotImplementedError


class ProviderConfig(TypedDict, total=False):
    """上游 export interface ProviderConfig（id/label/... 全部字段照抄）。

    注意：上游把 `id` 之外的多数字段标成必填，但 Console 的 legacy 行会缺省，
    Koishi Schema 统一补默认值（见 hdsi/model_routing.py 的 normalizeProvider），
    因此这里统一 total=False，读取端一律 `.get()`。
    """

    # Legacy internal identifier. New Console rows derive identity from the model connection.
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
    # One model connection can be assigned directly to each HDSI task.
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
    dashscopeRegion: Literal['beijing', 'singapore', 'us']
    # Optional billing prices per one million tokens; 0 disables cost logging.
    priceInput: float
    priceOutput: float
    priceCachedInput: float


class FailoverConfig(TypedDict, total=False):
    """上游 export interface FailoverConfig。"""

    enabled: bool
    strategy: ProviderStrategy
    maxAttemptsPerProvider: int
    cooldownMinutes: int


class ModelConfig(TypedDict, total=False):
    """上游 export interface ModelConfig。"""

    # @deprecated Remote mode is inferred from enabled provider rows.
    mode: Literal['fallback', 'openai-compatible']
    providers: List[ProviderConfig]
    failover: FailoverConfig
    mainPrompt: str
    formatPrompt: str
    fixedPrompt: str
    stylePrompt: str
    # Central model catalogue. Task-specific settings may reference an entry by id.
    models: List['ModelProfile']
    mainModelId: str
    mainTemperature: float
    mainTopP: float
    mainMaxTokens: int
    mainTimeout: int
    mainResponseFormat: ProviderResponseFormat
    # Manual opt-in for streaming JSON transport; unavailable providers remain on full-response mode.
    mainStreamingMode: Literal['off', 'experimental']
    # cache-first reorders the user payload so stable blocks (history, memory layers) precede
    # per-turn fields, letting provider prefix caches hit across consecutive turns.
    mainPayloadOrder: Literal['legacy', 'cache-first']
    compaction: 'CompactionConfig'
    embedding: 'EmbeddingConfig'
    # OpenAI-compatible native image inputs for the current private-message turn.
    vision: 'VisionConfig'
    # OpenAI-compatible native audio inputs for the current private-message turn.
    audio: 'AudioConfig'


class VisionConfig(TypedDict, total=False):
    """上游 export interface VisionConfig。"""

    enabled: bool
    # native passes image_url to main narration; sidecar makes temporary factual observations.
    mode: Literal['native', 'sidecar']
    detail: 'VisionDetail'
    # Longest allowed image edge for native vision inputs; 0 disables downscaling.
    # Downscaling re-renders the image through the optional Puppeteer service and
    # silently passes the original through when Puppeteer is unavailable.
    maxImageDimension: Literal[0, 512, 768, 1024]


# TS: export type VisionDetail = 'low' | 'high' | 'auto'
VisionDetail = Literal['low', 'high', 'auto']


class AudioConfig(TypedDict, total=False):
    """上游 export interface AudioConfig。"""

    enabled: bool
    # SnowLuma server-side transcode container for QQ voice records.
    # Raw SILK cannot be read by multimodal models, so the OneBot get_record
    # action is always asked for this output format.
    outFormat: Literal['mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr']
    # Hard upper bound for one native audio attachment; larger files are skipped.
    maxFileSizeMB: float
    # Audio attachments accepted per incoming event.
    maxPerMessage: int


class ModelProfile(TypedDict, total=False):
    """上游 export interface ModelProfile。"""

    id: str
    label: str
    enabled: bool
    providerId: str
    model: str
    maxTokens: int
    timeout: int
    responseFormat: ProviderResponseFormat


class CompactionConfig(TypedDict, total=False):
    """上游 export interface CompactionConfig。"""

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


class EmbeddingConfig(TypedDict, total=False):
    """上游 export interface EmbeddingConfig。

    Embedding is deliberately configured separately from chat generation. A single
    provider can be reused for its credentials, while the endpoint and model may
    point at a cheaper or local vector model.
    """

    enabled: bool
    # Enable semantic query embedding on the latency-sensitive live turn.
    liveQuery: bool
    # Filter the sticker catalog to the most semantically relevant entries before injection.
    semanticStickerFilter: bool
    # Vectorize raw history entries and recall the most relevant older moments per turn.
    semanticHistory: bool
    # Reuses apiKey and extraHeaders from a configured chat provider.
    providerId: str
    modelId: str
    # OpenAI-compatible /embeddings endpoint. Leave empty to derive it from the chat endpoint.
    endpoint: str
    model: str
    # 0 omits the optional OpenAI dimensions parameter.
    dimensions: int
    timeout: int
    maxInputCharacters: int
    # Number of legacy facts to vectorize in each background maintenance pass.
    backfillBatchSize: int


# ========== 上游同区间的文件私有 interface（未 export，仅供本包标注） ==========

class ChatCompletionResponse(TypedDict, total=False):
    """上游 interface ChatCompletionResponse（未 export）。"""

    choices: List[Dict[str, Any]]  # {text?, message?: {content?, reasoning_content?, refusal?}}
    output_text: Any
    usage: Any


class EmbeddingResponse(TypedDict, total=False):
    """上游 interface EmbeddingResponse（未 export）。"""

    data: List[Dict[str, Any]]  # {embedding?: number[]}


class ChatRequestOverrides(TypedDict, total=False):
    """上游 interface ChatRequestOverrides（未 export）。"""

    model: str
    temperature: float
    topP: float
    maxTokens: int
    timeout: int
    responseFormat: ProviderResponseFormat


__all__ = [
    'ProviderResponseFormat',
    'ProviderStrategy',
    'ZhipuReasoningEffort',
    'DeepSeekThinkingMode',
    'ProviderMode',
    'ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT',
    'StickerDescription',
    'StickerDescriber',
    'VisionDescriber',
    'ProviderConfig',
    'FailoverConfig',
    'ModelConfig',
    'VisionConfig',
    'VisionDetail',
    'AudioConfig',
    'ModelProfile',
    'CompactionConfig',
    'EmbeddingConfig',
]
