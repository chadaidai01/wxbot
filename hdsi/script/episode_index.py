# -*- coding: utf-8 -*-
"""剧集索引 / 摘录 / 标签，对应上游 `.hdsi_reference/src/script/episode-index.ts`（1.0.1-beta6-rebuild，75 行）。

对「已经做过可见性过滤」的原文条目重建导航：
- 一个 frame 标识一个场景；时间间隔与消息条数都不切分它；没有 frame 的旧条目保持单条锚点。
- `episodeExcerpt` 先保住真实命中，再取最近的原文；source ids 永远只描述结果里包含的文本，
  不包含被省略的那部分剧集。

依赖关系：上游是单向 `./recall-navigation`（recall-navigation 不反向 import 本文件）。
本移植用 Python 相对导入 `from .recall_navigation import ...`，函数名与上游一致地 snake_case。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from .recall_navigation import RecallSpan, index_original, original_window, score_original

__all__ = [
    'EpisodeSource',
    'build_episode_index',
    'episode_excerpt',
    'grounded_episode_tags',
    'episode_tag_score',
]

# 上游 Number.isSafeInteger 的安全整数上界：2 ** 53 - 1。
_SAFE_INTEGER_MAX = 9007199254740991

# 上游 groundedEpisodeTags 内联的标签键，顺序逐字保留。
_TAG_KEYS = ('people', 'places', 'objects', 'topics', 'commitments', 'outcomes', 'dates')


class _EpisodeCheckpoint(TypedDict):
    """上游 EpisodeSource 内联的 `{ sceneId; firstEntryId; lastEntryId }`。"""

    sceneId: int
    firstEntryId: int
    lastEntryId: int


class EpisodeSource(TypedDict, total=False):
    """上游 export interface EpisodeSource（content/occurredAt/participantId/kind 必填，其余可选）。"""

    content: str
    occurredAt: str
    participantId: str
    kind: str
    frameId: str
    tags: List[str]
    spans: List[RecallSpan]
    checkpoint: _EpisodeCheckpoint


def _is_safe_integer(value: Any) -> bool:
    """对应 JS Number.isSafeInteger。"""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -_SAFE_INTEGER_MAX <= value <= _SAFE_INTEGER_MAX
    )


def _source_of(row: Any) -> Dict[str, Any]:
    """取 `[id, EpisodeSource]` 里的 EpisodeSource；形状不对时退化成空 dict。"""
    return row[1] if isinstance(row, (list, tuple)) and len(row) > 1 and isinstance(row[1], dict) else {}


def build_episode_index(rows: List[Tuple[int, EpisodeSource]]) -> Dict[int, List[int]]:
    """上游 buildEpisodeIndex：返回 `Map<entryId, 同组 entryIds>`（Python dict 同语义）。"""
    groups: Dict[str, List[int]] = {}
    # 上游只保留 firstEntryId / lastEntryId 都是安全整数的 checkpoint，并保持原顺序。
    checkpoints: List[Dict[str, Any]] = []
    for row in rows:
        checkpoint = _source_of(row).get('checkpoint')
        if (
            isinstance(checkpoint, dict)
            and _is_safe_integer(checkpoint.get('firstEntryId'))
            and _is_safe_integer(checkpoint.get('lastEntryId'))
        ):
            checkpoints.append(checkpoint)
    for row in rows:
        entry_id = row[0]
        source = _source_of(row)
        checkpoint = next(
            (
                item for item in checkpoints
                if entry_id >= item['firstEntryId'] and entry_id <= item['lastEntryId']
            ),
            None,
        )
        if checkpoint is not None:
            group_key = f"scene:{checkpoint.get('sceneId')}"
        else:
            frame_id = source.get('frameId')
            # 上游 `row.frameId || \`entry:${id}\``：空字符串也回落到 entry:id。
            group_key = frame_id if frame_id else f'entry:{entry_id}'
        # 上游 `JSON.stringify([row.participantId, key])`：紧凑分隔符、非 ASCII 原样；仅作分组键。
        key = json.dumps(
            [source.get('participantId'), group_key],
            ensure_ascii=False,
            separators=(',', ':'),
        )
        groups.setdefault(key, []).append(entry_id)
    by_entry: Dict[int, List[int]] = {}
    # 上游把同一个数组实例挂到组内每个 entry 上；这里同样共享同一个 list。
    for ids in groups.values():
        for entry_id in ids:
            by_entry[entry_id] = ids
    return by_entry


def episode_excerpt(
    rows: List[Tuple[int, EpisodeSource]],
    anchor_id: int,
    budget: int = 4000,
    query_keys: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """上游 episodeExcerpt：返回 `{'sourceEntryIds': ..., 'content': ...}`，锚点不存在则 None。"""
    if query_keys is None:
        query_keys = []
    position = -1
    for index, row in enumerate(rows):
        if row[0] == anchor_id:
            position = index
            break
    if position < 0:
        return None
    selected: Dict[int, str] = {}
    remaining = budget
    for index in (position, position + 1, position - 1, position + 2, position - 2):
        # JS `rows[index]`：越界与负下标都是 undefined；Python 负下标会回卷，必须显式挡掉。
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        if remaining < 40:
            continue
        entry_id = row[0]
        source = _source_of(row)
        content = source.get('content') if isinstance(source.get('content'), str) else ''
        # 上游 `if (index !== position && row[1].content.length + 100 > remaining) continue`。
        if index != position and len(content) + 100 > remaining:
            continue
        kind = source.get('kind')
        owner = (
            'user-delivered-message' if kind == 'user-message'
            else 'protagonist-narrative' if kind == 'script'
            else 'group-member-message' if kind == 'group-message'
            else 'protagonist-delivered-message'
        )
        # 上游 `row[1].spans ??= indexOriginal(row[1].content)`：就地 memo，只有缺失/None 才重建。
        spans = source.get('spans')
        if spans is None:
            spans = index_original(content)
            source['spans'] = spans
        hit = score_original(query_keys, spans)
        window = original_window(content, spans, hit.get('index', 0), max(0, remaining - 180), query_keys)
        partial = window['start'] > 0 or window['end'] < len(content)
        # 注：上游窗口坐标 / 长度都是 UTF-16 码元；这里用 Python 码点（len/切片）计数，
        # 对 BMP 文本（含常用汉字）逐字等价，只有代理对（emoji 等）会让编号比上游小。
        # 保持「content == content[start:end]」的自洽性优先（上游测试亦如此断言），不做码元索引。
        annotation = (
            f"; source-view UTF-16 [{window['start']},{window['end']})/{len(content)}, not a complete event"
            if partial
            else ''
        )
        text = f"[{source.get('occurredAt')}; {owner}; entry:{entry_id}{annotation}] {window['content']}"
        if index != position and len(text) > remaining:
            continue
        selected[entry_id] = text
        remaining -= len(text) + 1
    ordered = [row for row in rows if row[0] in selected]
    return {
        'sourceEntryIds': [row[0] for row in ordered],
        'content': '\n'.join(selected[row[0]] for row in ordered),
    }


def grounded_episode_tags(content: str, draft: Dict[str, Any]) -> Dict[str, List[str]]:
    """上游 groundedEpisodeTags：标签必须是原文里的字面导航 span，不是生成的摘要或事实。"""
    result: Dict[str, List[str]] = {}
    source = draft if isinstance(draft, dict) else {}
    for key in _TAG_KEYS:
        values = source.get(key)
        if not isinstance(values, list):
            continue
        # 上游 [...new Set(values.filter(value => typeof value === 'string' && value.trim().length >= 2
        #   && value.length <= 100 && content.includes(value)))].slice(0, 8)。
        grounded = list(dict.fromkeys(
            value for value in values
            if isinstance(value, str)
            and len(value.strip()) >= 2
            and len(value) <= 100
            and value in content
        ))[:8]
        if grounded:
            result[key] = grounded
    return result


def episode_tag_score(query: str, tags: Optional[List[str]] = None) -> float:
    """上游 episodeTagScore：任一标签字面出现在 query 里即 0.8，否则 0。"""
    values = tags if isinstance(tags, list) else []
    return 0.8 if any(isinstance(tag, str) and tag in query for tag in values) else 0
