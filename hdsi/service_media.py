# -*- coding: utf-8 -*-
"""HDS-Interlude 移植：上游 `.hdsi_reference/src/service.ts` 行号 1953-3140（1.0.1-beta6-rebuild）。

ServiceMediaMixin —— 聊天动作能力（引用回复/消息表情回应/原生表情）、贴纸库扫描与
语义贴纸检索、用户事件描述、原生图片与语音读取（视觉侧端/原生双通道）、实验性流式
首条回复提前投递、短时消息合并缓冲（bufferUserNarrative/flushBufferedNarrative）、
史官剧本检索向量缓存与回填。

约定（与 SERVICE_PORTING_SPEC.md / PORTING_GUIDE.md 一致）：
- 同步方法；上游 async/Promise 一律同步化，跨 mixin 调用用 snake_case 方法名。
- 本 mixin 不定义 __init__；以下共享状态/能力由 hdsi/service_base.py 提供：
  self.config / self.ctx / self.platform / self.embedder / self.narrator /
  self.sticker_describer / self.vision_describer / self.sticker_catalog /
  self.sticker_by_id / self.sticker_scan_running / self.history_vectors /
  self.history_vectors_ready / self.history_vector_loads / self.history_backfills /
  self.history_backoff / self.automatic_recall_cache / self.buffered_narrative_turns /
  self.buffered_group_turns / self.group_willingness / self.due_intent_wake_timers /
  self.interrupted_typing_participants / self.narrating_stories / self.compaction_backoff /
  self.database_resetting / self.desktop_runtime_phase / self.expression_threshold /
  self.audio_config / self.sticker_config / self.memory_config / self.shared_story_config /
  self.db_get / self.db_create / self.db_set / self.serial / self.embed_text /
  self.report / self.report_operation / self.report_standalone / self.report_standalone_operation。
- 上游日志、报错文案、截断长度与阈值逐字保留（含中文标点）。
- 平台映射：h('img')→self.platform.send_sticker；h('face')→send_native_face；
  setMsgEmojiLike→send_reaction；bot.getImage/ctx.http→fetch_image/fetch_audio；
  node:fs/promises→read_file/list_files/file_size；ctx.puppeteer→
  downscale_image/render_animated_frame（不可用时走上游降级分支）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from .platform.session import InboundSession
from .qq_face import normalize_qq_native_face_segments
from .script.commit_builder import find_outgoing_script_event
from .script.contract import message_event_reference
from .script.delivery_ledger import platform_action_reference
from .script.intent_lifecycle import consumed_live_intent_ids, live_narrative_intents
from .story_state import decode_story_state, encode_story_state
from .time_utils import iso, now_utc, parse_time
from .types import ChatActionCapabilities, NarrativeAudio, NarrativeImage, StickerAsset, StickerCatalogEntry
from .utils import clip, is_array, is_record

# ===================== 上游 service.ts 顶层常量（逐字保留） =====================

# 上游 173：可进入语义史官检索的条目类型。
_RECALLABLE_ENTRY_KINDS = [
    'user-message', 'character-message', 'script', 'group-message', 'character-group-message',
]

# 上游 223：STICKER_DESCRIPTION_RETRY_COOLDOWN = 30 * Time.minute
_STICKER_DESCRIPTION_RETRY_COOLDOWN_MS = 30 * 60 * 1000

# 上游 8284：SEMANTIC_STICKER_LIMIT = 12
_SEMANTIC_STICKER_LIMIT = 12

# 上游 7253-7264：聊天动作枚举与 QQ 传输 ID。
_CHAT_REACTION_NAMES = ['like', 'smile', 'laugh', 'heart', 'surprised', 'sad', 'angry']
_QQ_REACTION_IDS: Dict[str, str] = {
    'like': '76', 'smile': '14', 'laugh': '182', 'heart': '66', 'surprised': '0', 'sad': '5', 'angry': '106',
}
_NATIVE_FACE_SEMANTICS = ['smile', 'laugh', 'sweat', 'awkward', 'heart', 'surprised', 'sad', 'angry']
_QQ_NATIVE_FACE_IDS: Dict[str, str] = {
    'smile': '14', 'laugh': '182', 'sweat': '27', 'awkward': '111', 'heart': '66', 'surprised': '0', 'sad': '5', 'angry': '106',
}

# 上游 254-259：只信任 QQ/OneBot CDN 主机名，避免用户 URL 变成内网抓取代理。
_TRUSTED_IMAGE_HOSTS = ['gchat.qpic.cn', 'c2cpicdw.qpic.cn', 'multimedia.nt.qq.com.cn', 'thirdqq.qlogo.cn', 'q.qlogo.cn']

_AUDIO_FILE_EXTENSIONS = re.compile(r'\.(mp3|wav|ogg|m4a|flac|amr|aac|wma)$', re.IGNORECASE)
_IMAGE_FILE_EXTENSIONS = re.compile(r'\.(?:png|jpe?g|webp|gif)$', re.IGNORECASE)
_INLINE_AUDIO_FORMATS = ('mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr')


# ===================== 通用小工具（上游 service.ts 内的局部函数） =====================

def _field(value: Any, key: str, default: Any = None) -> Any:
    """读取 config/路由/规则字段。

    上游 `this.config.x`、`rule.y` 在并行移植里既可能以 dict（本包约定）也可能以
    轻量对象暴露，两种形状都兼容，避免形状未定时崩溃。
    """
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _config_get(config: Any, *keys: str, default: Any = None) -> Any:
    """按上游 `this.config.a.b.c` 逐级读取，任一级缺失返回 default。"""
    value = config
    for key in keys:
        value = _field(value, key)
        if value is None:
            return default
    return value if value is not None else default


def _text(value: Any) -> str:
    """TS `String(value ?? '')`。"""
    return '' if value is None else str(value)


def _bool_text(value: Any) -> str:
    """上游日志里 `%s` 打印 JS 布尔值时的文本（true/false）。"""
    return 'true' if value else 'false'


def _now_ms() -> int:
    """Date.now()。"""
    return int(now_utc().timestamp() * 1000)


def _unique(values: Sequence[Any]) -> List[Any]:
    """Array.from(new Set(values))：保持首次出现顺序。"""
    seen: Set[Any] = set()
    result: List[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _safe_int_number(value: Any) -> int:
    """Number(value) || 0 的整数化（用于 <file size> 等）。"""
    try:
        return int(float(value)) if value not in (None, '') else 0
    except (TypeError, ValueError):
        return 0


def _is_safe_integer(value: Any) -> bool:
    """Number.isSafeInteger。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(value) <= 9007199254740991


def _base64_byte_length(value: str) -> int:
    """Buffer.byteLength(value, 'base64') 的等价长度估算（JS 忽略非法字符）。"""
    text = re.sub(r'\s+', '', value or '')
    padding = text.count('=')
    return max(0, (len(text) * 3) // 4 - padding)


def _decode_base64(value: str) -> bytes:
    return base64.b64decode(re.sub(r'\s+', '', value or '') + '=' * (-len(re.sub(r'\s+', '', value or '')) % 4))


def _encode_base64(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def _data_uri_bytes(data_uri: Any) -> bytes:
    """取 data URI 的 base64 载荷字节（上游 Buffer.from(dataUri.split(',')[1], 'base64')）。"""
    text = _text(data_uri)
    _, _, payload = text.partition(',')
    if not payload:
        return b''
    try:
        return _decode_base64(payload)
    except Exception:  # noqa: BLE001 - 畸形 payload 按空处理
        return b''


def _data_uri_mime(data_uri: Any) -> str:
    text = _text(data_uri)
    match = re.match(r'^data:([^;,]+)', text, re.IGNORECASE)
    return match.group(1).lower() if match else ''


def _url_hostname(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or ''
    except ValueError:
        return ''


def _embedder_identity(embedder: Any) -> Optional[str]:
    """上游 `this.embedder.identity?.()`（可选链；异常按无身份处理）。"""
    getter = getattr(embedder, 'identity', None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - 身份探测失败不影响主流程
        return None


def _reaction_api_available(platform: Any, session: Any) -> bool:
    """上游 `typeof (session as any).bot?.internal?.setMsgEmojiLike === 'function'`。

    平台层约定（见最终交付说明）：可选实现 `supports_message_reaction(session)` 作为
    传输探针；未实现时视为 PlatformAdapter.send_reaction 可用（返回 False 表示该会话
    不支持消息表情回应，对应上游 cancelled/reaction-api-unavailable 分支）。
    """
    probe = getattr(platform, 'supports_message_reaction', None)
    if callable(probe):
        try:
            return bool(probe(session))
        except Exception:  # noqa: BLE001 - 探针失败按不支持处理
            return False
    return True


def _normalize_group_display_name(*candidates: Any) -> str:
    """上游 7570-7576 normalizeGroupDisplayName。"""
    for candidate in candidates:
        name = re.sub(r'[\r\n]', ' ', _text(candidate)).strip()
        if name:
            return name[:80]
    return ''


# ===================== 上游 service.ts 顶层导出 helper（hdsi/service_helpers.py） =====================
# 上游 7102-7446 / 7649-7709 / 8133-8330 的纯函数由并行移植的 service_helpers.py 提供；
# 尚未落地时回退到本文件内联副本（与上游逐行等价，便于本 mixin 独立可用）。

try:  # pragma: no cover - 取决于并行移植进度
    from .service_helpers import (
        calibrated_native_face_willingness,
        describe_quoted_message,
        extract_session_audio_sources,
        extract_session_file_facts,
        guess_audio_format,
        normalize_allowed_reactions,
        normalize_quoted_message_content,
        rank_sticker_catalog,
        should_downscale_image,
        should_supersede_narrative_request,
        stable_sticker_asset_id,
    )
except ImportError:  # TODO(并行移植): service_helpers 就绪后自动切换
    def extract_session_file_facts(session: Any) -> List[Dict[str, Any]]:
        """上游 7158-7187 extractSessionFileFacts（Python 会话形状：session.elements）。"""
        raw = _text(getattr(session, 'content', ''))
        facts: List[Dict[str, Any]] = []

        def push(url: Any, name: Any, size: Any) -> None:
            url_text = _text(url)
            name_text = _text(name)
            if (not url_text and not name_text) or len(facts) >= 3:
                return
            if any(item.get('url') == url_text and item.get('name') == name_text for item in facts):
                return
            facts.append({
                'name': name_text[:200], 'url': url_text[:1000], 'size': size,
                'audio': bool(_AUDIO_FILE_EXTENSIONS.search(name_text)),
            })

        for element in getattr(session, 'elements', None) or []:
            if _text(getattr(element, 'type', '')).lower() != 'file':
                continue
            push(
                _text(getattr(element, 'url', '')) or _text(getattr(element, 'value', '')),
                _text(getattr(element, 'name', '')) or _text(getattr(element, 'value', '')),
                _safe_int_number(getattr(element, 'size', 0)),
            )
        if not facts:
            for match in re.finditer(r'<file\b([^>]*)/?>', raw, re.IGNORECASE):
                attrs = match.group(1)

                def pick(key: str) -> str:
                    found = re.search(key + r'=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
                    return found.group(1).strip() if found else ''

                push(pick('src') or pick('url'), pick('name') or pick('file') or pick('title'),
                     _safe_int_number(pick('size') or pick('file-size')))
        return facts

    def extract_session_audio_sources(session: Any) -> List[str]:
        """上游 7102-7156 extractSessionAudioSources（语音优先 OneBot file token）。"""
        raw = _text(getattr(session, 'content', ''))
        sources: List[str] = []

        def add(value: Any, kind: str = 'file') -> None:
            source = _text(value).strip()
            if not source or source in sources or len(source) > 512:
                return
            if re.match(r'^data:audio/', source, re.IGNORECASE):
                sources.append(source)
                return
            # http(s) 语音 URL 是原始 SILK，没有 file token 就没有服务端转码路径，故意跳过。
            if re.match(r'^https?://', source, re.IGNORECASE):
                return
            if kind == 'file':
                sources.append('onebot-file:' + source)

        for element in getattr(session, 'elements', None) or []:
            if _text(getattr(element, 'type', '')).lower() not in ('audio', 'record'):
                continue
            file_value = _text(getattr(element, 'value', ''))
            if file_value and not re.match(r'^(?:https?://|data:audio/)', file_value, re.IGNORECASE):
                add(file_value, 'file')
            else:
                add(file_value or _text(getattr(element, 'url', '')), 'url')
        if not sources:
            for match in re.finditer(r'<(?:audio|record)\b[^>]*(?:file|src|url)=["\']([^"\']+)["\'][^>]*>', raw, re.IGNORECASE):
                add(match.group(1))
        for match in re.finditer(r'\[CQ:record,([^\]]+)\]', raw, re.IGNORECASE):
            fields: Dict[str, str] = {}
            for part in match.group(1).split(','):
                index = part.find('=')
                if index > 0:
                    fields[part[:index].strip().lower()] = part[index + 1:].strip()
            add(fields.get('file'), 'file')
        # QQ 音频文件走 <file> 元素（CDN 直链 + 文件名扩展），原始字节可直接作为
        # input_audio；与语音的 SILK 转码路径不同，标记 file-url 前缀。
        for fact in extract_session_file_facts(session):
            if not fact.get('audio') or not re.match(r'^https?://', _text(fact.get('url')), re.IGNORECASE):
                continue
            encoded = 'file-url:%s#%s:%d' % (
                fact.get('url'),
                urllib.parse.quote(_text(fact.get('name')), safe="~()*!.'-"),
                _safe_int_number(fact.get('size')),
            )
            if encoded not in sources:
                sources.append(encoded)
        return sources

    def guess_audio_format(bytes_: bytes, hinted_name: Any = None) -> str:
        """上游 7229-7245 guessAudioFormat。"""
        hinted = re.search(r'\.(mp3|wav|ogg|m4a|flac|amr)\b', _text(hinted_name), re.IGNORECASE)
        if hinted:
            return hinted.group(1).lower()
        if len(bytes_) >= 12 and bytes_[:4] == b'RIFF' and bytes_[8:12] == b'WAVE':
            return 'wav'
        if len(bytes_) >= 4 and bytes_[:4] == b'OggS':
            return 'ogg'
        if len(bytes_) >= 8 and bytes_[4:8] == b'ftyp':
            return 'm4a'
        if len(bytes_) >= 4 and bytes_[:4] == b'fLaC':
            return 'flac'
        if len(bytes_) >= 5 and bytes_[:5] == b'#!AMR':
            return 'amr'
        if len(bytes_) >= 3 and bytes_[:3] == b'ID3':
            return 'mp3'
        if len(bytes_) >= 2 and bytes_[0] == 0xff and (bytes_[1] & 0xe0) == 0xe0:
            return 'mp3'
        return ''

    def stable_sticker_asset_id(file_path: Any, hash_: Any) -> str:
        """上游 7337-7350 stableStickerAssetId。"""
        stem = re.sub(r'\\', '/', _text(file_path))
        stem = re.sub(r'\.[^.]+$', '', stem)
        stem = re.sub(r'[^a-zA-Z0-9/_-]', '-', stem)
        stem = re.sub(r'-+', '-', stem)
        stem = re.sub(r'^[-/]+|[-/]+$', '', stem)[:220] or 'sticker'
        suffix = re.sub(r'[^a-fA-F0-9]', '', _text(hash_))[:16].lower() or 'unhashed'
        return ('%s-%s' % (stem, suffix))[:255]

    def normalize_quoted_message_content(value: Any) -> str:
        """上游 7417-7444 normalizeQuotedMessageContent。"""
        content = normalize_qq_native_face_segments(value)
        content = re.sub(r'<(?:img|image)\b[^>]*/?>(?:</(?:img|image)>)?', '[图片]', content, flags=re.IGNORECASE)
        content = re.sub(r'<(?:audio|record)\b[^>]*/?>(?:</(?:audio|record)>)?', '[语音]', content, flags=re.IGNORECASE)
        content = re.sub(r'<video\b[^>]*/?>(?:</video>)?', '[视频]', content, flags=re.IGNORECASE)
        content = re.sub(r'<(?:face|mface)\b[^>]*/?>(?:</(?:face|mface)>)?', '[表情]', content, flags=re.IGNORECASE)
        content = re.sub(r'<at\b[^>]*(?:name|id)=["\']?([^\s"\'>]+)[^>]*/?>(?:</at>)?', r'[@\1]', content, flags=re.IGNORECASE)
        content = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', content, flags=re.IGNORECASE)
        content = re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', content, flags=re.IGNORECASE)
        content = re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', content, flags=re.IGNORECASE)
        content = re.sub(r'\[CQ:face,[^\]]*\]', '[表情]', content, flags=re.IGNORECASE)
        content = re.sub(r'<[^>]+>', '', content)
        content = re.sub(r'[\r\n]+', ' ', content)
        content = re.sub(r'\s{2,}', ' ', content)
        return clip(content.strip(), 1500)



# ===================== 其余顶层 helper（沿用 hdsi/service_helpers.py 的命名） =====================

try:  # pragma: no cover - 取决于并行移植进度
    from .service_helpers import (
        cosine_similarity,
        extract_session_image_sources,
        guess_image_mime,
        is_animated_image_mime,
        is_one_bot_platform,
        normalize_allowed_native_faces,
        normalize_expression_threshold,
        normalize_visible_message_content,
    )
except ImportError:  # TODO(并行移植): service_helpers 缺名时回退到本文件内联副本
    def is_one_bot_platform(platform: Any) -> bool:
        """上游 7026-7034 isOneBotPlatform。"""
        value = _text(platform).lower()
        return (
            value == 'onebot' or value.startswith('onebot:')
            or value == 'napcat' or value.startswith('napcat:')
            or value == 'qq:onebot' or value.startswith('qq:onebot:')
        )

    def guess_image_mime(bytes_: bytes, hinted: Any = None) -> str:
        """上游 7217-7227 guessImageMime。"""
        hint = _text(hinted).lower()
        if hint.startswith('image/'):
            return hint
        if len(bytes_) >= 3 and bytes_[0] == 0xff and bytes_[1] == 0xd8 and bytes_[2] == 0xff:
            return 'image/jpeg'
        if len(bytes_) >= 8 and bytes_[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10]):
            return 'image/png'
        if len(bytes_) >= 6 and bytes_[:6] in (b'GIF87a', b'GIF89a'):
            return 'image/gif'
        if len(bytes_) >= 12 and bytes_[:4] == b'RIFF' and bytes_[8:12] == b'WEBP':
            return 'image/webp'
        return ''

    def is_animated_image_mime(mime: Any) -> bool:
        """上游 7242-7244 isAnimatedImageMime。"""
        return _text(mime) in ('image/gif', 'image/webp', 'image/apng')

    def normalize_allowed_native_faces(value: Any) -> List[str]:
        """上游 7266-7269 normalizeAllowedNativeFaces。"""
        if not is_array(value):
            return []
        return _unique([item for item in value if item in _NATIVE_FACE_SEMANTICS])[:len(_NATIVE_FACE_SEMANTICS)]

    def normalize_expression_threshold(value: Any) -> float:
        """上游 7271-7274 normalizeExpressionThreshold（默认 0.7）。"""
        if isinstance(value, bool):
            number = math.nan
        elif isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip()) if value.strip() else 0.0
            except ValueError:
                number = math.nan
        else:
            number = math.nan
        if math.isnan(number) or math.isinf(number):
            return 0.7
        return max(0.0, min(1.0, number))

    def normalize_visible_message_content(value: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
        """上游 7649-7663 normalizeVisibleMessageContent。"""
        separator_text = (separator.strip() if isinstance(separator, str) else '') or '<sep/>'
        content = _text(value)
        content = re.sub(r'[<＜]\s*sep\s*/?\s*[>＞]', separator_text, content, flags=re.IGNORECASE)
        content = re.sub(r'</?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*/?>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]', '', content)
        try:
            limit = int(max(1, math.floor(float(max_characters))))
        except (TypeError, ValueError):
            limit = 0  # Math.max(1, NaN) → NaN → slice(0, NaN) → ''
        return content.strip()[:limit]

    def extract_session_image_sources(session: Any) -> List[str]:
        """上游 7036-7082 extractSessionImageSources（session.elements + 原始 content）。"""
        raw = _text(getattr(session, 'content', ''))
        sources: List[str] = []

        def add(value: Any, kind: str = 'url') -> None:
            source = _text(value).strip()
            if not source or source in sources:
                return
            if len(source) > 8 * 1024 * 1024:
                return
            if re.match(r'^https?://', source, re.IGNORECASE):
                sources.append('onebot-url:' + source if kind == 'adapter-url' else source)
            elif re.match(r'^data:image/', source, re.IGNORECASE):
                sources.append(source)
            elif kind == 'file':
                sources.append('onebot-file:' + source)

        for element in getattr(session, 'elements', None) or []:
            if _text(getattr(element, 'type', '')).lower() not in ('img', 'image'):
                continue
            src = _text(getattr(element, 'url', ''))
            value = _text(getattr(element, 'value', ''))
            if src:
                add(src)
            elif value and re.match(r'^(?:https?://|data:image/)', value, re.IGNORECASE):
                add(value)
            else:
                add(value, 'file')
        if not sources:
            for match in re.finditer(r'<(?:img|image)\b[^>]*(?:src|url)=["\']([^"\']+)["\'][^>]*>', raw, re.IGNORECASE):
                add(match.group(1))
        for match in re.finditer(r'\[CQ:image,([^\]]+)\]', raw, re.IGNORECASE):
            fields: Dict[str, str] = {}
            for part in match.group(1).split(','):
                index = part.find('=')
                if index > 0:
                    fields[part[:index].strip().lower()] = part[index + 1:].strip()
            add(fields.get('url') or fields.get('cache_url'), 'adapter-url')
            if not fields.get('url') and not fields.get('cache_url'):
                add(fields.get('file'), 'file')
        return sources

    def cosine_similarity(left: Any, right: Any) -> Optional[float]:
        """上游 8269-8281 cosineSimilarity（长度不等返回 None）。"""
        if not left or not right or len(left) != len(right):
            return None
        dot = 0.0
        left_magnitude = 0.0
        right_magnitude = 0.0
        for index in range(len(left)):
            dot += left[index] * right[index]
            left_magnitude += left[index] * left[index]
            right_magnitude += right[index] * right[index]
        if not left_magnitude or not right_magnitude:
            return None
        return dot / math.sqrt(left_magnitude * right_magnitude)


def is_trusted_image_host(hostname: Any) -> bool:
    """上游 254-259 isTrustedImageHost（service_helpers 未导出，本文件自带）。"""
    host = _text(hostname).lower()
    if host.endswith('.'):
        host = host[:-1]
    return any(host == domain or host.endswith('.' + domain) for domain in _TRUSTED_IMAGE_HOSTS)


def should_request_turn_embedding(embedding: Any, sticker_library_enabled: Any, sticker_count: Any) -> bool:
    """上游 200-205 shouldRequestTurnEmbedding（service_helpers 未导出，本文件自带）。"""
    if not _field(embedding, 'enabled'):
        return False
    return (
        _field(embedding, 'liveQuery') is True
        or _field(embedding, 'semanticHistory') is True
        or (_field(embedding, 'semanticStickerFilter') is True
            and sticker_library_enabled and sticker_count > _SEMANTIC_STICKER_LIMIT)
    )


def is_history_entry_visible_to_participant(entry: Any, participant_id: Any, share_participant_details: Any) -> bool:
    """上游 191-196 isHistoryEntryVisibleToParticipant（service_helpers 未导出，本文件自带）。"""
    if share_participant_details:
        return True
    if _field(entry, 'kind') in ('group-message', 'character-group-message'):
        return False
    return not _field(entry, 'participantId') or _field(entry, 'participantId') == participant_id


# ===================== 上游 src/narrator.ts 的 prompt 视图 helper =====================

try:  # pragma: no cover - narrator 子模块（narrator_prompts）尚未落地时回退
    from .narrator import prompt_visible_message_content, recent_script_ownership
except ImportError:  # TODO(并行移植): narrator 就绪后自动切换
    def recent_script_ownership(entry: Any) -> str:
        """上游 narrator.ts 1580-1594 recentScriptOwnership。"""
        kind = _field(entry, 'kind')
        actor = _field(entry, 'actor')
        if kind == 'group-message':
            return 'external-group-message'
        if kind == 'user-message' or actor == 'user':
            return 'user-delivered-message'
        if kind in ('character-message', 'character-group-message') or actor == 'character':
            return 'protagonist-delivered-message'
        if kind == 'script' or actor == 'narrator':
            return 'protagonist-narrative'
        return 'system-event'

    def prompt_visible_message_content(content: Any, ownership: str) -> str:
        """上游 narrator.ts 1901-1909 promptVisibleMessageContent。"""
        if ownership != 'protagonist-delivered-message':
            return content
        text = _text(content)
        text = re.sub(r'[\[【]流汗[\]】]', '〈附带汗颜表情〉', text)
        text = re.sub(r'[\[【]微笑[\]】]', '〈附带微笑表情〉', text)
        text = re.sub(r'[\[【]笑哭[\]】]', '〈附带笑哭表情〉', text)
        text = re.sub(r'[\[【]尴尬[\]】]', '〈附带尴尬表情〉', text)
        text = re.sub(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', '〈附带未识别媒体表达〉', text, flags=re.IGNORECASE)
        return text


# ===================== 上游 src/script/ 的检索导航纯函数 =====================

try:  # pragma: no cover - script 模块并行移植
    from .script.episode_index import (
        build_episode_index,
        episode_excerpt,
        episode_tag_score,
        grounded_episode_tags,
    )
    from .script.recall_navigation import index_original, recall_keys, score_original
except ImportError:  # TODO(并行移植): episode_index / recall_navigation 就绪后自动切换
    def recall_keys(text: str) -> List[str]:
        """上游 recall-navigation.ts 4-11 recallKeys。"""
        normalized = _text(text).lower()
        words = re.findall(r'[a-z0-9]{2,}', normalized)
        for run in re.findall(r'[\u3400-\u9fff]{2,}', normalized):
            for index in range(len(run) - 1):
                words.append(run[index:index + 2])
        return _unique(words)

    def index_original(content: str) -> List[Dict[str, Any]]:
        """上游 recall-navigation.ts 13-29 indexOriginal（句子粒度 RecallSpan）。"""
        spans: List[Dict[str, Any]] = []
        sentences = list(re.finditer(r'[^。！？\n]+[。！？\n]*|[。！？\n]+', content))
        start = 0
        end = 0

        def push() -> None:
            nonlocal start
            if end > start:
                spans.append({'start': start, 'end': end, 'keys': set(recall_keys(content[start:end]))})
            start = end

        for sentence in sentences:
            if end > start and end - start + len(sentence.group(0)) > 700:
                push()
            end = sentence.start() + len(sentence.group(0))
        push()
        return spans

    def score_original(keys: List[str], spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        """上游 recall-navigation.ts 31-40 scoreOriginal。"""
        score = 0.0
        index = 0
        if not keys:
            return {'score': score, 'index': index}
        for position, span in enumerate(spans):
            value = len([key for key in keys if key in span.get('keys', set())]) / len(keys)
            if value > score:
                score = value
                index = position
        return {'score': score, 'index': index}

    def _original_window(content: str, spans: List[Dict[str, Any]], index: int, budget: int,
                         query_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        """上游 recall-navigation.ts 42-65 originalWindow。"""
        query_keys = query_keys or []
        if len(content) <= budget:
            return {'content': content, 'start': 0, 'end': len(content)}
        anchor = spans[index] if 0 <= index < len(spans) else None
        if anchor is None:
            return {'content': content[:budget], 'start': 0, 'end': min(budget, len(content))}
        start = anchor['start']
        end = anchor['end']
        if end - start > budget and query_keys:
            lower = content.lower()
            offsets = [lower.find(key, anchor['start']) for key in query_keys]
            offsets = [offset for offset in offsets if start <= offset < end]
            if offsets:
                hit = min(offsets)
                start = max(start, min(hit - budget // 3, end - budget))
        for neighbor_index in (index - 1, index + 1):
            if 0 <= neighbor_index < len(spans):
                neighbor = spans[neighbor_index]
                if max(end, neighbor['end']) - min(start, neighbor['start']) <= budget:
                    start = min(start, neighbor['start'])
                    end = max(end, neighbor['end'])
        end = min(end, start + budget)
        return {'content': content[start:end], 'start': start, 'end': end}

    def build_episode_index(rows: List[Any]) -> Dict[int, List[int]]:
        """上游 episode-index.ts 17-32 buildEpisodeIndex。"""
        groups: Dict[str, List[int]] = {}
        checkpoints = []
        for _, row in rows:
            checkpoint = _field(row, 'checkpoint')
            if is_record(checkpoint) and _is_safe_integer(checkpoint.get('firstEntryId')) and _is_safe_integer(checkpoint.get('lastEntryId')):
                checkpoints.append(checkpoint)
        for entry_id, row in rows:
            checkpoint = next((item for item in checkpoints
                               if item['firstEntryId'] <= entry_id <= item['lastEntryId']), None)
            key = json.dumps([
                _field(row, 'participantId'),
                ('scene:%s' % checkpoint.get('sceneId')) if checkpoint else (_field(row, 'frameId') or ('entry:%s' % entry_id)),
            ], ensure_ascii=False)
            groups.setdefault(key, []).append(entry_id)
        by_entry: Dict[int, List[int]] = {}
        for ids in groups.values():
            for entry_id in ids:
                by_entry[entry_id] = ids
        return by_entry

    def episode_excerpt(rows: List[Any], anchor_id: int, budget: int = 4000,
                        query_keys: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """上游 episode-index.ts 35-58 episodeExcerpt。"""
        query_keys = query_keys or []
        position = next((index for index, (entry_id, _) in enumerate(rows) if entry_id == anchor_id), -1)
        if position < 0:
            return None
        selected: Dict[int, str] = {}
        remaining = budget
        for index in (position, position + 1, position - 1, position + 2, position - 2):
            if index < 0 or index >= len(rows) or remaining < 40:
                continue
            entry_id, row = rows[index]
            content = _text(_field(row, 'content'))
            if index != position and len(content) + 100 > remaining:
                continue
            kind = _field(row, 'kind')
            owner = 'user-delivered-message' if kind == 'user-message' \
                else 'protagonist-narrative' if kind == 'script' \
                else 'group-member-message' if kind == 'group-message' else 'protagonist-delivered-message'
            spans = _field(row, 'spans')
            if not spans:
                spans = index_original(content)
                row['spans'] = spans
            hit = score_original(query_keys, spans)
            window = _original_window(content, spans, hit['index'], max(0, remaining - 180), query_keys)
            partial = window['start'] > 0 or window['end'] < len(content)
            text = '[%s; %s; entry:%d%s] %s' % (
                _field(row, 'occurredAt'), owner, entry_id,
                ('; source-view UTF-16 [%d,%d)/%d, not a complete event'
                 % (window['start'], window['end'], len(content))) if partial else '',
                window['content'],
            )
            if index != position and len(text) > remaining:
                continue
            selected[entry_id] = text
            remaining -= len(text) + 1
        ordered = [(entry_id, row) for entry_id, row in rows if entry_id in selected]
        return {
            'sourceEntryIds': [entry_id for entry_id, _ in ordered],
            'content': chr(10).join(selected[entry_id] for entry_id, _ in ordered),
        }

    def grounded_episode_tags(content: str, draft: Dict[str, Any]) -> Dict[str, List[str]]:
        """上游 episode-index.ts 61-70 groundedEpisodeTags。"""
        result: Dict[str, List[str]] = {}
        for key in ('people', 'places', 'objects', 'topics', 'commitments', 'outcomes', 'dates'):
            values = _field(draft, key)
            if not is_array(values):
                continue
            grounded = _unique([
                value for value in values
                if isinstance(value, str) and len(value.strip()) >= 2 and len(value) <= 100 and value in content
            ])[:8]
            if grounded:
                result[key] = grounded
        return result

    def episode_tag_score(query: str, tags: Optional[List[str]] = None) -> float:
        """上游 episode-index.ts 73-75 episodeTagScore。"""
        return 0.8 if any(tag in query for tag in (tags or [])) else 0


def _to_ms(value: Any) -> int:
    """Date.getTime()：把 datetime/ISO 文本换算成 epoch 毫秒（无效值按 0）。"""
    parsed = parse_time(value)
    if parsed is None:
        return 0
    return int(parsed.timestamp() * 1000)


def _sticker_mime(file_path: str) -> str:
    """上游 7325-7331 stickerMime。"""
    extension = os.path.splitext(_text(file_path))[1].lower()
    if extension == '.gif':
        return 'image/gif'
    if extension == '.webp':
        return 'image/webp'
    if extension in ('.jpg', '.jpeg'):
        return 'image/jpeg'
    return 'image/png'


def _list_sticker_files(platform: Any, root: str) -> List[str]:
    """本地递归扫描贴纸库，等价上游 listStickerFiles（最多 3 层子目录、只收图片、排序）。

    平台适配：上游是递归 readdir；本项目平台适配器的 list_files 只列一层，
    而贴纸库是 emojis/<心情>/xxx 这种多级结构，所以直接用本地递归扫描。
    """
    from .service_helpers import list_sticker_files as scan_sticker_files
    return scan_sticker_files(root)


def _narrative_cursor(story: Dict[str, Any], now: datetime) -> datetime:
    """上游 8200-8206 narrativeCursor：损坏/未来游标不能把提示时间倒着填。"""
    cursor = parse_time(_field(story, 'cursorAt')) or now
    return now if cursor > now else cursor


def _format_buffered_user_messages(messages: List[Dict[str, Any]]) -> str:
    """上游 8317-8324 formatBufferedUserMessages：多条短消息合并成一次事件。"""
    if len(messages) == 1:
        return _text(messages[0].get('content'))
    parts = [
        '[连续消息 %d，收到时间 %s]\n%s' % (index + 1, iso(message.get('occurredAt')), _text(message.get('content')))
        for index, message in enumerate(messages)
    ]
    return '\n\n'.join(parts)


class ServiceMediaMixin:
    """上游 service.ts 1953-3140：能力 / 聊天动作 / 贴纸库 / 媒体 / 叙事缓冲 / 检索向量。"""

    @property
    def expression_threshold(self) -> float:
        """上游 2041-2043 expressionThreshold getter（本区间内，随本 mixin 提供）。"""
        return normalize_expression_threshold(_field(_field(self.config, 'chatActions'), 'expressionThreshold'))

    # ================= 上游 1963-1984：聊天动作能力 =================

    def group_chat_capabilities(self, session: Optional[InboundSession],
                                messages: List[Dict[str, Any]]) -> Optional[ChatActionCapabilities]:
        """上游 groupChatCapabilities：配置开启、OneBot 平台且消息含可引用 ID 时才给出能力。"""
        config = _field(self.config, 'chatActions')
        if not _field(config, 'enabled') or not session or not is_one_bot_platform(session.platform) \
                or 'qq' not in (_field(config, 'platforms') or []):
            return None
        if not any(message.get('messageRef') and message.get('messageId') for message in messages):
            return None
        quote_reply = _field(config, 'quoteReply') is True
        reactions = normalize_allowed_reactions(_field(config, 'allowedReactions')) \
            if (_field(config, 'messageReactions') is True and _reaction_api_available(self.platform, session)) else []
        native_faces = normalize_allowed_native_faces(_field(config, 'allowedNativeFaces')) \
            if _field(config, 'nativeFaces') is True else []
        if not quote_reply and not reactions and not native_faces:
            return None
        return {
            'platform': 'qq',
            'quoteReply': quote_reply,
            'reactions': reactions,
            'nativeFaces': native_faces,
            'expressionThreshold': normalize_expression_threshold(_field(config, 'expressionThreshold')),
        }

    def private_chat_capabilities(self, session: Optional[InboundSession]) -> Optional[ChatActionCapabilities]:
        """上游 privateChatCapabilities：私聊只声明原生表情。"""
        config = _field(self.config, 'chatActions')
        if not _field(config, 'enabled') or not session or not is_one_bot_platform(session.platform) \
                or 'qq' not in (_field(config, 'platforms') or []):
            return None
        native_faces = normalize_allowed_native_faces(_field(config, 'allowedNativeFaces')) \
            if _field(config, 'nativeFaces') is True else []
        if not native_faces:
            return None
        return {
            'platform': 'qq',
            'quoteReply': False,
            'reactions': [],
            'nativeFaces': native_faces,
            'expressionThreshold': normalize_expression_threshold(_field(config, 'expressionThreshold')),
        }

    # ================= 上游 1985-2034：执行群消息表情回应 =================

    def execute_group_reactions(self, story: Dict[str, Any], session: InboundSession, group_id: str,
                                reactions: List[Dict[str, Any]],
                                reference_for: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None) -> int:
        """上游 executeGroupReactions：每回合最多执行一条表情回应并写投递账本。"""
        completed = 0
        for reaction in list(reactions)[:1]:
            reference = reference_for(reaction) if callable(reference_for) else None
            platform_delivered = False
            try:
                emoji_id = _QQ_REACTION_IDS.get(_field(reaction, 'reaction'))
                if not _reaction_api_available(self.platform, session):
                    if reference:
                        self.record_platform_delivery_outcome(story.get('id'), reference, 'cancelled', 'reaction-api-unavailable')
                    continue
                delivered = self.platform.send_reaction(
                    story, group_id, _field(reaction, 'messageId') or '', emoji_id, 'add', session,
                )
                if not delivered:
                    # 平台层约定：False 表示该会话不支持消息表情回应（上游
                    # `typeof internal?.setMsgEmojiLike !== 'function'` 的 cancelled 分支）。
                    if reference:
                        self.record_platform_delivery_outcome(story.get('id'), reference, 'cancelled', 'reaction-api-unavailable')
                    continue
                platform_delivered = True
                completed_at = now_utc()

                def task(completed_at: datetime = completed_at, reaction: Dict[str, Any] = reaction,
                         reference: Optional[Dict[str, Any]] = reference) -> None:
                    self.append_entry(story.get('id'), {
                        'kind': 'character-platform-action',
                        'actor': 'character',
                        'content': '主角给群消息 %s 添加了 %s 表情回应。'
                                   % (_field(reaction, 'messageRef'), _field(reaction, 'reaction')),
                        'occurredAt': iso(completed_at),
                        'metadata': {
                            'platform': 'qq',
                            'action': 'message-reaction',
                            'groupId': group_id,
                            'messageRef': _field(reaction, 'messageRef'),
                            'reaction': _field(reaction, 'reaction'),
                            **({**dict(reference), 'deliverySegmentIndex': reference.get('segmentIndex')} if reference else {}),
                        },
                    }, completed_at)
                    if reference:
                        self.update_script_delivery_outcome(story.get('id'), reference, 'delivered', completed_at)

                self.serial(story.get('id'), task)
                completed += 1
                self.report_operation('standard', 'info', story, 'user-message',
                                      '聊天动作完成 类型=消息表情 群=%s 目标=%s 表情=%s',
                                      group_id, _field(reaction, 'messageRef'), _field(reaction, 'reaction'))
            except Exception as error:  # noqa: BLE001 - 单条表情失败不中断回合
                if reference:
                    self.record_platform_delivery_outcome(
                        story.get('id'), reference,
                        'delivered' if platform_delivered else 'failed',
                        None if platform_delivered else clip(str(error), 500),
                    )
                if platform_delivered:
                    completed += 1
                    self.report('warn', story, 'user-message',
                                '聊天动作已完成但结果记录不完整 类型=消息表情 群=%s 目标=%s 错误=%s',
                                group_id, _field(reaction, 'messageRef'), error)
                else:
                    self.report('warn', story, 'user-message',
                                '聊天动作失败 类型=消息表情 群=%s 目标=%s 错误=%s',
                                group_id, _field(reaction, 'messageRef'), error)
        return completed

    # ================= 上游 2035-2057：本地媒体与原生表情解析 =================

    def resolve_sticker(self, draft: Optional[Dict[str, Any]],
                        catalog: List[StickerCatalogEntry]) -> Optional[StickerAsset]:
        """上游 resolveSticker：草稿必须在目录内且意愿度过表达阈值。"""
        if not draft or not isinstance(draft.get('assetId'), str) \
                or not isinstance(draft.get('willingness'), (int, float)) or isinstance(draft.get('willingness'), bool) \
                or not any(item.get('assetId') == draft.get('assetId') for item in catalog):
            return None
        if normalize_expression_threshold(draft.get('willingness')) < self.expression_threshold:
            return None
        return self.sticker_by_id.get(draft.get('assetId'))

    def resolve_native_face(self, decision: Dict[str, Any],
                            capabilities: Optional[ChatActionCapabilities]) -> Optional[str]:
        """上游 resolveNativeFace：只接受能力声明内的语义，且必须有可见文本呼应。"""
        allowed = set(_field(capabilities, 'nativeFaces') or [])
        if not allowed:
            return None
        draft = decision.get('nativeFace')
        group_reply = decision.get('groupReply') or {}
        reply_content = group_reply.get('content')
        if reply_content is None:
            interaction = decision.get('interaction') or {}
            reply = interaction.get('reply') or {}
            reply_content = reply.get('content')
        if reply_content is None:
            reply_content = ''
        threshold = _field(capabilities, 'expressionThreshold') if _field(capabilities, 'expressionThreshold') is not None \
            else self.expression_threshold
        if draft and draft.get('semantic') in allowed \
                and calibrated_native_face_willingness(draft.get('semantic'), draft.get('willingness'), reply_content) >= threshold:
            return draft.get('semantic')
        # 旧版方括号标签由兼容层从可见文本中解析，但没有声明意愿度，因此永不绕过表达阈值。
        return None

    # ================= 上游 2059-2145：发送本地表情包 / 原生表情 =================

    def send_sticker(self, story: Dict[str, Any], session: InboundSession, channel_id: str,
                     asset: StickerAsset, group_id: Optional[str] = None,
                     reference: Optional[Dict[str, Any]] = None) -> bool:
        """上游 sendSticker：路径必须落在贴纸库根目录内，成功写 character-platform-action。"""
        root = os.path.abspath(os.path.join(_text(getattr(self.ctx, 'base_dir', '')), self.sticker_config.get('directory') or ''))
        file_path = os.path.abspath(os.path.join(root, asset.get('filePath') or ''))
        relative_path = os.path.relpath(file_path, root)
        if not relative_path or relative_path == '..' or relative_path.startswith('..' + os.sep) or ':' in relative_path:
            if reference:
                self.record_platform_delivery_outcome(story.get('id'), reference, 'cancelled', 'invalid-sticker-path')
            return False
        platform_delivered = False
        try:
            self.platform.send_sticker(story, channel_id, file_path, session)
            platform_delivered = True
            now = now_utc()

            def task(now: datetime = now, asset: StickerAsset = asset,
                     reference: Optional[Dict[str, Any]] = reference) -> None:
                self.append_entry(story.get('id'), {
                    'kind': 'character-platform-action',
                    'actor': 'character',
                    'content': '主角发送了本地表情包：%s' % asset.get('description'),
                    'occurredAt': iso(now),
                    'metadata': {
                        'platform': session.platform,
                        'action': 'local-sticker',
                        'assetId': asset.get('assetId'),
                        'group': asset.get('group'),
                        'animated': asset.get('animated'),
                        **({'groupId': group_id} if group_id else {}),
                        **({**dict(reference), 'deliverySegmentIndex': reference.get('segmentIndex')} if reference else {}),
                    },
                }, now)
                if reference:
                    self.update_script_delivery_outcome(story.get('id'), reference, 'delivered', now)

            self.serial(story.get('id'), task)
            self.report_operation('standard', 'info', story, 'user-message',
                                  '聊天动作完成 类型=本地表情包 素材=%s', asset.get('assetId'))
            return True
        except Exception as error:  # noqa: BLE001 - 平台失败按上游 warn 分支处理
            if reference:
                self.record_platform_delivery_outcome(
                    story.get('id'), reference,
                    'delivered' if platform_delivered else 'failed',
                    None if platform_delivered else clip(str(error), 500),
                )
            if platform_delivered:
                self.report('warn', story, 'user-message',
                            '聊天动作已完成但结果记录不完整 类型=本地表情包 素材=%s 错误=%s', asset.get('assetId'), error)
                return True
            self.report('warn', story, 'user-message', '聊天动作失败 类型=本地表情包 素材=%s 错误=%s', asset.get('assetId'), error)
            return False

    def send_native_face(self, story: Dict[str, Any], session: InboundSession, channel_id: str,
                         semantic: str, group_id: Optional[str] = None,
                         reference: Optional[Dict[str, Any]] = None) -> bool:
        """上游 sendNativeFace。

        OneBot 11 规范 face.id 是 int32；字符串 id 会被严格校验的实现直接拒绝
        （"is not a valid segment"）。宽容实现两者都收，统一发数字。
        """
        platform_delivered = False
        try:
            face_id = int(_QQ_NATIVE_FACE_IDS.get(semantic))
            self.platform.send_native_face(story, channel_id, face_id, session)
            platform_delivered = True
            now = now_utc()

            def task(now: datetime = now, reference: Optional[Dict[str, Any]] = reference) -> None:
                self.append_entry(story.get('id'), {
                    'kind': 'character-platform-action',
                    'actor': 'character',
                    'content': '主角发送了 %s 原生表情。' % semantic,
                    'occurredAt': iso(now),
                    'metadata': {
                        'platform': session.platform,
                        'action': 'native-face',
                        'semantic': semantic,
                        **({'groupId': group_id} if group_id else {}),
                        **({**dict(reference), 'deliverySegmentIndex': reference.get('segmentIndex')} if reference else {}),
                    },
                }, now)
                if reference:
                    self.update_script_delivery_outcome(story.get('id'), reference, 'delivered', now)

            self.serial(story.get('id'), task)
            self.report_operation('standard', 'info', story, 'user-message',
                                  '聊天动作完成 类型=原生表情 语义=%s', semantic)
            return True
        except Exception as error:  # noqa: BLE001 - 平台失败按上游 warn 分支处理
            if reference:
                self.record_platform_delivery_outcome(
                    story.get('id'), reference,
                    'delivered' if platform_delivered else 'failed',
                    None if platform_delivered else clip(str(error), 500),
                )
            if platform_delivered:
                self.report('warn', story, 'user-message',
                            '聊天动作已完成但结果记录不完整 类型=原生表情 语义=%s 错误=%s', semantic, error)
                return True
            self.report('warn', story, 'user-message', '聊天动作失败 类型=原生表情 语义=%s 错误=%s', semantic, error)
            return False

    # ================= 上游 2147-2187：群消息投递 =================

    def send_group_message(self, story: Dict[str, Any], channel_id: str, content: str,
                           reply_to_message_id: Optional[str] = None,
                           session: Optional[InboundSession] = None) -> Dict[str, Any]:
        """上游 sendGroupMessage：现场群会话优先，其次按故事账号找机器人。"""
        # The live group session is authoritative: it already represents the bot
        # account that received this exact group message. Shared stories may have
        # been created under an old account or platform and must not override it.
        session_bot = getattr(session, 'bot', None) if session is not None else None
        bot = session_bot if session_bot is not None else self.platform.find_bot(
            _field(story, 'platform'), _field(story, 'selfId'))
        segments = self.split_outgoing_message(content)
        if not bot:
            self.report('warn', story, 'user-message',
                        '没有可用机器人账号投递群消息 群频道=%s 故事平台=%s 故事账号=%s',
                        channel_id, _field(story, 'platform'), _field(story, 'selfId'))
            return {
                'deliveredSegments': [],
                'complete': False,
                'segmentOutcomes': [
                    {'index': index, 'content': segment, 'status': 'failed', 'reason': 'bot-not-found'}
                    for index, segment in enumerate(segments)
                ],
            }
        all_delivered = True
        delivered_segments: List[str] = []
        segment_outcomes: List[Dict[str, Any]] = []
        for index, segment in enumerate(segments):
            try:
                self.platform.send_group(
                    story, channel_id, segment,
                    reply_to_message_id if (index == 0 and reply_to_message_id) else None,
                    session,
                )
                delivered_segments.append(segment)
                segment_outcomes.append({'index': index, 'content': segment, 'status': 'delivered'})
            except Exception as error:  # noqa: BLE001 - 单段失败继续投递后续分段
                all_delivered = False
                segment_outcomes.append({'index': index, 'content': segment, 'status': 'failed', 'reason': clip(str(error), 500)})
                self.report('warn', story, 'user-message', '群消息投递失败 群频道=%s 错误=%s', channel_id, error)
        return {'deliveredSegments': delivered_segments, 'complete': all_delivered, 'segmentOutcomes': segment_outcomes}

    # ================= 上游 2189-2220：短时消息合并缓冲 =================

    def buffer_user_narrative(self, story: Dict[str, Any], participant: Dict[str, Any],
                              session: InboundSession, now: datetime,
                              superseded_intents: List[Dict[str, Any]],
                              content: Optional[str] = None,
                              image_sources: Optional[List[str]] = None,
                              audio_sources: Optional[List[str]] = None,
                              quote: Optional[Dict[str, Any]] = None) -> None:
        """上游 bufferUserNarrative：持久化后的消息先短暂驻留，凑成一次事件。"""
        if content is None:
            content = _text(getattr(session, 'content', ''))
        key = participant.get('id')
        existing = self.buffered_narrative_turns.get(key)
        turn = existing if existing is not None else {
            'storyId': story.get('id'),
            'participantId': participant.get('id'),
            'messages': [],
            'nextRevision': 0,
            'obsoleteRequestIds': set(),
        }
        if should_supersede_narrative_request(turn.get('inFlightRequestId'), turn.get('firstMessageCommittedRequestId'),
                                              turn.get('obsoleteRequestIds') or set()):
            (turn.get('obsoleteRequestIds') or set()).add(turn.get('inFlightRequestId'))
            self.report_operation('standard', 'info', story, 'user-message',
                                  '新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d',
                                  participant.get('id'), turn.get('inFlightRequestId'))
        message = {
            'content': content,
            'occurredAt': now,
            'supersededIntents': superseded_intents,
            'imageSources': image_sources or [],
            'audioSources': audio_sources or [],
        }
        if quote:
            message['quote'] = quote
        turn['messages'].append(message)
        turn['latestSession'] = session
        if turn.get('timer'):
            self.ctx.clear_timer(turn['timer'])
        turn['nextRevision'] = (turn.get('nextRevision') or 0) + 1
        revision = turn['nextRevision']
        debounce = _field(_field(self.config, 'runtime'), 'userMessageDebounceSeconds', 2)
        delay = max(0, 2 if debounce is None else debounce) * 1000
        # 先登记再定时：delay=0 时线程可能在赋值前就跑 flush（上游 setTimeout 是宏任务，
        # 当前同步函数必然先跑完；Python threading.Timer(0) 没有这个保证）。
        self.buffered_narrative_turns[key] = turn
        turn['timer'] = self.ctx.set_timeout(lambda: self.flush_buffered_narrative(key, revision), delay)
        self.report_operation('diagnostic', 'debug', story, 'user-message',
                              '短时消息合并 参与者=%s 待处理=%d 等待=%dms',
                              participant.get('id'), len(turn['messages']), delay)

    def signal_incoming_interruption(self, story: Dict[str, Any], participant: Dict[str, Any]) -> None:
        """上游 signalIncomingInterruption：新输入打断正在生成的回复链。"""
        self.interrupted_typing_participants.add(participant.get('id'))
        turn = self.buffered_narrative_turns.get(participant.get('id'))
        if not turn or not should_supersede_narrative_request(turn.get('inFlightRequestId'),
                                                              turn.get('firstMessageCommittedRequestId'),
                                                              turn.get('obsoleteRequestIds') or set()):
            return
        (turn.get('obsoleteRequestIds') or set()).add(turn.get('inFlightRequestId'))
        self.report_operation('standard', 'info', story, 'user-message',
                              '新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d',
                              participant.get('id'), turn.get('inFlightRequestId'))

    # ================= 上游 2221-2253：实验性流式首条回复 =================

    def deliver_early_private_reply(self, story: Dict[str, Any], participant: Dict[str, Any],
                                    session: Optional[InboundSession], turn: Dict[str, Any],
                                    request_id: int, reply: Dict[str, Any]) -> Any:
        """上游 deliverEarlyPrivateReply：只有完整校验通过的私聊首条回复可以提前离开。"""
        if reply.get('kind') != 'private':
            return False
        interaction = reply.get('interaction')
        if not interaction or _field(_field(interaction, 'reply'), 'mode') != 'immediate':
            return False
        if turn.get('nextRevision') != request_id or request_id in (turn.get('obsoleteRequestIds') or set()) \
                or turn.get('firstMessageCommittedRequestId') == request_id:
            return False
        if not self.can_handle_participant(participant):
            return False
        # 早发内容与常规投递共用同一可见文本合约：长度上限与括号表情标签清理一致。
        runtime = _field(self.config, 'runtime')
        content = normalize_visible_message_content(
            _field(_field(interaction, 'reply'), 'content'),
            _field(runtime, 'maxMessageCharacters'),
            _field(runtime, 'messageSeparator') or '<sep/>',
        )
        if not content or len(self.split_outgoing_message(content)) != 1:
            return False
        delivered = self.send_outgoing_messages(story, [{
            'participantId': participant.get('id'),
            'content': content,
            'interaction': interaction,
            'userInitiated': True,
        }], participant, session)
        if not delivered:
            return False
        confirmed = self.confirm_outgoing_deliveries(story, delivered)
        if not confirmed:
            return False
        turn['firstMessageCommittedRequestId'] = request_id
        self.report_operation('standard', 'info', story, 'user-message',
                              '实验性流式首条回复已提前投递 参与者=%s 请求=%d', participant.get('id'), request_id)
        return confirmed[0]

    # ================= 上游 2276-2301：用户事件描述 =================

    def describe_user_event(self, story: Dict[str, Any], session: InboundSession) -> Dict[str, Any]:
        """上游 describeUserEvent：文字、图片、语音折叠成一条可读事实。"""
        visual = self.describe_vision_event(session)
        audio_sources = extract_session_audio_sources(session)
        files = extract_session_file_facts(session)
        audio_files = [item for item in files if item.get('audio')]
        plain_files = [item for item in files if not item.get('audio')]
        content = visual.get('content')
        if not content:
            if visual.get('sources'):
                content = '[用户发送了图片；图片内容以本轮视觉输入为准，未提供视觉内容时保持未知。]'
            elif audio_sources:
                if audio_files:
                    content = '[用户发送了音频文件：%s；音频内容以本轮原生音频输入为准，未提供音频内容时保持未知。]' \
                        % '、'.join([item.get('name') or '音频文件' for item in audio_files])
                else:
                    content = '[用户发送了一条语音；语音内容以本轮原生音频输入为准，未提供音频内容时保持未知。]'
            elif plain_files:
                content = '[用户发送了文件：%s；文件内容未知。]' \
                    % '、'.join([item.get('name') or '未命名文件' for item in plain_files])
            else:
                content = ''
        elif plain_files:
            # 有文字时音频走原生通道（currentEvent.audioCount），普通文件没有
            # 独立通道，必须以文字事实补记，否则模型不知道有文件到达。
            content = '%s\n[用户同时发送了文件：%s；文件内容未知。]' \
                % (content, '、'.join([item.get('name') or '未命名文件' for item in plain_files]))
        return {
            'content': content,
            'sources': visual.get('sources') or [],
            'audioSources': audio_sources,
            'quote': describe_quoted_message(session, _config_get(story, 'setting', 'character', 'name', default='主角')),
        }

    # ================= 上游 2303-2384：贴纸库扫描与描述 =================

    def scan_sticker_library(self) -> None:
        """上游 scanStickerLibrary：扫描贴纸目录、登记素材、调用视觉模型描述。"""
        config = self.sticker_config
        if not config.get('enabled') or self.sticker_scan_running:
            return
        self.sticker_scan_running = True
        try:
            root = os.path.abspath(os.path.join(_text(getattr(self.ctx, 'base_dir', '')), config.get('directory') or ''))
            files = _list_sticker_files(self.platform, root)
            existing = self.db_get('interlude_sticker', {})
            by_path = {item.get('filePath'): item for item in existing}
            seen: Set[str] = set()
            pending: List[Dict[str, Any]] = []
            for file in files:
                file_path = os.path.relpath(file, root).replace(os.sep, '/')
                if not file_path or file_path.startswith('../'):
                    continue
                seen.add(file_path)
                try:
                    if self.platform.file_size(file) > config.get('maxFileSizeMB') * 1024 * 1024:
                        continue
                    bytes_ = self.platform.read_file(file)
                    if bytes_ is None:
                        continue
                    hash_ = hashlib.sha256(bytes_).hexdigest()
                    prior = by_path.get(file_path)
                    if prior and prior.get('hash') == hash_ and prior.get('status') == 'active':
                        continue
                    if prior and prior.get('hash') == hash_ and prior.get('status') == 'pending' \
                            and _now_ms() - _to_ms(prior.get('updatedAt')) < _STICKER_DESCRIPTION_RETRY_COOLDOWN_MS:
                        continue
                    group = file_path.split('/')[0] if '/' in file_path else 'default'
                    asset_id = stable_sticker_asset_id(file_path, hash_)
                    now = now_utc()
                    base = {
                        'assetId': asset_id,
                        'filePath': file_path,
                        'group': group[:128],
                        'mimeType': _sticker_mime(file_path),
                        'animated': re.search(r'\.gif$', file_path, re.IGNORECASE) is not None,
                        'size': len(bytes_),
                        'hash': hash_,
                        'description': '',
                        'aliases': [],
                        'status': 'pending',
                        'updatedAt': now,
                    }
                    if prior:
                        self.db_set('interlude_sticker', {'id': prior.get('id')}, base)
                        asset = {**prior, **base}
                    else:
                        asset = self.db_create('interlude_sticker', {**base, 'createdAt': now})
                    pending.append({'asset': asset, 'bytes': bytes_})
                except Exception as error:  # noqa: BLE001 - 单个坏文件不该饿死整库
                    # A malformed file, old duplicate row, or transient SQLite write must
                    # not starve the rest of the library for another five-minute cycle.
                    self.report_standalone_operation('standard', 'warn',
                                                     '表情包素材跳过 文件=%s 错误=%s', file_path, error)
            for asset in existing:
                if asset.get('status') != 'missing' and asset.get('filePath') not in seen:
                    self.db_set('interlude_sticker', {'id': asset.get('id')},
                                {'status': 'missing', 'updatedAt': now_utc()})
            if pending and not self.sticker_describer.available():
                self.report_standalone('warn', '表情包库发现新素材，但没有配置 useForStickers 的视觉模型；已等待描述。')
            for item in pending[:5]:
                asset = item['asset']
                if not self.sticker_describer.available():
                    break
                try:
                    visual = self.image_bytes_to_native(item['bytes'], asset.get('mimeType'))
                    description = self.sticker_describer.describe_sticker(
                        visual.get('dataUri'), visual.get('mimeType'), asset.get('filePath'), asset.get('animated'),
                        config.get('descriptionResponseFormat'), config.get('descriptionMaxTokens'),
                    ) if visual else None
                except Exception as error:  # noqa: BLE001 - 描述失败进入冷却重试
                    self.report_standalone_operation('standard', 'warn', '表情包描述失败，已冷却后重试 素材=%s 错误=%s',
                                                     asset.get('assetId'), error)
                    self.db_set('interlude_sticker', {'id': asset.get('id')}, {'updatedAt': now_utc()})
                    continue
                if not description:
                    self.report_standalone_operation('standard', 'warn', '表情包描述未返回可用 JSON，已冷却后重试 素材=%s',
                                                     asset.get('assetId'))
                    self.db_set('interlude_sticker', {'id': asset.get('id')}, {'updatedAt': now_utc()})
                    continue
                self.db_set('interlude_sticker', {'id': asset.get('id')}, {
                    'description': description.get('description'),
                    'aliases': description.get('aliases'),
                    'status': 'active',
                    'updatedAt': now_utc(),
                })
                if self.semantic_sticker_embedding_enabled():
                    # Index a freshly described asset immediately so the semantic filter
                    # can consider it on the very next turn instead of the next scan.
                    embedding = self.embed_text(('%s %s' % (description.get('description'),
                                                           ' '.join(description.get('aliases') or []))).strip())
                    if embedding:
                        self.db_set('interlude_sticker', {'id': asset.get('id')},
                                    {'embedding': embedding, 'updatedAt': now_utc()})
                self.report_standalone_operation('standard', 'info', '表情包描述完成 素材=%s 分组=%s',
                                                 asset.get('assetId'), asset.get('group'))
            self.refresh_sticker_catalog()
            self.backfill_sticker_embeddings()
            self.refresh_sticker_catalog()
        except Exception as error:  # noqa: BLE001 - 整库扫描失败只记日志
            self.report_standalone('warn', '表情包库扫描失败：%s', error)
        finally:
            self.sticker_scan_running = False

    def refresh_sticker_catalog(self) -> None:
        """上游 refreshStickerCatalog：重载 active 素材并按 assetId 建索引。"""
        rows = self.db_get('interlude_sticker', {'status': 'active'}, {'sort': {'updatedAt': 'desc'}})
        self.sticker_catalog = rows
        self.sticker_by_id = {item.get('assetId'): item for item in rows}

    def semantic_sticker_embedding_enabled(self) -> bool:
        """上游 semanticStickerEmbeddingEnabled。"""
        return _config_get(self.config, 'model', 'embedding', 'semanticStickerFilter') is True

    def backfill_sticker_embeddings(self) -> None:
        """上游 backfillStickerEmbeddings：每轮最多补 8 条已描述未索引素材。"""
        if not self.semantic_sticker_embedding_enabled():
            return
        pending = [asset for asset in self.sticker_catalog
                   if asset.get('description') and not asset.get('embedding')][:8]
        for asset in pending:
            text = ('%s %s' % (asset.get('description'), ' '.join(asset.get('aliases') or []))).strip()
            embedding = self.embed_text(text)
            if embedding:
                self.db_set('interlude_sticker', {'id': asset.get('id')},
                            {'embedding': embedding, 'updatedAt': now_utc()})

    # ================= 上游 2410-2438：贴纸目录与语义排序 =================

    def sticker_catalog_for_session(self, session: Optional[InboundSession],
                                    turn_query_embedding: Optional[List[float]] = None) -> List[StickerCatalogEntry]:
        """上游 stickerCatalogForSession：只向 OneBot 会话语境暴露贴纸目录。"""
        config = self.sticker_config
        if not config.get('enabled') or not session:
            return []
        # 平台适配：上游只向 OneBot 暴露贴纸目录；微信侧同样实现了 send_sticker 与本地贴纸库，
        # 这里放开，让角色也能使用本地表情包（偷来的表情包也靠这个进入模型目录）。
        if not is_one_bot_platform(session.platform) and session.platform != 'wechat':
            return []
        assets = self.rank_sticker_assets(turn_query_embedding)
        return [{
            'assetId': asset.get('assetId'),
            'group': asset.get('group'),
            'description': asset.get('description'),
            'aliases': asset.get('aliases') if is_array(asset.get('aliases')) else [],
            'animated': asset.get('animated'),
        } for asset in assets]

    def rank_sticker_assets(self, turn_query_embedding: Optional[List[float]] = None) -> List[StickerAsset]:
        """上游 rankStickerAssets：语义筛选只在开启且目录超过 12 条时生效。"""
        assets = self.sticker_catalog[:self.sticker_config.get('catalogLimit')]
        if not _config_get(self.config, 'model', 'embedding', 'semanticStickerFilter'):
            return assets
        if not turn_query_embedding or len(assets) <= _SEMANTIC_STICKER_LIMIT:
            return assets
        return rank_sticker_catalog(assets, turn_query_embedding, _SEMANTIC_STICKER_LIMIT)

    def semantic_turn_embedding_enabled(self) -> bool:
        """上游 semanticTurnEmbeddingEnabled。"""
        return should_request_turn_embedding(
            _config_get(self.config, 'model', 'embedding'),
            self.sticker_config.get('enabled'),
            len(self.sticker_catalog),
        )

    # ================= 上游 2440-2464：场景摘要与工作细节修剪 =================

    def previous_scene_summaries(self, story_id: str) -> List[Dict[str, Any]]:
        """上游 previousSceneSummaries：活跃场景之前的已关闭场景摘要。"""
        limit = self.memory_config.get('previousSceneSummaries')
        if not limit or not self.memory_config.get('enabled'):
            return []
        rows = self.db_get('interlude_scene', {'storyId': story_id, 'status': 'closed'},
                           {'limit': limit, 'sort': {'endedAt': 'desc'}})
        summaries: List[Dict[str, Any]] = []
        for scene in rows:
            summary = _text(scene.get('summary')).strip()
            if not summary or not scene.get('endedAt'):
                continue
            summaries.append({
                'startedAt': iso(scene.get('startedAt')),
                'endedAt': iso(scene.get('endedAt') or scene.get('startedAt')),
                'summary': summary[:2000],
            })
        return summaries

    def prune_working_details(self, details: Optional[List[Dict[str, Any]]],
                              now: datetime) -> Optional[List[Dict[str, Any]]]:
        """上游 pruneWorkingDetails：过期条目静默丢弃，最多保留 10 条。"""
        if not details:
            return None
        live: List[Dict[str, Any]] = []
        for item in details:
            expires_at = item.get('expiresAt')
            if not expires_at:
                live.append(item)
                continue
            parsed = parse_time(expires_at)
            if parsed is not None and parsed > now:
                live.append(item)
        return live[-10:] if live else None

    # ================= 上游 2466-2537：史官剧本的多通道召回 =================

    def recall_history(self, story_id: str, participant_id: str, query: str,
                       turn_query_embedding: List[float], exclude_ids: Set[int],
                       preferred_entry_ids: Optional[Set[int]] = None,
                       max_results: int = 3) -> List[Dict[str, Any]]:
        """上游 recallHistory：Embeddings 只改善排序，字面措辞与事实出处始终可用。"""
        preferred_entry_ids = preferred_entry_ids if preferred_entry_ids is not None else set()
        self.ensure_history_vectors(story_id)
        cache = self.history_vectors.get(story_id)
        if not cache:
            return []
        share_details = bool(participant_id) and bool(_field(self.shared_story_config, 'shareParticipantDetails'))
        cache_key = json.dumps([
            participant_id, query, list(exclude_ids), list(preferred_entry_ids), share_details,
        ], ensure_ascii=False)
        memo = self.automatic_recall_cache.get(story_id)
        if max_results == 1 and memo and memo.get('source') is cache and memo.get('size') == len(cache) \
                and memo.get('key') == cache_key and memo.get('until', 0) > _now_ms():
            return [{**item, 'sourceEntryIds': list(item.get('sourceEntryIds') or [])}
                    for item in (memo.get('result') or [])]
        scored: List[Dict[str, Any]] = []
        keys = recall_keys(query)[:120]
        primary_keys = recall_keys(_text(query).split('\n')[0])[:80]
        identity = _embedder_identity(self.embedder)
        for entry_id, item in cache.items():
            if entry_id in exclude_ids:
                continue
            if not is_history_entry_visible_to_participant(item, participant_id, share_details):
                continue
            semantic: Optional[float] = None
            if not item.get('embeddingIdentity') or item.get('embeddingIdentity') == identity:
                semantic = cosine_similarity(turn_query_embedding, item.get('vector') or [])
            spans = item.get('spans')
            if not spans:
                spans = index_original(_text(item.get('content')))
                item['spans'] = spans
            lexical = max(
                score_original(primary_keys, spans).get('score', 0),
                score_original(keys, spans).get('score', 0) * 0.8,
            )
            tagged = episode_tag_score(query, item.get('tags'))
            source = 1 if entry_id in preferred_entry_ids else 0
            if source == 0 and tagged == 0 and (semantic is None or semantic < 0.25) and lexical < 0.12:
                continue
            # Prose similarity alone often recalls a familiar gesture rather than
            # the event. Literal/source matches retain priority over writing style.
            scored.append({
                'id': entry_id,
                'score': source * 2 + tagged
                + max(0.0, semantic or 0.0) * (0.45 if item.get('kind') == 'script' else 1)
                + lexical,
            })
        visible = [(entry_id, item) for entry_id, item in cache.items()
                   if entry_id not in exclude_ids
                   and is_history_entry_visible_to_participant(item, participant_id, share_details)]
        visible.sort(key=lambda pair: (_text(pair[1].get('occurredAt')), pair[0]))
        episodes = build_episode_index(visible)
        positions = {entry_id: index for index, (entry_id, _) in enumerate(visible)}
        used: Set[int] = set()
        result: List[Dict[str, Any]] = []
        for anchor in sorted(scored, key=lambda item: (-item['score'], -item['id'])):
            position = positions.get(anchor['id'])
            if position is None or anchor['id'] in used:
                continue
            cache_entry = cache.get(anchor['id']) or {}
            episode_ids = episodes.get(anchor['id']) or [anchor['id']]
            if len(episode_ids) > 1:
                neighborhood = [pair for pair in visible if pair[0] in episode_ids]
            else:
                neighborhood = [pair for pair in visible
                                if pair[1].get('participantId') == cache_entry.get('participantId')]
            excerpt = episode_excerpt(neighborhood, anchor['id'], 2400 if max_results == 1 else 4000, keys)
            if not excerpt:
                continue
            source_entry_ids = excerpt.get('sourceEntryIds') or []
            for entry_id in source_entry_ids:
                used.add(entry_id)
            result.append({
                'id': anchor['id'],
                'occurredAt': cache_entry.get('occurredAt'),
                'content': excerpt.get('content'),
                'sourceEntryIds': source_entry_ids,
            })
            if len(result) >= max_results:
                break
        if max_results == 1:
            self.automatic_recall_cache[story_id] = {
                'source': cache,
                'size': len(cache),
                'key': cache_key,
                'until': _now_ms() + 60_000,
                'result': result,
            }
        return result

    # ================= 上游 2539-2571：史官向量缓存的装载 =================

    def ensure_history_vectors(self, story_id: str) -> None:
        """上游 ensureHistoryVectors：整表装载一条故事的全部可召回条目（进程内一次性）。"""
        pending = self.history_vector_loads.get(story_id)
        if pending is not None:
            return pending
        if story_id in self.history_vectors_ready:
            return None
        cache: Dict[int, Dict[str, Any]] = {}
        self.history_vectors[story_id] = cache
        try:
            rows = self.db_get('interlude_script_entry', {'storyId': story_id})
            for row in rows:
                if row.get('kind') not in _RECALLABLE_ENTRY_KINDS:
                    continue
                if row.get('id') in cache:
                    continue  # a live append/backfill won the race
                metadata = row.get('metadata') if is_record(row.get('metadata')) else {}
                entry: Dict[str, Any] = {
                    'embeddingIdentity': metadata.get('embeddingIdentity') if isinstance(metadata.get('embeddingIdentity'), str) else None,
                    'tags': [tag for values in grounded_episode_tags(row.get('content'), metadata.get('episodeTags') or {}).values() for tag in values],
                    'checkpoint': metadata.get('sceneCheckpoint'),
                    'frameId': metadata.get('frameId') if isinstance(metadata.get('frameId'), str) else None,
                    'content': prompt_visible_message_content(row.get('content'), recent_script_ownership(row)),
                    'occurredAt': iso(row.get('occurredAt')),
                    'participantId': row.get('participantId'),
                    'kind': row.get('kind'),
                }
                if row.get('embedding'):
                    entry['vector'] = row.get('embedding')
                cache[row.get('id')] = entry
            if self.history_vectors.get(story_id) is cache:
                self.history_vectors_ready.add(story_id)
        except Exception as error:  # noqa: BLE001 - 缓存装载失败只降级为下一轮重试
            self.history_vectors_ready.discard(story_id)
            self.report_standalone_operation('diagnostic', 'debug', '历史向量缓存加载失败 错误=%s', error)

    def invalidate_history_vectors(self, story_id: Optional[str] = None) -> None:
        """上游 invalidateHistoryVectors：行被删除/改写时丢弃进程内副本。"""
        if story_id:
            self.history_vectors.pop(story_id, None)
            self.history_vectors_ready.discard(story_id)
            self.history_vector_loads.pop(story_id, None)
            self.automatic_recall_cache.pop(story_id, None)
            return
        self.history_vectors.clear()
        self.history_vectors_ready.clear()
        self.history_vector_loads.clear()
        self.automatic_recall_cache.clear()

    # ================= 上游 2590-2649：史官向量的后台回填 =================

    def backfill_history_embeddings(self, story_id: str) -> None:
        """上游 backfillHistoryEmbeddings：新条目优先，整表在多次维护中逐步补齐。"""
        embedding_config = _config_get(self.config, 'model', 'embedding')
        if not _field(embedding_config, 'enabled') or not _field(embedding_config, 'semanticHistory'):
            return
        raw_batch = _field(embedding_config, 'backfillBatchSize')
        try:
            number = float(5 if raw_batch is None else raw_batch)
        except (TypeError, ValueError):
            number = math.nan
        if isinstance(raw_batch, bool) or math.isnan(number) or math.isinf(number):
            number = math.nan
        batch_size = 0 if math.isnan(number) else min(20, max(0, int(math.floor(number))))
        if not batch_size or story_id in self.history_backfills \
                or _now_ms() < (self.history_backoff.get(story_id) or 0):
            return
        self.history_backfills.add(story_id)
        started = _now_ms()
        try:
            identity = _embedder_identity(self.embedder)
            story = self.get_story(story_id)
            state = decode_story_state(_field(story, 'state'))
            saved = _field(_field(state, 'extensions'), 'historyBackfill')
            cursor = 0
            if is_record(saved) and saved.get('identity') == identity and _is_safe_integer(saved.get('cursor')):
                cursor = max(0, saved.get('cursor'))
            older = self.db_get('interlude_script_entry', {'storyId': story_id, 'id': {'$gt': cursor}},
                                {'limit': 128, 'sort': {'id': 'asc'}})
            recent = self.db_get('interlude_script_entry', {'storyId': story_id},
                                 {'limit': 64, 'sort': {'id': 'desc'}})

            def needs_vector(row: Dict[str, Any]) -> bool:
                if row.get('kind') not in _RECALLABLE_ENTRY_KINDS or not _text(row.get('content')).strip():
                    return False
                return not row.get('embedding') or (bool(identity) and _field(row.get('metadata'), 'embeddingIdentity') != identity)

            attempts = 0
            completed: Set[int] = set()

            def fill(row: Dict[str, Any]) -> bool:
                nonlocal attempts
                attempts += 1
                embedding = self.embed_text(_text(row.get('content')))
                if not embedding:
                    self.history_backoff[story_id] = _now_ms() + 60_000
                    return False
                if _embedder_identity(self.embedder) != identity:
                    return False

                def task() -> None:
                    fresh_rows = self.db_get('interlude_script_entry', {'storyId': story_id, 'id': row.get('id')})
                    fresh = fresh_rows[0] if fresh_rows else None
                    if not fresh or fresh.get('content') != row.get('content'):
                        return
                    metadata = fresh.get('metadata') if is_record(fresh.get('metadata')) else {}
                    self.db_set('interlude_script_entry', {'storyId': story_id, 'id': row.get('id')}, {
                        'embedding': embedding,
                        'metadata': {**metadata, **({'embeddingIdentity': identity} if identity else {})},
                    })
                    cached = (self.history_vectors.get(story_id) or {}).get(row.get('id'))
                    if cached:
                        cached['vector'] = embedding
                        cached['embeddingIdentity'] = identity

                self.serial(story_id, task)
                completed.add(row.get('id'))
                return True
            # Reserve one live slot only when an archive slot remains. Batch=1 still progresses through history.
            live = next((row for row in recent if needs_vector(row)), None) if batch_size > 1 else None
            if live and not fill(live):
                return
            for row in older:
                if needs_vector(row) and row.get('id') not in completed:
                    if attempts >= batch_size or _now_ms() - started >= 20_000:
                        break
                    if not fill(row):
                        break
                cursor = row.get('id')
            if not older:
                cursor = 0

            def persist_cursor() -> None:
                fresh = self.get_story(story_id)
                fresh_state = decode_story_state(_field(fresh, 'state'))
                extensions = _field(fresh_state, 'extensions')
                extensions = dict(extensions) if is_record(extensions) else {}
                self.db_set('interlude_story', {'id': story_id}, {
                    'state': encode_story_state({
                        **fresh_state,
                        'extensions': {**extensions, 'historyBackfill': {'cursor': cursor, 'identity': identity}},
                    }),
                })

            self.serial(story_id, persist_cursor)
        finally:
            self.history_backfills.discard(story_id)

    # ================= 上游 2651-2717：原生语音（含 SnowLuma 服务端转码） =================

    def load_native_audio(self, story: Dict[str, Any], sources: List[str],
                          session: Optional[InboundSession] = None) -> List[NarrativeAudio]:
        """上游 loadNativeAudio：语音作为原生音频附件随本轮输入下发。"""
        config = self.audio_config
        if not config.get('enabled') or not sources:
            return []
        audio: List[NarrativeAudio] = []
        for index, source in enumerate(sources[:config.get('maxPerMessage')]):
            try:
                item = self.fetch_native_audio(source, session)
                if item:
                    audio.append({'id': 'turn-audio-%d' % (index + 1), **item})
            except Exception as error:  # noqa: BLE001 - 语音读取失败只保留事实
                self.report('warn', story, 'user-message', '语音读取失败，已保留语音事实 错误=%s', error)
        return audio

    def fetch_native_audio(self, source: str,
                           session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        """上游 fetchNativeAudio：OneBot file token 必须走服务端转码，其余走平台抓取。

        平台映射：`internal._request('get_record', ...)` 与 `ctx.http.get(...)` 统一收敛为
        PlatformAdapter.fetch_audio(source, session)（30 秒超时由平台层负责）。
        """
        config = self.audio_config
        out_format = config.get('outFormat') or 'mp3'
        max_bytes = config.get('maxFileSizeMB') * 1024 * 1024
        value = _text(source).strip()
        if value.startswith('onebot-file:'):
            if not value[len('onebot-file:'):]:
                return None
            item = self.platform.fetch_audio(value, session)
            if not item:
                return None
            base64_text = re.sub(r'\s+', '', _text(item.get('base64')))
            if not base64_text or _base64_byte_length(base64_text) > max_bytes:
                return None
            return {'format': _text(item.get('format')) or out_format, 'base64': base64_text}
        if value.startswith('file-url:'):
            # QQ 音频文件的 CDN 直链（带 rkey，短期有效）：原始字节可直接喂给
            # 模型；格式用文件名扩展 + 魔数双保险，避免把 SILK 语音误当文件。
            payload = value[len('file-url:'):]
            split = payload.rfind('#')
            url = (payload[:split] if split > 0 else payload).strip()
            fragment = payload[split + 1:] if split > 0 else ''
            name = urllib.parse.unquote(re.sub(r':\d+$', '', fragment)) if fragment else ''
            size_match = re.search(r':(\d+)$', fragment)
            declared_size = int(size_match.group(1)) if size_match else 0
            if not re.match(r'^https?://', url, re.IGNORECASE):
                return None
            if declared_size > max_bytes:
                return None
            item = self.platform.fetch_audio(url, session)
            if not item:
                return None
            base64_text = re.sub(r'\s+', '', _text(item.get('base64')))
            if not base64_text:
                return None
            try:
                bytes_ = _decode_base64(base64_text)
            except Exception:  # noqa: BLE001 - 畸形载荷按不可用处理
                return None
            if not bytes_ or len(bytes_) > max_bytes:
                return None
            format_ = guess_audio_format(bytes_, name)
            if not format_:
                return None
            return {'format': format_, 'base64': base64_text}
        if re.match(r'^data:audio/', value, re.IGNORECASE):
            # Adapter-provided inline audio: accept only formats models can read.
            match = re.match(r'^data:audio/([a-z0-9]+);base64,([a-z0-9+/=\s]+)$', value, re.IGNORECASE)
            inline_format = (match.group(1) or '').lower() if match else ''
            if not match or inline_format not in _INLINE_AUDIO_FORMATS:
                return None
            base64_text = re.sub(r'\s+', '', match.group(2))
            if not base64_text or _base64_byte_length(base64_text) > max_bytes:
                return None
            return {'format': inline_format, 'base64': base64_text}
        # Plain http(s) record URLs serve raw SILK; without a file token there is
        # no server-side transcode path, so the attachment is skipped deliberately.
        return None

    # ================= 上游 2719-2767：视觉事件与侧端识图 =================

    def describe_vision_event(self, session: InboundSession) -> Dict[str, Any]:
        """上游 describeVisionEvent：附件走原生多模态通道，普通文本不带占位符。"""
        raw = _text(getattr(session, 'content', ''))
        sources = extract_session_image_sources(session)
        text = normalize_qq_native_face_segments(raw)
        text = re.sub(r'</?(?:img|image|audio|record|file)\b[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\[CQ:(?:image|record|file),[^\]]*\]', '', text, flags=re.IGNORECASE)
        # The attachment itself is passed through the native multimodal channel.
        # Keep ordinary text free of image placeholders: a failed/filtered fetch
        # must look like no visual input rather than an invitation to invent one.
        content = text.strip()
        return {'content': content, 'sources': sources}

    def load_native_images(self, story: Dict[str, Any], sources: List[str],
                           session: Optional[InboundSession] = None) -> List[NarrativeImage]:
        """上游 loadNativeImages：最多取 3 张图片作为原生视觉输入。"""
        if not _config_get(self.config, 'model', 'vision', 'enabled') or not sources:
            return []
        images: List[NarrativeImage] = []
        for index, source in enumerate(sources[:3]):
            try:
                image = self.fetch_native_image(source, getattr(session, 'bot', None) if session is not None else None)
                if image:
                    images.append({'id': 'turn-image-%d' % (index + 1), **image})
            except Exception as error:  # noqa: BLE001 - 图片失败不阻塞文字回合
                self.report('warn', story, 'user-message', '图片读取失败，已继续处理文字消息 错误=%s', error)
        return images

    def describe_current_images(self, story: Dict[str, Any], images: List[NarrativeImage],
                                user_message: Optional[str]) -> Optional[List[str]]:
        """上游 describeCurrentImages：侧端识图只把事实结果并入当前事件。"""
        if not images:
            return None
        if not self.vision_describer.available():
            self.report_operation('diagnostic', 'warn', story, 'user-message',
                                  '侧端识图跳过：没有配置 useForVision 的视觉模型')
            return None
        try:
            observations = self.vision_describer.describe_images(
                images, user_message, _config_get(self.config, 'model', 'vision', 'detail', default='auto'))
            if observations:
                self.report_operation('diagnostic', 'debug', story, 'user-message',
                                      '侧端识图完成 图片=%d 观察=%d', len(images), len(observations))
            else:
                self.report_operation('diagnostic', 'warn', story, 'user-message',
                                      '侧端识图未返回内容，已继续处理文字消息')
            return observations
        except Exception as error:  # noqa: BLE001 - 侧端识图失败只降级
            self.report_operation('diagnostic', 'warn', story, 'user-message',
                                  '侧端识图失败，已继续处理文字消息 错误=%s', error)
            return None

    # ================= 上游 2769-2896：原生图片获取、抽帧与降采样 =================

    def fetch_native_image(self, source: str, bot: Any = None,
                           adapter_provided: bool = False) -> Optional[Dict[str, Any]]:
        """上游 fetchNativeImage。

        平台映射：`bot.getImage`/`ctx.http.get` → PlatformAdapter.fetch_image；
        本地路径读取 → PlatformAdapter.read_file（上游 node:fs/promises readFile）。
        """
        value = _text(source).strip()
        if value.startswith('onebot-url:'):
            return self.fetch_native_image(value[len('onebot-url:'):], bot, True)
        if value.startswith('onebot-file:'):
            file_token = value[len('onebot-file:'):]
            if not file_token:
                return None
            # 平台层封装 bot.getImage(file)：既接受 {url, file, path, type} 候选形状，
            # 也接受已经下载好的 {mimeType, dataUri, bytes}。
            info = self.platform.fetch_image(value, None) or {}
            if info.get('dataUri') or info.get('bytes'):
                payload = info.get('bytes') if isinstance(info.get('bytes'), (bytes, bytearray)) \
                    else _data_uri_bytes(info.get('dataUri'))
                if not payload:
                    return None
                return self.image_bytes_to_native(bytes(payload), _text(info.get('mimeType')))
            candidates = [_text(info.get('url')).strip(), _text(info.get('file')).strip(), _text(info.get('path')).strip()]
            for candidate in [item for item in candidates if item]:
                if re.match(r'^https?://', candidate, re.IGNORECASE):
                    image = self.fetch_native_image(candidate, None, True)
                    if image:
                        return image
                else:
                    try:
                        payload = self.platform.read_file(candidate)
                        if not payload:
                            continue
                        image = self.image_bytes_to_native(payload, guess_image_mime(payload, info.get('type')))
                        if image:
                            return image
                    except Exception:  # noqa: BLE001 - adapter may return a non-local alias
                        continue
            return None
        if re.match(r'^data:image/', value, re.IGNORECASE):
            match = re.match(r'^data:(image/[a-z0-9.+-]+);base64,([a-z0-9+/=\s]+)$', value, re.IGNORECASE)
            if not match:
                return None
            try:
                payload = _decode_base64(match.group(2))
            except Exception:  # noqa: BLE001 - 畸形内联图片按不可用处理
                return None
            if not payload or len(payload) > 4 * 1024 * 1024:
                return None
            return self.image_bytes_to_native(payload, match.group(1).lower())
        if not re.match(r'^https?://', value, re.IGNORECASE):
            return None
        if not adapter_provided and not is_trusted_image_host(_url_hostname(value)):
            return None
        image = self.platform.fetch_image(value, None)
        if not image:
            return None
        payload = image.get('bytes') if isinstance(image.get('bytes'), (bytes, bytearray)) else _data_uri_bytes(image.get('dataUri'))
        if not payload or len(payload) > 4 * 1024 * 1024:
            return None
        mime_type = _text(image.get('mimeType')).split(';')[0].strip().lower() or guess_image_mime(payload)
        return self.image_bytes_to_native(bytes(payload), mime_type)

    def image_bytes_to_native(self, bytes_: bytes, mime_type: str) -> Optional[Dict[str, Any]]:
        """上游 imageBytesToNative：动图优先抽帧，否则按配置降采样。"""
        normalized = (_text(mime_type) or guess_image_mime(bytes_) or '').lower()
        if not normalized.startswith('image/'):
            return None
        data_uri = 'data:%s;base64,%s' % (normalized, _encode_base64(bytes_))
        if is_animated_image_mime(normalized):
            frame = self.render_animated_image_frame(data_uri)
            if frame:
                return frame
            # 平台没有抽帧能力时（当前微信适配器即如此），直接把原文件交给视觉模型；
            # deepseek-flash 已实测可直接吃 GIF，不再需要 Puppeteer/GIF 抽帧。
            self.report_standalone('debug', '动态图片未抽帧，已直接使用原文件输入。')
        scaled = self.downscale_image_for_vision({'mimeType': normalized, 'dataUri': data_uri})
        return scaled if scaled else {'mimeType': normalized, 'dataUri': data_uri}

    def downscale_image_for_vision(self, image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """上游 downscaleImageForVision。

        `ctx.puppeteer` 在本移植里映射为 PlatformAdapter.downscale_image：返回压缩后的
        dataUri，能力不可用/渲染失败时返回 None（上游降级为透传原图）。
        """
        max_dimension = _config_get(self.config, 'model', 'vision', 'maxImageDimension', default=0) or 0
        if not max_dimension or not should_downscale_image(_text(image.get('mimeType')), _text(image.get('dataUri'))):
            return None
        renderer = getattr(self.platform, 'downscale_image', None)
        if not callable(renderer):
            return None
        try:
            scaled_uri = renderer(image.get('mimeType'), image.get('dataUri'))
        except Exception as error:  # noqa: BLE001 - 降采样失败透传原图
            self.report_standalone('debug', '视觉图片降采样失败，已透传原图：%s', error)
            return None
        if not scaled_uri:
            return None
        scaled_bytes = _data_uri_bytes(scaled_uri)
        original_bytes = _data_uri_bytes(image.get('dataUri'))
        if not scaled_bytes or len(scaled_bytes) >= len(original_bytes):
            return None
        self.report_standalone_operation('diagnostic', 'debug',
                                         '视觉图片已降采样 原始=%dB 降采样后=%dB 上限=%dpx',
                                         len(original_bytes), len(scaled_bytes), max_dimension)
        return {'mimeType': _data_uri_mime(scaled_uri) or 'image/jpeg', 'dataUri': scaled_uri}

    def render_animated_image_frame(self, data_uri: str) -> Optional[Dict[str, Any]]:
        """上游 renderAnimatedImageFrame：GIF/WebP 抽一帧 PNG，失败由调用方降级。"""
        renderer = getattr(self.platform, 'render_animated_frame', None)
        if not callable(renderer):
            return None
        try:
            frame = renderer(data_uri)
        except Exception as error:  # noqa: BLE001 - 抽帧失败只降级
            self.report_standalone('debug', '动态图片抽帧失败：%s', error)
            return None
        if not frame:
            return None
        frame_bytes = _data_uri_bytes(frame)
        if not frame_bytes or len(frame_bytes) > 4 * 1024 * 1024:
            return None
        return {'mimeType': 'image/png', 'dataUri': frame}

    # ================= 上游 2898-2935：缓冲失效与前台回合判定 =================

    def invalidate_buffered_narratives(self, story_id: Optional[str] = None) -> None:
        """上游 invalidateBufferedNarratives：重置/清库后阻止定时器或旧模型结果复活数据。"""
        if story_id:
            self.compaction_backoff.pop(story_id, None)
        else:
            self.compaction_backoff.clear()
        for key, turn in list(self.buffered_narrative_turns.items()):
            if story_id and turn.get('storyId') != story_id:
                continue
            if turn.get('timer'):
                self.ctx.clear_timer(turn['timer'])
            if turn.get('inFlightRequestId'):
                (turn.get('obsoleteRequestIds') or set()).add(turn.get('inFlightRequestId'))
            self.buffered_narrative_turns.pop(key, None)
        # Group turns have their own debounce timers. They must be cancelled by
        # the same reset/purge path, otherwise an old buffered group message can
        # write a fresh entry after the administrator has cleared the story.
        for key, turn in list(self.buffered_group_turns.items()):
            if story_id and turn.get('storyId') != story_id:
                continue
            if turn.get('timer'):
                self.ctx.clear_timer(turn['timer'])
            self.buffered_group_turns.pop(key, None)
        for key in list(self.group_willingness.keys()):
            if not story_id or key.startswith('%s:' % story_id):
                self.group_willingness.pop(key, None)
        for key, wake in list(self.due_intent_wake_timers.items()):
            if story_id and key != story_id:
                continue
            cancel = getattr(wake, 'cancel', None)
            if callable(cancel):
                cancel()
            self.due_intent_wake_timers.pop(key, None)

    def has_pending_narrative(self, story_id: str) -> bool:
        """上游 hasPendingNarrative：存在前台或待合并回合时后台任务让路。"""
        if story_id in self.narrating_stories:
            return True
        for turn in self.buffered_narrative_turns.values():
            if turn.get('storyId') == story_id and (turn.get('messages') or turn.get('timer') or turn.get('inFlightRequestId')):
                return True
        for turn in self.buffered_group_turns.values():
            if turn.get('storyId') == story_id and (turn.get('messages') or turn.get('timer')):
                return True
        return False

    # ================= 上游 2937-3138：短时消息缓冲的 flush =================

    def flush_buffered_narrative(self, key: str, revision: int) -> None:
        """上游 flushBufferedNarrative：一个合并写作回合的完整生命周期。"""
        if self.database_resetting or self.desktop_runtime_phase == 'paused':
            return
        turn = self.buffered_narrative_turns.get(key)
        if not turn or turn.get('nextRevision') != revision:
            return
        # One shared story has one narrator at a time. If another relationship is
        # currently waiting on the provider, keep this batch intact and retry
        # shortly instead of taking an inconsistent cursor snapshot.
        if turn.get('storyId') in self.narrating_stories:
            turn['timer'] = self.ctx.set_timeout(lambda: self.flush_buffered_narrative(key, revision), 250)
            return
        self.narrating_stories.add(turn.get('storyId'))
        turn['timer'] = None
        batch = list(turn.get('messages') or [])
        del turn['messages'][:]
        if not batch:
            self.narrating_stories.discard(turn.get('storyId'))
            return
        request_id = revision
        turn['inFlightRequestId'] = request_id
        try:
            # Snapshot only the lightweight decision inputs under the story lock.
            # The network request stays outside it, so a new user message can be
            # recorded immediately and invalidate this request when appropriate.
            def snapshot_task() -> Optional[Dict[str, Any]]:
                story = self.get_story(turn.get('storyId'))
                participant = self.get_participant(turn.get('participantId'))
                if not participant or participant.get('status') != 'active' or story.get('status') != 'active':
                    return None
                now = now_utc()
                due = live_narrative_intents(self.due_intents(story.get('id'), now))
                due = [intent for intent in due
                       if not intent.get('participantId') or intent.get('participantId') == participant.get('id')]
                return {'story': story, 'participant': participant,
                        'from': _narrative_cursor(story, now), 'now': now, 'due': due}

            snapshot = self.serial(turn.get('storyId'), snapshot_task)
            if not snapshot:
                return
            user_message = _format_buffered_user_messages(batch)
            turn_query_embedding = None
            if user_message and user_message.strip() and self.semantic_turn_embedding_enabled():
                max_input = _field(_config_get(self.config, 'model', 'embedding'), 'maxInputCharacters')
                turn_query_embedding = self.embed_text(
                    user_message.strip()[:4000 if max_input is None else max_input])
            quoted_messages: List[Dict[str, Any]] = []
            for index, message in enumerate(batch):
                if message.get('quote'):
                    quoted_messages.append({**message.get('quote'), 'messageIndex': index + 1})
            sticker_catalog = self.sticker_catalog_for_session(turn.get('latestSession'), turn_query_embedding)
            chat_capabilities = self.private_chat_capabilities(turn.get('latestSession'))
            image_sources = _unique([source for message in batch for source in (message.get('imageSources') or [])])[:3]
            loaded_images = self.load_native_images(snapshot['story'], image_sources, turn.get('latestSession'))
            vision_mode = _config_get(self.config, 'model', 'vision', 'mode')
            vision_mode = 'native' if vision_mode is None else vision_mode
            visual_observations = self.describe_current_images(snapshot['story'], loaded_images, user_message) \
                if vision_mode == 'sidecar' else None
            images = loaded_images if vision_mode == 'native' else []
            # Voice records ride the native-audio channel: SnowLuma transcodes each
            # record server-side and the main model receives it as input_audio.
            audio_sources = _unique([source for message in batch for source in (message.get('audioSources') or [])])
            audio = self.load_native_audio(snapshot['story'], audio_sources, turn.get('latestSession'))
            # If another message arrived while an image was being downloaded, put
            # this batch back and let the newer revision compose one combined event.
            if turn.get('nextRevision') != revision:
                turn['messages'][0:0] = batch
                return
            superseded = [intent for message in batch for intent in (message.get('supersededIntents') or [])]
            early: Dict[str, Any] = {'delivered': False, 'interaction': None, 'deliveryEntry': None}

            def on_early_reply(reply: Dict[str, Any]) -> bool:
                if early.get('delivered'):
                    return False
                delivery_entry = self.deliver_early_private_reply(
                    snapshot['story'], snapshot['participant'], turn.get('latestSession'), turn, request_id, reply)
                if delivery_entry:
                    early['delivered'] = True
                    early['interaction'] = reply.get('interaction')
                    early['deliveryEntry'] = delivery_entry
                return bool(delivery_entry)

            narrative = self.try_decide(
                snapshot['story'], snapshot['participant'], 'user-message', snapshot['from'], snapshot['now'],
                user_message, snapshot['due'], superseded, None, images, audio, chat_capabilities,
                quoted_messages, sticker_catalog, turn_query_embedding, visual_observations, on_early_reply,
            )
            succeeded = narrative.get('succeeded')
            effective_now = narrative.get('effectiveNow')
            immediate_observations = narrative.get('immediateObservations') or []
            decision = narrative.get('decision') or {}
            if early.get('delivered') and early.get('interaction'):
                decision = {**decision, 'interaction': early.get('interaction')}
            sticker = self.resolve_sticker(decision.get('localMedia'), sticker_catalog)
            native_face = None if sticker else self.resolve_native_face(decision, chat_capabilities)

            def result_task() -> Dict[str, Any]:
                if self.database_resetting:
                    return {'obsolete': True, 'requeue': False, 'messages': [], 'commit': None, 'scriptEntry': None}
                if request_id in (turn.get('obsoleteRequestIds') or set()):
                    return {'obsolete': True, 'requeue': True, 'messages': [], 'commit': None, 'scriptEntry': None}
                current = self.get_story(turn.get('storyId'))
                current_participant = self.get_participant(turn.get('participantId'))
                if not current_participant or current_participant.get('status') != 'active' \
                        or current.get('status') != 'active':
                    return {'obsolete': True, 'requeue': False, 'messages': [], 'commit': None, 'scriptEntry': None}
                now = now_utc()
                # Persist a successful/failed immediate observation only after this
                # request has survived debounce invalidation. This keeps an obsolete
                # result from contaminating the next combined user turn.
                for observation in immediate_observations:
                    self.persist_collected_web_observation(observation)
                if early.get('delivered') and not succeeded:
                    self.append_entry(current.get('id'), {
                        'kind': 'stream-finalization-failed',
                        'actor': 'system',
                        'content': '主角首条消息已经提前投递，但流式叙事未能完成；本轮不会自动重发可见回复。',
                        'occurredAt': iso(now),
                        'metadata': {'requestId': request_id},
                    }, now, current_participant.get('id'))
                    self.schedule_stream_script_recovery(current.get('id'), current_participant.get('id'), now)
                    self.report_operation('standard', 'warn', current, 'user-message',
                                          '流式叙事在首条回复后未完成，已保留投递且停止自动重试 参与者=%s 请求=%d',
                                          current_participant.get('id'), request_id)
                    return {'obsolete': False, 'requeue': False, 'messages': [], 'commit': None, 'scriptEntry': None}
                interaction = decision.get('interaction') or {}
                reply = interaction.get('reply') or {}
                commits_first_reply = bool(succeeded) and reply.get('mode') == 'immediate' \
                    and isinstance(reply.get('content'), str) and bool(reply.get('content').strip())
                if commits_first_reply:
                    turn['firstMessageCommittedRequestId'] = request_id
                persisted = self.persist_decision(current, current_participant, {
                    **decision,
                    'localMedia': decision.get('localMedia') if sticker else None,
                    'nativeFace': decision.get('nativeFace') if native_face else None,
                }, snapshot['from'], effective_now, True, 'user-message', [], early.get('delivered'))
                if early.get('deliveryEntry') and persisted.get('commit') and reply.get('content'):
                    event = find_outgoing_script_event(
                        persisted.get('commit'), current_participant.get('id'), 'immediate', reply.get('content'),
                        _field(_field(self.config, 'runtime'), 'messageSeparator'),
                    )
                    script_event = message_event_reference(
                        event, 0, (persisted.get('scriptEntry') or {}).get('id')) if event else None
                    if script_event:
                        self.db_set('interlude_script_entry', {'id': (early.get('deliveryEntry') or {}).get('id')}, {
                            'metadata': {**((early.get('deliveryEntry') or {}).get('metadata') or {}),
                                         **script_event, 'earlyStreamingDelivery': True},
                        })
                        self.update_script_delivery_outcome(current.get('id'), script_event, 'delivered', now)
                if succeeded:
                    self.db_set('interlude_story', {'id': current.get('id')},
                                {'cursorAt': effective_now, 'updatedAt': now})
                    consumed_due_ids = consumed_live_intent_ids(snapshot['due'])
                    if consumed_due_ids:
                        self.db_set('interlude_intent', {'id': {'$in': consumed_due_ids}},
                                    {'status': 'completed', 'updatedAt': now})
                else:
                    self.schedule_narrative_retry(current.get('id'), current_participant.get('id'), now)
                if succeeded:
                    self.schedule_conversation_follow_ups_after_turn(
                        current.get('id'), effective_now, decision.get('interaction'), current_participant.get('id'))
                self.report_operation('diagnostic', 'debug', current, 'user-message',
                                      '写作回合统计 参与者=%s 合并消息=%d 成功=%s 可见消息=%d',
                                      current_participant.get('id'), len(batch), _bool_text(succeeded),
                                      len(persisted.get('messages') or []))
                return {'obsolete': False, 'requeue': False,
                        'messages': persisted.get('messages') or [],
                        'commit': persisted.get('commit'), 'scriptEntry': persisted.get('scriptEntry')}

            result = self.serial(turn.get('storyId'), result_task)
            if result.get('obsolete'):
                if result.get('requeue'):
                    turn['messages'][0:0] = batch
                self.report_operation('standard', 'info', snapshot['story'], 'user-message',
                                      '已丢弃过期主模型结果 参与者=%s 请求=%d',
                                      snapshot['participant'].get('id'), request_id)
                return
            if self.can_handle_participant(snapshot['participant']):
                delivered = self.send_outgoing_messages(snapshot['story'], result.get('messages') or [],
                                                        snapshot['participant'], turn.get('latestSession'))
                self.confirm_outgoing_deliveries(snapshot['story'], delivered)
                if sticker and turn.get('latestSession'):
                    self.send_sticker(
                        snapshot['story'], turn.get('latestSession'), snapshot['participant'].get('channelId'),
                        sticker, None,
                        platform_action_reference(result.get('commit'), (result.get('scriptEntry') or {}).get('id'),
                                                  'local-media', sticker.get('assetId')),
                    )
                if native_face and turn.get('latestSession'):
                    self.send_native_face(
                        snapshot['story'], turn.get('latestSession'), snapshot['participant'].get('channelId'),
                        native_face, None,
                        platform_action_reference(result.get('commit'), (result.get('scriptEntry') or {}).get('id'),
                                                  'native-face', native_face),
                    )
            self.schedule_compaction(turn.get('storyId'))
        except Exception as error:  # noqa: BLE001 - 合并任务失败只记日志并回收缓冲
            self.report_standalone('warn', '合并写作任务失败：参与者=%s 错误=%s', turn.get('participantId'), error)
        finally:
            if turn.get('inFlightRequestId') == request_id:
                turn['inFlightRequestId'] = None
                turn['firstMessageCommittedRequestId'] = None
                self.narrating_stories.discard(turn.get('storyId'))
            (turn.get('obsoleteRequestIds') or set()).discard(request_id)
            if not turn.get('messages') and turn.get('nextRevision') == revision:
                # threading.Timer(0) 可能在 set_timeout 返回前就触发：本次 flush 结束时
                # 这个 handle 已经是"已触发"的死句柄，清掉才能让 has_pending_narrative 归零。
                turn['timer'] = None
            if not turn.get('messages') and not turn.get('timer') and not turn.get('inFlightRequestId'):
                self.buffered_narrative_turns.pop(key, None)
