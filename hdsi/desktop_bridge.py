# -*- coding: utf-8 -*-
"""typ-0 桌面桥，对应上游 src/desktop-bridge.ts（1.0.1-beta6-rebuild，305 行）。

上游只在 typ-0 worker 内启用，依赖 Koishi 桌面端 IPC
（process.send / process.on('message') / ctx.server.port）。本项目没有桌面端：

- 纯数据/协议部分完整移植：DesktopRuntimePhase、DesktopDeliveryStatus、
  DesktopInboundEvent 及其类型、事件名/命令名常量、payload 校验
  （is_phase、is_request_id、is_inbound_event、is_timeline_range_request）、
  序列化（escape_attribute、desktop_session）以及 install_desktop_bridge 的
  命令分发与投递回执。
- 真实 IPC 通道收敛为本文件内的 DesktopTransport 协议类占位，不 import
  koishi / websocket。
- install_desktop_bridge(service, transport=None)：transport 为 None 时对应上游
  `process.env.HDSI_DESKTOP_BRIDGE !== '1' || typeof process.send !== 'function'`
  的降级分支——不触碰 service，直接返回 None（全部空操作）。
"""

from __future__ import annotations

import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, TypedDict

from .time_utils import ensure_aware, now_utc, parse_time
from .utils import is_array

# ---------------------------------------------------------------------------
# 协议常量（上游为内联字面量，这里提为常量便于校验/复用）
# ---------------------------------------------------------------------------

# 上游 DesktopCommand['type']
DESKTOP_CHANNEL = 'hdsi-desktop'
# 上游 bridge-ready payload：protocol: 4
DESKTOP_PROTOCOL_VERSION = 4

# 事件名：上游 sendToDesktop(event, payload) 的第一个参数
EVENT_DELIVERY = 'delivery'
EVENT_PHASE_RESULT = 'phase-result'
EVENT_INBOUND_RESULT = 'inbound-result'
EVENT_CURSOR_SET_RESULT = 'cursor-set-result'
EVENT_REPLAY_RESULT = 'replay-result'
EVENT_SNAPSHOT_RESULT = 'snapshot-result'
EVENT_TIMELINE_RANGE_RESULT = 'timeline-range-result'
EVENT_PURGE_RANGE_RESULT = 'purge-range-result'
EVENT_ERROR = 'error'
EVENT_HEARTBEAT = 'heartbeat'
EVENT_CONSOLE_PORT = 'console-port'
EVENT_BRIDGE_READY = 'bridge-ready'

DESKTOP_EVENT_NAMES: Tuple[str, ...] = (
    EVENT_DELIVERY, EVENT_PHASE_RESULT, EVENT_INBOUND_RESULT, EVENT_CURSOR_SET_RESULT,
    EVENT_REPLAY_RESULT, EVENT_SNAPSHOT_RESULT, EVENT_TIMELINE_RANGE_RESULT,
    EVENT_PURGE_RANGE_RESULT, EVENT_ERROR, EVENT_HEARTBEAT, EVENT_CONSOLE_PORT,
    EVENT_BRIDGE_READY,
)

# 命令名：上游 DesktopCommand['command']
COMMAND_PHASE = 'phase'
COMMAND_INBOUND = 'inbound'
COMMAND_REPLAY_INBOX = 'replay-inbox'
COMMAND_SNAPSHOT = 'snapshot'
COMMAND_TIMELINE_RANGE = 'timeline-range'
COMMAND_DELIVERY_RESULT = 'delivery-result'
COMMAND_PURGE_RANGE = 'purge-range'
COMMAND_CURSOR_SET = 'cursor-set'

# 上游错误/结果事件的响应名映射（上游为命令三元链，cursor-set 与未知命令落到 error）
_RESPONSE_BY_COMMAND: Dict[str, str] = {
    COMMAND_PHASE: EVENT_PHASE_RESULT,
    COMMAND_INBOUND: EVENT_INBOUND_RESULT,
    COMMAND_REPLAY_INBOX: EVENT_REPLAY_RESULT,
    COMMAND_SNAPSHOT: EVENT_SNAPSHOT_RESULT,
    COMMAND_TIMELINE_RANGE: EVENT_TIMELINE_RANGE_RESULT,
    COMMAND_PURGE_RANGE: EVENT_PURGE_RANGE_RESULT,
}

# 上游 45_000 / 10_000 / 1_000 / 60_000 毫秒
DELIVERY_TIMEOUT_SECONDS = 45.0
HEARTBEAT_INTERVAL_SECONDS = 10.0
CONSOLE_PORT_POLL_INTERVAL_SECONDS = 1.0
CONSOLE_PORT_POLL_TIMEOUT_SECONDS = 60.0

# 上游 isRequestId 的 8..128、isInboundEvent 的 128_000 字符
REQUEST_ID_MIN_LENGTH = 8
REQUEST_ID_MAX_LENGTH = 128
INBOUND_CONTENT_MAX_LENGTH = 128000

# 上游 isTimelineRangeRequest 的 tracks<=8、cursor<=80、limit 1..500
TIMELINE_TRACKS_MAX = 8
TIMELINE_CURSOR_MAX_LENGTH = 80
TIMELINE_LIMIT_MIN = 1
TIMELINE_LIMIT_MAX = 500

# 上游启用条件所用的环境变量；本移植由 hdsi/index.py 决定是否注入 transport 取代
DESKTOP_BRIDGE_ENV = 'HDSI_DESKTOP_BRIDGE'
DESKTOP_PHASE_ENV = 'HDSI_PHASE'

# ---------------------------------------------------------------------------
# 类型（字段名与上游 JSON key 完全一致，保持 camelCase）
# ---------------------------------------------------------------------------

DesktopRuntimePhase = Literal['running', 'muted', 'paused']
DesktopDeliveryStatus = Literal['sent', 'retryable-failed', 'permanent-failed']
DesktopInboundTransport = Literal['snowluma', 'onebot-external', 'sandbox']
DesktopInboundKind = Literal['private', 'group']


class _DesktopInboundEventRequired(TypedDict):
    transport: str
    accountKey: str
    platform: str
    selfId: str
    senderId: str
    kind: DesktopInboundKind
    content: str
    occurredAt: str


class DesktopInboundEvent(_DesktopInboundEventRequired, total=False):
    senderName: str
    channelId: str
    quote: Any
    imageSources: List[str]
    voice: Any
    rawMessageId: str


class _DesktopDeliveryPayloadRequired(TypedDict):
    accountKey: str
    transport: str
    platform: str
    selfId: str
    channelId: str
    kind: DesktopInboundKind
    content: str


class DesktopDeliveryPayload(_DesktopDeliveryPayloadRequired, total=False):
    """上游 requestDelivery 的入参：Omit<DesktopDeliveryRequest, 'deliveryId' | 'occurredAt'>。"""

    replyTo: str


class _DesktopDeliveryRequestRequired(_DesktopDeliveryPayloadRequired):
    deliveryId: str
    occurredAt: str


class DesktopDeliveryRequest(_DesktopDeliveryRequestRequired, total=False):
    """上游 interface DesktopDeliveryRequest（除 replyTo 外均为必填）。"""

    replyTo: str


class DesktopDeliveryResult(TypedDict, total=False):
    """上游 desktopDeliveryHandler 的返回值 { ok, messageIds?, error? }。"""

    ok: bool
    messageIds: List[str]
    error: str


class DesktopCommand(TypedDict):
    """上游 DesktopCommand 联合类型；value 的形状见下面的各 *CommandValue。"""

    type: str
    command: str
    value: Dict[str, Any]


class DesktopPhaseCommandValue(TypedDict):
    requestId: str
    phase: DesktopRuntimePhase


class DesktopInboundCommandValue(TypedDict):
    requestId: str
    event: DesktopInboundEvent


class DesktopReplayRecord(TypedDict):
    id: str
    event: DesktopInboundEvent


class DesktopReplayInboxCommandValue(TypedDict):
    requestId: str
    records: List[DesktopReplayRecord]


class DesktopRequestCommandValue(TypedDict):
    requestId: str


class DesktopTimelineRangeCommandValue(DesktopRequestCommandValue, total=False):
    # 上游从 service.ts 导入 DesktopTimelineRangeRequest；该类型归 hdsi/service.py 所有。
    query: Dict[str, Any]


class DesktopCursorSetCommandValue(TypedDict):
    requestId: str
    cursorAt: str


class DesktopDeliveryResultCommandValue(TypedDict, total=False):
    deliveryId: str
    status: DesktopDeliveryStatus
    messageIds: List[str]
    error: str


# TS 的 key 是 'from'，数据里必须保持 'from'（不能用 Python 关键字），因此用函数式 TypedDict。
DesktopPurgeRangeCommandValue = TypedDict(
    'DesktopPurgeRangeCommandValue', {'requestId': str, 'from': str, 'to': str}
)

# ---------------------------------------------------------------------------
# IPC 占位
# ---------------------------------------------------------------------------


class DesktopTransport:
    """桌面端 IPC 通道（上游无此类；对应 process.send / process.on('message') / ctx.server）。

    上游用 Koishi IPC，无桌面端时由 hdsi/index.py 以 Null 实现注入；也可以直接给
    install_desktop_bridge 传 transport=None，此时整条桥完全空操作。
    """

    def send(self, event: str, payload: Any) -> None:
        """上游 sendToDesktop：process.send({ type: 'hdsi-desktop', event, payload })。"""
        raise NotImplementedError('上游用 Koishi IPC，无桌面端时由 hdsi/index.py 以 Null 实现注入')

    def on_message(self, handler: Callable[[Any], None]) -> None:
        """上游 process.on('message', handle)。"""
        raise NotImplementedError('上游用 Koishi IPC，无桌面端时由 hdsi/index.py 以 Null 实现注入')

    def off_message(self, handler: Callable[[Any], None]) -> None:
        """上游 process.off('message', handle)。"""
        raise NotImplementedError('上游用 Koishi IPC，无桌面端时由 hdsi/index.py 以 Null 实现注入')

    def read_server_port(self) -> Optional[int]:
        """上游通过 (service as any).ctx?.server?.port 只读探测 Console 端口（M6）。

        无桌面端时返回 None，reportConsolePort 会走 1 秒轮询、60 秒后放弃的上游分支。
        """
        raise NotImplementedError('上游用 Koishi IPC，无桌面端时由 hdsi/index.py 以 Null 实现注入')


# ---------------------------------------------------------------------------
# 纯函数：payload 校验 / 序列化
# ---------------------------------------------------------------------------


def is_phase(value: Any) -> bool:
    """上游 isPhase：DesktopRuntimePhase 类型守卫。"""
    return value == 'running' or value == 'muted' or value == 'paused'


def is_request_id(value: Any) -> bool:
    """上游 isRequestId：string 且 8 <= length <= 128。"""
    return isinstance(value, str) and REQUEST_ID_MIN_LENGTH <= _js_text_length(value) <= REQUEST_ID_MAX_LENGTH


def is_inbound_event(value: Any) -> bool:
    """上游 isInboundEvent：入站事件必填字段与长度校验。"""
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get('accountKey'), str)
        and isinstance(value.get('platform'), str)
        and isinstance(value.get('selfId'), str)
        and isinstance(value.get('senderId'), str)
        and isinstance(value.get('content'), str)
        and _js_text_length(value['content']) <= INBOUND_CONTENT_MAX_LENGTH
        and (value.get('kind') == 'private' or value.get('kind') == 'group')
        and (value.get('kind') != 'group' or isinstance(value.get('channelId'), str))
        and _parse_protocol_date(value.get('occurredAt')) is not None
    )


def is_timeline_range_request(value: Any) -> bool:
    """上游 isTimelineRangeRequest：query 缺省（null）视为合法空查询。"""
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        # 上游用 typeof 判断：JS 数组也是 object，且没有 from/to/tracks 等属性，直接通过。
        return True
    if not isinstance(value, dict):
        return False
    start = value.get('from')
    if start is not None and (not isinstance(start, str) or parse_time(start) is None):
        return False
    end = value.get('to')
    if end is not None and (not isinstance(end, str) or parse_time(end) is None):
        return False
    tracks = value.get('tracks')
    if tracks is not None and (
        not is_array(tracks)
        or len(tracks) > TIMELINE_TRACKS_MAX
        or any(not isinstance(track, str) for track in tracks)
    ):
        return False
    cursor = value.get('cursor')
    if cursor is not None and (
        not isinstance(cursor, str) or _js_text_length(cursor) > TIMELINE_CURSOR_MAX_LENGTH
    ):
        return False
    limit = value.get('limit')
    if limit is not None and (not _is_finite_number(limit) or limit < TIMELINE_LIMIT_MIN or limit > TIMELINE_LIMIT_MAX):
        return False
    detail_level = value.get('detailLevel')
    return detail_level is None or detail_level == 'summary' or detail_level == 'full'


def escape_attribute(value: Any) -> str:
    """上游 escapeAttribute：<img src="..."> 的 HTML 属性转义（String(value) 语义）。"""
    return _ATTRIBUTE_PATTERN.sub(lambda match: _ATTRIBUTE_ESCAPES[match.group(0)], _js_string_value(value))


def desktop_session(event: DesktopInboundEvent, request_delivery: Callable[[Dict[str, Any]], List[str]]) -> Dict[str, Any]:
    """上游 desktopSession：把 DesktopInboundEvent 包装成会话对象。

    上游返回 Koishi Session；本项目按上游字段原样构造 dict（key 保持 camelCase），
    `send` 与上游一致地调用 request_delivery 并返回 messageIds。
    """
    kind = event.get('kind')
    if kind == 'group':
        # 上游 group 分支是 event.channelId!（isInboundEvent 保证存在）
        channel_id = event.get('channelId')
    elif event.get('channelId'):
        channel_id = event.get('channelId')
    else:
        sender_id = event.get('senderId')
        # 上游模板串 ${event.senderId}：缺失 → undefined，JSON null → null。
        fallback = 'null' if 'senderId' in event else 'undefined'
        channel_id = f"private:{_js_string_value(sender_id) if sender_id is not None else fallback}"
    image_sources = event.get('imageSources') or []
    parts = [event.get('content') or '']
    parts.extend(f'<img src="{escape_attribute(source)}">' for source in image_sources)
    content = ''.join(part for part in parts if part)
    # 上游 new Date(event.occurredAt).getTime()：JSON null → epoch 0，解析失败 → NaN。
    if 'occurredAt' in event and event.get('occurredAt') is None:
        timestamp: Optional[int] = 0
    else:
        occurred_at = _parse_protocol_date(event.get('occurredAt'))
        timestamp = int(occurred_at.timestamp() * 1000) if occurred_at else None

    def send(outgoing: Any) -> List[str]:
        return request_delivery({
            'accountKey': event.get('accountKey'),
            'transport': event.get('transport'),
            'platform': event.get('platform'),
            'selfId': event.get('selfId'),
            'channelId': channel_id,
            'kind': kind,
            'replyTo': event.get('rawMessageId'),
            'content': str(outgoing),
        })

    return {
        'platform': event.get('platform'),
        'selfId': event.get('selfId'),
        'userId': event.get('senderId'),
        'username': event.get('senderName') or event.get('senderId'),
        'channelId': channel_id,
        # 上游 group 分支赋值 channelId，其余显式 undefined；这里私聊用 None 占位。
        'guildId': channel_id if kind == 'group' else None,
        'isDirect': kind == 'private',
        'subtype': 'private' if kind == 'private' else 'group',
        'messageId': event.get('rawMessageId'),
        'content': content,
        # 上游 new Date(event.occurredAt).getTime()（epoch 毫秒）
        'timestamp': timestamp,
        'quote': event.get('quote'),
        'send': send,
    }


# ---------------------------------------------------------------------------
# 桥安装
# ---------------------------------------------------------------------------


def install_desktop_bridge(service: Any, transport: Optional[DesktopTransport] = None) -> Optional[Callable[[], None]]:
    """上游 installDesktopBridge。

    上游启用条件：`process.env.HDSI_DESKTOP_BRIDGE === '1'` 且 `process.send` 可用。
    本移植把启用条件收敛为 transport 是否注入：transport 为 None 时全部降级为空操作
    并返回 None（hdsi/index.py 在无桌面端时不注入任何 transport 即可）。注入 transport
    时按上游顺序注册事件 sink、后台投递 handler、心跳、Console 端口上报与命令处理，
    返回与上游 `process.off` 等价的卸载函数。

    service 需要提供上游 InterludeService 的对应方法（snake_case）：
    set_desktop_event_sink / set_desktop_delivery_handler / get_desktop_runtime_phase /
    set_desktop_runtime_phase / receive_desktop_event / set_desktop_cursor_at /
    desktop_timeline_snapshot / desktop_timeline_range / desktop_purge_range。
    """
    if transport is None:
        # 上游：无桌面端（HDSI_DESKTOP_BRIDGE 未开启或 process.send 不存在）时直接 return undefined。
        return None

    pending_deliveries: Dict[str, '_PendingDelivery'] = {}
    pending_lock = threading.Lock()

    def send_to_desktop(event: str, payload: Any) -> None:
        try:
            transport.send(event, payload)
        except Exception:
            # 上游：Parent can disappear during shutdown.（宿主可能在关闭过程中消失）
            pass

    def register_pending(delivery_id: str) -> '_PendingDelivery':
        def on_settle() -> None:
            with pending_lock:
                pending_deliveries.pop(delivery_id, None)

        pending = _PendingDelivery(DELIVERY_TIMEOUT_SECONDS, on_settle)
        with pending_lock:
            pending_deliveries[delivery_id] = pending
        pending.start()
        return pending

    def request_delivery(payload: Dict[str, Any]) -> List[str]:
        """上游 requestDelivery：发送 delivery 并等待 delivery-result（45 秒超时）。"""
        delivery_id = str(uuid.uuid4())
        pending = register_pending(delivery_id)
        send_to_desktop(EVENT_DELIVERY, {**payload, 'deliveryId': delivery_id, 'occurredAt': _js_to_iso(now_utc())})
        return pending.wait()

    def settle_delivery(value: Any) -> bool:
        """上游 settleDelivery：把 delivery-result 回执交给对应 pending。"""
        if not isinstance(value, dict) or not isinstance(value.get('deliveryId'), str):
            return False
        delivery_id = value['deliveryId']
        with pending_lock:
            pending = pending_deliveries.pop(delivery_id, None)
        if pending is None:
            return False
        message_ids = value.get('messageIds')
        if not is_array(message_ids):
            message_ids = []
        if value.get('status') == 'sent':
            pending.resolve([message_id for message_id in message_ids if isinstance(message_id, str)])
        else:
            pending.reject(RuntimeError(value.get('error') or f"渠道投递失败：{value.get('status')}"))
        return True

    service.set_desktop_event_sink(lambda event, payload: send_to_desktop(event, payload))

    # typ-0 后台投递通道：delayed/split/advance 消息没有实时 session，经宿主
    # Outbox/渠道适配器投递，delivery-result 回执与实时路径共用同一 pending。
    def delivery_handler(delivery: Dict[str, Any]) -> Dict[str, Any]:
        delivery_id = str(uuid.uuid4())
        pending = register_pending(delivery_id)
        send_to_desktop(EVENT_DELIVERY, {
            'deliveryId': delivery_id,
            'accountKey': f"desktop:{delivery.get('selfId')}",
            'transport': 'onebot-external',
            'platform': delivery.get('platform'),
            'selfId': delivery.get('selfId'),
            'channelId': delivery.get('channelId'),
            'kind': delivery.get('kind'),
            'replyTo': delivery.get('quoteMessageId'),
            'content': delivery.get('content'),
            'occurredAt': _js_to_iso(now_utc()),
        })
        try:
            message_ids = pending.wait()
        except Exception as error:
            # 上游 .catch(error => ({ ok: false, error: String(error) }))
            return {'ok': False, 'error': str(error)}
        return {
            'ok': True,
            'messageIds': [message_id for message_id in message_ids if isinstance(message_id, str)],
        }

    service.set_desktop_delivery_handler(delivery_handler)

    phase_env = os.environ.get(DESKTOP_PHASE_ENV)
    initial_phase: DesktopRuntimePhase = phase_env if is_phase(phase_env) else 'running'

    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            send_to_desktop(EVENT_HEARTBEAT, {
                'phase': service.get_desktop_runtime_phase(),
                'at': _js_to_iso(now_utc()),
            })

    heartbeat_thread = threading.Thread(target=heartbeat_loop, name='hdsi-desktop-heartbeat', daemon=True)
    heartbeat_thread.start()

    # M6：worker 把 loopback server 实际绑定的端口回传宿主。上游在 Service 基类上
    # 只读探测 ctx.server.port；Python 侧交给 transport（无桌面端返回 None）。
    def read_server_port() -> Optional[int]:
        try:
            port = transport.read_server_port()
        except NotImplementedError:
            return None
        return port if isinstance(port, int) and not isinstance(port, bool) and port > 0 else None

    def report_console_port() -> bool:
        port = read_server_port()
        if port:
            send_to_desktop(EVENT_CONSOLE_PORT, {'port': port, 'uiPath': '/console/'})
            return True
        return False

    port_poller_stop = threading.Event()

    def console_port_poller() -> None:
        elapsed = 0.0
        while not port_poller_stop.wait(CONSOLE_PORT_POLL_INTERVAL_SECONDS):
            elapsed += CONSOLE_PORT_POLL_INTERVAL_SECONDS
            if report_console_port() or elapsed >= CONSOLE_PORT_POLL_TIMEOUT_SECONDS:
                return

    # server 可能晚于本插件就绪：先立即探测，未就绪时短暂轮询（1 秒一次，最多 60 秒）。
    if not report_console_port():
        port_poller = threading.Thread(target=console_port_poller, name='hdsi-desktop-console-port', daemon=True)
        port_poller.start()

    def handle(message: Any) -> None:
        command = message if isinstance(message, dict) else None
        if not command or command.get('type') != DESKTOP_CHANNEL:
            return
        name = command.get('command')
        value = command.get('value') if isinstance(command.get('value'), dict) else None
        if name == COMMAND_DELIVERY_RESULT:
            settle_delivery(command.get('value'))
            return
        try:
            if name == COMMAND_PHASE:
                value = value or {}
                if not is_request_id(value.get('requestId')) or not is_phase(value.get('phase')):
                    raise ValueError('无效 typ-0 运行状态请求。')
                service.set_desktop_runtime_phase(value['phase'])
                send_to_desktop(EVENT_PHASE_RESULT, {
                    'requestId': value['requestId'], 'accepted': True, 'phase': value['phase'],
                })
                return
            if name == COMMAND_INBOUND:
                value = value or {}
                if not is_request_id(value.get('requestId')) or not is_inbound_event(value.get('event')):
                    raise ValueError('无效 typ-0 入站事件。')
                event = value['event']
                accepted = service.get_desktop_runtime_phase() == 'running' and service.receive_desktop_event(
                    event, desktop_session(event, request_delivery)
                )
                send_to_desktop(EVENT_INBOUND_RESULT, {
                    'requestId': value['requestId'], 'accepted': bool(accepted),
                    'error': None if accepted else '当前剧本未接收该入站事件。',
                })
                return
            if name == COMMAND_CURSOR_SET:
                value = value or {}
                if not is_request_id(value.get('requestId')) or not isinstance(value.get('cursorAt'), str):
                    raise ValueError('无效 typ-0 游标设置请求。')
                cursor_at = parse_time(value['cursorAt'])
                if cursor_at is None:
                    raise ValueError('游标时间无法解析。')
                service.set_desktop_cursor_at(cursor_at)
                send_to_desktop(EVENT_CURSOR_SET_RESULT, {
                    'requestId': value['requestId'], 'accepted': True, 'cursorAt': _js_to_iso(cursor_at),
                })
                return
            if name == COMMAND_REPLAY_INBOX:
                value = value or {}
                if not is_request_id(value.get('requestId')):
                    raise ValueError('回放请求缺少 requestId。')
                records = value.get('records')
                if not is_array(records):
                    records = []
                results: List[Dict[str, Any]] = []
                for record in records:
                    record_id = record.get('id') if isinstance(record, dict) else None
                    event = record.get('event') if isinstance(record, dict) else None
                    if not record_id or not is_inbound_event(event):
                        results.append({'id': str(record_id or ''), 'accepted': False, 'error': '无效收件箱记录。'})
                        continue
                    try:
                        accepted = service.get_desktop_runtime_phase() == 'running' and service.receive_desktop_event(
                            event, desktop_session(event, request_delivery)
                        )
                        results.append({
                            'id': record_id, 'accepted': bool(accepted),
                            'error': None if accepted else '当前剧本未接收该入站事件。',
                        })
                    except Exception as error:
                        results.append({'id': record_id, 'accepted': False, 'error': str(error)})
                send_to_desktop(EVENT_REPLAY_RESULT, {'requestId': value['requestId'], 'results': results})
                return
            if name == COMMAND_SNAPSHOT:
                value = value or {}
                if not is_request_id(value.get('requestId')):
                    raise ValueError('快照请求缺少 requestId。')
                send_to_desktop(EVENT_SNAPSHOT_RESULT, {
                    'requestId': value['requestId'], 'snapshot': service.desktop_timeline_snapshot(),
                })
                return
            if name == COMMAND_TIMELINE_RANGE:
                value = value or {}
                if not is_request_id(value.get('requestId')) or not is_timeline_range_request(value.get('query')):
                    raise ValueError('无效时间线范围请求。')
                # 上游 query 缺省时走 desktopTimelineRange(undefined) 的默认参数 {}。
                query = value['query'] if 'query' in value else {}
                send_to_desktop(EVENT_TIMELINE_RANGE_RESULT, {
                    'requestId': value['requestId'], 'projection': service.desktop_timeline_range(query),
                })
                return
            if name == COMMAND_PURGE_RANGE:
                # 选区删除：桌面 GUI 化的 purge。desktopPurgeRange 内部走 serial 队列
                # （与写作回合互斥）并复用 purgeStoryRange 的软删语义。
                value = value or {}
                if not is_request_id(value.get('requestId')):
                    raise ValueError('选区删除请求缺少 requestId。')
                start_text = value.get('from') if value.get('from') is not None else ''
                end_text = value.get('to') if value.get('to') is not None else ''
                start = parse_time(str(start_text))
                end = parse_time(str(end_text))
                if start is None or end is None or start > end:
                    raise ValueError('选区删除时间范围无效。')
                result = service.desktop_purge_range(start, end)
                send_to_desktop(EVENT_PURGE_RANGE_RESULT, {
                    'requestId': value['requestId'], 'accepted': True, 'storyId': result['storyId'],
                })
                return
        except Exception as error:
            request_id = value.get('requestId') if isinstance(value, dict) else None
            response = _RESPONSE_BY_COMMAND.get(name, EVENT_ERROR)
            send_to_desktop(response, {
                'requestId': request_id,
                'accepted': False,
                'error': str(error),
                'results': [] if name == COMMAND_REPLAY_INBOX else None,
            })
            send_to_desktop(EVENT_ERROR, {'command': name, 'requestId': request_id, 'message': str(error)})

    transport.on_message(handle)

    try:
        service.set_desktop_runtime_phase(initial_phase)
    except Exception as error:
        send_to_desktop(EVENT_ERROR, {'command': 'initial-phase', 'message': str(error)})
    finally:
        # v4 为 timeline-range projection 增加 deliveryActions/sceneCheckpoint 字段，
        # 并增加宿主后台投递通道；v2/v3 命令保持兼容，旧桌面端可优雅降级。
        send_to_desktop(EVENT_BRIDGE_READY, {
            'protocol': DESKTOP_PROTOCOL_VERSION,
            'phase': service.get_desktop_runtime_phase(),
        })

    def dispose() -> None:
        stop_event.set()
        port_poller_stop.set()
        service.set_desktop_delivery_handler(None)
        transport.off_message(handle)
        with pending_lock:
            pendings = list(pending_deliveries.values())
            pending_deliveries.clear()
        for pending in pendings:
            pending.reject(RuntimeError('typ-0 bridge 已关闭。'))

    return dispose


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


class _PendingDelivery:
    """上游 interface PendingDelivery（resolve / reject / timeout）的同步实现。

    同步世界没有 Promise：wait() 直接阻塞到 resolve/reject（超时由 45 秒的
    threading.Timer 触发），返回 messageIds 或抛出异常，语义与 await 一致。
    """

    def __init__(self, timeout_seconds: float, on_settle: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._settled = False
        self._message_ids: Optional[List[str]] = None
        self._error: Optional[BaseException] = None
        self._on_settle = on_settle
        # 上游是 setTimeout 后立刻 set 进 Map；同步实现先登记再 start()，
        # 避免极小超时下计时器先于登记触发。
        self.timeout_seconds = timeout_seconds
        self.timeout: Optional[threading.Timer] = None

    def start(self) -> None:
        self.timeout = threading.Timer(self.timeout_seconds, self._expire)
        self.timeout.daemon = True
        self.timeout.start()

    def _finish(self) -> None:
        if self.timeout is not None and self.timeout.is_alive():
            self.timeout.cancel()
        on_settle = self._on_settle
        self._on_settle = None
        if on_settle is not None:
            on_settle()

    def _expire(self) -> None:
        self.reject(TimeoutError('等待 typ-0 渠道投递确认超时。'))

    def resolve(self, message_ids: List[str]) -> None:
        with self._lock:
            if self._settled:
                return
            self._settled = True
            self._message_ids = message_ids
        self._finish()
        self._event.set()

    def reject(self, error: BaseException) -> None:
        with self._lock:
            if self._settled:
                return
            self._settled = True
            self._error = error
        self._finish()
        self._event.set()

    def wait(self) -> List[str]:
        self._event.wait()
        if self._error is not None:
            raise self._error
        return self._message_ids or []


def _js_string_value(value: Any) -> str:
    """JS String(value)：用于 escapeAttribute 与 desktopSession 的字符串拼接。"""
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _js_text_length(value: str) -> int:
    """JS String.length：UTF-16 code unit 数（非 BMP 字符在 Python len 中会少算）。"""
    try:
        return len(value.encode('utf-16-le', 'surrogatepass')) // 2
    except UnicodeError:
        return len(value)


def _js_to_iso(value: datetime) -> str:
    """JS Date.toISOString()：UTC、毫秒固定 3 位。

    上游 IPC payload 用 toISOString()；time_utils.iso() 会省略 0 毫秒并保留微秒，
    因此协议报文单独用它保证线上格式一比一。
    """
    utc = ensure_aware(value).astimezone(timezone.utc)
    return f'{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z'


def _parse_protocol_date(value: Any) -> Optional[datetime]:
    """TS new Date(value) / Date.parse(value)：字符串或 datetime，失败返回 None。"""
    if isinstance(value, str):
        return parse_time(value)
    if isinstance(value, datetime):
        return ensure_aware(value)
    return None


def _is_finite_number(value: Any) -> bool:
    """TS Number.isFinite：只接受真正的有限数字（布尔、字符串都算 false）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


_ATTRIBUTE_PATTERN = re.compile(r'[&<>"\']')
_ATTRIBUTE_ESCAPES: Dict[str, str] = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
}
