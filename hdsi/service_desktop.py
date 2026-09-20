# -*- coding: utf-8 -*-
"""上游 src/service.ts 行 769-931：typ-0 桌面端 / worker 运行时与时间线投影。

对应 HDS-Interlude 1.0.1-beta6-rebuild（Gitee MomoiCore/hds-interlude master）。
本文件只包含 ``ServiceDesktopMixin``：

- setDesktopRuntimePhase（769-798）
- desktopRuntimeSnapshot（800-807）
- desktopTimelineSnapshot（809-844）
- desktopPurgeRange（846-862）
- desktopTimelineRange（864-909）
- setDesktopCursorAt（911-917）
- receiveDesktopEvent（918-926）

以及这些方法引用的上游 65-180 模块级 helper：

- DesktopTimelineTrack / DesktopTimelineRangeRequest 类型（65-74，类型本身归
  ``hdsi/service_types.py`` 所有，这里只导入使用）；
- DESKTOP_TIMELINE_TRACKS / desktopTimelineTrackForEntry / parseDesktopTimelineDate /
  normalizeDesktopTimelineRangeRequest / desktopRangeOverlaps /
  desktopDeliveryRealitySummary / desktopSceneCheckpoint /
  desktopTimelineEntryView / emptyDesktopTimelineRange（76-170）。

移植约定（见 hdsi/PORTING_GUIDE.md / hdsi/SERVICE_PORTING_SPEC.md）：

- 同步化：上游 async/Promise 去掉；``db_get`` / ``db_set`` / ``serial`` 等由
  ``ServiceBaseMixin`` 提供。
- ``desktopTimelineEntryView`` 属于上游 65-180 段，当前 ``hdsi/service_helpers.py``
  只负责 7009-8524 且尚未导出该名字；因此这里优先尝试从 ``service_helpers`` 导入
  同名实现，缺名时在本文件内按上游逐行实现（见下方 try/except）。其余 helper
  上游没有 export，直接在本文件实现。
- 字段名与 JSON key 保持 camelCase；日志/错误文案逐字保留。
- Python 3.9：不使用 ``X | Y`` 注解、不用 match、不用 ``dict | dict``。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from .desktop_bridge import DesktopInboundEvent, DesktopRuntimePhase
from .platform.session import InboundSession
from .service_types import DesktopTimelineRangeRequest, DesktopTimelineTrack
from .time_utils import iso, now_utc, parse_time
from .utils import is_array

__all__ = [
    'ServiceDesktopMixin',
    'DESKTOP_TIMELINE_TRACKS',
    'desktop_timeline_track_for_entry',
    'parse_desktop_timeline_date',
    'normalize_desktop_timeline_range_request',
    'desktop_range_overlaps',
    'desktop_delivery_reality_summary',
    'desktop_scene_checkpoint',
    'desktop_timeline_entry_view',
    'empty_desktop_timeline_range',
]

# ===================== JS 语义小工具（本区间专用，非上游导出） =====================

# JS undefined：JSON null 在 Python 里是 None，缺 key / 无属性用该哨兵区分。
_UNDEFINED = object()
# JS Number.MAX_SAFE_INTEGER
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _field(value: Any, key: str, default: Any = None) -> Any:
    """dict / DTO 字段读取（None 安全），对应 TS 的 ``value?.[key]``。"""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _js_string(value: Any) -> str:
    """TS ``String(value)``：undefined → 'undefined'，null → 'null'，bool → 'true'/'false'。"""
    if value is _UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        return _js_number_text(value)
    if isinstance(value, int):
        return str(value)
    return str(value)


def _js_number_text(number: float) -> str:
    """JS 里 ``${number}`` / ``String(number)`` 的文本形式。"""
    if math.isfinite(number) and number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER:
        return str(int(number))
    return repr(number)


def _js_number(value: Any) -> Optional[float]:
    """TS ``Number(value)``；不可转换的 undefined / NaN 返回 None。

    注意：与 hdsi/service_helpers.py 的同名内部工具不同，这里把 ``None`` 当作 JSON
    null（JS ``Number(null) === 0``）；真正的 undefined 用 ``_UNDEFINED`` 哨兵表示。
    """
    if value is _UNDEFINED:
        return None
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            # JS 支持 0x / 0o / 0b 前缀（如 Number('0x10') === 16）。
            try:
                return float(int(text, 0))
            except (TypeError, ValueError):
                return None
    return None


def _js_number_value(number: float) -> Any:
    """把内部 float 还原成 JS number 的 Python 表示：安全整数用 int，其余用 float。"""
    if math.isfinite(number) and number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER:
        return int(number)
    return number


def _js_is_finite_number(value: Any) -> bool:
    """JS ``Number.isFinite``（只对 number 类型且有限返回 true）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _js_is_safe_integer_number(value: Any) -> bool:
    """JS ``Number.isSafeInteger``。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    number = float(value)
    return math.isfinite(number) and number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER


def _js_truthy(value: Any) -> bool:
    """JS truthiness：0 / NaN / '' / null / undefined 为假，空 dict / 空 list 为真。"""
    if value is _UNDEFINED or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ''
    return True


def _js_floor(number: float) -> float:
    """JS ``Math.floor``：NaN / ±Infinity 原样返回，不抛 ValueError/OverflowError。"""
    if not math.isfinite(number):
        return number
    return float(math.floor(number))


# ===================== 上游 65-170：时间线投影 helper =====================

# 上游 76：const DESKTOP_TIMELINE_TRACKS: DesktopTimelineTrack[] = [...]
DESKTOP_TIMELINE_TRACKS: List[DesktopTimelineTrack] = [
    'script', 'messages', 'system', 'scenes', 'facts', 'preplan',
]


def desktop_timeline_track_for_entry(entry: Any) -> DesktopTimelineTrack:
    """上游 78-82 desktopTimelineTrackForEntry。"""
    kind = _field(entry, 'kind')
    actor = _field(entry, 'actor')
    if kind == 'script' or actor == 'narrator':
        return 'script'
    if re.search(r'message|chat|reply', _js_string(kind)) is not None:
        return 'messages'
    return 'system'


def parse_desktop_timeline_date(value: Any) -> Optional[datetime]:
    """上游 84-88 parseDesktopTimelineDate：只接受字符串，解析失败返回 undefined。"""
    if not isinstance(value, str):
        return None
    return parse_time(value)


def normalize_desktop_timeline_range_request(value: Any) -> Dict[str, Any]:
    """上游 90-105 normalizeDesktopTimelineRangeRequest。

    返回的 ``from`` / ``to`` 是 datetime，``tracks`` 是 set，``cursorId`` 是 int/float 或
    None，``limit`` / ``detailLevel`` 与上游一致。
    """
    now = now_utc()
    from_ = parse_desktop_timeline_date(_field(value, 'from', _UNDEFINED))
    if from_ is None:
        from_ = now - timedelta(hours=2)  # 上游 `now.getTime() - 2 * Time.hour`
    to = parse_desktop_timeline_date(_field(value, 'to', _UNDEFINED))
    if to is None:
        to = now + timedelta(hours=12)  # 上游 `now.getTime() + 12 * Time.hour`
    if to < from_:
        from_, to = to, from_
    # A desktop viewport never needs an accidental multi-year dump. Paging is
    # the explicit route for history, and this cap protects the worker database.
    max_window = timedelta(days=14)  # 上游 `14 * Time.day`
    if to - from_ > max_window:
        to = from_ + max_window

    raw_tracks = _field(value, 'tracks', _UNDEFINED)
    if not is_array(raw_tracks):
        raw_tracks = DESKTOP_TIMELINE_TRACKS
    tracks: Set[DesktopTimelineTrack] = set()
    for track in raw_tracks:
        if track in DESKTOP_TIMELINE_TRACKS:
            tracks.add(track)
    if not tracks:
        tracks.update(DESKTOP_TIMELINE_TRACKS)  # 上游 `if (!tracks.size) DESKTOP_TIMELINE_TRACKS.forEach(...)`

    cursor = _field(value, 'cursor', _UNDEFINED)
    cursor_id: Any = None
    if isinstance(cursor, str) and re.fullmatch(r'entry:[0-9]+', cursor) is not None:
        # 上游 `/^entry:\d+$/u.test(cursor)` + `Number(value.cursor.slice('entry:'.length))`
        number = _js_number(cursor[len('entry:'):])
        if _js_is_finite_number(number):
            cursor_id = _js_number_value(number)

    limit_number = _js_number(_field(value, 'limit', _UNDEFINED))
    if limit_number is None or math.isnan(limit_number) or limit_number == 0:
        limit_number = 240.0  # 上游 `Number(value.limit) || 240`
    limit_number = _js_floor(limit_number)
    limit_number = min(limit_number, 500.0)  # 上游 Math.min(..., 500)
    limit_number = max(limit_number, 1.0)    # 上游 Math.max(1, ...)
    limit = int(limit_number)

    return {
        'from': from_,
        'to': to,
        'tracks': tracks,
        'cursorId': cursor_id,
        'limit': limit,
        # 上游 `value.detailLevel === 'full' ? 'full' : 'summary' as const`
        'detailLevel': 'full' if _field(value, 'detailLevel', _UNDEFINED) == 'full' else 'summary',
    }


def desktop_range_overlaps(from_: datetime, to: datetime, start: Optional[datetime],
                           end: Optional[datetime] = None) -> bool:
    """上游 107-110 desktopRangeOverlaps。"""
    final = start if end is None else end  # 上游 `end ?? start`
    if start is None or final is None:
        # JS 里 undefined 参与比较结果为 false；这里显式等价返回，避免 TypeError。
        return False
    return start <= to and final >= from_


def desktop_delivery_reality_summary(entry: Any) -> Optional[List[Dict[str, Any]]]:
    """上游 113-131 desktopDeliveryRealitySummary（Protocol 4 投递账本投影）。"""
    actions = _field(_field(entry, 'metadata'), 'deliveryActions')
    if not is_array(actions) or not actions:
        return None
    items: List[Dict[str, Any]] = []
    for action in actions:
        if not _js_truthy(action) or not isinstance(action, dict):
            continue
        event_id = action.get('eventId')
        segments = action.get('segments')
        if not isinstance(event_id, str) or not is_array(segments):
            continue
        mapped_segments: List[Dict[str, Any]] = []
        for segment in segments:
            if not _js_truthy(segment) or not isinstance(segment, dict):
                continue
            content = segment.get('content')
            status = segment.get('status')
            if not isinstance(content, str) or not isinstance(status, str):
                continue
            kind_value = _field(segment, 'kind', _UNDEFINED)
            if kind_value is None or kind_value is _UNDEFINED:
                kind_value = 'message'  # 上游 `String(segment.kind ?? 'message')`
            mapped_segments.append({
                'kind': _js_string(kind_value),
                'status': _js_string(status),
                'content': _js_string(content),
            })
        items.append({
            'eventId': _js_string(event_id),
            'eventKind': _js_string(action.get('eventKind')) if isinstance(action.get('eventKind'), str)
            else 'outgoing-message',
            'status': _js_string(action.get('status')) if isinstance(action.get('status'), str) else 'pending',
            'segments': mapped_segments,
        })
    items = [item for item in items if item['segments']]
    return items or None


def desktop_scene_checkpoint(entry: Any) -> Optional[Dict[str, Any]]:
    """上游 133-149 desktopSceneCheckpoint（V2 commit ledger 的场景断点投影）。"""
    checkpoint = _field(_field(entry, 'metadata'), 'sceneCheckpoint')
    if not _js_truthy(checkpoint):
        return None
    scene_id = _js_number(_field(checkpoint, 'sceneId', _UNDEFINED))
    if not _js_is_finite_number(scene_id):
        return None
    started_at = parse_desktop_timeline_date(_field(checkpoint, 'startedAt', _UNDEFINED))
    if started_at is None:
        return None
    out: Dict[str, Any] = {
        'sceneId': _js_number_value(scene_id),
        'startedAt': iso(started_at),
    }
    ended_at = parse_desktop_timeline_date(_field(checkpoint, 'endedAt', _UNDEFINED))
    if ended_at is not None:
        out['endedAt'] = iso(ended_at)
    reason = _field(checkpoint, 'reason', _UNDEFINED)
    if isinstance(reason, str):
        out['reason'] = reason
    first_entry_id = _js_number(_field(checkpoint, 'firstEntryId', _UNDEFINED))
    if _js_is_safe_integer_number(first_entry_id):
        out['firstEntryId'] = _js_number_value(first_entry_id)
    last_entry_id = _js_number(_field(checkpoint, 'lastEntryId', _UNDEFINED))
    if _js_is_safe_integer_number(last_entry_id):
        out['lastEntryId'] = _js_number_value(last_entry_id)
    boundary = _field(checkpoint, 'boundarySourceEntryIds', _UNDEFINED)
    if is_array(boundary):
        boundary_ids: List[Any] = []
        for item in boundary:
            number = _js_number(item)
            if _js_is_safe_integer_number(number):
                boundary_ids.append(_js_number_value(number))
        out['boundarySourceEntryIds'] = boundary_ids
    return out


def _desktop_timeline_entry_view_fallback(entry: Any) -> Dict[str, Any]:
    """上游 151-165 desktopTimelineEntryView（service_helpers 缺名时的本文件实现）。"""
    metadata = _field(entry, 'metadata')
    window = _field(metadata, 'timelineWindow')
    started_at = parse_desktop_timeline_date(_field(window, 'from', _UNDEFINED))
    if started_at is None:
        started_at = _field(entry, 'occurredAt', _UNDEFINED)  # 上游 `?? entry.occurredAt`
    ended_at = parse_desktop_timeline_date(_field(window, 'to', _UNDEFINED))
    view: Dict[str, Any] = {
        'entityId': 'entry:%s' % _js_string(_field(entry, 'id')),
        'id': _field(entry, 'id'),
        'storyId': _field(entry, 'storyId'),
        'participantId': _field(entry, 'participantId'),
        'kind': _field(entry, 'kind'),
        'actor': _field(entry, 'actor'),
        'track': desktop_timeline_track_for_entry(entry),
        'content': _field(entry, 'content'),
        'occurredAt': iso(_field(entry, 'occurredAt')),
        'startedAt': iso(started_at),
        'endedAt': iso(ended_at) if ended_at is not None else None,
    }
    # Protocol 4 annotations: identity for cross-referencing logs/outbox, and
    # execution truth for the delivery ledger. Absent on legacy entries.
    commit_id = _field(metadata, 'commitId', _UNDEFINED)
    if isinstance(commit_id, str):
        view['commitId'] = commit_id
    delivery_actions = desktop_delivery_reality_summary(entry)
    if delivery_actions:
        view['deliveryActions'] = delivery_actions
    scene_checkpoint = desktop_scene_checkpoint(entry)
    if scene_checkpoint:
        view['sceneCheckpoint'] = scene_checkpoint
    return view


try:  # pragma: no cover - 取决于并行移植进度（service_helpers 目前只覆盖 7009-8524）
    from .service_helpers import desktop_timeline_entry_view  # type: ignore[attr-defined]  # noqa: F401
except ImportError:
    # service_helpers 尚未导出 desktop_timeline_entry_view：用本文件内按上游 151-165
    # 逐行实现的副本，保证 desktop_timeline_range 的条目视图一致。
    desktop_timeline_entry_view = _desktop_timeline_entry_view_fallback


def empty_desktop_timeline_range() -> Dict[str, Any]:
    """上游 167-170 emptyDesktopTimelineRange。"""
    now = iso(now_utc())  # 上游 `const now = new Date().toISOString()`
    return {
        'protocol': 4,
        'storyId': '',
        'revision': 'empty',
        'range': {'from': now, 'to': now},
        'entries': [],
        'scenes': [],
        'facts': [],
    }


# ===================== 上游 769-931：ServiceDesktopMixin =====================


class ServiceDesktopMixin:
    """typ-0 桌面端/worker 入口；方法与上游 InterludeService 逐行对应。

    依赖 ``ServiceBaseMixin`` 提供的字段与方法：``desktop_runtime_phase``、
    ``desktop_event_sink``、``due_intent_wake_timers``、``buffered_narrative_turns``、
    ``buffered_group_turns``、``ctx``、``db_get``、``db_set``、``serial``、
    ``get_canonical_story``、``get_schedule_preplan``、``purge_story_range``、
    ``flush_buffered_narrative``、``flush_group_turn``、``receive``、``receive_group``。
    """

    # 上游 769-798
    def set_desktop_runtime_phase(self, phase: DesktopRuntimePhase) -> None:
        """上游 769-798 setDesktopRuntimePhase。

        上游 TS 只有类型标注，没有运行时校验：``phase`` 直接赋值，分支只判断
        'paused' / 'running'（'muted' 或非法值都只赋值 + 发快照），这里照抄。
        """
        self.desktop_runtime_phase = phase
        if phase == 'paused':
            for wake in list(self.due_intent_wake_timers.values()):
                # 上游 `for (const timer of ...) timer.cancel()`；本项目 wake 值形如
                # {'cancel': handle, 'dueAt': ...}，统一交给 ctx.clear_timer。
                self.ctx.clear_timer(wake.get('cancel') if isinstance(wake, dict) else wake)
            self.due_intent_wake_timers.clear()
            for turn in list(self.buffered_narrative_turns.values()):
                if turn.get('timer'):
                    self.ctx.clear_timer(turn.get('timer'))
                turn['timer'] = None
            for turn in list(self.buffered_group_turns.values()):
                if turn.get('timer'):
                    self.ctx.clear_timer(turn.get('timer'))
                turn['timer'] = None
        elif phase == 'running':
            # Resume only turns which were already persisted before pause. This
            # avoids both losing a message and manufacturing a new user event.
            for key, turn in list(self.buffered_narrative_turns.items()):
                if turn.get('timer') or turn.get('inFlightRequestId') or not turn.get('messages'):
                    continue
                revision = (turn.get('nextRevision') or 0) + 1  # 上游 `++turn.nextRevision`
                turn['nextRevision'] = revision
                turn['timer'] = self.ctx.set_timeout(
                    lambda key=key, revision=revision: self.flush_buffered_narrative(key, revision), 0)
            for key, turn in list(self.buffered_group_turns.items()):
                if turn.get('timer') or not turn.get('messages'):
                    continue
                revision = (turn.get('revision') or 0) + 1  # 上游 `++turn.revision`
                turn['revision'] = revision
                turn['timer'] = self.ctx.set_timeout(
                    lambda key=key, revision=revision: self.flush_group_turn(key, revision), 0)
        sink = self.desktop_event_sink
        if sink is not None:
            # 上游 `this.desktopEventSink?.('runtime-snapshot', await this.desktopRuntimeSnapshot())`
            sink('runtime-snapshot', self.desktop_runtime_snapshot())

    # 上游 800-807
    def desktop_runtime_snapshot(self) -> Dict[str, Any]:
        """上游 800-807 desktopRuntimeSnapshot（快照刻意保持最小，时间线见下）。"""
        stories = self.db_get('interlude_story', {}, {'limit': 20, 'sort': {'updatedAt': 'desc'}})
        return {
            'phase': self.desktop_runtime_phase,
            'stories': [
                {
                    'id': story.get('id'),
                    'status': story.get('status'),
                    'cursorAt': iso(story.get('cursorAt')),
                    'updatedAt': iso(story.get('updatedAt')),
                }
                for story in stories
            ],
        }

    # 上游 809-844
    def desktop_timeline_snapshot(self) -> Dict[str, Any]:
        """上游 809-844 desktopTimelineSnapshot（只读投影；宿主绝不直接改 HDSI 表）。"""
        story = self.get_canonical_story()
        if not story:
            return {'storyId': '', 'entries': [], 'scenes': [], 'facts': []}
        # 上游 Promise.all；同步移植按顺序执行，语义一致。
        entries = self.db_get(
            'interlude_script_entry', {'storyId': story.get('id')},
            {'limit': 120, 'sort': {'occurredAt': 'desc'}},
        )
        scenes = self.db_get(
            'interlude_scene', {'storyId': story.get('id')},
            {'limit': 12, 'sort': {'startedAt': 'desc'}},
        )
        facts = self.db_get(
            'interlude_fact', {'storyId': story.get('id'), 'status': 'active'},
            {'limit': 24, 'sort': {'importance': 'desc', 'updatedAt': 'desc'}},
        )
        preplan = self.get_schedule_preplan(story.get('id'))
        return {
            'storyId': story.get('id'),
            'cursorAt': iso(story.get('cursorAt')),
            'updatedAt': iso(story.get('updatedAt')),
            'timezone': (story.get('setting') or {}).get('timezone'),
            'entries': [
                {
                    'id': entry.get('id'),
                    'storyId': entry.get('storyId'),
                    'participantId': entry.get('participantId'),
                    'kind': entry.get('kind'),
                    'actor': entry.get('actor'),
                    'content': entry.get('content'),
                    'occurredAt': iso(entry.get('occurredAt')),
                    'metadata': entry.get('metadata'),
                }
                for entry in list(reversed(entries))  # 上游 `entries.reverse().map(...)`
            ],
            'scenes': [
                {
                    'id': scene.get('id'),
                    'status': scene.get('status'),
                    'startedAt': iso(scene.get('startedAt')),
                    'endedAt': iso(scene.get('endedAt')),
                    'hook': scene.get('hook'),
                    'summary': scene.get('summary'),
                    'entryCount': scene.get('entryCount'),
                }
                for scene in scenes
            ],
            'facts': [
                {
                    'id': fact.get('id'),
                    'scope': fact.get('scope'),
                    'content': fact.get('content'),
                    'importance': fact.get('importance'),
                    'confidence': fact.get('confidence'),
                    'unresolved': fact.get('unresolved'),
                    'updatedAt': iso(fact.get('updatedAt')),
                }
                for fact in facts
            ],
            'preplan': {
                'revision': preplan.get('revision'),
                'timezone': preplan.get('timezone'),
                'validFrom': preplan.get('validFrom'),
                'validThrough': preplan.get('validThrough'),
                'materializedDays': preplan.get('materializedDays'),
            } if preplan else None,
        }

    # 上游 846-862
    def desktop_purge_range(self, from_: datetime, to: datetime) -> Dict[str, Any]:
        """上游 846-862 desktopPurgeRange。

        typ-0 选区删除的受控入口：QQ 指令路径有人工确认间隔，bridge 路径没有，
        因此 purge 必须在 worker 内的 serial 队列中执行，保证与写作回合互斥。
        purgeStoryRange 本身是软删（redacted/deleted/superseded）并处理
        sourceEntryIds 级联；Canon 与参与者身份保持不动。
        """
        story = self.get_canonical_story()
        if not story:
            raise RuntimeError('当前 worker 没有可操作的剧本。')
        if not isinstance(from_, datetime) or not isinstance(to, datetime) or from_ > to:
            # 上游 `Number.isNaN(from.getTime()) || Number.isNaN(to.getTime()) || from > to`
            raise RuntimeError('选区删除时间范围无效。')

        def task() -> Dict[str, Any]:
            self.purge_story_range(story.get('id'), from_, to)
            return {'storyId': story.get('id')}

        return self.serial(story.get('id'), task)

    # 上游 864-909
    def desktop_timeline_range(self, request: Optional[DesktopTimelineRangeRequest] = None) -> Dict[str, Any]:
        """上游 864-909 desktopTimelineRange。

        Versioned, bounded read model for typ-0 Arrangement.  It deliberately
        exposes HDSI's stored temporal facts only: callers cannot create timeline
        objects, and legacy entries without an explicit automatic window remain
        point events instead of receiving a guessed duration from their prose.
        """
        story = self.get_canonical_story()
        if not story:
            return empty_desktop_timeline_range()
        query = normalize_desktop_timeline_range_request({} if request is None else request)
        story_id = story.get('id')
        entry_query: Dict[str, Any] = {
            'storyId': story_id,
            'occurredAt': {'$gte': query['from'], '$lte': query['to']},
        }
        if query['cursorId']:
            # 上游 `if (query.cursorId) entryQuery.id = { $lt: query.cursorId }`
            entry_query['id'] = {'$lt': query['cursorId']}
        candidate_limit = min(query['limit'] * 4 + 1, 501)
        # 上游 Promise.all；未选中的 track 直接返回空数组（同步移植按顺序执行）。
        entry_rows = self.db_get(
            'interlude_script_entry', entry_query,
            {'limit': candidate_limit, 'sort': {'occurredAt': 'desc', 'id': 'desc'}},
        )
        if 'scenes' in query['tracks']:
            scene_rows = self.db_get(
                'interlude_scene', {'storyId': story_id},
                {'limit': 100, 'sort': {'startedAt': 'desc'}},
            )
        else:
            scene_rows = []
        if 'facts' in query['tracks']:
            fact_rows = self.db_get(
                'interlude_fact', {'storyId': story_id, 'status': 'active'},
                {'limit': 100, 'sort': {'updatedAt': 'desc'}},
            )
        else:
            fact_rows = []
        preplan = self.get_schedule_preplan(story_id) if 'preplan' in query['tracks'] else None

        selected_entries = [entry for entry in entry_rows if entry.get('kind') != 'redacted']
        selected_entries = [
            entry for entry in selected_entries
            if desktop_timeline_track_for_entry(entry) in query['tracks']
        ]
        selected_entries = list(reversed(selected_entries[:query['limit']]))

        entries = [desktop_timeline_entry_view(entry) for entry in selected_entries]
        scenes = [
            {
                'id': scene.get('id'),
                'status': scene.get('status'),
                'startedAt': iso(scene.get('startedAt')),
                'endedAt': iso(scene.get('endedAt')),
                'hook': scene.get('hook'),
                'summary': scene.get('summary'),
                'entryCount': scene.get('entryCount'),
            }
            for scene in scene_rows
            if desktop_range_overlaps(query['from'], query['to'], scene.get('startedAt'), scene.get('endedAt'))
        ]
        facts = [
            {
                'id': fact.get('id'),
                'scope': fact.get('scope'),
                'content': fact.get('content'),
                'importance': fact.get('importance'),
                'confidence': fact.get('confidence'),
                'unresolved': fact.get('unresolved'),
                'updatedAt': iso(fact.get('updatedAt')),
            }
            for fact in fact_rows
            if fact.get('updatedAt') is not None
            and query['from'] <= fact.get('updatedAt') <= query['to']
        ]

        latest_entry_id = entry_rows[0].get('id') if entry_rows else 0
        if latest_entry_id is None:
            latest_entry_id = 0  # 上游 `entryRows[0]?.id ?? 0`
        latest_scene = ''
        for scene in scenes:
            # 上游 `scenes.reduce((latest, scene) => !latest || scene.startedAt > latest ? scene.startedAt : latest, '')`
            if not latest_scene or scene['startedAt'] > latest_scene:
                latest_scene = scene['startedAt']
        latest_fact = ''
        for fact in facts:
            # 上游 `facts.reduce((latest, fact) => !latest || fact.updatedAt > latest ? fact.updatedAt : latest, '')`
            if not latest_fact or fact['updatedAt'] > latest_fact:
                latest_fact = fact['updatedAt']
        preplan_revision = preplan.get('revision') if preplan else 0
        if preplan_revision is None:
            preplan_revision = 0  # 上游 `preplan?.revision ?? 0`

        next_cursor = None
        if len(entry_rows) == candidate_limit and selected_entries:
            # 上游 `entryRows.length === candidateLimit && selectedEntries[0] ? \`entry:${selectedEntries[0].id}\` : undefined`
            next_cursor = 'entry:%s' % _js_string(selected_entries[0].get('id'))

        return {
            'protocol': 4,
            'storyId': story_id,
            'revision': '%s:%s:%s:%s:%s' % (
                iso(story.get('updatedAt')), latest_entry_id, preplan_revision, latest_scene, latest_fact,
            ),
            'range': {'from': iso(query['from']), 'to': iso(query['to'])},
            'cursorAt': iso(story.get('cursorAt')),
            'updatedAt': iso(story.get('updatedAt')),
            'timezone': (story.get('setting') or {}).get('timezone'),
            'entries': entries,
            'scenes': scenes,
            'facts': facts,
            'preplan': {
                'revision': preplan.get('revision'),
                'timezone': preplan.get('timezone'),
                'validFrom': preplan.get('validFrom'),
                'validThrough': preplan.get('validThrough'),
                'materializedDays': preplan.get('materializedDays'),
            } if preplan else None,
            'nextCursor': next_cursor,
        }

    # 上游 911-917
    def set_desktop_cursor_at(self, cursor_at: datetime) -> None:
        """上游 911-917 setDesktopCursorAt。

        批次 4：桌面设置叙事游标（分支截断后回拨到 forkPoint）。串行队列内执行。
        """
        story = self.get_canonical_story()
        if not story:
            raise RuntimeError('当前 worker 没有可操作的剧本。')

        def task() -> None:
            self.db_set('interlude_story', {'id': story.get('id')}, {'cursorAt': cursor_at, 'updatedAt': now_utc()})

        self.serial(story.get('id'), task)

    # 上游 918-926
    def receive_desktop_event(self, event: DesktopInboundEvent, session: InboundSession) -> bool:
        """上游 918-926 receiveDesktopEvent（已归一化的 typ-0 事件，不引入第二条叙事路径）。"""
        if self.desktop_runtime_phase != 'running':
            return False
        received_at = parse_time(_field(event, 'occurredAt', _UNDEFINED))  # 上游 `new Date(event.occurredAt)`
        if _field(event, 'kind', _UNDEFINED) == 'group':
            return self.receive_group(session, received_at)
        return self.receive(session, received_at)
