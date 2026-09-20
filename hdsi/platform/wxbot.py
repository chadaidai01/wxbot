# -*- coding: utf-8 -*-
"""微信（wxautox + localdb）平台适配（非上游文件）。

上游 PlatformAdapter 的微信实现：发送走 bot.py 注入的 callable，媒体/群成员/表情库
走 localdb_listener 与本地文件系统，网络观察默认关闭（无 puppeteer 时按上游降级）。
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .adapter import PlatformAdapter
from .session import InboundSession


def guess_mime(path: str) -> str:
    mime = mimetypes.guess_type(path)[0]
    if mime:
        return mime
    ext = os.path.splitext(path)[1].lower()
    return {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
        '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
        '.m4a': 'audio/mp4', '.flac': 'audio/flac', '.amr': 'audio/amr',
    }.get(ext, 'application/octet-stream')


class WxbotAdapter(PlatformAdapter):
    """bot.py 与 hdsi 之间的桥。

    所有平台动作以 callable 注入，避免 hdsi 反向 import bot.py：
    - send_text(who, text) -> Any                发送文本（群名或联系人名 = 会话窗口）
    - send_file(who, path) -> Any                发送文件/图片
    - is_group(who) -> bool                      是否群聊
    - resolve_sender(who, sender) -> str         群成员昵称
    - fetch_messages(username, limit) -> list    本地库最近消息
    - recognize_image(path) -> str               识图（当前消息已由 bot.py 识图，可选）
    - op_lock                                    发送互斥锁（可选）
    """

    def __init__(self, base_dir: str, logger: Any,
                 send_text: Callable[[str, str], Any],
                 send_file: Callable[[str, str], Any],
                 is_group: Callable[[str], bool],
                 record_sent: Optional[Callable[[str], Any]] = None,
                 resolve_sender: Optional[Callable[[str, str], str]] = None,
                 fetch_messages: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
                 recognize_image: Optional[Callable[[str], str]] = None,
                 op_lock: Any = None,
                 sticker_dir: str = '',
                 platform_name: str = 'wechat',
                 self_id: str = 'wechat-bot',
                 capabilities_provider: Optional[Callable[[str, Optional[InboundSession]], Optional[Dict[str, Any]]]] = None):
        self.base_dir = base_dir
        # 上游把「机器人账号」当投递句柄；微信侧没有多账号，适配器自身即句柄。
        self.platform = platform_name
        self.selfId = self_id
        self.logger = logger
        self._send_text = send_text
        self._send_file = send_file
        self._is_group = is_group
        self._record_sent = record_sent
        self._resolve_sender = resolve_sender
        self._fetch_messages = fetch_messages
        self._recognize_image = recognize_image
        self._op_lock = op_lock
        self.sticker_dir = sticker_dir
        self._capabilities_provider = capabilities_provider
        self._sent_counter = 0

    # ---- 内部 ----
    def _send(self, who: str, content: str) -> Any:
        lock = self._op_lock
        acquired = False
        if lock is not None:
            lock.acquire()
            acquired = True
        try:
            if self._record_sent:
                try:
                    self._record_sent(content)
                except Exception:  # noqa: BLE001
                    pass
            return self._send_text(who, content)
        finally:
            if acquired:
                lock.release()

    def _synthetic_message_id(self, who: str, content: str) -> str:
        self._sent_counter += 1
        raw = f'{who}|{content}|{time.time()}|{self._sent_counter}'.encode('utf-8')
        return 'wx-' + hashlib.sha1(raw).hexdigest()[:16]

    # ---- 发送 ----
    def send_private(self, participant: Dict[str, Any], content: str, quote_message_id: Optional[str] = None) -> Optional[str]:
        who = participant.get('channelId') or participant.get('userId') or ''
        if not who or not content:
            return None
        result = self._send(who, content)
        if result is False:
            return None
        return self._synthetic_message_id(who, content)

    def send_group(self, story: Dict[str, Any], channel_id: str, content: str,
                   reply_to_message_id: Optional[str] = None, session: Optional[InboundSession] = None) -> Optional[str]:
        if not channel_id or not content:
            return None
        result = self._send(channel_id, content)
        if result is False:
            return None
        return self._synthetic_message_id(channel_id, content)

    def send_sticker(self, story: Dict[str, Any], channel_id: str, file_path: str,
                     session: Optional[InboundSession] = None) -> Optional[str]:
        if not channel_id or not file_path or not os.path.exists(file_path):
            return None
        lock = self._op_lock
        acquired = False
        if lock is not None:
            lock.acquire()
            acquired = True
        try:
            result = self._send_file(channel_id, file_path)
        finally:
            if acquired:
                lock.release()
        if result is False:
            return None
        return self._synthetic_message_id(channel_id, file_path)

    def send_native_face(self, story: Dict[str, Any], channel_id: str, face_id: Any,
                         session: Optional[InboundSession] = None) -> Optional[str]:
        # 微信没有 QQ 原生 face.id；上游语义是"一个表情回应"，这里降级为返回 None，
        # 由 service 走 nativeFace 的可用性判断（capabilities.nativeFaces 为空则不会调用）。
        return None

    def send_reaction(self, story: Dict[str, Any], channel_id: str, message_id: str,
                      reaction: str, direction: str = 'add', session: Optional[InboundSession] = None) -> bool:
        # wxautox 无按消息 id 加 reaction 的能力；上游允许 capabilities.reactions=[] 时跳过。
        return False

    # ---- 文件 ----
    def read_file(self, path: str) -> Optional[bytes]:
        try:
            with open(path, 'rb') as handle:
                return handle.read()
        except OSError:
            return None

    def file_size(self, path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def list_files(self, directory: str) -> List[Dict[str, Any]]:
        if not directory or not os.path.isdir(directory):
            return []
        entries: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append({'path': path, 'name': name, 'size': stat.st_size, 'mtime': stat.st_mtime})
        return entries

    # ---- 媒体 ----
    def fetch_image(self, source: str, session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        if not source:
            return None
        data: Optional[bytes] = None
        mime = 'image/jpeg'
        if os.path.isfile(source):
            data = self.read_file(source)
            mime = guess_mime(source)
        else:
            local = self._resolve_local_media(source, session)
            if local:
                data = self.read_file(local)
                mime = guess_mime(local)
        if not data:
            return None
        return {
            'mimeType': mime,
            'dataUri': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii'),
            'bytes': data,
        }

    def _resolve_local_media(self, source: str, session: Optional[InboundSession]) -> Optional[str]:
        if session is not None and session.raw is not None:
            try:
                resolver = getattr(session.raw, 'resolve_media_path', None)
                if callable(resolver):
                    path = resolver(source)
                    if path and os.path.isfile(path):
                        return path
            except Exception:  # noqa: BLE001
                pass
        return source if os.path.isfile(source) else None

    def fetch_audio(self, source: str, session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        path = self._resolve_local_media(source, session)
        if not path:
            return None
        data = self.read_file(path)
        if not data:
            return None
        ext = os.path.splitext(path)[1].lower().lstrip('.') or 'mp3'
        if ext == 'silk':
            ext = 'mp3'
        return {'format': ext, 'base64': base64.b64encode(data).decode('ascii')}

    # ---- 群成员 / 机器人 ----
    def group_member_name(self, group_id: str, user_id: str) -> str:
        if self._resolve_sender:
            try:
                return self._resolve_sender(group_id, user_id) or ''
            except Exception:  # noqa: BLE001
                return ''
        return ''

    def find_bot(self, platform: str, self_id: str) -> Any:
        # 微信侧只有一个账号：返回适配器自身，让上游的 sendGroupMessage 等
        # 「必须有可用机器人账号」的分支能正常走到 self.platform.send_*。
        return self

    def list_bots(self) -> List[Any]:
        return [self]

    # ---- 网络观察 ----
    def http_get(self, url: str) -> Optional[str]:
        return None

    def search_web(self, query: str) -> Optional[str]:
        return None

    def render_page(self, url: str) -> Optional[str]:
        return None

    # ---- 能力 ----
    def chat_capabilities(self, kind: str, session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        if self._capabilities_provider:
            try:
                return self._capabilities_provider(kind, session)
            except Exception:  # noqa: BLE001
                return None
        # 微信默认能力：不支持 reaction / native face；是否支持引用回复由 bot.py 配置决定。
        return {'platform': 'wechat', 'quoteReply': False, 'reactions': [], 'nativeFaces': []}

    def capabilities_for_participant(self, participant: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.chat_capabilities('private', None)

    # ---- 供 hdsi 构造会话时使用 ----
    def group_messages(self, group_id: str, limit: int) -> List[Dict[str, Any]]:
        if self._fetch_messages:
            try:
                return self._fetch_messages(group_id, limit) or []
            except Exception:  # noqa: BLE001
                return []
        return []
