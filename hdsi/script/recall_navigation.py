# -*- coding: utf-8 -*-
"""本地召回导航，对应上游 `.hdsi_reference/src/script/recall-navigation.ts`（1.0.1-beta6-rebuild，70 行）。

只在本地做导航：这些 span 永不替换不可变原文，只用来给窗口定位。
- `RecallSpan.keys` 对应上游 `Set<string>`，本移植用 Python 的 `set`（只做 O(1) 成员判定，
  与上游一致是不参与 JSON 序列化的内部结构）。
- JS 字符串按 UTF-16 码元计数与切片；Python len()/切片按码点，对 BMP 文本（含常用汉字）逐字等价。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Set, TypedDict

__all__ = ['RecallSpan', 'recall_keys', 'index_original', 'score_original', 'original_window', 'recall_focus']


class RecallSpan(TypedDict):
    """上游 export interface RecallSpan。"""

    start: int
    end: int
    keys: Set[str]


# 上游 `/[a-z0-9]{2,}/g` 与 `/[\u3400-\u9fff]{2,}/g`（CJK 扩展 A + 基本区）。
_WORD = re.compile(r'[a-z0-9]{2,}')
_CJK_RUN = re.compile(r'[\u3400-\u9fff]{2,}')
# 上游 `/[^。！？\n]+[。！？\n]*|[。！？\n]+/g`：句边界让条件与它所属的句子待在一起，不同于按 token 切。
_SENTENCE = re.compile(r'[^。！？\n]+[。！？\n]*|[。！？\n]+')


def recall_keys(text: str) -> List[str]:
    """上游 recallKeys：英文/数字词 + 中文 run 的相邻二字组，`[...new Set(words)]` 去重保序。"""
    normalized = text.lower()
    words: List[str] = _WORD.findall(normalized)
    for run in _CJK_RUN.findall(normalized):
        for index in range(len(run) - 1):
            words.append(run[index:index + 2])
    return list(dict.fromkeys(words))


def index_original(content: str) -> List[RecallSpan]:
    """上游 indexOriginal：按句切 span，单个 span 超过 700 字时先断开。"""
    spans: List[RecallSpan] = []
    start = 0
    end = 0

    def push() -> None:
        nonlocal start
        if end > start:
            spans.append({'start': start, 'end': end, 'keys': set(recall_keys(content[start:end]))})
        start = end

    for sentence in _SENTENCE.finditer(content):
        text = sentence.group(0)
        if end > start and end - start + len(text) > 700:
            push()
        end = sentence.start() + len(text)
    push()
    return spans


def _span_at(spans: List[RecallSpan], index: Any) -> Optional[RecallSpan]:
    """JS `spans[index]`：越界与负下标都是 undefined；Python 负下标会回卷，必须显式挡掉。"""
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index < 0 or index >= len(spans):
        return None
    span = spans[index]
    return span if isinstance(span, dict) else None


def score_original(keys: List[str], spans: List[RecallSpan]) -> Dict[str, Any]:
    """上游 scoreOriginal：key 命中占比最高的 span；没有 keys 时固定 `{'score': 0, 'index': 0}`。"""
    score: Any = 0
    index = 0
    if not keys:
        return {'score': score, 'index': index}
    for position in range(len(spans)):
        span_keys = spans[position].get('keys') if isinstance(spans[position], dict) else None
        value = sum(1 for key in keys if span_keys is not None and key in span_keys) / len(keys)
        if value > score:
            score = value
            index = position
    return {'score': score, 'index': index}


def original_window(
    content: str,
    spans: List[RecallSpan],
    index: int,
    budget: int,
    query_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """上游 originalWindow：围绕命中句取窗口，向可容纳的相邻句扩张，最后用 budget 截断。"""
    if query_keys is None:
        query_keys = []
    if len(content) <= budget:
        return {'content': content, 'start': 0, 'end': len(content)}
    anchor = _span_at(spans, index)
    if anchor is None:
        return {'content': content[:budget], 'start': 0, 'end': min(budget, len(content))}
    start = anchor['start']
    end = anchor['end']
    if end - start > budget and query_keys:
        lower = content.lower()
        # 上游 queryKeys.map(key => lower.indexOf(key, anchor.start)).filter(offset => offset >= start && offset < end)。
        offsets = [
            offset for offset in (
                lower.find(key, anchor['start']) if isinstance(key, str) else -1
                for key in query_keys
            )
            if offset >= start and offset < end
        ]
        if offsets:
            hit = min(offsets)
            start = max(start, min(hit - math.floor(budget / 3), end - budget))
    for neighbor in (_span_at(spans, index - 1), _span_at(spans, index + 1)):
        if neighbor is not None and max(end, neighbor['end']) - min(start, neighbor['start']) <= budget:
            start = min(start, neighbor['start'])
            end = max(end, neighbor['end'])
    # 单句超长时明确是摘录，而不是完整记录。
    end = min(end, start + budget)
    return {'content': content[start:end], 'start': start, 'end': end}


def recall_focus(message: Optional[str], topics: List[str], intent_summaries: List[str]) -> str:
    """上游 recallFocus：有界查询线索；计划只负责导航到证据，永不成为证据本身。"""
    trimmed = [
        value.strip() for value in list(intent_summaries) + list(topics)
        if isinstance(value, str) and value.strip()
    ]
    cues = list(dict.fromkeys(trimmed))[:3]
    head = message.strip()[:400] if isinstance(message, str) else None
    return '\n'.join(part for part in [head] + [cue[:80] for cue in cues] if part)
