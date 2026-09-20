# -*- coding: utf-8 -*-
"""连续性检查点，对应上游 `.hdsi_reference/src/script/continuity-checkpoint.ts`（1.0.1-beta6-rebuild，21 行）。

检查点消费的是连续前缀：这里刻意保留最新后缀会永久跳过更早的证据（lastEntryId 前移时）。
空 / 静默的 provider 响应必须抛错，让证据留在原地等待重试。
"""

from __future__ import annotations

from typing import Any, List

from ..types import CompactionDecision, ScriptEntry

__all__ = ['compaction_prefix', 'assert_continuity_review']


def _content_length(entry: Any) -> int:
    """上游 `entry.content.length`（JS 按 UTF-16 码元计数，Python len() 对 BMP 文本等价）。"""
    content = entry.get('content') if isinstance(entry, dict) else None
    return len(content) if isinstance(content, str) else 0


def compaction_prefix(entries: List[ScriptEntry], budget: int) -> List[ScriptEntry]:
    """上游 compactionPrefix：按字符预算取连续前缀，即使超预算也至少保留第一条。"""
    selected: List[ScriptEntry] = []
    used = 0
    for entry in entries:
        length = _content_length(entry)
        if selected and used + length > budget:
            break
        selected.append(entry)
        used += length
    return selected


def assert_continuity_review(decision: CompactionDecision) -> None:
    """上游 assertContinuityReview：scene 与 arc 都必须有非空 summary，否则保留检查点等待重试。"""
    scene = decision.get('scene') if isinstance(decision, dict) else None
    arc = decision.get('arc') if isinstance(decision, dict) else None
    scene_summary = scene.get('summary') if isinstance(scene, dict) else None
    arc_summary = arc.get('summary') if isinstance(arc, dict) else None
    if (
        not (isinstance(scene_summary, str) and scene_summary.strip())
        or not (isinstance(arc_summary, str) and arc_summary.strip())
    ):
        # 上游 `throw new Error(...)`；Python 里语义最近似的是 RuntimeError，文案逐字保留。
        raise RuntimeError(
            'Continuity review needs both scene and arc summaries; checkpoint retained for retry'
        )
