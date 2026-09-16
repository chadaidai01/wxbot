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
import threading
import logging
import urllib.parse
from collections import OrderedDict

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

# 去重缓存上限（每会话）
_SEEN_MAX = 2000


def _safe_int(v, default=0):
    """把任意值稳健地转成 int（兼容字符串数字/None/bool）。"""
    if v is None or isinstance(v, bool):
        return int(v) if isinstance(v, bool) else default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _normalize_ts(v):
    """把 createTime 归一为 Unix 秒；兼容毫秒/微秒；无法解析返回 0.0。

    微信本地库的时间戳单位可能是 秒/毫秒/微秒，若直接当作秒与 time.time() 相减，
    毫秒值会得到巨大负数，导致"只处理最近N秒"的窗口判断恒不触发而回灌历史。
    """
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    # 当前 Unix 秒约 1.7e9；> 1e11 视为毫秒，再 > 视为微秒，逐级折算到秒。
    while ts > 1e11:
        ts /= 1000.0
    return ts


def _mask_token(token):
    """日志用：屏蔽访问令牌，仅保留长度与首尾少量字符。"""
    if not token:
        return '(空)'
    t = str(token)
    if len(t) <= 6:
        return f'***(len={len(t)})'
    return f'{t[:2]}***(len={len(t)})'


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
        # localType 归一为 int：WeFlow 可能返回字符串 "3"/"47"/"10002"，
        # 不归一会让下面的 ==/in 比较全部落空，媒体被误判为 text、系统消息漏进 AI。
        self.raw_type = _safe_int(self.raw.get('localType'), default=-1)
        self.is_send = _safe_int(self.raw.get('isSend'), default=0)
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
        # 卡片类消息(转账/链接/文件等)的 content 是原始 XML，转成人类可读摘要。
        # 注意：类型判定基于 raw_content，而 parsedContent 可能已是不含 XML 的可读文本，
        # 因此优先从 raw_content 抽取，抽不到再退回 content，避免摘要丢失成 "[卡片消息]"。
        if self.type == 'link':
            extracted = self._extract_xml_text(self.raw_content) or self._extract_xml_text(self.content)
            self.content = extracted or self.content or '[卡片消息]'
        elif self.type == 'quote' and str(self.content).strip().startswith('<'):
            # 引用消息的 content 若仍是 XML，则提取其中的文字
            self.content = self._extract_xml_text(self.content) or ''
        # 按 isSend 正确标记属性：1=机器人自己发送(self)，0=收到(friend)。
        # 之前一律写死 'friend' 会让 bot 的 self 分支/拍自己/撤回/轰炸自指令全部失效。
        self.attr = 'self' if self.is_send == 1 else 'friend'
        # createTime 归一为 Unix 秒，兼容下游时间判断
        self.time = _normalize_ts(self.create_time)

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
            # 净化 local_id，避免其中的 / \ 等把文件写到意外路径
            safe_id = re.sub(r'[^\w.-]', '_', str(self.local_id)) if self.local_id is not None else 'na'
            dest = os.path.join(self.media_dir, f'media_{int(time.time() * 1000)}_{safe_id}{ext}')
            # 流式下载并限制大小，避免大视频把内存吃满
            max_bytes = 50 * 1024 * 1024
            with requests.get(self.media_url, timeout=10, stream=True) as resp:
                if resp.status_code != 200:
                    return None
                written = 0
                with open(dest, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            logger.warning(f'媒体超过 {max_bytes // (1024*1024)}MB，已截断: {self.media_url}')
                            break
                        f.write(chunk)
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
        # 防御性 clamp：poll_interval 为 0/None 会退化成忙轮询；only_recent_seconds 为 0 会丢弃全部消息
        try:
            self.poll_interval = max(0.5, float(poll_interval))
        except (TypeError, ValueError):
            self.poll_interval = 2.0
        self.media_dir = media_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'Memory_Temp', 'localdb_media')
        try:
            self.only_recent_seconds = max(1.0, float(only_recent_seconds))
        except (TypeError, ValueError):
            self.only_recent_seconds = 15.0
        self.on_message = on_message      # callback(session_display_name, LocalDbMessage)
        self.on_sessions = on_sessions    # callback(sessions_map) 可选

        self.sessions = {}    # display_name -> {username, display_name, is_group, ...}
        self._seen_ids = {}   # username -> OrderedDict(键->True)，按插入序淘汰最旧
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._startup_time = time.time()
        self._api_ok = True       # 最近一次会话拉取是否成功
        self._api_warned_at = 0.0 # 用于抑制 API 未就绪时的重复报错
        self._members_cache = {}  # 群username -> {'ts': 时间, 'map': {wxid: 显示名}}
        self._members_fail_at = {}  # 群username -> 上次失败时间（负缓存，避免每条群消息都打一次 HTTP）
        self._collision_warned = set()  # 已告警过的"同名会话冲突"显示名，避免每轮轮询刷屏

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
                        logger.warning(f'WeFlow HTTP API 返回 401 未授权：请检查 LOCALDB_API_TOKEN 与 WeFlow「设置→HTTP API→访问令牌」是否一致（当前配置: {_mask_token(self.token)}）。')
                    else:
                        logger.warning(f'获取会话列表失败: HTTP {r.status_code}: {r.text[:200]}')
                return self.sessions
            data = r.json()
            # 校验业务成功标志：WeFlow 重启/DB 解密中可能返回 200 但 success=false 或空列表，
            # 此时绝不能清空会话表，否则会全量漏消息。
            if not isinstance(data, dict) or not data.get('success'):
                self._api_ok = False
                now = time.time()
                if now - self._api_warned_at > 30:
                    self._api_warned_at = now
                    logger.warning(f"获取会话列表失败: {data.get('error') or data.get('message') if isinstance(data, dict) else '响应格式异常'}")
                return self.sessions
            items = data.get('sessions') or []
            if not items:
                # success 但空列表：保留旧快照，避免瞬时空响应清掉会话表
                now = time.time()
                if now - self._api_warned_at > 30:
                    self._api_warned_at = now
                    logger.warning('获取会话列表为空，保留上一次会话快照（如持续为空请确认 WeFlow 已读取到聊天）')
                self._api_ok = True
                return self.sessions
            self._api_ok = True
            with self._lock:
                new_sessions = {}
                for it in items:
                    username = it.get('username')
                    display_name = it.get('displayName') or username
                    if not username:
                        continue
                    is_group = self._is_group_session(username, it.get('type'))
                    info = {
                        'username': username,
                        'display_name': display_name,
                        'is_group': is_group,
                        'lastTimestamp': it.get('lastTimestamp'),
                    }
                    # 同名会话冲突检测：避免后一个会话静默覆盖前一个，导致串话/漏听。
                    # 仅当该显示名在监听列表内时才告警（无关聊天不刷屏），且每个名字只告警一次。
                    existing = new_sessions.get(display_name)
                    if existing and existing.get('username') != username:
                        if display_name in self.listen_names and display_name not in self._collision_warned:
                            self._collision_warned.add(display_name)
                            logger.warning(f"会话显示名冲突：'{display_name}' 同时对应 {existing.get('username')} 与 {username}，已仅以 username 区分（可能监听到错误会话，建议给该聊天设置唯一备注名）")
                    else:
                        new_sessions[display_name] = info
                    # username 键始终保留，供精确解析使用
                    new_sessions.setdefault(username, info)
                self.sessions = new_sessions
            if self.on_sessions:
                try:
                    with self._lock:
                        snapshot = dict(self.sessions)
                    self.on_sessions(snapshot)
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
                self._api_ok = False
                logger.warning(f'获取消息失败: HTTP {r.status_code}: {r.text[:200]}')
                return []
            data = r.json()
            if not data.get('success'):
                logger.warning(f"获取消息失败: {data.get('error') or data.get('message')}")
                return []
            return data.get('messages') or []
        except requests.exceptions.RequestException as e:
            # 连接类错误同样置 _api_ok=False，让 poll_once 进入静默重试而非每会话刷错误日志
            self._api_ok = False
            now = time.time()
            if now - self._api_warned_at > 30:
                self._api_warned_at = now
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
            # 1) 精确匹配显示名
            if name in sessions:
                result.append((name, sessions[name]))
                continue
            # 2) 精确匹配 username（配置里可能直接填了 wxid）
            matched = None
            for display_name, info in sessions.items():
                if info.get('username') == name:
                    matched = (display_name, info)
                    break
            # 3) 模糊匹配：必须"唯一命中"才接受，多命中不猜，避免短名（如"小""群""测试"）匹配到错误聊天
            if matched is None:
                candidates = []
                for display_name, info in sessions.items():
                    if display_name and name and (name in display_name or display_name in name):
                        candidates.append((display_name, info))
                if len(candidates) == 1:
                    matched = candidates[0]
                elif len(candidates) > 1:
                    logger.error(f"会话 '{name}' 模糊匹配到 {len(candidates)} 个候选（{[c[0] for c in candidates]}），为避免回错聊天已跳过；请使用更精确的昵称/备注")
            if matched:
                result.append(matched)
                logger.info(f"会话 '{name}' 匹配到本地库会话 '{matched[0]}'")
            elif self._api_ok:
                logger.error(f'本地库中未找到会话 "{name}"，请确认 WeFlow 已读取到该聊天且昵称/备注一致')
        return result

    # ---- 群成员 / wxid→昵称解析 ----

    def fetch_group_members(self, username, force=False):
        """拉取群成员并缓存 wxid->显示名 映射，返回 {wxid: 显示名}。
        显示名优先级：群里备注(groupNickname) > displayName > nickname > remark > wxid。"""
        key = username
        with self._lock:
            cached = self._members_cache.get(key)
            fail_at = self._members_fail_at.get(key, 0.0)
        if not force and cached and (time.time() - cached.get('ts', 0)) < 600:
            return dict(cached['map'])
        # 失败负缓存：30 秒内不重复请求，避免每条群消息都打一次 HTTP（请求风暴）
        if not force and fail_at and (time.time() - fail_at) < 30:
            return {}
        try:
            params = {'chatroomId': username, 'includeMessageCounts': '0'}
            if self.token:
                params['access_token'] = self.token
            r = requests.get(f'{self.api_base}/api/v1/group-members', params=params,
                             headers=self._headers(), timeout=10)
            if r.status_code != 200:
                with self._lock:
                    self._members_fail_at[key] = time.time()
                logger.warning(f'获取群成员失败: HTTP {r.status_code} ({username})')
                return {}
            data = r.json()
            m = {}
            for mb in data.get('members') or []:
                wxid = mb.get('wxid')
                if not wxid:
                    continue
                name = (mb.get('groupNickname') or mb.get('displayName') or mb.get('nickname')
                        or mb.get('remark') or wxid)
                m[wxid] = name
            with self._lock:
                self._members_cache[key] = {'ts': time.time(), 'map': m}
                self._members_fail_at.pop(key, None)
            return dict(m)
        except Exception as e:
            with self._lock:
                self._members_fail_at[key] = time.time()
            logger.warning(f'获取群成员失败 {username}: {e}')
            return {}

    def resolve_sender(self, who, sender):
        """把群消息里的 wxid 发送者解析成真实昵称（私聊直接用 who）。"""
        if not sender:
            return who
        username = None
        with self._lock:
            info = self.sessions.get(who)
        if info and info.get('is_group'):
            username = info.get('username')
        elif isinstance(who, str) and who.endswith('@chatroom'):
            username = who
        if not username:
            return who  # 私聊：who 就是对方昵称
        m = self.fetch_group_members(username)
        return m.get(sender, sender)

    # ---- 轮询 ----

    def _seen_get(self, username):
        """取得（或创建）某会话的去重 OrderedDict（需在 self._lock 内调用）。"""
        od = self._seen_ids.get(username)
        if od is None:
            od = OrderedDict()
            self._seen_ids[username] = od
        return od

    @staticmethod
    def _dedup_key(m):
        """稳健的去重键：优先 serverId，其次 localId（注意 0 是合法值，不能用 or），
        都没有则用 (createTime, sender, content) 组合哈希，保证任何消息都有稳定键。"""
        sid = m.get('serverId')
        lid = m.get('localId')
        if sid is not None:
            return ('s', sid)
        if lid is not None:
            return ('l', lid)
        basis = f"{m.get('createTime')}|{m.get('senderUsername')}|{str(m.get('content'))[:64]}"
        return ('h', hash(basis))

    def poll_once(self):
        """轮询一轮：对每个目标会话拉取新消息并派发。"""
        if not self._api_ok:
            # API 未就绪/未授权：静默重试（fetch_sessions 会按需提示），避免刷屏
            self.fetch_sessions()
            return
        targets = self.resolve_target_sessions()
        for display_name, info in targets:
            username = info['username']
            messages = self.fetch_messages(username, limit=30)
            if not messages:
                continue
            # 同时投递收到与自己发送的消息（attr 由 isSend 决定），按时间升序处理。
            # 时间戳先归一为秒再排序，避免毫秒/秒混用导致排序错乱。
            messages = sorted(messages, key=lambda m: _normalize_ts(m.get('createTime')))
            for m in messages:
                # 单条消息整体保护：任一条解析失败都不应中断本轮、更不能导致已派发消息下轮重复
                try:
                    if not isinstance(m, dict):
                        continue
                    # 系统提示（撤回/拍一拍等）不进入 AI；localType 归一后再比较
                    if _safe_int(m.get('localType'), default=-1) == TYPE_SYSTEM:
                        continue
                    # 只处理最近 N 秒内的消息（now 每条现取，避免长轮次里窗口过期误判）
                    ct = _normalize_ts(m.get('createTime'))
                    now = time.time()
                    if ct and (now - ct) > self.only_recent_seconds:
                        continue  # 启动/漏检时的历史消息不处理
                    key = self._dedup_key(m)
                    with self._lock:
                        od = self._seen_get(username)
                        if key in od:
                            continue
                        # 先提交去重键再派发（at-most-once）：即使派发抛异常也不会下轮重复投递
                        od[key] = True
                        while len(od) > _SEEN_MAX:
                            od.popitem(last=False)  # 淘汰最旧（插入序），而非 set 的任意序
                    msg = LocalDbMessage(m, display_name, self.media_dir)
                    if self.on_message:
                        self.on_message(display_name, msg)
                except Exception as e:
                    logger.error(f'处理/派发本地消息失败: {e}', exc_info=True)

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
        # 允许 stop() 之后重新 start()：必须清除停止标志，否则新线程会立即退出
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name='LocalDbListener', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
