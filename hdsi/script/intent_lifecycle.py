# -*- coding: utf-8 -*-
"""意图生命周期，对应上游 `.hdsi_reference/src/script/intent-lifecycle.ts`（1.0.1-beta6-rebuild，10 行）。

这些任务有自己的执行器，绝不由在场叙事者的完成动作消费掉；待用户回应的承诺（follow-up-commitment）
在有执行器之前也保持挂起。
"""

from __future__ import annotations

from typing import Any, List

from ..types import NarrativeIntent

__all__ = ['live_narrative_intents', 'consumed_live_intent_ids']

# 上游内联的排除表，成员与顺序逐字保留。
_HOST_OWNED_INTENT_TYPES = (
    'split-message',
    'browser-research',
    'proactive-check',
    'active-consequence',
)


def live_narrative_intents(intents: List[NarrativeIntent]) -> List[NarrativeIntent]:
    """上游 liveNarrativeIntents：剔除自有执行器的任务类型，其余保持原顺序。"""
    return [intent for intent in intents if intent.get('type') not in _HOST_OWNED_INTENT_TYPES]


def consumed_live_intent_ids(intents: List[NarrativeIntent]) -> List[Any]:
    """上游 consumedLiveIntentIds：在场叙事真正消费掉的意图 id（承诺型不算消费）。"""
    return [
        intent.get('id') for intent in live_narrative_intents(intents)
        if intent.get('type') != 'follow-up-commitment'
    ]
