# -*- coding: utf-8 -*-
"""narrator 模块入口，对应上游 src/narrator.ts。

为便于并行移植，narrator.ts 按职责拆成三个文件：
- narrator_types.py     上游 types/config 接口（约 29-234 行）
- narrator_providers.py 模型提供者与工厂（约 234-1030 行）
- narrator_prompts.py   提示词/payload/JSON 解析/Token 用量（约 1030-2155 行）

本文件只做 re-export，保证 `from .narrator import X` 与上游同名可用。
"""

from __future__ import annotations

from .narrator_types import (  # noqa: F401
    AudioConfig,
    CompactionConfig,
    DeepSeekThinkingMode,
    EmbeddingConfig,
    FailoverConfig,
    ModelConfig,
    ModelProfile,
    ProviderConfig,
    ProviderMode,
    ProviderResponseFormat,
    ProviderStrategy,
    StickerDescriber,
    StickerDescription,
    VisionConfig,
    VisionDescriber,
    VisionDetail,
    ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT,
    ZhipuReasoningEffort,
)
from .narrator_providers import (  # noqa: F401
    OpenAICompatibleEmbedder,
    OpenAICompatibleNarrator,
    SilentCompactor,
    SilentEmbedder,
    SilentNarrator,
    create_compactor,
    create_embedder,
    create_narrator,
    create_sticker_describer,
    create_vision_describer,
)
from .narrator_prompts import (  # noqa: F401
    RecentScriptOwnership,
    TokenUsageRecord,
    aggregate_token_usages,
    compact_prompt_entries,
    compact_script_tag,
    compute_token_cost,
    extract_early_narrative_reply,
    format_token_usage_line,
    normalize_decoded_decision,
    parse_token_usage,
    prompt_visible_message_content,
    recent_script_ownership,
    story_state_for_prompt,
    system_prompt,
    to_prompt_payload,
    writing_affordances,
)
