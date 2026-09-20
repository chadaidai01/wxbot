# -*- coding: utf-8 -*-
"""模型路由，对应上游 src/model-routing.ts（HDS-Interlude 1.0.1-beta6-rebuild）。

上游把「任务 → 连接/模型」的解析规则集中在这里，顺序是：
1) 显式勾选的任务用途开关（useForMain/useForCompaction/...）；
2) 模型目录（config.models 的 ModelProfile）指定的 providerId/model；
3) legacy 回退连接（但不把纯 Embedding/表情/识图连接当聊天回退）。

类型来源：
- ProviderConfig / ModelProfile / ModelConfig 的形状来自上游 src/narrator.ts 的 interface，
  字段默认值以 src/index.ts 的 Koishi Schema（ProviderIdentity / ProviderAssignments /
  Provider 各 mode 分支 / Model）为准；本文件用 TypedDict 只做标注，运行时不参与。
- AlterSystemConfig 复用 hdsi/types.py（上游 src/types.ts），它提供 modelId/providerId/model/enabled。

移植时逐处保留的 JS → Python 语义：
- `??`（只在 null/undefined 时兜底）用 `is None`；`||`（falsy 兜底）用 `or`，两者不混用。
- `=== false` / `=== true` 用 `is False` / `is True`，与 JS 恒等比较一致（0 不会等于 false）。
- resolve_model_target 始终返回 providerId/model/maxTokens/timeout/responseFormat 五个 key；
  上游未命中时是 undefined，这里写 None（消费端统一用 .get()）。
- resolve_assigned_only_route 的 target 与上游一样只含 providerId/model 两个 key。
- provider_key 对缺失的 label/model/endpoint 退化为空串（上游会抛 TypeError 或渲染 undefined）。
- format_model_routing 是日志文案，按上游模板字符串语义把缺失值渲染成 'undefined'；
  '未指定'/'未配置' 等中文与空格分隔逐字保留。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from .types import AlterSystemConfig

# TS: type ModelTask = 'main' | 'compaction' | 'timeline' | 'alter' | 'embedding' | 'stickers' | 'vision'
ModelTask = str
# TS: type ProviderResponseFormat = 'json-object' | 'prompt-only'
ProviderResponseFormat = str
# TS: type ProviderMode = 'openai-compatible' | 'zhipu-official' | 'openai-official'
#   | 'deepseek-official' | 'moonshot-official' | 'dashscope-official'
#   | 'siliconflow-official' | 'openrouter' | 'gemini-openai'（见 src/index.ts 的 mode 联合）
ProviderMode = str


class ProviderConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface ProviderConfig（字段名照抄 JSON key）。"""

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
    reasoningEffort: str  # ZhipuReasoningEffort = 'low' | 'high' | 'max'
    deepseekOfficial: bool
    deepseekThinking: str  # DeepSeekThinkingMode = 'disabled' | 'enabled'
    deepseekReasoningEffort: str  # ZhipuReasoningEffort
    dashscopeRegion: str  # 'beijing' | 'singapore' | 'us'
    # Optional billing prices per one million tokens; 0 disables cost logging.
    priceInput: float
    priceOutput: float
    priceCachedInput: float


class ModelProfile(TypedDict, total=False):
    """上游 src/narrator.ts interface ModelProfile（config.models 的条目）。"""

    id: str
    label: str
    enabled: bool
    providerId: str
    model: str
    maxTokens: int
    timeout: int
    responseFormat: ProviderResponseFormat


class CompactionConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface CompactionConfig（本文件只读 enabled/modelId/providerId/model）。"""

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
    """上游 src/narrator.ts interface EmbeddingConfig（本文件只读 enabled/modelId/providerId/model）。"""

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


class VisionConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface VisionConfig（vision 任务的开关，本文件不读）。"""

    enabled: bool
    mode: str  # 'native' | 'sidecar'
    detail: str  # 'low' | 'high' | 'auto'
    maxImageDimension: int  # 0 | 512 | 768 | 1024


class AudioConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface AudioConfig（本文件不读，仅为形状完整保留）。"""

    enabled: bool
    outFormat: str  # 'mp3' | 'wav' | 'ogg' | 'm4a' | 'flac' | 'amr'
    maxFileSizeMB: int
    maxPerMessage: int


class FailoverConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface FailoverConfig（本文件不读，仅为形状完整保留）。"""

    enabled: bool
    strategy: str  # 'priority' | 'round-robin'
    maxAttemptsPerProvider: int
    cooldownMinutes: int


class ModelConfig(TypedDict, total=False):
    """上游 src/narrator.ts interface ModelConfig；字段名照抄，运行时可缺省。"""

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
    mainPayloadOrder: str  # 'legacy' | 'cache-first'
    compaction: CompactionConfig
    embedding: EmbeddingConfig
    vision: VisionConfig
    audio: AudioConfig


class ResolvedModelTarget(TypedDict, total=False):
    """上游 src/model-routing.ts interface ResolvedModelTarget。

    本文件的构造函数始终写入全部 5 个 key（None 表示上游的 undefined）。
    """

    providerId: str
    model: str
    maxTokens: Optional[int]
    timeout: Optional[int]
    responseFormat: Optional[ProviderResponseFormat]


class ResolvedModelRoute(TypedDict, total=False):
    """上游 src/model-routing.ts interface ResolvedModelRoute。"""

    task: ModelTask
    target: ResolvedModelTarget
    # Ordered static candidates. Runtime cooldown may temporarily reorder them.
    providers: List[ProviderConfig]
    assigned: bool
    available: bool
    reason: str  # 'assigned-provider' | 'model-profile' | 'task-config'
    #             | 'legacy-fallback' | 'disabled' | 'unavailable'


class ModelRoutingTable(TypedDict, total=False):
    """上游 src/model-routing.ts interface ModelRoutingTable（key 顺序也一致）。"""

    providers: List[ProviderConfig]
    main: ResolvedModelRoute
    compaction: ResolvedModelRoute
    timeline: ResolvedModelRoute
    alter: ResolvedModelRoute
    embedding: ResolvedModelRoute
    stickers: ResolvedModelRoute
    vision: ResolvedModelRoute


ZHIPU_OFFICIAL_CHAT_ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'


def resolve_model_routing(config: ModelConfig, alter_config: Optional[AlterSystemConfig] = None) -> ModelRoutingTable:
    """上游 resolveModelRouting(config, alterConfig?)。"""
    providers = configured_providers(config)
    main_model_id = effective_main_model_id(config)
    main_target = resolve_model_target(config, main_model_id, '', '')

    compact = _record_or_none(config.get('compaction'))
    compaction_target = resolve_model_target(
        config,
        (compact.get('modelId') if compact else None) or main_model_id,
        compact.get('providerId') if compact else None,
        compact.get('model') if compact else None,
    )

    alter = _record_or_none(alter_config)
    alter_target = resolve_model_target(
        config,
        (alter.get('modelId') if alter else None) or main_model_id,
        alter.get('providerId') if alter else None,
        alter.get('model') if alter else None,
    )

    embedding = _record_or_none(config.get('embedding'))
    embedding_target = resolve_model_target(
        config,
        embedding.get('modelId') if embedding else None,
        embedding.get('providerId') if embedding else None,
        embedding.get('model') if embedding else None,
    )

    main = _resolve_route('main', providers, main_target, True)
    compaction = (
        _disabled_route('compaction', compaction_target)
        if compact is not None and compact.get('enabled') is False
        else _resolve_route('compaction', providers, compaction_target, True)
    )
    return {
        'providers': providers,
        'main': main,
        'compaction': compaction,
        'timeline': {**compaction, 'task': 'timeline'},
        'alter': (
            _disabled_route('alter', alter_target)
            if alter is not None and alter.get('enabled') is False
            else _resolve_route('alter', providers, alter_target, True)
        ),
        'embedding': (
            _resolve_route('embedding', providers, embedding_target, False)
            if embedding is not None and embedding.get('enabled')
            else _disabled_route('embedding', embedding_target)
        ),
        'stickers': _resolve_assigned_only_route('stickers', providers),
        'vision': _resolve_assigned_only_route('vision', providers),
    }


def resolve_model_target(
    config: ModelConfig,
    model_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
) -> ResolvedModelTarget:
    """上游 resolveModelTarget(config, modelId, providerId, model)。

    注意：modelId 先 trim，再与「未 trim 的 entry.id」严格比较（上游就是如此）。
    """
    wanted = _trim(model_id)
    selected: Optional[ModelProfile] = None
    if wanted:
        for entry in _records(config.get('models')):
            if entry.get('enabled') is not False and entry.get('id') == wanted:
                selected = entry
                break
    return {
        'providerId': (_trim(selected.get('providerId')) if selected else '') or _trim(provider_id),
        'model': (_trim(selected.get('model')) if selected else '') or _trim(model),
        'maxTokens': selected.get('maxTokens') if selected else None,
        'timeout': selected.get('timeout') if selected else None,
        'responseFormat': selected.get('responseFormat') if selected else None,
    }


def effective_main_model_id(config: ModelConfig) -> str:
    """上游 effectiveMainModelId(config)：显式 mainModelId 优先，否则唯一可用模型条目的 id。

    返回的是条目里未 trim 的原始 id（上游 `available[0].id` 就是如此）。
    """
    explicit = _trim(config.get('mainModelId'))
    if explicit:
        return explicit
    available = _enabled_model_profiles(config)
    return available[0].get('id') if len(available) == 1 else ''


def configured_providers(config: ModelConfig) -> List[ProviderConfig]:
    """上游 configuredProviders(config)：config.providers.map(normalizeProvider)。"""
    return [_normalize_provider(provider) for provider in _records(config.get('providers'))]


def uses_remote_providers(config: ModelConfig) -> bool:
    """上游 usesRemoteProviders(config)。"""
    routing = resolve_model_routing(config)
    return bool(
        routing['main']['available'] or routing['compaction']['available'] or routing['embedding']['available']
        or routing['stickers']['available'] or routing['vision']['available']
    )


def provider_key(provider: ProviderConfig) -> str:
    """上游 providerKey(provider)：id.trim() 优先，否则 label:model:endpoint。"""
    provider_id = _trim(provider.get('id'))
    if provider_id:
        return provider_id
    return f"{_trim(provider.get('label'))}:{_trim(provider.get('model'))}:{_trim(provider.get('endpoint'))}"


def is_assigned_to(provider: ProviderConfig, task: ModelTask) -> bool:
    """上游 isAssignedTo(provider, task)：嵌套三元，vision 是最终 else 分支。"""
    if task == 'main':
        return provider.get('useForMain') is True
    if task == 'compaction':
        return provider.get('useForCompaction') is True
    if task == 'alter':
        return provider.get('useForAlter') is True
    if task == 'embedding':
        return provider.get('useForEmbedding') is True
    if task == 'stickers':
        return provider.get('useForStickers') is True
    return provider.get('useForVision') is True


def format_model_routing(table: ModelRoutingTable) -> str:
    """上游 formatModelRouting(table)：`main=label/model[reason]` 空格分隔。"""
    tasks: List[ModelTask] = ['main', 'compaction', 'timeline', 'alter', 'embedding', 'stickers', 'vision']
    parts: List[str] = []
    for task in tasks:
        route = table.get(task) or {}
        providers = route.get('providers')
        provider = providers[0] if isinstance(providers, list) and providers else None
        target = route.get('target') or {}
        if route.get('assigned'):
            model = provider.get('model') if isinstance(provider, dict) else None
        else:
            model = target.get('model') or (provider.get('model') if isinstance(provider, dict) else None)
        if route.get('available'):
            label = (
                (provider.get('label') if isinstance(provider, dict) else None)
                or (provider.get('id') if isinstance(provider, dict) else None)
            )
            body = '%s/%s' % (_js_text(label), _js_text(model) if model else '未指定')
        else:
            body = '未配置'
        parts.append('%s=%s[%s]' % (task, body, _js_text(route.get('reason'))))
    return ' '.join(parts)


# ========== 上游模块内私有函数 ==========

def _resolve_route(
    task: ModelTask,
    providers: List[ProviderConfig],
    target: ResolvedModelTarget,
    require_chat_model: bool,
) -> ResolvedModelRoute:
    """上游 resolveRoute(task, providers, target, requireChatModel)。"""
    assigned = [
        provider for provider in providers
        if provider.get('enabled') and provider.get('endpoint') and provider.get('model') and is_assigned_to(provider, task)
    ]
    if assigned:
        return {'task': task, 'target': target, 'providers': assigned, 'assigned': True, 'available': True, 'reason': 'assigned-provider'}

    target_provider_id = target.get('providerId')
    targeted = [
        provider for provider in providers
        if provider.get('enabled') and provider.get('endpoint')
        and (provider.get('id') == target_provider_id or provider_key(provider) == target_provider_id)
    ] if target_provider_id else []
    targeted_usable = [provider for provider in targeted if target.get('model') or provider.get('model')]
    if targeted_usable:
        profile_selected = bool(target_provider_id) and bool(target.get('model'))
        return {
            'task': task,
            'target': target,
            'providers': targeted_usable,
            'assigned': False,
            'available': True,
            'reason': 'model-profile' if profile_selected else 'task-config',
        }

    # Keep legacy installations operational, but never treat an embedding-only
    # connection as a chat fallback. Explicit task assignment remains preferred.
    fallback = [
        provider for provider in providers
        if provider.get('enabled') and provider.get('endpoint')
        and (not require_chat_model or bool(provider.get('model')))
        and not _is_exclusively_non_chat(provider)
    ]
    if fallback:
        return {'task': task, 'target': target, 'providers': fallback, 'assigned': False, 'available': True, 'reason': 'legacy-fallback'}
    return {'task': task, 'target': target, 'providers': [], 'assigned': False, 'available': False, 'reason': 'unavailable'}


def _resolve_assigned_only_route(task: ModelTask, providers: List[ProviderConfig]) -> ResolvedModelRoute:
    """上游 resolveAssignedOnlyRoute(task, providers)（stickers / vision 专用）。"""
    assigned = [
        provider for provider in providers
        if provider.get('enabled') and provider.get('endpoint') and provider.get('model') and is_assigned_to(provider, task)
    ]
    first = assigned[0] if assigned else None
    first_id = first.get('id') if first is not None else None
    first_model = first.get('model') if first is not None else None
    return {
        'task': task,
        # 上游这里只构造 providerId/model 两个 key，且用的是 ??（空串保留，只有缺省才补 ''）。
        'target': {
            'providerId': '' if first_id is None else first_id,
            'model': '' if first_model is None else first_model,
        },
        'providers': assigned,
        'assigned': len(assigned) > 0,
        'available': len(assigned) > 0,
        'reason': 'assigned-provider' if assigned else 'unavailable',
    }


def _disabled_route(task: ModelTask, target: ResolvedModelTarget) -> ResolvedModelRoute:
    """上游 disabledRoute(task, target)。"""
    return {'task': task, 'target': target, 'providers': [], 'assigned': False, 'available': False, 'reason': 'disabled'}


def _is_exclusively_non_chat(provider: ProviderConfig) -> bool:
    """上游 isExclusivelyNonChat(provider)。"""
    chat = provider.get('useForMain') or provider.get('useForCompaction') or provider.get('useForAlter')
    sidecar = provider.get('useForEmbedding') or provider.get('useForStickers') or provider.get('useForVision')
    return bool(sidecar) and not chat


def _enabled_model_profiles(config: ModelConfig) -> List[ModelProfile]:
    """上游 enabledModelProfiles(config)：id/providerId/model 三者 trim 后都非空且未禁用。"""
    return [
        entry for entry in _records(config.get('models'))
        if entry.get('enabled') is not False
        and _trim(entry.get('id'))
        and _trim(entry.get('providerId'))
        and _trim(entry.get('model'))
    ]


def _normalize_provider(provider: ProviderConfig) -> ProviderConfig:
    """上游 normalizeProvider(provider)：填充 preset endpoint 与所有默认值（?? 语义）。"""
    zhipu_official = provider.get('mode') == 'zhipu-official'
    deepseek_official = provider.get('mode') == 'deepseek-official'
    official_endpoint = _preset_endpoint(provider.get('mode'), provider.get('dashscopeRegion'))
    return {
        **provider,
        'id': _trim(provider.get('id')) or '%s:%s' % (_trim(provider.get('label')) or 'provider', _trim(provider.get('model')) or ''),
        'label': _trim(provider.get('label')) or (
            'Zhipu Official' if zhipu_official else 'DeepSeek Official' if deepseek_official else 'Model connection'
        ),
        'endpoint': official_endpoint or provider.get('endpoint'),
        'apiKey': _coalesce(provider.get('apiKey'), ''),
        'model': _coalesce(provider.get('model'), ''),
        'temperature': _coalesce(provider.get('temperature'), 1 if zhipu_official else 0.8),
        'topP': _coalesce(provider.get('topP'), 0.95 if zhipu_official else 1),
        'maxTokens': _coalesce(provider.get('maxTokens'), 4096),
        'timeout': _coalesce(provider.get('timeout'), 45_000 if zhipu_official else 60_000),
        'responseFormat': _coalesce(provider.get('responseFormat'), 'json-object'),
        'extraHeaders': _coalesce(provider.get('extraHeaders'), ''),
        'extraBody': _coalesce(provider.get('extraBody'), ''),
        'zhipuOfficial': zhipu_official,
        'reasoningEffort': _coalesce(provider.get('reasoningEffort'), 'high'),
        'deepseekOfficial': deepseek_official,
        'deepseekThinking': 'enabled' if provider.get('deepseekThinking') == 'enabled' else 'disabled',
        'deepseekReasoningEffort': _coalesce(provider.get('deepseekReasoningEffort'), 'low'),
        'useForMain': provider.get('useForMain') is True,
        'useForCompaction': provider.get('useForCompaction') is True,
        'useForAlter': provider.get('useForAlter') is True,
        'useForEmbedding': provider.get('useForEmbedding') is True,
        'useForStickers': provider.get('useForStickers') is True,
        'useForVision': provider.get('useForVision') is True,
    }


def _preset_endpoint(mode: Optional[ProviderMode], dashscope_region: Optional[str] = None) -> str:
    """上游 presetEndpoint(mode, dashscopeRegion)。"""
    if mode == 'zhipu-official':
        return ZHIPU_OFFICIAL_CHAT_ENDPOINT
    if mode == 'openai-official':
        return 'https://api.openai.com/v1/chat/completions'
    if mode == 'deepseek-official':
        return 'https://api.deepseek.com/v1/chat/completions'
    if mode == 'moonshot-official':
        return 'https://api.moonshot.cn/v1/chat/completions'
    if mode == 'siliconflow-official':
        return 'https://api.siliconflow.cn/v1/chat/completions'
    if mode == 'openrouter':
        return 'https://openrouter.ai/api/v1/chat/completions'
    if mode == 'gemini-openai':
        return 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    if mode == 'dashscope-official':
        if dashscope_region == 'singapore':
            return 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions'
        if dashscope_region == 'us':
            return 'https://dashscope-us.aliyuncs.com/compatible-mode/v1/chat/completions'
        return 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
    return ''


# ========== 小工具（与 JS 运算符一一对应） ==========

def _trim(value: Any) -> str:
    """TS: value?.trim()；缺省/非字符串按上游的 undefined 语义退化为 ''。"""
    return value.strip() if isinstance(value, str) else ''


def _coalesce(value: Any, fallback: Any) -> Any:
    """TS: value ?? fallback（只有 None/undefined 才兜底）。"""
    return fallback if value is None else value


def _record_or_none(value: Any) -> Optional[Dict[str, Any]]:
    """TS: config.compaction / config.embedding / alterConfig 这类可选对象。"""
    return value if isinstance(value, dict) else None


def _records(value: Any) -> List[Dict[str, Any]]:
    """上游 providers / models 是对象数组；非数组或非对象条目直接忽略。"""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _js_text(value: Any) -> str:
    """TS 模板字符串插值：缺失值（undefined）渲染为 'undefined'，保证日志逐字一致。"""
    if value is None:
        return 'undefined'
    if isinstance(value, str):
        return value
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
