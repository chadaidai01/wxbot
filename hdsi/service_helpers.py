# -*- coding: utf-8 -*-
"""上游 src/service.ts 行 7009-8524：InterludeService 类之后的全部模块级函数/常量。

对应上游 HDS-Interlude 1.0.1-beta6-rebuild（Gitee MomoiCore/hds-interlude master）。
本文件不包含 InterludeService 类（上游 624-7080）；类本体由 hdsi/service.py 组合各 mixin。

移植约定（见 hdsi/PORTING_GUIDE.md / hdsi/SERVICE_PORTING_SPEC.md）：
- Koishi Session → hdsi.platform.session.InboundSession；附件已解析进 session.elements
  （MessageElement: type=image/audio/file/video/face/at/text），函数同时兼容 dict 形状与
  上游的 attrs/data 嵌套。
- Buffer → bytes；Date → datetime（UTC aware）；Time.hour/minute → timedelta；
  node:crypto 只在本区间之外使用：scan_sticker_library 用 hashlib.sha256 计算 hexdigest，
  再传给 stable_sticker_asset_id（与上游 createHash('sha256').digest('hex') 一致）。
- h('img', ...) 等元素构造不进入本文件；仅产出描述性文本/事实占位（具体由 service_media 按平台映射）。
- 正则、阈值、截断长度、中文/英文文案逐字保留；JS 的 ?? / || / Number / Math.floor / Math.round /
  Number.isSafeInteger 等语义由文件头部的内部小工具复刻。
- normalize_database_row 已由 hdsi/store.py 实现，本文件只做导入转发（见 8169 段）。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .alter import normalize_alter_value
from .platform.session import InboundSession
from .qq_face import normalize_qq_native_face_segments
from .script.authored_actions import resolve_authored_actions
from .script.development import development_scenes
from .script.life_handoff import normalize_life_handoff
# 上游 8169-8189 normalizeDatabaseRow：本项目实现在 hdsi/store.py，这里只做转发（不再重复实现）。
from .store import normalize_database_row
from .story_state import normalize_continuity_snapshot, normalize_scene_presence_state
from .time_utils import (
    calendar_day_key,
    iso,
    local_clock_minutes,
    now_utc,
    parse_time,
    story_local_time_context,
)
from .utils import clamp_number, clip, is_record


# ===================== 非上游内部小工具（JS 语义复刻） =====================

# JS 的 undefined：Python 用私有哨兵区分「缺省」与 None（JSON null）。
_UNDEFINED = object()
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _text(value: Any) -> str:
    """TS ``String(value ?? '')``。None → ''，bool → 'true'/'false'。"""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return value
    return str(value)


def _field(value: Any, key: str, default: Any = None) -> Any:
    """dict/对象字段读取；None 安全（本项目 dict 与轻量对象形状并存）。"""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _attr(element: Any, key: str) -> Any:
    """元素字段读取：兼容 dict / MessageElement / attrs / data / raw 嵌套。"""
    if element is None:
        return None
    if isinstance(element, dict):
        if key in element:
            return element.get(key)
        for container_key in ('attrs', 'data'):
            container = element.get(container_key)
            if isinstance(container, dict) and key in container:
                return container.get(key)
        return None
    value = getattr(element, key, None)
    if value is not None:
        return value
    raw = getattr(element, 'raw', None)
    if isinstance(raw, dict):
        if key in raw:
            return raw.get(key)
        for container_key in ('attrs', 'data'):
            container = raw.get(container_key)
            if isinstance(container, dict) and key in container:
                return container.get(key)
    return None


def _first(*values: Any) -> Any:
    """TS ``a ?? b``：返回第一个非 None 的值。"""
    for value in values:
        if value is not None:
            return value
    return None


def _children_of(element: Any) -> Sequence[Any]:
    children = _attr(element, 'children')
    return children if isinstance(children, (list, tuple)) else ()


def _is_number(value: Any) -> bool:
    """TS ``typeof value === 'number'``（bool 不算）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _js_number(value: Any) -> Optional[float]:
    """JS ``Number(value)``；不可转换 / 非有限返回 None（等价 NaN）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _js_number_or_zero(value: Any) -> float:
    number = _js_number(value)
    return 0.0 if number is None else number


def _js_number_text(value: float) -> str:
    """JS 模板里 ``${number}`` 的文本形式（整数不带 .0）。"""
    number = float(value)
    if number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER:
        return str(int(number))
    return repr(number)


def _js_floor(value: Any) -> Optional[int]:
    number = _js_number(value)
    return None if number is None else math.floor(number)


def _js_round(value: float) -> int:
    """JS ``Math.round``：.5 向 +∞ 取整（Python round 是银行家取整）。"""
    return math.floor(value + 0.5)


def _is_safe_integer(value: Any) -> bool:
    """JS ``Number.isSafeInteger``。"""
    if not _is_number(value):
        return False
    number = float(value)
    return number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER


def _safe_int_or_none(value: Any) -> Optional[int]:
    return int(value) if _is_safe_integer(value) else None


def _time_ms(value: Any) -> Optional[float]:
    """``toDate(value)?.getTime()`` 等价的 epoch 毫秒。"""
    parsed = parse_time(value)
    return None if parsed is None else parsed.timestamp() * 1000.0


def _time_ms_or_zero(value: Any) -> float:
    ms = _time_ms(value)
    return 0.0 if ms is None else ms


def _diff_ms(later: Any, earlier: Any) -> Optional[float]:
    later_date = parse_time(later)
    earlier_date = parse_time(earlier)
    if later_date is None or earlier_date is None:
        return None
    return (later_date - earlier_date).total_seconds() * 1000.0


def _datetime_from_ms(value: float) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def _strip_punct_symbol_space(text: str) -> str:
    """JS ``replace(/[\\p{P}\\p{S}\\s]+/gu, '')``（Unicode 标点/符号用 unicodedata 判定）。"""
    kept: List[str] = []
    for character in text:
        if character.isspace():
            continue
        if unicodedata.category(character)[0] in ('P', 'S'):
            continue
        kept.append(character)
    return ''.join(kept)


def _json_stringify(value: Any) -> str:
    """诊断文案用的 ``JSON.stringify`` 等价（覆盖本文件出现的标量/小对象）。"""
    if value is _UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if _is_number(value):
        number = float(value)
        if not math.isfinite(number):
            return 'null'  # JSON.stringify(NaN/Infinity) → 'null'
        if number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER:
            return str(int(number))
        return repr(number)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=_text)


def _js_falsy(value: Any) -> bool:
    """JS ``!value`` 的常用分支（''/0/None/False/空数组为空；dict 恒真）。"""
    if value is None or value is False:
        return True
    if value is True:
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value == ''
    return False


# ===================== 上游 7009-7025：故事/参与者 ID =====================

def story_id_for_character(platform: str, self_id: str) -> str:
    """上游 7009 ``storyIdForCharacter``。"""
    return f'character:{platform}:{self_id}'


def legacy_story_id_for(platform: str, self_id: str, user_id: str) -> str:
    """上游 7011 ``legacyStoryIdFor``。"""
    return f'{platform}:{self_id}:{user_id}'


def participant_id_for(platform: str, self_id: str, user_id: str) -> str:
    """上游 7013 ``participantIdFor``。"""
    return f'{platform}:{self_id}:{user_id}'


def participant_id_for_story(story_id: str, platform: str, self_id: str, user_id: str) -> str:
    """上游 7015-7017 ``participantIdForStory``。"""
    return f'{participant_id_for(platform, self_id, user_id)}:{story_id}'[:255]


def same_participant_endpoint(participant: Any, session: InboundSession) -> bool:
    """上游 7019-7024 ``sameParticipantEndpoint``。"""
    onebot_pair = is_one_bot_platform(_field(participant, 'platform')) and is_one_bot_platform(_field(session, 'platform'))
    return (
        (_field(participant, 'platform') == _field(session, 'platform') or onebot_pair)
        and normalize_account_id(_field(participant, 'selfId')) == normalize_account_id(_field(session, 'selfId'))
        and normalize_account_id(_field(participant, 'userId')) == normalize_account_id(_field(session, 'userId'))
    )


def is_one_bot_platform(platform: Any) -> bool:
    """上游 7026-7034 ``isOneBotPlatform``。"""
    value = _text(platform).lower()
    return (
        value == 'onebot'
        or value.startswith('onebot:')
        or value == 'napcat'
        or value.startswith('napcat:')
        or value == 'qq:onebot'
        or value.startswith('qq:onebot:')
    )


# ===================== 上游 7036-7082：入站图片提取 =====================

def extract_session_image_sources(session: InboundSession) -> List[str]:
    """上游 7036-7082 ``extractSessionImageSources``（非导出）。

    上游解析 ``h.parse(session.content)`` 得到的元素树；本项目元素由平台层解析进
    ``session.elements``，因此优先访问元素，再对原始 content 回退 HTML/CQ 正则。
    旧图片元素不会粘到后续纯文本回合：与上游一致，只处理本消息内容。
    """
    raw = _text(_field(session, 'content'))
    sources: List[str] = []

    def add(value: Any, kind: str = 'url') -> None:
        source = _text(value).strip()
        if not source or source in sources:
            return
        if len(source) > 8 * 1024 * 1024:
            return
        if re.match(r'^https?:\/\/', source, re.IGNORECASE):
            sources.append(f'onebot-url:{source}' if kind == 'adapter-url' else source)
        elif re.match(r'^data:image\/', source, re.IGNORECASE):
            sources.append(source)
        elif kind == 'file':
            sources.append(f'onebot-file:{source}')

    def visit(element: Any) -> None:
        if not element:
            return
        type_ = _text(_attr(element, 'type')).lower()
        if type_ in ('img', 'image'):
            src = _first(_attr(element, 'src'), _attr(element, 'url'))
            if src:
                add(src)
            else:
                add(_first(_attr(element, 'file'), _attr(element, 'value')), 'file')
        for child in _children_of(element):
            visit(child)

    # Session.elements 由适配器持有且可能被其它中间件复用；上游只解析本消息原始内容，
    # 避免旧图片元素被意外挂到后续纯文本回合上。
    for element in (_field(session, 'elements') or []):
        visit(element)
    if not sources:
        pattern = re.compile(r'<(?:img|image)\b[^>]*(?:src|url)=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
        for match in pattern.finditer(raw):
            add(match.group(1))
    # OneBot/NapCat 可能在原始消息里留下 CQ image 段。优先 CDN URL；只有 file token
    # 时保留 token，让当前机器人可以调用 get_image(file)，而不信任任意用户 URL。
    cq_pattern = re.compile(r'\[CQ:image,([^\]]+)\]', re.IGNORECASE)
    for match in cq_pattern.finditer(raw):
        fields: Dict[str, str] = {}
        for part in match.group(1).split(','):
            index = part.find('=')
            if index > 0:
                fields[part[:index].strip().lower()] = part[index + 1:].strip()
        add(fields.get('url') or fields.get('cache_url'), 'adapter-url')
        if not fields.get('url') and not fields.get('cache_url'):
            add(fields.get('file'), 'file')
    return sources


# ===================== 上游 7084-7100：语音计数 =====================

def extract_session_voice_count(session: InboundSession) -> int:
    """上游 7084-7100 ``extractSessionVoiceCount``。

    从 Koishi 元素与原始 OneBot CQ 回退里识别 record/audio 段，不保留二进制语音载荷。
    """
    raw = _text(_field(session, 'content'))
    count = 0

    def visit(element: Any) -> None:
        nonlocal count
        if not element:
            return
        type_ = _text(_attr(element, 'type')).lower()
        if type_ in ('audio', 'record'):
            count += 1
        for child in _children_of(element):
            visit(child)

    provider = _field(session, 'voice_count')
    if callable(provider):
        try:
            count = int(provider() or 0)
        except Exception:  # noqa: BLE001 - 平台便利方法失败时回退上游逻辑
            count = 0
    if count:
        return count
    for element in (_field(session, 'elements') or []):
        visit(element)
    if count:
        return count
    return len(re.findall(r'\[CQ:record,[^\]]*\]', raw, re.IGNORECASE))


# ===================== 上游 7102-7147：语音/音频来源 =====================

def extract_session_audio_sources(session: InboundSession) -> List[str]:
    """上游 7102-7147 ``extractSessionAudioSources``。

    与图片不同，语音优先 OneBot file token：原始 record URL 提供 SILK，只有
    SnowLuma 服务端转码（get_record out_format）能把它变成模型可读的音频载荷。
    """
    raw = _text(_field(session, 'content'))
    sources: List[str] = []

    def add(value: Any, kind: str = 'file') -> None:
        source = _text(value).strip()
        if not source or source in sources or len(source) > 512:
            return
        if re.match(r'^data:audio\/', source, re.IGNORECASE):
            sources.append(source)
            return
        # http(s) record URL 提供原始 SILK；没有 OneBot file token 无法转码，
        # 因此不进入 native-audio 通道。
        if re.match(r'^https?:\/\/', source, re.IGNORECASE):
            return
        if kind == 'file':
            sources.append(f'onebot-file:{source}')

    def visit(element: Any) -> None:
        if not element:
            return
        type_ = _text(_attr(element, 'type')).lower()
        if type_ in ('audio', 'record'):
            explicit_file = _attr(element, 'file')
            value = _attr(element, 'value')
            if explicit_file:
                add(explicit_file, 'file')
            elif value and not re.match(r'^(?:https?:\/\/|data:audio\/)', value, re.IGNORECASE):
                # MessageElement 把 OneBot file token 放在 value：必须按 file 语义保留。
                add(value, 'file')
            else:
                add(value or _attr(element, 'url'), 'url')
        for child in _children_of(element):
            visit(child)

    # 只解析本消息原始内容；Session.elements 由适配器持有且可能跨回合复用。
    for element in (_field(session, 'elements') or []):
        visit(element)
    if not sources:
        pattern = re.compile(r'<(?:audio|record)\b[^>]*(?:file|src|url)=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
        for match in pattern.finditer(raw):
            add(match.group(1))
    # OneBot 可能留下只有 file token 的 CQ record 段（典型 NapCat/SnowLuma 私聊语音），
    # 或一个无法转码的额外 url 字段。
    cq_pattern = re.compile(r'\[CQ:record,([^\]]+)\]', re.IGNORECASE)
    for match in cq_pattern.finditer(raw):
        fields: Dict[str, str] = {}
        for part in match.group(1).split(','):
            index = part.find('=')
            if index > 0:
                fields[part[:index].strip().lower()] = part[index + 1:].strip()
        add(fields.get('file'), 'file')
    # QQ 音频文件走 <file> 元素（CDN 直链 + 文件名扩展），原始字节可直接
    # 作为 input_audio；与语音的 SILK 转码路径不同，标记 file-url 前缀。
    for fact in extract_session_file_facts(session):
        if not fact.get('audio') or not re.match(r'^https?:\/\/', _text(fact.get('url')), re.IGNORECASE):
            continue
        encoded = 'file-url:%s#%s:%s' % (
            fact.get('url'),
            urllib.parse.quote(_text(fact.get('name')), safe="!'()*-._~"),
            _js_number_text(_js_number_or_zero(fact.get('size'))),
        )
        if encoded not in sources:
            sources.append(encoded)
    return sources


# ===================== 上游 7149-7185：文件事实 =====================

# 上游 7151 AUDIO_FILE_EXTENSIONS
AUDIO_FILE_EXTENSIONS = re.compile(r'\.(mp3|wav|ogg|m4a|flac|amr|aac|wma)$', re.IGNORECASE)


def extract_session_file_facts(session: InboundSession) -> List[Dict[str, Any]]:
    """上游 7158-7185 ``extractSessionFileFacts``。

    入站 <file> 元素携带 QQ CDN URL、展示名与大小，是附件事实：原始标记绝不能
    作为文本进入模型，音频扩展名的文件同时喂给 native-audio 通道。
    """
    raw = _text(_field(session, 'content'))
    facts: List[Dict[str, Any]] = []

    def push(url: Any, name: Any, size: Any) -> None:
        url_text = _text(url)
        name_text = _text(name)
        if (not url_text and not name_text) or len(facts) >= 3:
            return
        if any(item.get('url') == url_text and item.get('name') == name_text for item in facts):
            return
        # JS Number(attrs.size ?? attrs['file-size'] ?? 0) || 0；整数保持整型，
        # 与上游数字的 JSON 表现一致（2048 而非 2048.0）。
        size_number: Any = _js_number_or_zero(size)
        if isinstance(size_number, float) and size_number.is_integer():
            size_number = int(size_number)
        facts.append({
            'name': name_text[:200],
            'url': url_text[:1_000],
            'size': size_number,
            'audio': bool(AUDIO_FILE_EXTENSIONS.search(name_text)),
        })

    def visit(element: Any) -> None:
        if not element:
            return
        if _text(_attr(element, 'type')).lower() == 'file':
            push(
                _text(_attr(element, 'src') or _attr(element, 'url') or _attr(element, 'value')).strip(),
                _text(_attr(element, 'name') or _attr(element, 'file') or _attr(element, 'title')
                      or _attr(element, 'value')).strip(),
                _js_number_or_zero(_first(_attr(element, 'size'), _attr(element, 'file-size'), 0)),
            )
        for child in _children_of(element):
            visit(child)

    for element in (_field(session, 'elements') or []):
        visit(element)
    if not facts:
        pattern = re.compile(r'<file\b([^>]*)\/?>', re.IGNORECASE)
        for match in pattern.finditer(raw):
            attrs = match.group(1)

            def pick(key: str) -> str:
                found = re.search(key + r'=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
                return found.group(1).strip() if found else ''

            push(
                pick('src') or pick('url'),
                pick('name') or pick('file') or pick('title'),
                _js_number_or_zero(pick('size') or pick('file-size')),
            )
    return facts


# ===================== 上游 7186-7205：群聊附件占位 =====================

def describe_group_attachments(content: Any) -> str:
    """上游 7188-7205 ``describeGroupAttachments``。

    群聊入站没有原生附件通道：把 <img>/<file>/<record> 等元素标记转成事实占位
    （保留「发过什么」的信息），URL 污水不进群上下文，也不再被模型复述。
    """
    text = normalize_qq_native_face_segments(_text(content))
    text = re.sub(r'<(?:record|audio)\b[^>]*\/?>', '[语音]', text, flags=re.IGNORECASE)
    text = re.sub(r'<(?:img|image)\b[^>]*\/?>', '[图片]', text, flags=re.IGNORECASE)
    text = re.sub(r'<video\b[^>]*\/?>', '[视频]', text, flags=re.IGNORECASE)

    def replace_file(match: 're.Match[str]') -> str:
        name = re.search(r'(?:name|file|title)=["\']([^"\']+)["\']', match.group(0), re.IGNORECASE)
        return f'[文件：{name.group(1)}]' if name else '[文件]'

    text = re.sub(r'<file\b[^>]*\/?>', replace_file, text, flags=re.IGNORECASE)
    text = re.sub(r'<\/(?:file|img|image|audio|record|video)>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', text, flags=re.IGNORECASE)

    def replace_cq_file(match: 're.Match[str]') -> str:
        name = re.search(r'(?:name|file)=([^,\]]+)', match.group(1), re.IGNORECASE)
        return f'[文件：{name.group(1)}]' if name else '[文件]'

    return re.sub(r'\[CQ:file,([^\]]*)\]', replace_cq_file, text, flags=re.IGNORECASE)


# ===================== 上游 7207-7244：Promise/媒体小工具 =====================

def with_timeout(work: Any, timeout_ms: Any) -> Any:
    """上游 7207-7215 ``withTimeout``（同步降级）。

    上游给 Promise 加 setTimeout 超时；本项目所有上游调用点都是同步阻塞调用，
    无法在不引入线程/进程的情况下中断。语义降级为「直接执行并返回结果」：
    - 可调用对象（等价 Promise 工厂 / 尚未 resolve 的调用）→ 调用并返回其返回值；
    - 其它值（等价已 resolve 的 Promise）→ 原样返回。
    ``timeout_ms`` 仅为对齐上游调用点保留，不参与执行。
    """
    return work() if callable(work) else work


def guess_image_mime(bytes_: Any, hinted: Any = None) -> str:
    """上游 7217-7227 ``guessImageMime``（非导出）。"""
    if not isinstance(bytes_, (bytes, bytearray)):
        return ''
    data = bytes(bytes_)
    hint = _text(hinted).lower()
    if hint.startswith('image/'):
        return hint
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return 'image/jpeg'
    if len(data) >= 8 and data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10]):
        return 'image/png'
    if len(data) >= 6 and (data[:6] == b'GIF87a' or data[:6] == b'GIF89a'):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def guess_audio_format(bytes_: Any, hinted_name: Any = None) -> str:
    """上游 7229-7245 ``guessAudioFormat``。

    音频文件以原始字节到达（不同于 SILK 语音）；只接受 OpenAI 兼容
    input_audio 通道声明的格式。
    """
    if not isinstance(bytes_, (bytes, bytearray)):
        return ''
    data = bytes(bytes_)
    hinted = re.search(r'\.(mp3|wav|ogg|m4a|flac|amr)\b', _text(hinted_name), re.IGNORECASE)
    if hinted:
        return hinted.group(1).lower()
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return 'wav'
    if len(data) >= 4 and data[:4] == b'OggS':
        return 'ogg'
    if len(data) >= 8 and data[4:8] == b'ftyp':
        return 'm4a'
    if len(data) >= 4 and data[:4] == b'fLaC':
        return 'flac'
    if len(data) >= 5 and data[:5] == b'#!AMR':
        return 'amr'
    if len(data) >= 3 and data[:3] == b'ID3':
        return 'mp3'
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return 'mp3'
    return ''


def is_animated_image_mime(mime: str) -> bool:
    """上游 7242-7244 ``isAnimatedImageMime``（非导出）。"""
    return mime in ('image/gif', 'image/webp', 'image/apng')


# ===================== 上游 7246-7253：群 ID =====================

def session_group_id(session: InboundSession) -> str:
    """上游 7246-7249 ``sessionGroupId``（非导出）。"""
    raw = _text(_field(session, 'guildId') or _field(session, 'channelId') or '')
    return normalize_group_id(raw)


def normalize_group_id(value: Any) -> str:
    """上游 7251-7253 ``normalizeGroupId``。"""
    return re.sub(r'^(?:group|guild):', '', _text(value).strip(), count=1, flags=re.IGNORECASE)


# ===================== 上游 7255-7259：reaction / 原生表情常量 =====================

# 上游 7255 CHAT_REACTION_NAMES
CHAT_REACTION_NAMES: List[str] = ['like', 'smile', 'laugh', 'heart', 'surprised', 'sad', 'angry']

# 上游 7257-7259 QQ_REACTION_IDS
QQ_REACTION_IDS: Dict[str, str] = {
    'like': '76', 'smile': '14', 'laugh': '182', 'heart': '66', 'surprised': '0', 'sad': '5', 'angry': '106',
}

# 上游 7261 NATIVE_FACE_SEMANTICS
NATIVE_FACE_SEMANTICS: List[str] = ['smile', 'laugh', 'sweat', 'awkward', 'heart', 'surprised', 'sad', 'angry']

# 上游 7262-7264 QQ_NATIVE_FACE_IDS
QQ_NATIVE_FACE_IDS: Dict[str, str] = {
    'smile': '14', 'laugh': '182', 'sweat': '27', 'awkward': '111', 'heart': '66',
    'surprised': '0', 'sad': '5', 'angry': '106',
}


def normalize_allowed_native_faces(value: Any) -> List[str]:
    """上游 7266-7268 ``normalizeAllowedNativeFaces``（非导出）。"""
    if not isinstance(value, list):
        return []
    filtered = [item for item in value if item in NATIVE_FACE_SEMANTICS]
    return list(dict.fromkeys(filtered))[:len(NATIVE_FACE_SEMANTICS)]


def normalize_expression_threshold(value: Any) -> float:
    """上游 7271-7274 ``normalizeExpressionThreshold``（非导出）。"""
    number = _js_number(value)
    if number is None:
        return 0.7
    return max(0.0, min(1.0, number))


def calibrated_native_face_willingness(semantic: str, willingness: Any, reply_content: Any) -> float:
    """上游 7276-7298 ``calibratedNativeFaceWillingness``。

    模型的 willingness 是意图估计而非传输许可。原生表情需要可见文本对应物，
    否则模型只要返回 willingness=1 就能把每条例行回复变成表情。0.90 上限
    刻意让高于 0.90 的阈值成为事实上的近似禁用模式。
    """
    text = re.sub(r'<sep/>', ' ', _text(reply_content)).strip()
    if not text:
        return 0
    patterns: Dict[str, 're.Pattern[str]'] = {
        'smile': re.compile(r'(?:微笑|开心|高兴|谢谢|好耶|好呀|可以|行吧|嘿|哈哈)', re.IGNORECASE),
        'laugh': re.compile(r'(?:哈{2,}|笑死|好笑|乐|绷不住|蚌埠|草|救命)', re.IGNORECASE),
        'sweat': re.compile(r'(?:流汗|尴尬|无语|服了|麻了|救命|离谱|完了|累|忙|不知道怎么说)', re.IGNORECASE),
        'awkward': re.compile(r'(?:尴尬|那个|呃|emm|……|\.{3,}|我真的|怎么说呢)', re.IGNORECASE),
        'heart': re.compile(r'(?:喜欢|爱你|抱抱|可爱|谢谢|好耶|开心|高兴)', re.IGNORECASE),
        'surprised': re.compile(r'(?:不会吧|真的假的|居然|什么|怎么会|\?{1,}|？{1,}|!{1,}|！{1,})', re.IGNORECASE),
        'sad': re.compile(r'(?:难过|哭|委屈|可怜|遗憾|心疼|唉)', re.IGNORECASE),
        'angry': re.compile(r'(?:生气|气死|烦|闭嘴|别[再乱闹说]|离谱|过分|你.*(?:啊|吧|？|!|！))', re.IGNORECASE),
    }
    pattern = patterns.get(semantic)
    semantic_match = bool(pattern.search(text)) if pattern is not None else False
    evidence = 0.9 if semantic_match else 0.2
    return min(0.9, normalize_expression_threshold(willingness) * (0.25 + evidence * 0.75))


# ===================== 上游 7300-7323：消息引用 / 贴纸文件 =====================

def targetable_message_id(value: Any) -> Optional[str]:
    """上游 7300-7303 ``targetableMessageId``（非导出）。"""
    message_id = _text(value).strip()
    return message_id if re.fullmatch(r'-?\d+', message_id) and message_id != '0' else None


def group_message_ref(entry_id: Any) -> str:
    """上游 7305-7307 ``groupMessageRef``（非导出）。"""
    return f'msg-{max(0, math.floor(_js_number_or_zero(entry_id)))}'


def list_sticker_files(root: str) -> List[str]:
    """上游 7309-7323 ``listStickerFiles``（同步移植）。

    上游 async readdir/resolve 递归（深度 ≤3）收集图片扩展名文件并排序；
    这里用 os.scandir 实现同样语义（node:path 用 os.path 的约定）。
    """
    files: List[str] = []
    pattern = re.compile(r'\.(?:png|jpe?g|webp|gif)$', re.IGNORECASE)

    def visit(directory: str, depth: int) -> None:
        if depth > 3:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            full = os.path.join(directory, entry.name)
            try:
                if entry.is_dir():
                    visit(full, depth + 1)
                elif entry.is_file() and pattern.search(entry.name):
                    files.append(full)
            except OSError:
                continue

    visit(root, 0)
    return sorted(files)


def sticker_mime(file_path: str) -> str:
    """上游 7325-7331 ``stickerMime``（非导出）。"""
    extension = os.path.splitext(_text(file_path))[1].lower()
    if extension == '.gif':
        return 'image/gif'
    if extension == '.webp':
        return 'image/webp'
    if extension == '.jpg' or extension == '.jpeg':
        return 'image/jpeg'
    return 'image/png'


def stable_sticker_asset_id(file_path: str, hash: str) -> str:
    """上游 7337-7350 ``stableStickerAssetId``。

    旧的纯标点 id 可能碰撞（例如两个文件名都归一化成 bq--6-）。保留可读路径前缀，
    再附加内容哈希片段，让每行全局唯一且对未变化的文件稳定。hash 由调用方用
    hashlib.sha256(bytes).hexdigest() 计算（与上游 createHash('sha256') 一致）。
    """
    stem = re.sub(r'\\', '/', _text(file_path))
    stem = re.sub(r'\.[^.]+$', '', stem)
    stem = re.sub(r'[^a-zA-Z0-9/_-]', '-', stem)
    stem = re.sub(r'-+', '-', stem)
    stem = re.sub(r'^[-/]+|[-/]+$', '', stem)[:220] or 'sticker'
    suffix = re.sub(r'[^a-fA-F0-9]', '', _text(hash))[:16].lower() or 'unhashed'
    return f'{stem}-{suffix}'[:255]


# ===================== 上游 7351-7397：用户口述时间 =====================

def extract_user_reported_times(content: str, now: datetime, timezone: str) -> List[Dict[str, Any]]:
    """上游 7351-7397 ``extractUserReportedTimes``。

    只从实时用户消息里提取明确的钟点陈述；这是小的事实辅助，不尝试推断所有
    时间表达。
    """
    text = _text(content)
    current_minutes = local_clock_minutes(now, timezone)
    date = calendar_day_key(now, timezone)
    facts: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def add(hour: Any, minute: Any, statement: Any) -> None:
        if isinstance(hour, bool) or isinstance(minute, bool):
            return
        if not isinstance(hour, int) or not isinstance(minute, int):
            return
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return
        clock = f'{hour:02d}:{minute:02d}'
        relation = 'past' if hour * 60 + minute < current_minutes else 'future' if hour * 60 + minute > current_minutes else 'current'
        key = f'{clock}:{statement}'
        if key in seen:
            return
        seen.add(key)
        facts.append({'localTime': f'{date} {clock}', 'relation': relation, 'statement': clip(statement, 240).strip()})

    for match in re.finditer(r'(?:今天|今晚|下午|晚上|早上|上午)?\s*(\d{1,2})\s*(?:[:：.]|点)\s*(\d{2}|半)?', text):
        hour = int(match.group(1))
        second = match.group(2)
        minute = 30 if second == '半' else (int(second) if second else 0)
        prefix = match.group(0)
        if ('下午' in prefix or '晚上' in prefix) and hour < 12:
            hour += 12
        elif not re.search(r'(?:早上|上午|下午|晚上|中午)', prefix) and 0 < hour < 12:
            # 裸写的「6.30」有歧义。优先取离当前故事时钟最近的 12 小时制解释，
            # 让傍晚的报告通常表示 18:30 而不是遥远得不合常理的 06:30。
            morning = hour * 60 + minute
            evening = (hour + 12) * 60 + minute
            if abs(evening - current_minutes) < abs(morning - current_minutes):
                hour += 12
        index = match.start()
        add(hour, minute, text[max(0, index - 48):min(len(text), index + len(match.group(0)) + 96)])
    # 时段词（中午/下午/晚上等）作为该时段代表性的钟点锚点，供守卫与 prompt
    # 理解「中午一起吃饭」这类不含数字的时间约定。
    for match in re.finditer(r'(早上|上午|中午|下午|傍晚|晚上)', text):
        hour = {'早上': 8, '上午': 9, '中午': 12, '下午': 15, '傍晚': 18, '晚上': 20}[match.group(1)]
        index = match.start()
        add(hour, 0, text[max(0, index - 48):min(len(text), index + len(match.group(0)) + 96)])
    # 中文数字钟点（「八点」「八点半」「九点一刻」）：口语消息最常用的写法，此前
    # 只有守卫的正则认识它们，prompt 侧的 userReportedTimes 反而漏掉。
    for match in re.finditer(r'([零一二三四五六七八九十两]+)点(?:(?:零|([一二三四五六七八九十两]+))分?|半)?', text):
        hour = chinese_clock_number(match.group(1))
        second = match.group(2)
        minute = 30 if second == '半' else (chinese_clock_number(second) if second else 0)
        if hour is None or minute is None:
            continue
        index = match.start()
        add(hour, minute, text[max(0, index - 48):min(len(text), index + len(match.group(0)) + 96)])
    return facts[:4]


# ===================== 上游 7399-7415：引用消息描述 =====================

def describe_quoted_message(session: InboundSession, character_name: Any = '主角') -> Optional[Dict[str, Any]]:
    """上游 7399-7415 ``describeQuotedMessage``。

    Python 会话形状：``session.quote`` 是 QuotedMessage（senderId/senderName/speaker/content），
    同时兼容上游的 ``quote.user.id`` / ``quote.member.nick`` 嵌套。
    """
    quote = _field(session, 'quote')
    if not quote:
        return None
    content = normalize_quoted_message_content(_field(quote, 'content'))
    if not content:
        return None
    user = _field(quote, 'user')
    member = _field(quote, 'member')
    sender_id = _text(_first(_field(quote, 'senderId'), _field(user, 'id'))).strip()
    is_character = bool(sender_id) and sender_id == _text(_field(session, 'selfId'))
    if is_character:
        sender_name = _text(character_name).strip() or '主角'
        speaker = f'主角「{sender_name}」'
    else:
        sender_name = normalize_group_display_name(
            _field(member, 'nick'), _field(member, 'name'), _field(user, 'nick'), _field(user, 'name'),
            _field(quote, 'senderName'), sender_id,
        ) or '未知发送者'
        speaker = (f'消息发送者「{sender_name}」（ID：{sender_id}）' if sender_id
                   else f'消息发送者「{sender_name}」')
    return {'senderId': sender_id, 'senderName': sender_name, 'speaker': speaker, 'content': content}


# ===================== 上游 7417-7434：引用内容归一化 =====================

def normalize_quoted_message_content(value: Any) -> str:
    """上游 7417-7434 ``normalizeQuotedMessageContent``。"""
    raw = normalize_qq_native_face_segments(value)
    content = raw
    content = re.sub(r'<(?:img|image)\b[^>]*\/?>(?:<\/(?:img|image)>)?', '[图片]', content, flags=re.IGNORECASE)
    content = re.sub(r'<(?:audio|record)\b[^>]*\/?>(?:<\/(?:audio|record)>)?', '[语音]', content, flags=re.IGNORECASE)
    content = re.sub(r'<video\b[^>]*\/?>(?:<\/video>)?', '[视频]', content, flags=re.IGNORECASE)
    content = re.sub(r'<(?:face|mface)\b[^>]*\/?>(?:<\/(?:face|mface)>)?', '[表情]', content, flags=re.IGNORECASE)
    content = re.sub(r'<at\b[^>]*(?:name|id)=["\']?([^\s"\'>]+)[^>]*\/?>(?:<\/at>)?', r'[@\1]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:face,[^\]]*\]', '[表情]', content, flags=re.IGNORECASE)
    content = re.sub(r'<[^>]+>', '', content)
    content = re.sub(r'[\r\n]+', ' ', content)
    content = re.sub(r'\s{2,}', ' ', content)
    return clip(content.strip(), 1_500)


# ===================== 上游 7436-7444：引用上下文归一化 =====================

def normalize_quoted_message_context(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7436-7444 ``normalizeQuotedMessageContext``（非导出）。"""
    if not is_record(value):
        return None
    content = normalize_quoted_message_content(value.get('content'))
    if not content:
        return None
    sender_id = clip(_text(value.get('senderId')), 127)
    sender_name = clip(_text(value.get('senderName')), 255) or '未知发送者'
    speaker = clip(_text(value.get('speaker')), 500) or (
        f'消息发送者「{sender_name}」（ID：{sender_id}）' if sender_id
        else f'消息发送者「{sender_name}」'
    )
    return {'senderId': sender_id, 'senderName': sender_name, 'speaker': speaker, 'content': content}


# ===================== 上游 7446-7448：允许的 reaction =====================

def normalize_allowed_reactions(value: Any) -> List[str]:
    """上游 7446-7448 ``normalizeAllowedReactions``。"""
    if not isinstance(value, list):
        return []
    filtered = [item for item in value if item in CHAT_REACTION_NAMES]
    return list(dict.fromkeys(filtered))[:len(CHAT_REACTION_NAMES)]


# ===================== 上游 7450-7519：时间线计划 =====================

# 上游 7453-7459 TIMELINE_KIND_ALIASES
TIMELINE_KIND_ALIASES: Dict[str, str] = {
    'activity': 'activity', 'action': 'activity', 'event': 'activity', 'scene': 'activity', 'behavior': 'activity',
    'thought': 'thought', 'think': 'thought', 'feeling': 'thought', 'mood': 'thought', 'inner': 'thought',
    'state': 'state', 'status': 'state', 'condition': 'state',
    '活动': 'activity', '行动': 'activity', '事件': 'activity', '场景': 'activity',
    '想法': 'thought', '心情': 'thought', '思绪': 'thought',
    '状态': 'state',
}


def coerce_timeline_kind(value: Any) -> str:
    """上游 7463-7466 ``coerceTimelineKind``（非导出）。"""
    if not isinstance(value, str):
        return ''
    normalized = value.strip().lower()
    return TIMELINE_KIND_ALIASES.get(normalized, 'activity')


def coerce_timeline_position(value: Any) -> Optional[float]:
    """上游 7469-7481 ``coerceTimelinePosition``（非导出）。

    接受数字字符串（"0.5"）与百分比字符串（"50%"），避免一个草率字段丢掉整个节点。
    """
    if _is_number(value):
        number = float(value)
        if math.isnan(number):
            return None
        return max(0.0, min(1.0, number))
    if isinstance(value, str):
        text = value.strip()
        if text.endswith('%'):
            text = text[:-1]
        parsed = _js_number(text)
        if parsed is not None:
            return max(0.0, min(1.0, parsed if parsed <= 1 else parsed / 100))
    return None


def normalize_timeline_plan(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7483-7493 ``normalizeTimelinePlan``。

    只解析这一种窄事件账本形状；未知模型字段与空计划在成为世界状态来源前被丢弃。
    """
    if not is_record(value) or not isinstance(value.get('beats'), list):
        return None
    beats: List[Dict[str, Any]] = []
    for item in value['beats']:
        if not is_record(item):
            continue
        summary = item.get('summary')
        beats.append({
            'at': coerce_timeline_position(item.get('at')),
            'kind': coerce_timeline_kind(item.get('kind')),
            'summary': clip(summary, 240).strip() if isinstance(summary, str) else '',
        })
    beats = [beat for beat in beats if beat['at'] is not None and beat['kind'] and beat['summary']]
    beats.sort(key=lambda beat: beat['at'])
    beats = beats[:4]
    if not beats:
        return None
    carry: List[str] = []
    if isinstance(value.get('carry'), list):
        for item in value['carry']:
            if isinstance(item, str):
                text = clip(item, 180).strip()
                if text:
                    carry.append(text)
        carry = carry[:4]
    result: Dict[str, Any] = {'beats': beats}
    if carry:
        result['carry'] = carry
    return result


def describe_timeline_plan_rejection(value: Any) -> str:
    """上游 7503-7517 ``describeTimelinePlanRejection``。"""
    if not is_record(value):
        return '返回不是 JSON 对象'
    if not isinstance(value.get('beats'), list):
        return '缺少 beats 数组'
    if not value['beats']:
        return 'beats 为空数组（模型未产出任何节点）'
    details: List[str] = []
    for item in value['beats']:
        if not is_record(item):
            continue
        problems: List[str] = []
        at_raw = item.get('at', _UNDEFINED)
        if coerce_timeline_position(None if at_raw is _UNDEFINED else at_raw) is None:
            problems.append(f'at={_json_stringify(at_raw)} 无法解析')
        kind_raw = item.get('kind', _UNDEFINED)
        if not coerce_timeline_kind(None if kind_raw is _UNDEFINED else kind_raw):
            problems.append(f'kind={_json_stringify(kind_raw)} 非法')
        summary = item.get('summary')
        if not isinstance(summary, str) or not summary.strip():
            problems.append('summary 为空')
        details.append('，'.join(problems) or '通过')
    return f'节点校验详情：{"；".join(details)}'


def timeline_entry_prompt_projection(entry: Dict[str, Any]) -> Dict[str, Any]:
    """上游 7521-7529 ``timelineEntryPromptProjection``。

    自动剧本散文是渲染结果，不是下一回合的时间来源。紧凑的宿主账本保留真实顺序，
    同时不让上一段文字被复制进新的时间窗口。
    """
    metadata = _field(entry, 'metadata')
    if _field(metadata, 'narrativeAuthority') == 'original-v2':
        return entry
    if _field(entry, 'kind') != 'script':
        return entry
    plan = normalize_timeline_plan(_field(metadata, 'timelinePlan'))
    if not plan:
        return entry
    beats = ' | '.join(f'{_js_round(beat["at"] * 100)}% {beat["kind"]}: {beat["summary"]}' for beat in plan['beats'])
    carry = f' Carry: {" | ".join(plan["carry"])}' if plan.get('carry') else ''
    return {**entry, 'content': f'[Host timeline ledger for this completed automatic window: {beats}.{carry}]'}


# ===================== 上游 7531-7562：可执行群聊动作 =====================

def normalize_group_chat_actions(decision: Any, capabilities: Any, context: Any) -> Dict[str, Any]:
    """上游 7531-7562 ``normalizeGroupChatActions``。"""
    if not capabilities:
        return {'reactions': []}
    targets: Dict[str, str] = {}
    for message in (_field(context, 'messages') or []):
        if not is_record(message):
            continue
        message_ref = message.get('messageRef')
        message_id = message.get('messageId')
        if message_ref and message_id:
            targets[message_ref] = message_id

    group_reply = _field(decision, 'groupReply')
    interaction = _field(decision, 'interaction')
    raw_reply_to: Any = None
    if is_record(group_reply) and group_reply.get('mode') == 'immediate':
        raw_reply_to = group_reply.get('replyTo')
    else:
        reply = _field(interaction, 'reply') if is_record(interaction) else None
        if is_record(reply) and reply.get('mode') == 'immediate':
            raw_reply_to = reply.get('replyTo')
    reply_message_id = targets.get(raw_reply_to) if _field(capabilities, 'quoteReply') and isinstance(raw_reply_to, str) else None
    reply_to = {'messageRef': raw_reply_to, 'messageId': reply_message_id} if reply_message_id else None

    allowed = set(_field(capabilities, 'reactions') or [])
    reactions: List[Dict[str, Any]] = []
    raw_reactions = _field(decision, 'messageReactions')
    if isinstance(raw_reactions, list):
        for item in raw_reactions:
            if (not is_record(item) or not isinstance(item.get('messageRef'), str)
                    or not isinstance(item.get('reaction'), str)):
                continue
            message_ref = str(item.get('messageRef'))
            reactions.append({
                'messageRef': message_ref,
                'reaction': str(item.get('reaction')),
                'messageId': targets.get(message_ref, '') or '',
            })
        reactions = [item for item in reactions if item['messageId'] and item['reaction'] in allowed][:1]
    return {**({'replyTo': reply_to} if reply_to else {}), 'reactions': reactions}


# ===================== 上游 7564-7583：群成员展示 / @ 判定 =====================

def format_group_speaker(sender_name: str, sender_id: str) -> str:
    """上游 7564-7568 ``formatGroupSpeaker``。"""
    speaker_id = _text(sender_id or 'unknown').strip() or 'unknown'
    name = re.sub(r'[\r\n]', ' ', _text(sender_name or '')).strip() or speaker_id
    return f'群成员（QQ：{speaker_id}）' if name == speaker_id else f'群成员「{name}」（QQ：{speaker_id}）'


def normalize_group_display_name(*candidates: Any) -> str:
    """上游 7570-7576 ``normalizeGroupDisplayName``（非导出）。"""
    for candidate in candidates:
        name = re.sub(r'[\r\n]', ' ', _text(candidate)).strip()
        if name:
            return name[:80]
    return ''


def mentions_bot(session: InboundSession) -> bool:
    """上游 7578-7583 ``mentionsBot``（非导出）。

    上游只看 ``session.content``；本项目平台层已把 @ 段从 content 剥离并写入
    ``InboundSession.mentioned_bot``，因此除保留上游文本/id 标签判定外也承认该标记。
    """
    self_id = normalize_account_id(_field(session, 'selfId'))
    if not self_id:
        return False
    if _field(session, 'mentioned_bot'):
        return True
    content = _text(_field(session, 'content') or '')
    return self_id in content or bool(
        re.search(r'<at[^>]+id=["\']?' + re.escape(self_id) + r'["\']?', content, re.IGNORECASE)
    )


# ===================== 上游 7585-7663：可见回复归一化 =====================

def normalize_group_visible_reply(raw: Any, interaction: Any, max_characters: Any,
                                  separator: Any = '<sep/>') -> str:
    """上游 7585-7587 ``normalizeGroupVisibleReply``。"""
    return (normalize_group_reply(raw, max_characters, separator)
            or normalize_group_interaction_reply(interaction, max_characters, separator))


def requires_visible_reply_recovery(phase: Any, group_context: Any, decision: Any) -> bool:
    """上游 7589-7592 ``requiresVisibleReplyRecovery``（非导出）。"""
    if phase != 'user-message':
        return False
    if group_context:
        return not has_structured_group_reply(decision)
    return not has_structured_interaction(_field(decision, 'interaction'))


def visible_reply_mode(decision: Any, phase: Any, group_context: Any = None) -> str:
    """上游 7594-7609 ``visibleReplyMode``。"""
    cross_actions = _field(decision, 'crossConversationActions') or []
    if phase == 'advance':
        if any(is_record(action) and action.get('mode') == 'immediate' for action in cross_actions):
            return '主动联系'
        if any(is_record(action) and action.get('mode') == 'delayed' for action in cross_actions):
            return '计划联系'
        return '无可见投递'
    if phase == 'conversation-follow-up' or phase == 'intent-due':
        interaction = _field(decision, 'interaction')
        if has_structured_interaction(interaction):
            return _field(_field(interaction, 'reply'), 'mode')
        if any(is_record(action) and action.get('mode') == 'immediate' for action in cross_actions):
            return '主动联系'
        return '无可见投递'
    interaction = _field(decision, 'interaction')
    if not group_context:
        return _field(_field(interaction, 'reply'), 'mode') if has_structured_interaction(interaction) else '未提供或无效'
    group_reply = _field(decision, 'groupReply')
    if has_structured_group_reply_field(group_reply):
        return f'group:{_field(group_reply, "mode")}'
    if has_structured_interaction(interaction):
        return f'group-fallback:{_field(_field(interaction, "reply"), "mode")}'
    return '未提供或无效'


def has_structured_group_reply(decision: Any) -> bool:
    """上游 7611-7613 ``hasStructuredGroupReply``（非导出）。"""
    return has_structured_group_reply_field(_field(decision, 'groupReply')) or has_structured_interaction(_field(decision, 'interaction'))


def has_structured_group_reply_field(value: Any) -> bool:
    """上游 7615-7618 ``hasStructuredGroupReplyField``（非导出）。"""
    if not is_record(value) or (value.get('mode') != 'none' and value.get('mode') != 'immediate'):
        return False
    return value.get('mode') == 'none' or (isinstance(value.get('content'), str) and bool(value['content'].strip()))


def has_structured_interaction(value: Any) -> bool:
    """上游 7620-7628 ``hasStructuredInteraction``（非导出）。"""
    if not is_record(value) or not isinstance(value.get('seen'), bool) or not is_record(value.get('reply')):
        return False
    reply = value['reply']
    mode = reply.get('mode')
    if mode != 'none' and mode != 'immediate' and mode != 'delayed':
        return False
    if mode == 'none':
        return True
    if not isinstance(reply.get('content'), str) or not reply['content'].strip():
        return False
    return mode == 'immediate' or (isinstance(reply.get('sendAt'), str) and bool(reply['sendAt'].strip()))


def safe_json_preview(value: Any = _UNDEFINED) -> str:
    """上游 7630-7636 ``safeJsonPreview``（非导出）：诊断日志用紧凑 JSON，不抛错、限长。"""
    try:
        text = 'undefined' if value is _UNDEFINED else _json_stringify(value)
        return _text(text)[:300]
    except Exception:  # noqa: BLE001 - 与上游 catch 一致
        return '(unserializable)'


def normalize_group_reply(raw: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 7639-7642 ``normalizeGroupReply``（非导出）。"""
    if not is_record(raw) or raw.get('mode') != 'immediate':
        return ''
    return normalize_visible_message_content(raw.get('content'), max_characters, separator)


def normalize_group_interaction_reply(raw: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 7644-7647 ``normalizeGroupInteractionReply``（非导出）。"""
    if not is_record(raw) or not is_record(raw.get('reply')) or raw['reply'].get('mode') != 'immediate':
        return ''
    return normalize_visible_message_content(raw['reply'].get('content'), max_characters, separator)


def normalize_visible_message_content(value: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
    """上游 7649-7663 ``normalizeVisibleMessageContent``（非导出）。"""
    fallback = separator.strip() if isinstance(separator, str) else ''
    text = _text(value)
    # 供应商偶尔会丢斜杠或写成全角括号。只归一化结构化可见回复；
    # 剧本散文与入站用户文本保持原样。
    text = re.sub(r'[<＜]\s*sep\s*\/?\s*[>＞]', lambda _match: fallback or '<sep/>', text, flags=re.IGNORECASE)
    # 模型可能复述入站消息里的附件标记（如 <file src="…qqdownload…">）。
    # 可见回复是纯文本合约：标记一旦漏出会被适配器解析成真实附件发出去。
    text = re.sub(r'<\/?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*\/?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]', '', text)
    text = text.strip()
    # JS: slice(0, Math.max(1, maxCharacters))；非数值参数在 slice 里按 NaN→0 处理。
    limit = max(1, int(max_characters)) if _is_number(max_characters) else 0
    return text[:limit]


# ===================== 上游 7665-7673：字面引用 =====================

def literal_quote_text(value: Any) -> str:
    """上游 7665-7668 ``literalQuoteText``（非导出）。"""
    match = re.fullmatch(r'\s*[「\[]引用[:：]\s*(.*?)\s*[」\]]\s*', _text(value))
    return match.group(1).strip() if match and match.group(1) else ''


def is_literal_quote_only(value: Any) -> bool:
    """上游 7670-7672 ``isLiteralQuoteOnly``（非导出）。"""
    return bool(literal_quote_text(value))


# ===================== 上游 7676-7709：平台/账号工具 =====================

def same_platform_family(left: Any, right: Any) -> bool:
    """上游 7676-7679 ``samePlatformFamily``（非导出）。

    把 OneBot 传输别名视为同一个管理员可见平台族。
    """
    if is_one_bot_platform(left) and is_one_bot_platform(right):
        return True
    return _text(left).strip().lower() == _text(right).strip().lower()


def normalize_account_id(value: Any) -> str:
    """上游 7682-7690 ``normalizeAccountId``（非导出）。

    归一化传输限定的 QQ id，如 private:123 或 onebot:123。
    """
    normalized = _text(value).strip().lower()
    for _ in range(3):
        next_value = re.sub(r'^(?:private|user|onebot|napcat|qq):', '', normalized, count=1, flags=re.IGNORECASE).strip()
        if next_value == normalized:
            break
        normalized = next_value
    return normalized


def signed_number(value: float) -> str:
    """上游 7692-7694 ``signedNumber``（非导出）。"""
    prefix = '+' if value > 0 else ''
    if isinstance(value, float) and not value.is_integer():
        return f'{prefix}{value:.2f}'
    return f'{prefix}{int(value)}'


def quotes_bot(session: InboundSession) -> bool:
    """上游 7696-7698 ``quotesBot``（非导出）。

    Python 会话：``quote.senderId`` 即上游 ``quote.user.id``；同时承认平台
    ``quoted_bot`` 标记。
    """
    if _field(session, 'quoted_bot'):
        return True
    quote = _field(session, 'quote')
    user = _field(quote, 'user')
    quote_user_id = _text(_first(_field(quote, 'senderId'), _field(user, 'id')))
    return quote_user_id == _text(_field(session, 'selfId'))


def is_transient_database_error(error: Any) -> bool:
    """上游 7700-7703 ``isTransientDatabaseError``（非导出）。"""
    message = str(error)
    return bool(re.search(r'disk\s*i\/o|database is locked|busy|unable to open', message, re.IGNORECASE))


def is_enabled_account(accounts: Any, qq: Any) -> bool:
    """上游 7705-7709 ``isEnabledAccount``（非导出）。"""
    normalized = normalize_account_id(qq)
    if not normalized:
        return False
    return any(
        _field(account, 'enabled') is not False and normalize_account_id(_field(account, 'qq')) == normalized
        for account in (accounts or [])
    )


# ===================== 上游 7711-7727：盲盒配置 / 叙事阶段 =====================

def has_required_narrative_script(value: Any) -> bool:
    """上游 7711-7713 ``hasRequiredNarrativeScript``。"""
    if not is_record(value):
        return False
    script = value.get('script')
    return isinstance(script, str) and len(script.strip()) > 0


def resolve_blind_mode_config(value: Any = None) -> Dict[str, Any]:
    """上游 7715-7720 ``resolveBlindModeConfig``。

    只依赖 config dict。上游对非数值输入会得到 NaN；这里与 service_base 既有回退一致，
    把不可数值化视为缺省 10（healthReportMinutes 恒为 1..1440 的整数）。
    """
    record = value if is_record(value) else {}
    number = _js_number(record.get('healthReportMinutes'))
    if number is None:
        number = 10.0
    return {
        'enabled': record.get('enabled') is True,
        'healthReportMinutes': max(1, min(1_440, math.floor(number))),
    }


# 上游 7723 ``resolveBlackBoxConfig = resolveBlindModeConfig``（@deprecated）
resolve_black_box_config = resolve_blind_mode_config


def is_automatic_narrative_phase(phase: Any) -> bool:
    """上游 7725-7727 ``isAutomaticNarrativePhase``（非导出）。"""
    return phase == 'advance' or phase == 'conversation-follow-up'



# ===================== 上游 7729-7796：跟进承诺 =====================

def normalize_automatic_delivery_summary(value: Any) -> str:
    """上游 7729-7731 ``normalizeAutomaticDeliverySummary``（非导出）。"""
    return clip(value, 240).strip() if isinstance(value, str) else ''


def normalize_follow_up_summary(value: Any) -> str:
    """上游 7733-7735 ``normalizeFollowUpSummary``（非导出）。"""
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', clip(value, 360).strip()).lower()


def follow_up_expires_at(value: Any, now: datetime) -> datetime:
    """上游 7737-7741 ``followUpExpiresAt``（非导出）。"""
    requested = to_date(value)
    maximum = now + timedelta(hours=24)
    if not requested or requested <= now:
        return maximum
    return requested if requested < maximum else maximum


def normalize_follow_up_commitment(value: Any, now: datetime) -> Optional[Dict[str, Any]]:
    """上游 7744-7761 ``normalizeFollowUpCommitment``（非导出）。"""
    if not is_record(value):
        return None
    kind = value.get('kind') if value.get('kind') in (
        'thinking', 'checking', 'decision', 'emotional-settle') else None
    summary = clip(value.get('summary'), 360).strip() if isinstance(value.get('summary'), str) else ''
    not_before = to_date(value.get('notBefore'))
    if not kind or not summary or not not_before:
        return None
    delay = _diff_ms(not_before, now)
    if delay is None or delay < 5 * 60 * 1_000 or delay > 12 * 60 * 60 * 1_000:
        return None
    source_entry_ids: List[int] = []
    if isinstance(value.get('sourceEntryIds'), list):
        for item in value['sourceEntryIds']:
            if _is_safe_integer(item) and item > 0:
                source_entry_ids.append(int(item))
        source_entry_ids = source_entry_ids[:4]
    expires_at = to_date(value.get('expiresAt'))
    result: Dict[str, Any] = {'kind': kind, 'summary': summary, 'notBefore': iso(not_before)}
    if expires_at and expires_at > not_before:
        result['expiresAt'] = iso(expires_at)
    if source_entry_ids:
        result['sourceEntryIds'] = source_entry_ids
    return result


def inferred_follow_up_commitment(content: str, now: datetime) -> Dict[str, Any]:
    """上游 7763-7768 ``inferredFollowUpCommitment``（非导出）。"""
    return {
        'kind': 'thinking',
        'summary': clip(f'The character promised to return after thinking: {content}', 360),
        'notBefore': iso(now + timedelta(minutes=20)),
    }


def interaction_promises_follow_up(content: Any) -> bool:
    """上游 7770-7773 ``interactionPromisesFollowUp``（非导出）。"""
    if not isinstance(content, str):
        return False
    return bool(re.search(
        r'我(?:先)?想想|我去(?:想想|看看|查查|确认)|晚点(?:回|说|告诉)|之后(?:回|说|告诉)|等我.{0,12}(?:回|说|告诉)|整理.{0,12}(?:回|说|告诉)',
        content,
    ))


def normalize_follow_up_resolutions(value: Any) -> List[Dict[str, Any]]:
    """上游 7775-7787 ``normalizeFollowUpResolutions``（非导出）。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        item_id = item.get('id')
        outcome = item.get('outcome')
        if not _is_number(item_id) or not float(item_id).is_integer() or item_id <= 0:
            continue
        if outcome != 'fulfilled' and outcome != 'rescheduled' and outcome != 'cancelled':
            continue
        entry: Dict[str, Any] = {'id': int(item_id), 'outcome': outcome}
        if isinstance(item.get('notBefore'), str):
            entry['notBefore'] = item['notBefore']
        result.append(entry)
    return result[:2]


def automatic_delivery_from_payload(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7789-7796 ``automaticDeliveryFromPayload``（非导出）。"""
    record = _field(value, 'automaticDelivery') if is_record(value) and is_record(_field(value, 'automaticDelivery')) else None
    summary = normalize_automatic_delivery_summary(_field(record, 'summary'))
    source_entry_id = _safe_int_or_none(_field(record, 'sourceEntryId'))
    if not summary:
        return None
    result: Dict[str, Any] = {'summary': summary}
    if source_entry_id:
        result['sourceEntryId'] = source_entry_id
    return result


def merge_delivery_summary(left: str, right: str) -> str:
    """上游 7798-7802 ``mergeDeliverySummary``（非导出）。"""
    if not left or left == right or right in left:
        return left or right
    if left in right:
        return right
    return clip(f'{left}；{right}', 240)


# ===================== 上游 7807-7833：场景在场草稿 =====================

def normalize_scene_presence_drafts(value: Any, entries: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """上游 7807-7826 ``normalizeScenePresenceDrafts``。

    场景压缩只有在明确的观察证据下才能更新小名单；让被点名配角可用，
    又不必把他们当成自动在场。
    """
    if now is None:
        now = now_utc()
    if not isinstance(value, list):
        return []
    by_id: Dict[Any, Dict[str, Any]] = {}
    for entry in entries or []:
        if is_record(entry) and entry.get('id') is not None:
            by_id[entry['id']] = entry
    next_items: List[Dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        name = clip(item.get('name'), 80).strip() if isinstance(item.get('name'), str) else ''
        status = item.get('status') if item.get('status') in ('present', 'off-scene', 'expected') else None
        basis = clip(item.get('basis'), 300).strip() if isinstance(item.get('basis'), str) else ''
        source_entry_ids: List[Any] = []
        if isinstance(item.get('sourceEntryIds'), list):
            for identifier in item['sourceEntryIds']:
                if _is_number(identifier) and identifier in by_id:
                    source_entry_ids.append(identifier)
            source_entry_ids = source_entry_ids[:8]
        evidence = [by_id[identifier] for identifier in source_entry_ids
                    if name in _text(_field(by_id[identifier], 'content'))]
        if not name or not status or not basis or not evidence or not has_explicit_presence_evidence(status, evidence):
            continue
        next_items.append({
            'name': name, 'status': status, 'basis': basis,
            'sourceEntryIds': source_entry_ids, 'updatedAt': iso(now),
        })
    return normalize_scene_presence_state(next_items)


def has_explicit_presence_evidence(status: Any, entries: List[Dict[str, Any]]) -> bool:
    """上游 7828-7833 ``hasExplicitPresenceEvidence``（非导出）。"""
    text = '\n'.join(_text(_field(entry, 'content')) for entry in entries)
    if status == 'off-scene':
        return bool(re.search(r'告别|道别|分别|先走|离开|离去|回家|回去了|独自|分开|告辞', text))
    if status == 'expected':
        return bool(re.search(r'约好|约在|等会|稍后|会来|准备来|约见', text))
    return bool(re.search(r'一起|同行|身边|来到|抵达|进入|走进|拉着|坐在|站在|陪着', text))


# ===================== 上游 7835-7879：决策归一化 =====================

def normalize_decision(raw: Any, from_: datetime, now: datetime, permit_messages: bool, runtime: Any,
                       shared: Any, current_participant_id: str, permitted_participant_ids: Any,
                       phase: Any = 'advance', memory: Any = None, refresh_continuity: bool = False) -> Dict[str, Any]:
    """上游 7835-7879 ``normalizeDecision``（非导出）。"""
    separator = _field(runtime, 'messageSeparator')
    raw = resolve_authored_actions(raw, False, separator) if isinstance(raw, dict) else raw
    record = raw if is_record(raw) else {}

    script = ''
    script_raw = record.get('script')
    script_limit = _field(runtime, 'maxScriptCharacters')
    if isinstance(script_raw, str):
        if script_limit is None:
            # JS slice(0, undefined) 等价于 slice(0)：保留全文。
            script = script_raw.strip()
        elif _is_number(script_limit):
            script = script_raw.strip()[:max(0, int(script_limit))]
        # 其余（NaN）等价 slice(0, NaN) → ''
    # 私聊回复的唯一通道是 interaction。自动生活回合没有实时参与者事件，
    # 因此根本不能发出它。
    interaction = None if phase == 'advance' else normalize_interaction(record.get('interaction'), now, runtime)
    # 可选记忆是模型建议而非事实来源。绝不能让模型把编造的线上联系人变成
    # 持久检索数据。
    memories: List[Dict[str, Any]] = []
    if isinstance(record.get('memories'), list):
        for item in record['memories']:
            if valid_memory(item):
                memories.append({
                    **item,
                    'participantId': permitted_or_global(item.get('participantId'), current_participant_id, permitted_participant_ids),
                })
    intents: List[Dict[str, Any]] = []
    if isinstance(record.get('intents'), list):
        for intent in record['intents']:
            if is_record(intent) and intent.get('type') == 'follow-up-commitment':
                continue
            if not valid_intent(intent, from_, now, memory):
                continue
            intents.append({
                **intent,
                'participantId': permitted_or_global(intent.get('participantId'), current_participant_id, permitted_participant_ids),
            })
        intents = intents[:8]
    intent_updates = normalize_intent_updates(record.get('intentUpdates'))
    browser_intents: List[Dict[str, Any]] = []
    if isinstance(record.get('browserIntents'), list):
        for item in record['browserIntents']:
            normalized_intent = normalize_browser_intent_draft_loose(item)
            if normalized_intent:
                browser_intents.append(normalized_intent)
            if len(browser_intents) >= 1:
                break
    proactive = phase == 'advance'
    agency_gated_proactive = proactive and not is_record(record.get('proactiveContact'))
    cross_conversation_actions: List[Dict[str, Any]] = []
    if permit_messages and _field(shared, 'allowCrossConversationMessages') and isinstance(record.get('crossConversationActions'), list):
        max_actions = _field(shared, 'maxCrossConversationActions')
        max_actions = int(max_actions) if _is_number(max_actions) else 0
        for action in record['crossConversationActions']:
            normalized_action = normalize_conversation_action(
                action, runtime, permitted_participant_ids, current_participant_id, now, agency_gated_proactive)
            if normalized_action:
                cross_conversation_actions.append(normalized_action)
        cross_conversation_actions = cross_conversation_actions[:max(0, max_actions)]
    state_patch = pick_participant_state_patch(record['statePatch']) if is_record(record.get('statePatch')) else None
    continuity = normalize_continuity_snapshot(record.get('continuity')) if refresh_continuity else None
    alter = normalize_alter_value(record.get('alter'))
    automatic_delivery_summary = (
        normalize_automatic_delivery_summary(record.get('automaticDeliverySummary')) or None
    ) if is_automatic_narrative_phase(phase) else None
    follow_up_commitment = normalize_follow_up_commitment(record.get('followUpCommitment'), now) if phase == 'user-message' else None
    follow_up_resolutions = (
        normalize_follow_up_resolutions(record.get('followUpResolutions'))
        if phase == 'user-message' or phase == 'intent-due' else []
    )
    agency_window = record.get('agencyWindow') if is_record(record.get('agencyWindow')) else None
    proactive_contact = record.get('proactiveContact') if is_record(record.get('proactiveContact')) else None
    return {
        'script': script,
        'authoredActions': record.get('authoredActions'),
        'lifeHandoff': normalize_life_handoff(record.get('lifeHandoff'), script),
        'alter': alter,
        'agencyWindow': agency_window,
        'proactiveContact': proactive_contact,
        'interaction': interaction,
        'automaticDeliverySummary': automatic_delivery_summary,
        'followUpCommitment': follow_up_commitment,
        'followUpResolutions': follow_up_resolutions,
        'continuity': continuity,
        'memories': memories,
        'intents': intents,
        'intentUpdates': intent_updates,
        'browserIntents': browser_intents,
        'statePatch': state_patch,
        'crossConversationActions': cross_conversation_actions,
    }


# ===================== 上游 7881-7955：浏览器意图 / 公共网页安全 =====================

def normalize_browser_intent_draft_loose(value: Any) -> Optional[Dict[str, Any]]:
    """上游 7881-7894 ``normalizeBrowserIntentDraftLoose``（非导出）。"""
    if not is_record(value) or (value.get('mode') != 'search' and value.get('mode') != 'visit'):
        return None
    if not isinstance(value.get('purpose'), str):
        return None
    query = clip(value.get('query'), 500) if isinstance(value.get('query'), str) else ''
    url = clip(value.get('url'), 2_000) if isinstance(value.get('url'), str) else ''
    if value['mode'] == 'search' and not query:
        return None
    if value['mode'] == 'visit' and not url:
        return None
    result: Dict[str, Any] = {'mode': value['mode']}
    if query:
        result['query'] = query
    if url:
        result['url'] = url
    result['purpose'] = clip(value.get('purpose'), 500)
    result['timing'] = 'immediate' if value.get('timing') == 'immediate' else 'deferred'
    if isinstance(value.get('participantId'), str):
        result['participantId'] = value['participantId'].strip()
    return result


def normalize_browser_intent_draft(draft: Any, config: Any) -> Optional[Dict[str, Any]]:
    """上游 7896-7902 ``normalizeBrowserIntentDraft``（非导出）。"""
    normalized = normalize_browser_intent_draft_loose(draft)
    if not normalized:
        return None
    if normalized['mode'] == 'search' and not _field(config, 'allowSearch'):
        return None
    if normalized['mode'] == 'visit' and not _field(config, 'allowVisit'):
        return None
    return normalized


def browser_intent_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """上游 7904-7912 ``browserIntentFromPayload``（非导出）。"""
    return normalize_browser_intent_draft_loose({
        'mode': _field(payload, 'mode'),
        'query': _field(payload, 'query'),
        'url': _field(payload, 'url'),
        'purpose': _field(payload, 'purpose') or 'The character planned to read a public web page.',
        'timing': 'deferred',
    })


def resolve_browser_target(draft: Any, config: Any) -> Optional[str]:
    """上游 7914-7922 ``resolveBrowserTarget``（非导出）。"""
    if not is_record(draft):
        return None
    if draft.get('mode') == 'search':
        template = _field(config, 'searchUrlTemplate')
        template = template.strip() if isinstance(template, str) else ''
        if not template or '{query}' not in template:
            return None
        target = template.replace('{query}', urllib.parse.quote(_text(draft.get('query')), safe="!'()*-._~"))
        return target if is_safe_public_web_url(target, config) else None
    url = draft.get('url')
    return url if isinstance(url, str) and url and is_safe_public_web_url(url, config) else None


def is_safe_public_web_url(value: Any, config: Any) -> bool:
    """上游 7924-7939 ``isSafePublicWebUrl``（非导出）。"""
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme != 'https' and parsed.scheme != 'http':
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or '').lower()
    if host.endswith('.'):
        host = host[:-1]
    if not host or host == 'localhost' or host.endswith('.localhost') or host == '::1':
        return False
    if is_private_host(host):
        return False
    blocked = normalize_domains(_field(config, 'blockedDomains'))
    allowed = normalize_domains(_field(config, 'allowedDomains'))
    if any(domain_matches(host, domain) for domain in blocked):
        return False
    return not allowed or any(domain_matches(host, domain) for domain in allowed)


def normalize_domains(values: Any) -> List[str]:
    """上游 7941-7943 ``normalizeDomains``（非导出）。"""
    if not isinstance(values, list):
        return []
    result: List[str] = []
    for value in values:
        text = re.sub(r'^\.+|\.+$', '', _text(value).strip().lower())
        if text:
            result.append(text)
    return result


def domain_matches(host: str, domain: str) -> bool:
    """上游 7945 ``domainMatches``（非导出）。"""
    return host == domain or host.endswith(f'.{domain}')


def is_private_host(host: str) -> bool:
    """上游 7947-7955 ``isPrivateHost``（非导出）。"""
    if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', host):
        parts = [int(part) for part in host.split('.')]
        a, b = parts[0], parts[1]
        return (a == 10 or a == 127 or a == 0 or (a == 169 and b == 254)
                or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168))
    # 字面 IPv6 与 IPv4-mapped 地址对公开网页叙事没有需要，按本地/私有目的地处理最安全。
    return ':' in host


def web_observation_entry_content(observation: Any) -> str:
    """上游 7957-7965 ``webObservationEntryContent``（非导出）。"""
    if _field(observation, 'status') == 'success':
        source = _field(observation, 'title') or _field(observation, 'url') or 'a public web page'
        # 完整有界摘录通过 webContext 提供；不放进普通剧本流可避免重复 token，
        # 也防止页面文本被误当成第一方叙事指令。
        return f'The character read a public web page: {source}.'
    return f"The character's attempted web lookup did not complete: {clip(_field(observation, 'summary'), 800)}"


# ===================== 上游 7968-7984：interaction 归一化 =====================

def normalize_interaction(value: Any, now: datetime, runtime: Any) -> Optional[Dict[str, Any]]:
    """上游 7968-7984 ``normalizeInteraction``。"""
    if not is_record(value) or not isinstance(value.get('seen'), bool) or not is_record(value.get('reply')):
        return None
    reply = value['reply']
    mode = reply.get('mode')
    if mode != 'none' and mode != 'immediate' and mode != 'delayed':
        return None
    content = (normalize_visible_message_content(reply.get('content'), _field(runtime, 'maxMessageCharacters'),
                                                 _field(runtime, 'messageSeparator'))
               if isinstance(reply.get('content'), str) else None)
    send_at = to_date(reply.get('sendAt'))
    # seen 只描述是否读了新消息；reply 是独立的发送通道。跟进/到期回合协议规定
    # seen=false，若在此处因 seen 抹掉回复，「稍后读到再回」的自救路径会被无声斩断
    # （模型写进了剧本的发送与投递现实分裂）。
    seen = value.get('seen') is True
    if mode == 'none':
        return {'seen': seen, 'reply': {'mode': 'none'}}
    if not content:
        return {'seen': seen, 'reply': {'mode': 'none'}}
    if mode == 'immediate':
        return {'seen': seen, 'reply': {'mode': mode, 'content': content}}
    minimum_seconds = _field(runtime, 'minimumDelayedReplySeconds')
    maximum_minutes = _field(runtime, 'maximumDelayedReplyMinutes')
    delay = _diff_ms(send_at, now)
    if (not send_at or delay is None
            or delay < _js_number_or_zero(minimum_seconds) * 1_000
            or delay > _js_number_or_zero(maximum_minutes) * 60_000):
        return {'seen': seen, 'reply': {'mode': 'none'}}
    return {'seen': seen, 'reply': {'mode': mode, 'content': content, 'sendAt': iso(send_at)}}


# ===================== 上游 7986-8020：记忆/意图校验 =====================

def valid_memory(value: Any) -> bool:
    """上游 7986-7988 ``validMemory``（非导出）。"""
    return (is_record(value) and isinstance(value.get('category'), str)
            and isinstance(value.get('content'), str) and bool(value['content'].strip()))


def valid_intent(value: Any, from_: datetime, now: datetime, memory: Any = None) -> bool:
    """上游 7990-8007 ``validIntent``（非导出）。"""
    if not is_record(value) or not isinstance(value.get('type'), str) or not isinstance(value.get('summary'), str):
        return False
    not_before = to_date(value.get('notBefore'))
    if not not_before:
        return False
    if not is_active_consequence_draft(value):
        return not_before > now
    expires_at = consequence_expires_at(value.get('payload'))
    payload = value.get('payload')
    effect = payload.get('effect').strip() if is_record(payload) and isinstance(payload.get('effect'), str) else ''
    strength = payload.get('strength') if is_record(payload) else None
    # 后果是刻意的中短期故事压力，不是永久改写正典的后门。把来源时间限制在
    # 本次写作回合附近，并把自然寿命封顶在 30 天（memory?.activeConsequenceMaxDays ?? 7）。
    days_raw = _field(memory, 'activeConsequenceMaxDays')
    days = 7.0 if days_raw is None else _js_number(days_raw)
    if days is None:  # NaN：上游 max(1, NaN) 仍是 NaN，所有寿命比较均不成立
        return False
    maximum_lifetime = max(1, days) * 24 * 60 * 60 * 1_000
    if not _field(memory, 'activeConsequencesEnabled') or not effect:
        return False
    if strength is not None and not (_is_number(strength) and math.isfinite(float(strength)) and 0 <= strength <= 1):
        return False
    if not (not_before <= now and not_before >= from_):
        return False
    if not expires_at or expires_at <= now:
        return False
    lifetime = _diff_ms(expires_at, now)
    return lifetime is not None and lifetime <= maximum_lifetime


def normalize_intent_updates(value: Any) -> List[Dict[str, Any]]:
    """上游 8011-8020 ``normalizeIntentUpdates``（非导出）。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        item_id = item.get('id')
        if not _is_number(item_id) or not float(item_id).is_integer() or not item_id > 0:
            continue
        if item.get('status') != 'completed' and item.get('status') != 'cancelled':
            continue
        entry: Dict[str, Any] = {'id': int(item_id), 'status': item.get('status')}
        if isinstance(item.get('resolution'), str) and item['resolution'].strip():
            entry['resolution'] = clip(item['resolution'], 1_000)
        result.append(entry)
    return result[:8]


# ===================== 上游 8022-8043：活跃后果 =====================

def is_active_consequence(intent: Any) -> bool:
    """上游 8022-8024 ``isActiveConsequence``（非导出）。"""
    return (_field(intent, 'type') == 'active-consequence' and is_record(_field(intent, 'payload'))
            and _field(_field(intent, 'payload'), 'lifecycle') == 'active')


def is_active_consequence_draft(intent: Any) -> bool:
    """上游 8026-8028 ``isActiveConsequenceDraft``（非导出）。"""
    return (_field(intent, 'type') == 'active-consequence' and is_record(_field(intent, 'payload'))
            and _field(_field(intent, 'payload'), 'lifecycle') == 'active')


def consequence_expires_at(payload: Any) -> Optional[datetime]:
    """上游 8030-8033 ``consequenceExpiresAt``（非导出）。"""
    if not is_record(payload):
        return None
    return to_date(payload.get('expiresAt'))


def consequence_strength(payload: Any, fallback: float = 0.55) -> float:
    """上游 8035-8037 ``consequenceStrength``（非导出）。"""
    return clamp_number(payload.get('strength') if is_record(payload) else None, fallback, 0, 1)


def has_compaction_evidence(source_entry_ids: Any, entries: List[Dict[str, Any]]) -> bool:
    """上游 8039-8043 ``hasCompactionEvidence``（非导出）。"""
    if not isinstance(source_entry_ids, list) or len(source_entry_ids) == 0:
        return False
    ids = {_field(entry, 'id') for entry in (entries or [])}
    return any(identifier in ids for identifier in source_entry_ids)


# ===================== 上游 8045-8101：会话动作 / 参与者状态 =====================

def normalize_conversation_action(value: Any, runtime: Any, permitted_participant_ids: Any,
                                  current_participant_id: str, now: Optional[datetime] = None,
                                  proactive: bool = False) -> Optional[Dict[str, Any]]:
    """上游 8045-8063 ``normalizeConversationAction``（非导出）。"""
    if now is None:
        now = now_utc()
    if (not is_record(value) or not isinstance(value.get('participantId'), str) or not value.get('participantId')
            or value.get('participantId') == current_participant_id):
        return None
    if value.get('participantId') not in permitted_participant_ids:
        return None
    if value.get('mode') != 'immediate' and value.get('mode') != 'delayed':
        return None
    # 主动联系与私聊回复共用同一可见文本合约：括号表情标签等不得漏出到投递。
    content = (normalize_visible_message_content(value.get('content'), _field(runtime, 'maxMessageCharacters'),
                                                 _field(runtime, 'messageSeparator'))
               if isinstance(value.get('content'), str) else '')
    if not content:
        return None
    willingness = (clamp_number(value.get('willingness'), 0, 0, 1)
                   if _is_number(value.get('willingness')) and math.isfinite(float(value['willingness'])) else None)
    threshold = _field(runtime, 'proactiveWillingnessThreshold')
    if threshold is None:
        threshold = 0.65
    if proactive and (willingness is None or willingness < threshold):
        return None
    reason = clip(value.get('reason'), 300) if isinstance(value.get('reason'), str) else None
    if value.get('mode') == 'immediate':
        result: Dict[str, Any] = {'participantId': value['participantId'], 'mode': value['mode'], 'content': content}
        if willingness is not None:
            result['willingness'] = willingness
        if reason:
            result['reason'] = reason
        return result
    send_at = to_date(value.get('sendAt'))
    delay = _diff_ms(send_at, now)
    if (not send_at or delay is None
            or delay < _js_number_or_zero(_field(runtime, 'minimumDelayedReplySeconds')) * 1_000
            or delay > _js_number_or_zero(_field(runtime, 'maximumDelayedReplyMinutes')) * 60_000):
        return None
    result = {'participantId': value['participantId'], 'mode': value['mode'], 'content': content, 'sendAt': iso(send_at)}
    if willingness is not None:
        result['willingness'] = willingness
    if reason:
        result['reason'] = reason
    return result


def permitted_or_global(value: Any, fallback: str, permitted_participant_ids: Any) -> str:
    """上游 8065-8069 ``permittedOrGlobal``（非导出）。"""
    candidate = value.strip() if isinstance(value, str) else ''
    if candidate and candidate in permitted_participant_ids:
        return candidate
    return fallback if fallback and fallback in permitted_participant_ids else ''


def pick_participant_state_patch(value: Dict[str, Any]) -> Dict[str, Any]:
    """上游 8071-8076 ``pickParticipantStatePatch``（非导出）。"""
    patch: Dict[str, Any] = {}
    if isinstance(value.get('openThreads'), list) and all(isinstance(item, str) for item in value['openThreads']):
        patch['openThreads'] = [clip(item, 500) for item in value['openThreads']][:50]
    if isinstance(value.get('relationshipNotes'), list) and all(isinstance(item, str) for item in value['relationshipNotes']):
        patch['relationshipNotes'] = [clip(item, 500) for item in value['relationshipNotes']][:50]
    return patch


def merge_setting(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """上游 8078-8080 ``mergeSetting``（非导出）。"""
    patch = patch or {}
    return {
        **base, **patch,
        'character': {**(base.get('character') or {}), **(patch.get('character') or {})},
        'user': {**(base.get('user') or {}), **(patch.get('user') or {})},
    }


def merge_participant_state(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """上游 8082-8088 ``mergeParticipantState``（非导出）。"""
    patch = patch or {}
    merged = {**(base or {}), **patch}
    merged['openThreads'] = patch['openThreads'] if isinstance(patch.get('openThreads'), list) else (base or {}).get('openThreads')
    merged['relationshipNotes'] = (patch['relationshipNotes'] if isinstance(patch.get('relationshipNotes'), list)
                                   else (base or {}).get('relationshipNotes'))
    return merged


def normalize_participant_state(value: Any) -> Dict[str, Any]:
    """上游 8090-8101 ``normalizeParticipantState``（非导出）。

    与 hdsi/story_state.py 的同名移植保持一致：上游为 undefined 的可选字段不落 key
    （项目约定允许「缺省 key」表达可选值），其余字段与截断长度逐字一致。
    """
    record = value if is_record(value) else {}

    def note_list(item: Any) -> List[str]:
        if not isinstance(item, list):
            return []
        return [clip(raw, 500) for raw in item if isinstance(raw, str)][:50]

    state: Dict[str, Any] = {
        'openThreads': note_list(record.get('openThreads')),
        'relationshipNotes': note_list(record.get('relationshipNotes')),
        'unreadMessageCount': max(0, math.floor(record.get('unreadMessageCount') if _is_number(record.get('unreadMessageCount')) else 0)),
        'pendingReplyCount': max(0, math.floor(record.get('pendingReplyCount') if _is_number(record.get('pendingReplyCount')) else 0)),
    }
    if isinstance(record.get('relationshipOverlay'), str):
        state['relationshipOverlay'] = clip(record.get('relationshipOverlay'), 4_000)
    if isinstance(record.get('lastUserMessageAt'), str):
        state['lastUserMessageAt'] = record.get('lastUserMessageAt')
    if isinstance(record.get('lastCharacterMessageAt'), str):
        state['lastCharacterMessageAt'] = record.get('lastCharacterMessageAt')
    return state


def participant_relevance(participant: Dict[str, Any]) -> float:
    """上游 8103-8107 ``participantRelevance``（非导出）。"""
    state = normalize_participant_state(_field(participant, 'state'))
    pending = state.get('pendingReplyCount', 0) * 2 + state.get('unreadMessageCount', 0)
    last = _time_ms(state.get('lastUserMessageAt'))
    if last is None:
        last = _time_ms(_field(participant, 'updatedAt'))
    if last is None:
        last = 0.0
    return pending * 1_000_000_000 + last


# ===================== 上游 8109-8129：到期意图批 / 参与者解析 =====================

def group_due_intents(intents: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """上游 8113-8123 ``groupDueIntents``。

    让单个到期回合只属于一段关系，同时保证扫描开始时每个已到期计划都有机会
    在下个扫描间隔前被判定。
    """
    batches: Dict[str, List[Dict[str, Any]]] = {}

    def sort_key(intent: Any) -> Tuple[float, float]:
        return (_time_ms_or_zero(_field(intent, 'notBefore')), _js_number_or_zero(_field(intent, 'id')))

    for intent in sorted(intents or [], key=sort_key):
        family = 'agency' if _field(intent, 'type') == 'proactive-check' else 'normal'
        key = f'{_field(intent, "participantId") or "__global__"}|{family}'
        batches.setdefault(key, []).append(intent)
    return list(batches.values())


def resolve_participant_id(explicit: Any, source_entry_ids: Any, entries: List[Dict[str, Any]]) -> str:
    """上游 8125-8129 ``resolveParticipantId``（非导出）。"""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    ids: List[str] = []
    for identifier in (source_entry_ids or []):
        for entry in (entries or []):
            if _field(entry, 'id') == identifier:
                participant_id = _field(entry, 'participantId')
                if participant_id:
                    ids.append(participant_id)
                break
    return ids[0] if ids else ''


# ===================== 上游 8133-8195：请求替换 / 数据库行 / 时间 =====================

def should_supersede_narrative_request(in_flight_request_id: Any, first_message_committed_request_id: Any,
                                       obsolete_request_ids: Any) -> bool:
    """上游 8133-8141 ``shouldSupersedeNarrativeRequest``。"""
    return bool(
        in_flight_request_id
        and first_message_committed_request_id != in_flight_request_id
        and in_flight_request_id not in obsolete_request_ids
    )


def to_date(value: Any) -> Optional[datetime]:
    """上游 8143-8148 ``toDate``（非导出）；复用 hdsi/time_utils.parse_time。"""
    return parse_time(value)


# 上游 8150-8167 DATABASE_DATE_FIELDS
# 与 hdsi/database.py 的同名表一致；normalizeDatabaseRow 本体由 hdsi/store.py 提供。
DATABASE_DATE_FIELDS: Dict[str, List[str]] = {
    'interlude_story': ['cursorAt', 'createdAt', 'updatedAt'],
    'interlude_participant': ['createdAt', 'updatedAt'],
    'interlude_script_entry': ['occurredAt', 'createdAt'],
    'interlude_memory': ['createdAt', 'updatedAt'],
    'interlude_intent': ['notBefore', 'createdAt', 'updatedAt'],
    'interlude_scene': ['startedAt', 'endedAt', 'createdAt', 'updatedAt'],
    'interlude_arc': ['createdAt', 'updatedAt'],
    'interlude_fact': ['lastSeenAt', 'createdAt', 'updatedAt'],
    'interlude_state_patch': ['createdAt', 'appliedAt'],
    'interlude_overlay_snapshot': ['periodStart', 'periodEnd', 'createdAt', 'updatedAt'],
    'interlude_sticker': ['createdAt', 'updatedAt'],
    'interlude_web_observation': ['accessedAt', 'createdAt'],
    'interlude_schedule_preplan': ['createdAt', 'updatedAt'],
}

# 上游 8169-8189 normalizeDatabaseRow：本项目已由 hdsi/store.py 实现（含同样的
# DATABASE_DATE_FIELDS 与 story.state / participant.state 归一化），这里只转发，
# 不再重复实现。名字保持上游同名 snake_case：normalize_database_row（文件顶部 import）。


def same_timestamp(left: Any, right: Any) -> bool:
    """上游 8191-8195 ``sameTimestamp``（非导出）。

    与 hdsi/time_utils.same_timestamp 语义一致（容差 2000ms），这里按上游
    toDate + 毫秒差值逐字实现。
    """
    a = to_date(left)
    b = to_date(right)
    if not a or not b:
        return False
    return abs((a - b).total_seconds() * 1000.0) < 2_000


def narrative_cursor(story: Dict[str, Any], now: datetime) -> datetime:
    """上游 8200-8203 ``narrativeCursor``（非导出）。

    损坏/未来的 cursor 绝不能让叙事者「倒着补时间」。只钳制 prompt 区间；
    正常成功持久化仍把存储 cursor 推进到实际墙钟时间。
    """
    cursor = to_date(_field(story, 'cursorAt')) or now
    return now if cursor > now else cursor


# ===================== 上游 8205-8226：文本/事实工具 =====================

def normalize_fact(value: str) -> str:
    """上游 8212 ``normalizeFact``（非导出）。"""
    return re.sub(r'\s+', ' ', _text(value).strip().lower())


def limit_entries_by_characters(entries: List[Dict[str, Any]], limit: Any) -> List[Dict[str, Any]]:
    """上游 8214-8226 ``limitEntriesByCharacters``（非导出）。"""
    if not _is_number(limit):
        # JS 中 undefined/NaN 与数字比较恒为 false：不触发 break，全部保留。
        return list(entries or [])
    if limit <= 0:
        return []
    used = 0
    selected: List[Dict[str, Any]] = []
    # 从最新条目向前保留，保证压缩请求优先看到场景接续点。
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if selected and used + len(_text(entry.get('content'))) > limit:
            break
        selected.insert(0, entry)
        used += len(_text(entry.get('content')))
    return selected


# ===================== 上游 8228-8304：事实打分 / 词法 / 贴纸排名 / 缩图 =====================

def fact_score(fact: Dict[str, Any], config: Any, query_embedding: Optional[List[float]] = None,
               query: str = '') -> float:
    """上游 8228-8245 ``factScore``（非导出）。"""
    if query_embedding is None:
        query_embedding = []
    age_days = max(0.0, (now_utc().timestamp() * 1000.0 - _time_ms_or_zero(_field(fact, 'lastSeenAt'))) / (24 * 60 * 60 * 1_000))
    recency = math.exp(-age_days / 30)
    similarity = cosine_similarity(query_embedding, _field(fact, 'embedding') or [])
    # 负相似度视为无语义支持。这防止一个不相关事实仅因余弦在数学上覆盖
    # -1..1 就拿到半分。
    semantic = 0.0 if similarity is None else max(0.0, similarity)
    lexical = history_lexical_score(query, _field(fact, 'content') or '')
    return (
        _js_number_or_zero(_field(fact, 'importance')) * _js_number_or_zero(_field(config, 'factImportanceWeight'))
        + _js_number_or_zero(_field(fact, 'confidence')) * _js_number_or_zero(_field(config, 'factConfidenceWeight'))
        + recency * _js_number_or_zero(_field(config, 'factRecencyWeight'))
        + semantic * _js_number_or_zero(_field(config, 'semanticWeight'))
        + lexical * max(1.0, _js_number_or_zero(_field(config, 'semanticWeight')))
        + (1 if _field(fact, 'scope') == 'promise' and _field(fact, 'unresolved') else 0)
        * _js_number_or_zero(_field(config, 'unresolvedWeight'))
    )


def history_lexical_score(query: str, content: str) -> float:
    """上游 8247-8256 ``historyLexicalScore``。

    原始剧本与持久事实共用的字面召回通道。中文 bigram 保留有用的姓名与物件，
    无需分词。
    """
    query_keys = lexical_recall_keys(query)
    if not query_keys:
        return 0
    content_keys = set(lexical_recall_keys(content))
    overlap = len([key for key in query_keys if key in content_keys])
    normalized_query = _strip_punct_symbol_space(_text(query).lower())
    normalized_content = _strip_punct_symbol_space(_text(content).lower())
    phrase = 0.5 if len(normalized_query) >= 3 and normalized_query in normalized_content else 0
    return min(1, overlap / len(query_keys) + phrase)


def lexical_recall_keys(text: str) -> List[str]:
    """上游 8258-8267 ``lexicalRecallKeys``（非导出）。"""
    normalized = _text(text).lower()
    words = re.findall(r'[a-z0-9]{3,}', normalized)
    chinese_runs = re.findall(r'[\u3400-\u9fff]{2,}', normalized)
    bigrams: List[str] = []
    for run in chinese_runs:
        bigrams.extend(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return list(dict.fromkeys([*words, *bigrams]))[:80]


def cosine_similarity(left: Any, right: Any) -> Optional[float]:
    """上游 8269-8282 ``cosineSimilarity``（非导出）。"""
    left_values = left if isinstance(left, list) else []
    right_values = right if isinstance(right, list) else []
    if not left_values or len(left_values) != len(right_values):
        return None
    dot = 0.0
    left_magnitude = 0.0
    right_magnitude = 0.0
    for index in range(len(left_values)):
        left_value = _js_number_or_zero(left_values[index])
        right_value = _js_number_or_zero(right_values[index])
        dot += left_value * right_value
        left_magnitude += left_value * left_value
        right_magnitude += right_value * right_value
    if not left_magnitude or not right_magnitude:
        return None
    return dot / math.sqrt(left_magnitude * right_magnitude)


# 上游 8284 SEMANTIC_STICKER_LIMIT（一个语义过滤回合注入多少贴纸描述）
SEMANTIC_STICKER_LIMIT = 12


def rank_sticker_catalog(assets: List[Any], query_embedding: List[float], limit: int) -> List[Any]:
    """上游 8289-8298 ``rankStickerCatalog``。

    语义贴纸过滤使用的纯排名。没有向量的资产仍能在已嵌入的资产之后填充剩余槽位，
    让半索引的库优雅降级而不是隐藏条目。
    """
    limit_number = _js_number(limit)
    if not query_embedding or (limit_number is not None and len(assets) <= limit_number):
        return assets
    if limit_number is None:
        # 上游 slice(0, Math.max(1, undefined)) → slice(0, NaN) → 空数组。
        return []
    ranked = [
        {'asset': asset, 'score': cosine_similarity(query_embedding, _field(asset, 'embedding') or [])}
        for asset in assets
    ]
    ranked.sort(key=lambda item: -1.0 if item['score'] is None else item['score'], reverse=True)
    return [item['asset'] for item in ranked[:max(1, int(limit_number))]]


def should_downscale_image(mime_type: str, data_uri: str) -> bool:
    """上游 8300-8303 ``shouldDownscaleImage``。

    只有值得重渲染的静态位图才进入 Puppeteer 缩图：小图不会再缩小，
    动图有自己的路径。
    """
    if not re.match(r'^image\/(?:jpeg|png|webp)$', _text(mime_type), re.IGNORECASE):
        return False
    parts = _text(data_uri).split(',')
    second = parts[1] if len(parts) > 1 else ''
    binary_length = math.floor(len(second) * 3 / 4)
    return binary_length >= 150 * 1024



# ===================== 上游 8308-8364：事实查询 / 缓冲消息 / 自动间隔 =====================

def create_fact_query(participant: Any, user_message: Any, due_intents: List[Dict[str, Any]],
                      superseded_intents: List[Dict[str, Any]]) -> str:
    """上游 8308-8315 ``createFactQuery``（非导出）。"""
    state = normalize_participant_state(_field(participant, 'state')) if participant else None
    lines: List[str] = []
    if user_message:
        lines.append(f'Current user message: {user_message}')
    for thread in ((state or {}).get('openThreads') or []):
        lines.append(f'Open thread: {thread}')
    for note in ((state or {}).get('relationshipNotes') or []):
        lines.append(f'Relationship note: {note}')
    for intent in (due_intents or []):
        lines.append(f'Due intent: {_field(intent, "summary")}')
    for intent in (superseded_intents or []):
        lines.append(f'Superseded plan: {_field(intent, "summary")}')
    return '\n'.join(line for line in lines if line)


def format_buffered_user_messages(messages: List[Dict[str, Any]]) -> str:
    """上游 8317-8323 ``formatBufferedUserMessages``（非导出）。"""
    if len(messages) == 1:
        return _text(messages[0].get('content'))
    parts: List[str] = []
    for index, message in enumerate(messages):
        time = iso(to_date(_field(message, 'occurredAt'))) or ''
        parts.append(f'[连续消息 {index + 1}，收到时间 {time}]\n{_text(message.get("content"))}')
    return '\n\n'.join(parts)


def automatic_interval_minutes(story: Dict[str, Any], now: datetime, config: Any) -> float:
    """上游 8325-8329 ``automaticIntervalMinutes``（非导出）。"""
    rest_window = active_rest_window(_field(config, 'restWindows'), _field(_field(story, 'setting'), 'timezone'), now)
    if rest_window:
        return random_integer(_field(rest_window, 'minIntervalMinutes'), _field(rest_window, 'maxIntervalMinutes'))
    jitter = _js_number_or_zero(_field(config, 'jitterMinutes'))
    return max(1, _js_number_or_zero(_field(config, 'intervalMinutes')) + random_integer(-jitter, jitter))


def normalize_follow_up_minutes(values: Any) -> List[int]:
    """上游 8331-8337 ``normalizeFollowUpMinutes``（非导出）。

    与 service_base.py 既有回退一致：默认 [10, 20]，取整后保留 1..240，
    去重升序，最多 6 个。
    """
    defaults = [10, 20]
    source = values if isinstance(values, (list, tuple)) else defaults
    normalized: List[int] = []
    for value in source:
        number = _js_number(value)
        if number is None:
            continue
        floored = math.floor(number)
        if math.isfinite(floored) and 1 <= floored <= 240:
            normalized.append(int(floored))
    return sorted(set(normalized))[:6]


def schedule_conversation_follow_ups(anchor: datetime, config: Any) -> List[datetime]:
    """上游 8339-8351 ``scheduleConversationFollowUps``（非导出）。"""
    anchor_ms = _time_ms_or_zero(anchor)
    previous = anchor_ms
    result: List[datetime] = []
    for minutes in (_field(config, 'followUpMinutes') or []):
        jitter_amount = _field(config, 'followUpJitterMinutes')
        jitter = random_integer(-_js_number_or_zero(jitter_amount), _js_number_or_zero(jitter_amount)) if jitter_amount else 0
        # 即使开启抖动或主人给出很紧凑的自定义序列，后面的配置轮次也绝不早于前一次。
        at = max(previous + 1_000, anchor_ms + max(1, _js_number_or_zero(minutes) + jitter) * 60_000)
        previous = at
        result.append(_datetime_from_ms(at))
    return result


def active_rest_window(windows: Any, timezone: str, now: datetime) -> Optional[Dict[str, Any]]:
    """上游 8353-8364 ``activeRestWindow``（非导出）。"""
    local_minutes = local_clock_minutes(now, timezone)
    for window in (windows or []):
        if not _field(window, 'enabled'):
            continue
        start = clock_minutes(_field(window, 'start'))
        end = clock_minutes(_field(window, 'end'))
        if start is None or end is None:
            continue
        if start <= end:
            if local_minutes >= start and local_minutes < end:
                return window
        elif local_minutes >= start or local_minutes < end:
            return window
    return None


def clock_minutes(value: str) -> Optional[int]:
    """上游 8366-8371 ``clockMinutes``（非导出）。"""
    matched = re.fullmatch(r'(\d{1,2}):(\d{2})', _text(value).strip())
    if not matched:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2))
    return hour * 60 + minute if 0 <= hour < 24 and 0 <= minute < 60 else None


# ===================== 上游 8373-8440：实时剧本时间越界守卫 =====================

# 上游 8381 TIME_OVERFLOW_GRACE_MINUTES
# 模型调用与投递存在分钟级延迟，加上分钟取整：超出 now 这个宽限内的时钟引用
# 视为「就是现在」，不因网络抖动丢弃整段剧本。
TIME_OVERFLOW_GRACE_MINUTES = 5
# 上游 8385 TIME_FORWARD_HORIZON_MINUTES
# 12 小时制的歧义视野：now=00:01 时提到「11:58」几乎总是指刚过去的 23:58
# （午夜前 3 分钟），而不是 11 小时 57 分钟后的未来。朴素前向距离超过该视野的
# 时钟引用一律按「刚过去的 12 小时制写法」处理，不再判未来。
TIME_FORWARD_HORIZON_MINUTES = 6 * 60
# 上游 8389 LIVE_SCRIPT_HEADLINE_CHARS
# 剧本开头的「叙事宣告位」：中文叙事在场景起始处声明时间。只检查这一小段，
# 中后段的钟点绝大多数是对约定/回忆/计划的引用，不应作为越界证据。
LIVE_SCRIPT_HEADLINE_CHARS = 30
# 上游 8391 PLAN_SEMANTICS
# 计划/约定语义：钟点作为未来安排被引用时（「八点赶到」「九点前」），不构成越界。
PLAN_SEMANTICS = re.compile(r'赶到|约定|答应|要在|得在|之前|以前|打算|计划|准备|约好|说好|出发|来不及|赶不上|预计|大概|左右|还没|尚未')
# 上游 8393 CONTEXT_WINDOW
CONTEXT_WINDOW = 8


def context_around(text: str, index: int, length: int) -> str:
    """上游 8396-8398 ``contextAround``（非导出）。

    钟点前后各 8 字的上下文窗口：计划/约定词常出现在钟点紧邻处（「八点【前】赶到」）。
    """
    return text[max(0, index - CONTEXT_WINDOW):min(len(text), index + length + CONTEXT_WINDOW)]


def clocks_in(text: str) -> List[Dict[str, Any]]:
    """上游 8400-8415 ``clocksIn``（非导出）。"""
    found: List[Dict[str, Any]] = []
    for match in re.finditer(r'(?:^|[^\d])(?:(\d{1,2})[:：](\d{2})|(\d{1,2})点(?:(\d{1,2})分?)?)', text):
        hour_raw = match.group(1) if match.group(1) is not None else match.group(3)
        if hour_raw is None:
            continue
        minute_raw = match.group(2) if match.group(2) is not None else (match.group(4) if match.group(4) is not None else '0')
        hour = int(hour_raw)
        minute = int(minute_raw)
        if hour > 23 or minute > 59:
            continue
        found.append({'hour': hour, 'minute': minute, 'around': context_around(text, match.start(), len(match.group(0)))})
    for match in re.finditer(r'([零一二三四五六七八九十两]+)点(?:(?:零|([一二三四五六七八九十两]+))分?|半)?', text):
        hour = chinese_clock_number(match.group(1))
        minute = chinese_clock_number(match.group(2)) if match.group(2) else 0
        if hour is None or minute is None or hour > 23 or minute > 59:
            continue
        found.append({'hour': hour, 'minute': minute, 'around': context_around(text, match.start(), len(match.group(0)))})
    return found


def detect_live_script_time_overflow(script: Any, phase: Any, from_: datetime, now: datetime, timezone: str,
                                     endorsed_clocks: Any = None) -> Optional[str]:
    """上游 8417-8440 ``detectLiveScriptTimeOverflow``。

    面向实时消息的窄口径最后守卫。它刻意不解读普通散文：只拒绝短窗口里包含
    明确未来钟点或多个已完成课程阶段的剧本。自动推进与合法的长补时窗口不受影响。
    """
    text = _text(script).strip()
    elapsed_minutes = max(0.0, (now - from_).total_seconds() / 60.0)
    if phase != 'user-message' or not text or elapsed_minutes > 60:
        return None
    endpoint = story_local_time_context(now, timezone)
    now_minutes = endpoint['hour'] * 60 + int(endpoint['time'][3:5])

    def overflow(value: int) -> bool:
        return value > now_minutes + TIME_OVERFLOW_GRACE_MINUTES and value - now_minutes <= TIME_FORWARD_HORIZON_MINUTES

    # 用户给出了未来期限（「九点前赶到」）时，其之间的叙事推进被授权：落在
    # (now, maxEndorsed] 区间的钟点一并豁免；endorsed 为空时行为不变。
    endorsed_deadline = max(endorsed_clocks) if endorsed_clocks and len(endorsed_clocks) else None
    # 只把剧本开头（叙事宣告位）里、且没有计划语义的钟点视为「把叙事时间写过头」。
    for clock in clocks_in(text[:LIVE_SCRIPT_HEADLINE_CHARS]):
        value = clock['hour'] * 60 + clock['minute']
        if endorsed_clocks and value in endorsed_clocks:
            continue
        if endorsed_deadline is not None and value > now_minutes and value <= endorsed_deadline:
            continue
        if PLAN_SEMANTICS.search(clock['around']):
            continue
        if overflow(value):
            return (f"explicit clock {clock['hour']:02d}:{clock['minute']:02d} "
                    f"exceeds {endpoint['time'][:5]}")
    # 全文（含开头）的课程阶段推进不变：多个已完成的课程节 = 时间被写飞。
    lesson_stages = list(dict.fromkeys(match.group(1) for match in re.finditer(r'第\s*([一二三四五六七八九十\d]+)\s*节', text)))
    if len(lesson_stages) >= 2:
        return (f"multiple lesson stages ({'→'.join(lesson_stages)}) inside a "
                f"{_js_round(elapsed_minutes)} minute live window")
    return None


def chinese_clock_number(value: str) -> Optional[int]:
    """上游 8442-8452 ``chineseClockNumber``（非导出）。"""
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    text = _text(value)
    if text == '十':
        return 10
    if '十' in text:
        parts = text.split('十')
        left = parts[0]
        right = parts[1] if len(parts) > 1 else ''
        tens = digits.get(left) if left else 1
        ones = digits.get(right) if right else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return digits.get(text) if len(text) == 1 else None


def random_integer(minimum: Any, maximum: Any) -> int:
    """上游 8454-8458 ``randomInteger``（非导出）。"""
    lower = math.floor(min(_js_number_or_zero(minimum), _js_number_or_zero(maximum)))
    upper = math.floor(max(_js_number_or_zero(minimum), _js_number_or_zero(maximum)))
    return lower + math.floor(random.random() * (upper - lower + 1))


# ===================== 上游 8460-8524：笔记/证据/覆盖层分组合并 =====================

def merge_note(existing: Optional[str], next: Any) -> Optional[str]:
    """上游 8460-8466 ``mergeNote``（非导出）。"""
    value = clip(next, 2_000)
    if not value:
        return existing
    if not existing:
        return value
    if normalize_fact(value) in normalize_fact(existing):
        return existing
    return f'{existing}\n{value}'[-6_000:]


def patch_claims_match(left: str, right: str) -> bool:
    """上游 8468-8476 ``patchClaimsMatch``（非导出）。"""
    a = re.sub(r'[，。！？、,.!?；;:：]', '', normalize_fact(left))
    b = re.sub(r'[，。！？、,.!?；;:：]', '', normalize_fact(right))
    if not a or not b:
        return False
    if a == b:
        return True
    # 允许少量措辞差异，同时避免太短的断言错误合并相互矛盾的变化。
    return min(len(a), len(b)) >= 8 and (a in b or b in a)


def state_patch_evidence(entries: List[Dict[str, Any]], timezone: str) -> Dict[str, int]:
    """上游 8478-8485 ``statePatchEvidence``（非导出）。"""
    narrative = [entry for entry in (entries or [])
                 if _field(entry, 'kind') == 'script' or _field(entry, 'actor') == 'narrator']
    # 用叙事时间戳作为回合键。同一瞬间创建的重复行不得算作独立证据。
    turns = len({_time_ms_or_zero(_field(entry, 'occurredAt')) for entry in narrative})
    days = len({
        calendar_day_key(to_date(_field(entry, 'occurredAt')) or now_utc(), timezone)
        for entry in narrative
    })
    return {'turns': turns, 'days': days, 'scenes': development_scenes(narrative)}


def start_of_utc_window(value: Any, window_days: Any) -> datetime:
    """上游 8487-8491 ``startOfUtcWindow``（非导出）。"""
    size = max(1, math.floor(_js_number_or_zero(window_days)))
    epoch_day = math.floor(_time_ms_or_zero(value) / (24 * 60 * 60 * 1_000))
    return _datetime_from_ms(math.floor(epoch_day / size) * size * 24 * 60 * 60 * 1_000)


def group_overlay_patches(patches: List[Dict[str, Any]], window_days: int = 5) -> List[Dict[str, Any]]:
    """上游 8493-8503 ``groupOverlayPatches``（非导出）。"""
    groups: Dict[str, Dict[str, Any]] = {}
    for patch in (patches or []):
        applied_at = _field(patch, 'appliedAt')
        created_at = _field(patch, 'createdAt')
        from_ = start_of_utc_window(applied_at if applied_at is not None else created_at, window_days)
        key = f'{_field(patch, "participantId")}|{_field(patch, "target")}|{iso(from_)}'
        group = groups.get(key)
        if group is None:
            group = {
                'participantId': _field(patch, 'participantId'),
                'target': _field(patch, 'target'),
                'from': from_,
                'to': from_ + timedelta(days=window_days),
                'patches': [],
            }
            groups[key] = group
        group['patches'].append(patch)
    return list(groups.values())


def group_overlay_snapshots(snapshots: List[Dict[str, Any]], window_days: int = 10) -> List[Dict[str, Any]]:
    """上游 8505-8515 ``groupOverlaySnapshots``（非导出）。"""
    groups: Dict[str, Dict[str, Any]] = {}
    for snapshot in (snapshots or []):
        from_ = start_of_utc_window(_field(snapshot, 'periodEnd'), window_days)
        key = f'{_field(snapshot, "participantId")}|{_field(snapshot, "target")}|{iso(from_)}'
        group = groups.get(key)
        if group is None:
            group = {
                'participantId': _field(snapshot, 'participantId'),
                'target': _field(snapshot, 'target'),
                'from': from_,
                'to': from_ + timedelta(days=window_days),
                'snapshots': [],
            }
            groups[key] = group
        group['snapshots'].append(snapshot)
    return list(groups.values())


def normalize_major_events(value: Any, patches: List[Dict[str, Any]],
                           snapshots: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """上游 8517-8524 ``normalizeMajorEvents``（非导出）。"""
    model_events = [clip(item, 600) for item in value if isinstance(item, str)] if isinstance(value, list) else []
    retained: List[Any] = []
    for snapshot in (snapshots or []):
        retained.extend(_field(snapshot, 'majorEvents') or [])
    for patch in (patches or []):
        if _field(patch, 'impact') == 'major':
            retained.append(clip(_field(patch, 'proposedValue') or _field(patch, 'evidence'), 600))
    return [item for item in dict.fromkeys([*retained, *model_events]) if item][-20:]


# ===================== 导出清单 =====================

# 规格里写作 is_onebot_platform，这里给一个别名（service_base.py 也做同样的别名）。
is_onebot_platform = is_one_bot_platform

__all__ = [
    # 上游 7009-7082（部分非导出，服务类/mixin 依赖）
    'story_id_for_character', 'legacy_story_id_for', 'participant_id_for', 'participant_id_for_story',
    'same_participant_endpoint', 'is_one_bot_platform', 'is_onebot_platform', 'extract_session_image_sources',
    # 上游 7084-7185
    'extract_session_voice_count', 'extract_session_audio_sources', 'extract_session_file_facts',
    'AUDIO_FILE_EXTENSIONS',
    # 上游 7186-7205
    'describe_group_attachments',
    # 上游 7207-7244
    'with_timeout', 'guess_image_mime', 'guess_audio_format', 'is_animated_image_mime',
    # 上游 7246-7298
    'session_group_id', 'normalize_group_id', 'CHAT_REACTION_NAMES', 'QQ_REACTION_IDS',
    'NATIVE_FACE_SEMANTICS', 'QQ_NATIVE_FACE_IDS', 'normalize_allowed_native_faces',
    'normalize_expression_threshold', 'calibrated_native_face_willingness',
    # 上游 7300-7350
    'targetable_message_id', 'group_message_ref', 'list_sticker_files', 'sticker_mime', 'stable_sticker_asset_id',
    # 上游 7351-7397
    'extract_user_reported_times',
    # 上游 7399-7448
    'describe_quoted_message', 'normalize_quoted_message_content', 'normalize_quoted_message_context',
    'normalize_allowed_reactions',
    # 上游 7450-7529
    'TIMELINE_KIND_ALIASES', 'coerce_timeline_kind', 'coerce_timeline_position', 'normalize_timeline_plan',
    'describe_timeline_plan_rejection', 'timeline_entry_prompt_projection',
    # 上游 7531-7583
    'normalize_group_chat_actions', 'format_group_speaker', 'normalize_group_display_name', 'mentions_bot',
    # 上游 7585-7663
    'normalize_group_visible_reply', 'requires_visible_reply_recovery', 'visible_reply_mode',
    'has_structured_group_reply', 'has_structured_group_reply_field', 'has_structured_interaction',
    'safe_json_preview', 'normalize_group_reply', 'normalize_group_interaction_reply',
    'normalize_visible_message_content',
    # 上游 7665-7709
    'literal_quote_text', 'is_literal_quote_only', 'same_platform_family', 'normalize_account_id',
    'signed_number', 'quotes_bot', 'is_transient_database_error', 'is_enabled_account',
    # 上游 7711-7727
    'has_required_narrative_script', 'resolve_blind_mode_config', 'resolve_black_box_config',
    'is_automatic_narrative_phase',
    # 上游 7729-7802
    'normalize_automatic_delivery_summary', 'normalize_follow_up_summary', 'follow_up_expires_at',
    'normalize_follow_up_commitment', 'inferred_follow_up_commitment', 'interaction_promises_follow_up',
    'normalize_follow_up_resolutions', 'automatic_delivery_from_payload', 'merge_delivery_summary',
    # 上游 7807-7833
    'normalize_scene_presence_drafts', 'has_explicit_presence_evidence',
    # 上游 7835-7879
    'normalize_decision',
    # 上游 7881-7965
    'normalize_browser_intent_draft_loose', 'normalize_browser_intent_draft', 'browser_intent_from_payload',
    'resolve_browser_target', 'is_safe_public_web_url', 'normalize_domains', 'domain_matches',
    'is_private_host', 'web_observation_entry_content',
    # 上游 7968-8069
    'normalize_interaction', 'valid_memory', 'valid_intent', 'normalize_intent_updates',
    'is_active_consequence', 'is_active_consequence_draft', 'consequence_expires_at',
    'consequence_strength', 'has_compaction_evidence', 'normalize_conversation_action', 'permitted_or_global',
    # 上游 8071-8129
    'pick_participant_state_patch', 'merge_setting', 'merge_participant_state', 'normalize_participant_state',
    'participant_relevance', 'group_due_intents', 'resolve_participant_id',
    # 上游 8131-8203
    'is_record', 'should_supersede_narrative_request', 'to_date', 'DATABASE_DATE_FIELDS',
    'normalize_database_row', 'same_timestamp', 'narrative_cursor', 'clip', 'clamp_number',
    # 上游 8205-8303
    'normalize_fact', 'limit_entries_by_characters', 'fact_score', 'history_lexical_score',
    'lexical_recall_keys', 'cosine_similarity', 'SEMANTIC_STICKER_LIMIT', 'rank_sticker_catalog',
    'should_downscale_image',
    # 上游 8308-8364
    'create_fact_query', 'format_buffered_user_messages', 'automatic_interval_minutes',
    'normalize_follow_up_minutes', 'schedule_conversation_follow_ups', 'active_rest_window', 'clock_minutes',
    # 上游 8373-8452
    'TIME_OVERFLOW_GRACE_MINUTES', 'TIME_FORWARD_HORIZON_MINUTES', 'LIVE_SCRIPT_HEADLINE_CHARS',
    'PLAN_SEMANTICS', 'CONTEXT_WINDOW', 'context_around', 'clocks_in', 'detect_live_script_time_overflow',
    'chinese_clock_number',
    # 上游 8454-8524
    'random_integer', 'merge_note', 'patch_claims_match', 'state_patch_evidence', 'start_of_utc_window',
    'group_overlay_patches', 'group_overlay_snapshots', 'normalize_major_events',
]

