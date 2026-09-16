# -*- coding: utf-8 -*-
"""
基于本地微信数据读取的监听器。

通过轮询 UserDataIsSafeFromUsers (WeFlow) 提供的 HTTP API
(http://127.0.0.1:5031) 读取微信4.x本地数据库里的聊天记录来"监听"新消息，
取代原 wxauto 的窗口监听。发送消息仍然由 wxauto 完成。

API 参考 (docs/HTTP-API.md)：
  GET  /api/v1/sessions            -> { success, sessions: [{username, displayName, type, lastTimestamp, ...}] }
  POST /api/v1/messages            -> body {"taller": "<sessionId>", "limit": 50}
                                     -> { success, messages: [{localId, serverId, localType, createTime,
                                          isSend, senderUsername, content, rawContent, parsedContent,
                                          mediaType, mediaUrl, mediaLocalPath, ...}] }
  isSend: 0=收到的消息, 1=自己发送的消息
"""

import os
import re
import time
import json
import threading
import logging
import urllib.parse

import requests

logger = logging.getLogger(__name__)

# 本地消息类型 -> wxauto 风格 type（与 bot.message_listener / handle_wxauto_message 兼容）
TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 34
TYPE_VIDEO = 43
TYPE_EMOJI = 47
TYPE_APP = 49
TYPE_SYSTEM = 10002


class LocalDbChat:
    """wxauto chat 对象的轻量替身：只提供 message_listener 需要的字段。"""
    __slots__ = ('who',)

    def __init__(self, who):
        self.who = who


class LocalDbMessage:
    """wxauto 消息对象的轻量替身，字段/方法与 bot 流水线兼容。"""

    def __init__(self, raw, session_display_name, media_dir):
        self.raw = raw or {}
        self.who = session_display_name
        self.media_dir = media_dir

        self.local_id = self.raw.get('localId')
        self.server_id = self.raw.get('serverId')
        self.raw_type = self.raw.get('localType')
        self.is_send = int(self.raw.get('isSend') or 0)
        self.sender = self.raw.get('senderUsername') or ''
        self.media_type = self.raw.get('mediaType') or ''
        self.media_url = self.raw.get('mediaUrl') or self.raw.get('mediaFileUrl') or ''
        self.media_local_path = self.raw.get('mediaLocalPath') or ''
        self.create_time = self.raw.get('createTime')
        self.parsed_content = self.raw.get('parsedContent') or ''
        self.raw_content = self.raw.get('rawContent') or ''
        self.content = self.parsed_content or self.raw.get('content') or ''
        quote = self.raw.get('quote') or {}
        self.quote_content = quote.get('content') or ''

        self.type = self._map_type()
        # 卡片类消息(转账/链接/文件等)的 content 是原始 XML，转成人类可读摘要
        if self.type == 'link':
            extracted = self._extract_xml_text(self.content)
            self.content = extracted or '[卡片消息]'
        elif self.type == 'quote' and str(self.content).strip().startswith('<'):
            # 引用消息的 content 若仍是 XML，则提取其中的文字
            self.content = self._extract_xml_text(self.content) or ''
        # 本地监听只投递"收到的消息"，属性一律标记为 friend（好友/群成员消息）
        self.attr = 'friend'
        # createTime 为 Unix 秒，兼容 _message_is_replay 的时间判断
        self.time = self.create_time

    @staticmethod
    def _extract_xml_text(text):
        """从微信卡片/转账等 XML 中提取 title/des 摘要。"""
        if not text or '<appmsg' not in text:
            return None
        try:
            parts = []
            m = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', text, re.S) or re.search(r'<title>(.*?)</title>', text, re.S)
            if m:
                parts.append(m.group(1))
            m2 = re.search(r'<des><!\[CDATA\[(.*?)\]\]></des>', text, re.S) or re.search(r'<des>(.*?)</des>', text, re.S)
            if m2:
                parts.append(m2.group(1))
            joined = ' | '.join(p.strip() for p in parts if p and p.strip())
            return joined or None
        except Exception:
            return None

    def _map_type(self):
        # 引用消息（带 quote 字段）：按内容/媒体类型映射为 quote/图片/表情等，绝不当卡片
        if self.raw.get('quote'):
            mt = str(self.media_type or '').lower()
            if mt == 'image' or self.raw_type in (TYPE_IMAGE, 3):
                return 'image'
            if mt == 'emoji' or self.raw_type in (TYPE_EMOJI, 47):
                return 'emotion'
            if mt in ('voice', 'video', 'file'):
                return mt
            return 'quote'
        raw_content = self.raw.get('rawContent') or self.raw.get('content') or ''
        if '<appmsg' in raw_content:
            return 'link'
        mt = str(self.media_type or '').lower()
        if mt == 'emoji':
            return 'emotion'
        if mt == 'voice':
            return 'voice'
        if mt == 'video':
            return 'video'
        if mt == 'file':
            return 'file'
        if mt == 'image' or self.raw_type == TYPE_IMAGE:
            return 'image'
        if self.raw_type == TYPE_VOICE:
            return 'voice'
        if self.raw_type == TYPE_EMOJI:
            return 'emotion'
        if self.raw_type == TYPE_VIDEO:
            return 'video'
        if self.raw_type == TYPE_APP:
            return 'link'
        return 'text'

    # ---- 与 wxauto 消息兼容的方法 ----

    def download(self):
        """返回图片本地路径（本地库已解密导出则直接用，否则尝试从 mediaUrl 下载）。"""
        if self.media_local_path and os.path.exists(self.media_local_path):
            return self.media_local_path
        return self._download_media()

    def capture(self):
        """表情/图片的截图等价物：返回本地路径或 None。"""
        return self.download() if self.type in ('emotion', 'image') else None

    def _download_media(self):
        if not self.media_url:
            return None
        try:
            os.makedirs(self.media_dir, exist_ok=True)
            ext = os.path.splitext(urllib.parse.urlparse(self.media_url).path)[1] or '.dat'
            dest = os.path.join(self.media_dir, f'media_{int(time.time() * 1000)}_{self.local_id}{ext}')
            resp = requests.get(self.media_url, timeout=10)
            if resp.status_code == 200:
                with open(dest, 'wb') as f:
                    f.write(resp.content)
                return dest
        except Exception as e:
            logger.warning(f'下载本地媒体失败: {e}')
        return None

    def forward(self, who):
        logger.info(f'[本地监听] 不支持转发 {self.type} 到 {who}，已忽略。')

    def tickle(self):
        logger.info('[本地监听] 本地数据库读取模式不支持拍一拍，已忽略。')

    def to_text(self):
        return ''

    def get_url(self):
        return ''

    def get_messages(self):
        return None


class LocalDbListener:
    """轮询本地微信数据 HTTP API，把新收到的消息交给回调处理。"""

    def __init__(self, api_base='http://127.0.0.1:5031', token='',
                 listen_names=None, poll_interval=2.0, media_dir=None,
                 only_recent_seconds=15.0, on_message=None, on_sessions=None):
        self.api_base = api_base.rstrip('/')
        self.token = token
        self.listen_names = list(listen_names or [])
        self.poll_interval = poll_interval
        self.media_dir = media_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'Memory_Temp', 'localdb_media')
        self.only_recent_seconds = only_recent_seconds
        self.on_message = on_message      # callback(session_display_name, LocalDbMessage)
        self.on_sessions = on_sessions    # callback(sessions_map) 可选

        self.sessions = {}    # display_name -> {username, display_name, is_group, ...}
        self._seen_ids = {}   # username -> set(localId)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._startup_time = time.time()
        self._api_ok = True       # 最近一次会话拉取是否成功
        self._api_warned_at = 0.0 # 用于抑制 API 未就绪时的重复报错

    # ---- HTTP ----

    def _headers(self):
        h = {'Content-Type': 'application/json'}
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        return h

    def _get(self, path, params=None, timeout=10):
        url = f'{self.api_base}{path}'
        headers = dict(self._headers())
        if self.token:
            params = dict(params or {})
            params['access_token'] = self.token
        return requests.get(url, params=params, headers=headers, timeout=timeout)

    def fetch_sessions(self):
        """拉取会话列表并建立 display_name -> 会话 映射。"""
        try:
            r = self._get('/api/v1/sessions', params={'limit': 500})
            if r.status_code != 200:
                self._api_ok = False
                now = time.time()
                if now - self._api_warned_at > 30:
                    self._api_warned_at = now
                    if r.status_code == 401:
                        logger.warning(f'WeFlow HTTP API 返回 401 未授权：请检查 LOCALDB_API_TOKEN 与 WeFlow「设置→HTTP API→访问令牌」是否一致（当前配置: {self.token!r}）。')
                    else:
                        logger.warning(f'获取会话列表失败: HTTP {r.status_code}: {r.text[:200]}')
                return self.sessions
            data = r.json()
            items = data.get('sessions') or []
            self._api_ok = True
            with self._lock:
                new_sessions = {}
                for it in items:
                    username = it.get('username')
                    display_name = it.get('displayName') or username
                    if not username:
                        continue
                    is_group = self._is_group_session(username, it.get('type'))
                    new_sessions[display_name] = {
                        'username': username,
                        'display_name': display_name,
                        'is_group': is_group,
                        'lastTimestamp': it.get('lastTimestamp'),
                    }
                    if display_name != username:
                        new_sessions[username] = new_sessions[display_name]
                self.sessions = new_sessions
            if self.on_sessions:
                try:
                    self.on_sessions(dict(self.sessions))
                except Exception as e:
                    logger.warning(f'on_sessions 回调异常: {e}')
            return self.sessions
        except requests.exceptions.RequestException as e:
            self._api_ok = False
            now = time.time()
            if now - self._api_warned_at > 30:
                self._api_warned_at = now
                logger.error(f'获取会话列表失败(连接错误): {e} —— 请确认 WeFlow 已启动，且在「设置 → HTTP API」中已开启（端口 {self.api_base.split(":")[-1]}）')
            return self.sessions
        except Exception as e:
            self._api_ok = False
            logger.error(f'获取会话列表异常: {e}', exc_info=True)
            return self.sessions

    @staticmethod
    def _is_group_session(username, session_type):
        # sessionType 优先（"group"/"private"），其次 @chatroom 后缀，最后数字 type
        if isinstance(session_type, str):
            st = session_type.lower()
            if st in ('group', 'private', 'friend'):
                return st == 'group'
        if isinstance(username, str) and username.endswith('@chatroom'):
            return True
        try:
            return int(session_type) == 2
        except Exception:
            return False

    def fetch_messages(self, username, limit=30):
        """拉取某个会话的最新消息（GET /api/v1/messages?talker=<username>&media=1）。
        media=1 让 WeFlow 解密/导出图片等媒体，返回 mediaLocalPath，供识图使用。"""
        try:
            params = {'talker': username, 'limit': limit, 'media': '1'}
            if self.token:
                params['access_token'] = self.token
            r = requests.get(f'{self.api_base}/api/v1/messages', params=params,
                             headers=self._headers(), timeout=10)
            if r.status_code != 200:
                logger.warning(f'获取消息失败: HTTP {r.status_code}: {r.text[:200]}')
                return []
            data = r.json()
            if not data.get('success'):
                logger.warning(f"获取消息失败: {data.get('error') or data.get('message')}")
                return []
            return data.get('messages') or []
        except requests.exceptions.RequestException as e:
            logger.error(f'获取消息失败(连接错误): {e}')
            return []
        except Exception as e:
            logger.error(f'获取消息异常: {e}', exc_info=True)
            return []

    # ---- 会话解析 ----

    def resolve_target_sessions(self):
        """把配置里的昵称列表映射到本地库会话；返回 [(display_name, session_info)]。"""
        self.fetch_sessions()
        result = []
        names = self.listen_names
        with self._lock:
            sessions = dict(self.sessions)
        for name in names:
            if name in sessions:
                result.append((name, sessions[name]))
                continue
            # 尝试部分匹配（备注名 vs 群名可能略有差异）
            matched = None
            for display_name, info in sessions.items():
                if info.get('username') == name:
                    matched = (display_name, info)
                    break
            if matched is None:
                for display_name, info in sessions.items():
                    if display_name and name and (name in display_name or display_name in name):
                        matched = (display_name, info)
                        break
            if matched:
                result.append(matched)
                logger.info(f"会话 '{name}' 匹配到本地库会话 '{matched[0]}'")
            elif self._api_ok:
                logger.error(f'本地库中未找到会话 "{name}"，请确认 WeFlow 已读取到该聊天且昵称/备注一致')
        return result

    # ---- 轮询 ----

    def poll_once(self):
        """轮询一轮：对每个目标会话拉取新消息并派发。"""
        if not self._api_ok:
            # API 未就绪/未授权：静默重试（fetch_sessions 会按需提示），避免刷屏
            self.fetch_sessions()
            return
        targets = self.resolve_target_sessions()
        now = time.time()
        for display_name, info in targets:
            username = info['username']
            messages = self.fetch_messages(username, limit=30)
            # 按时间升序，只处理新收到的消息
            incoming = [m for m in messages if int(m.get('isSend') or 0) == 0]
            incoming.sort(key=lambda m: float(m.get('createTime') or 0))
            with self._lock:
                seen = self._seen_ids.setdefault(username, set())
                new_seen = []
            for m in incoming:
                try:
                    local_id = m.get('localId') or m.get('serverId')
                except Exception:
                    local_id = None
                if local_id is not None:
                    if local_id in seen:
                        continue
                    new_seen.append(local_id)
                try:
                    ct = float(m.get('createTime') or 0)
                except Exception:
                    ct = 0
                if ct and (now - ct) > self.only_recent_seconds:
                    continue  # 启动/漏检时的历史消息不处理
                local_type = m.get('localType')
                if local_type == TYPE_SYSTEM:
                    continue  # 系统提示（撤回/拍一拍等）不进入 AI
                msg = LocalDbMessage(m, display_name, self.media_dir)
                try:
                    if self.on_message:
                        self.on_message(display_name, msg)
                except Exception as e:
                    logger.error(f'派发消息到 bot 失败: {e}', exc_info=True)
            if new_seen:
                with self._lock:
                    seen = self._seen_ids.setdefault(username, set())
                    seen.update(new_seen)
                    if len(seen) > 2000:
                        # 防止无限增长：清空旧 id（localId 通常单调）
                        self._seen_ids[username] = set(list(seen)[-1500:])

    def run(self):
        logger.info(f'本地数据监听线程启动（API: {self.api_base}，轮询间隔 {self.poll_interval}s）')
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f'本地监听轮询异常: {e}', exc_info=True)
            self._stop.wait(self.poll_interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run, name='LocalDbListener', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
