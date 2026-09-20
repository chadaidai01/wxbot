# -*- coding: utf-8 -*-
"""上游 src/service.ts 行号 1605-1953 的移植（HDS-Interlude 1.0.1-beta6-rebuild）。

ServiceInboundMixin —— 入站收发与群消息缓冲回合：

- receiveGroup（1605-1643）/ receive（1645-1718）
- groupSenderName（1720-1733）/ lookupGroupMemberName（1735-1748）
- bufferGroupMessage（1750-1767）/ flushGroupTurn（1769-1928）
- groupMessages（1929-1951）/ groupCooldownActive（1953-1961）

外加上述区间引用到的上游非导出局部 helper（定义在本文件，供其它 mixin 复用）：
sessionGroupId / normalizeGroupId / normalizeAccountId / isOneBotPlatform /
targetableMessageId / groupMessageRef / normalizeGroupDisplayName /
normalizeQuotedMessageContext / mentionsBot / quotesBot / narrativeCursor。

共享约定（SERVICE_PORTING_SPEC.md / PORTING_GUIDE.md）：
- 不写 __init__；self.config / self.ctx / self.platform / self.logger / self.db_get /
  self.db_set / self.serial / self.report_operation / self.database_resetting /
  self.desktop_runtime_phase / self.shared_story_config / self.audio_config /
  self.narrating_stories / self.buffered_group_turns / self.group_willingness /
  self.group_member_name_cache / self.group_member_name_lookups 等由 service_base.py 提供。
- Koishi Session → hdsi.platform.session.InboundSession；session.guildId 是群号；
  session.content 在平台层已剥离 @（mention 判定额外承认 session.mentioned_bot）。
- ctx.setTimeout / clearTimeout → self.ctx.set_timeout / self.ctx.clear_timer；
  延迟窗口与 revision 取消语义逐字复刻。
- 跨 mixin 调用一律使用 SERVICE_PORTING_SPEC.md 方法名对照表的 snake_case 名。
- 上游顶层导出 helper（describeQuotedMessage 等）由并行移植的 hdsi/service_helpers.py
  提供；该文件尚未落地时回退到本文件内的逐行等价内联副本，保证本 mixin 可独立导入。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .group_willingness import consume_group_willingness, evaluate_group_willingness
from .platform.session import InboundSession
from .qq_face import normalize_qq_native_face_segments
from .script.commit_builder import find_group_script_event
from .script.contract import message_event_reference
from .script.delivery_ledger import platform_action_reference
from .time_utils import iso, now_utc, parse_time
from .utils import clip, is_record


# ===================== 本文件通用小工具（非上游） =====================

def _now_ms() -> int:
    """``Date.now()`` 等价：epoch 毫秒（群意愿状态按毫秒时间戳计算）。"""
    return int(now_utc().timestamp() * 1000)


def _js_string(value: Any) -> str:
    """TS ``String(value ?? '')`` 的最小等价实现。"""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _field(value: Any, key: str, default: Any = None) -> Any:
    """读取配置/状态/元数据字段。

    本包约定数据形状是 dict，但并行移植里配置 getter、平台对象等也可能是轻量对象；
    两种形状都兼容，避免形状未定时崩溃或静默走错分支。
    """
    if value is None:
        return default
    if isinstance(value, (dict, str, bytes, int, float, bool, list, tuple, set)):
        return value.get(key, default) if isinstance(value, dict) else default
    if callable(value):
        try:
            value = value()
        except TypeError:
            return default
        if isinstance(value, dict):
            return value.get(key, default)
    return getattr(value, key, default)


def _config_get(config: Any, *keys: str, default: Any = None) -> Any:
    """按上游 ``this.config.a.b`` 逐级读取，任一级缺失返回 default。"""
    value = config
    for key in keys:
        value = _field(value, key)
        if value is None:
            return default
    return value if value is not None else default


# ===================== 上游 helper（hdsi/service_helpers.py） =====================
# 优先使用并行移植的 service_helpers.py（上游 7084-8524 的全部 helper，并与 service_story /
# service_base / service_media 共用同一份实现）；任一名缺失时回退到下面与上游逐行等价的
# 内联副本 —— 本区间引用到的非导出局部 helper（sessionGroupId / normalizeGroupId / ...
# / narrativeCursor）也在其中，因此本 mixin 可独立导入。

try:  # pragma: no cover - 取决于并行移植进度
    from .service_helpers import (
        describe_group_attachments,
        describe_quoted_message,
        extract_session_file_facts,
        extract_session_voice_count,
        format_group_speaker,
        group_message_ref,
        is_one_bot_platform,
        mentions_bot,
        narrative_cursor,
        normalize_account_id,
        normalize_group_chat_actions,
        normalize_group_display_name,
        normalize_group_id,
        normalize_group_visible_reply,
        normalize_quoted_message_content,
        normalize_quoted_message_context,
        quotes_bot,
        session_group_id,
        targetable_message_id,
    )
except ImportError:  # TODO(并行移植): service_helpers 就绪后自动切换

    # ===================== 上游非导出局部 helper =====================

    def session_group_id(session: InboundSession) -> str:
        """上游 7246-7249 ``sessionGroupId``：guildId 优先，回退 channelId。"""
        raw = _js_string(session.guildId or session.channelId or '')
        return normalize_group_id(raw)


    def normalize_group_id(value: Any) -> str:
        """上游 7251-7253 ``normalizeGroupId``：去掉 ``group:`` / ``guild:`` 前缀。"""
        return re.sub(r'^(?:group|guild):', '', _js_string(value or '').strip(), count=1, flags=re.IGNORECASE)


    def normalize_account_id(value: Any) -> str:
        """上游 7682-7690 ``normalizeAccountId``：剥离 private/user/onebot/napcat/qq 前缀。"""
        normalized = _js_string(value).strip().lower()
        for _ in range(3):
            next_value = re.sub(r'^(?:private|user|onebot|napcat|qq):', '', normalized, count=1,
                                flags=re.IGNORECASE).strip()
            if next_value == normalized:
                break
            normalized = next_value
        return normalized


    def is_one_bot_platform(platform: Any) -> bool:
        """上游 7026-7034 ``isOneBotPlatform``。"""
        value = _js_string(platform).lower()
        return (
            value == 'onebot'
            or value.startswith('onebot:')
            or value == 'napcat'
            or value.startswith('napcat:')
            or value == 'qq:onebot'
            or value.startswith('qq:onebot:')
        )


    def targetable_message_id(value: Any) -> Optional[str]:
        """上游 7300-7303 ``targetableMessageId``：只接受非 0 的整数消息 ID。"""
        message_id = _js_string(value).strip()
        return message_id if re.fullmatch(r'-?\d+', message_id) and message_id != '0' else None


    def group_message_ref(entry_id: Any) -> str:
        """上游 7305-7307 ``groupMessageRef``。"""
        number = float(entry_id) if isinstance(entry_id, (int, float)) and not isinstance(entry_id, bool) else 0.0
        return 'msg-%d' % max(0, math.floor(number))


    def normalize_group_display_name(*candidates: Any) -> str:
        """上游 7570-7576 ``normalizeGroupDisplayName``：逐项取第一个非空名，截断 80。"""
        for candidate in candidates:
            name = re.sub(r'[\r\n]', ' ', _js_string(candidate)).strip()
            if name:
                return name[:80]
        return ''


    def normalize_quoted_message_context(value: Any) -> Optional[Dict[str, str]]:
        """上游 7436-7444 ``normalizeQuotedMessageContext``。"""
        if not is_record(value):
            return None
        content = normalize_quoted_message_content(value.get('content'))
        if not content:
            return None
        sender_id = clip(_js_string(value.get('senderId')), 127)
        sender_name = clip(_js_string(value.get('senderName')), 255) or '未知发送者'
        speaker = clip(_js_string(value.get('speaker')), 500) or (
            '消息发送者「%s」（ID：%s）' % (sender_name, sender_id)
            if sender_id else '消息发送者「%s」' % sender_name
        )
        return {'senderId': sender_id, 'senderName': sender_name, 'speaker': speaker, 'content': content}


    def mentions_bot(session: InboundSession) -> bool:
        """上游 7578-7583 ``mentionsBot``。

        上游只看 ``session.content``；本项目平台层已把 @ 段从 content 剥离并写入
        ``InboundSession.mentioned_bot``，因此除保留上游的文本/id 标签判定外，也承认该标记。
        """
        self_id = normalize_account_id(session.selfId)
        if not self_id:
            return False
        if session.mentioned_bot:
            return True
        content = _js_string(session.content)
        return self_id in content or bool(
            re.search(r'<at[^>]+id=["\']?' + re.escape(self_id) + r'["\']?', content, re.IGNORECASE)
        )


    def quotes_bot(session: InboundSession) -> bool:
        """上游 7696-7698 ``quotesBot``（Python 会话：``quote.senderId`` = quote.user.id）。"""
        if session.quoted_bot:
            return True
        quote = session.quote
        quote_user_id = _js_string(getattr(quote, 'senderId', None)) if quote is not None else ''
        return quote_user_id == _js_string(session.selfId)


    def narrative_cursor(story: Dict[str, Any], now: datetime) -> datetime:
        """上游 8200-8203 ``narrativeCursor``：损坏/未来的 cursor 不得让叙事倒着补时间。"""
        cursor = parse_time(_field(story, 'cursorAt')) or now
        return now if cursor > now else cursor



    def extract_session_voice_count(session: Any) -> int:
        """上游 7084-7096 ``extractSessionVoiceCount``（本项目元素已解析进 session.elements）。"""
        counter = getattr(session, 'voice_count', None)
        count = counter() if callable(counter) else 0
        if count:
            return count
        raw = _js_string(getattr(session, 'content', None))
        return len(re.findall(r'\[CQ:record,[^\]]*\]', raw, re.IGNORECASE))

    def extract_session_file_facts(session: Any) -> List[Dict[str, Any]]:
        """上游 7158-7187 ``extractSessionFileFacts``（元素优先，回退 ``<file>`` 标记）。"""
        provider = getattr(session, 'file_facts', None)
        if callable(provider):
            facts = provider()
            if facts:
                return facts
        raw = _js_string(getattr(session, 'content', None))
        facts: List[Dict[str, Any]] = []

        def pick(attrs: str, key: str) -> str:
            found = re.search(key + r'=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            return found.group(1).strip() if found else ''

        for match in re.finditer(r'<file\b([^>]*)/?>', raw, re.IGNORECASE):
            attrs = match.group(1)
            url = pick(attrs, 'src') or pick(attrs, 'url')
            name = pick(attrs, 'name') or pick(attrs, 'file') or pick(attrs, 'title')
            if (not url and not name) or len(facts) >= 3:
                continue
            if any(item.get('url') == url and item.get('name') == name for item in facts):
                continue
            size_text = pick(attrs, 'size') or pick(attrs, 'file-size')
            try:
                size = int(float(size_text)) if size_text else 0
            except (TypeError, ValueError):
                size = 0
            facts.append({
                'name': name[:200], 'url': url[:1000], 'size': size,
                'audio': bool(re.search(r'\.(?:mp3|wav|ogg|m4a|flac|amr|aac|wma)$', name, re.IGNORECASE)),
            })
        return facts

    def normalize_quoted_message_content(value: Any) -> str:
        """上游 7417-7434 ``normalizeQuotedMessageContent``。"""
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

    def describe_group_attachments(content: Any) -> str:
        """上游 7188-7205 ``describeGroupAttachments``。"""
        text = normalize_qq_native_face_segments(content)
        text = re.sub(r'<(?:record|audio)\b[^>]*/?>', '[语音]', text, flags=re.IGNORECASE)
        text = re.sub(r'<(?:img|image)\b[^>]*/?>', '[图片]', text, flags=re.IGNORECASE)
        text = re.sub(r'<video\b[^>]*/?>', '[视频]', text, flags=re.IGNORECASE)

        def replace_file(match: 're.Match[str]') -> str:
            name = re.search(r'(?:name|file|title)=["\']([^"\']+)["\']', match.group(0), re.IGNORECASE)
            return '[文件：%s]' % name.group(1) if name else '[文件]'

        text = re.sub(r'<file\b[^>]*/?>', replace_file, text, flags=re.IGNORECASE)
        text = re.sub(r'</(?:file|img|image|audio|record|video)>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', text, flags=re.IGNORECASE)
        text = re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', text, flags=re.IGNORECASE)
        text = re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', text, flags=re.IGNORECASE)

        def replace_cq_file(match: 're.Match[str]') -> str:
            name = re.search(r'(?:name|file)=([^,\]]+)', match.group(1), re.IGNORECASE)
            return '[文件：%s]' % name.group(1) if name else '[文件]'

        return re.sub(r'\[CQ:file,([^\]]*)\]', replace_cq_file, text, flags=re.IGNORECASE)

    def describe_quoted_message(session: Any, character_name: Any = '主角') -> Optional[Dict[str, Any]]:
        """上游 7399-7415 ``describeQuotedMessage``（Python 会话：session.quote 是 QuotedMessage）。"""
        quote = getattr(session, 'quote', None)
        if not quote:
            return None
        content = normalize_quoted_message_content(getattr(quote, 'content', None))
        if not content:
            return None
        self_id = _js_string(getattr(session, 'selfId', None))
        sender_id = _js_string(getattr(quote, 'senderId', None)).strip()
        is_character = bool(sender_id) and sender_id == self_id
        if is_character:
            sender_name = _js_string(character_name).strip() or '主角'
            speaker = '主角「%s」' % sender_name
        else:
            sender_name = normalize_group_display_name(
                getattr(quote, 'senderName', None), getattr(quote, 'speaker', None), sender_id,
            ) or '未知发送者'
            speaker = ('消息发送者「%s」（ID：%s）' % (sender_name, sender_id) if sender_id
                       else '消息发送者「%s」' % sender_name)
        return {'senderId': sender_id, 'senderName': sender_name, 'speaker': speaker, 'content': content}

    def format_group_speaker(sender_name: Any, sender_id: Any) -> str:
        """上游 7564-7568 ``formatGroupSpeaker``。"""
        speaker_id = _js_string(sender_id or 'unknown').strip() or 'unknown'
        name = re.sub(r'[\r\n]', ' ', _js_string(sender_name or '')).strip() or speaker_id
        return '群成员（QQ：%s）' % speaker_id if name == speaker_id else '群成员「%s」（QQ：%s）' % (name, speaker_id)

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
        raw_reply_to = None
        if is_record(group_reply) and group_reply.get('mode') == 'immediate':
            raw_reply_to = group_reply.get('replyTo')
        elif is_record(interaction) and is_record(interaction.get('reply')) and interaction['reply'].get('mode') == 'immediate':
            raw_reply_to = interaction['reply'].get('replyTo')
        reply_message_id = (targets.get(raw_reply_to)
                            if _field(capabilities, 'quoteReply') and isinstance(raw_reply_to, str) else None)
        reactions: List[Dict[str, Any]] = []
        raw_reactions = _field(decision, 'messageReactions')
        if isinstance(raw_reactions, list):
            for item in raw_reactions:
                if (not is_record(item) or not isinstance(item.get('messageRef'), str)
                        or not isinstance(item.get('reaction'), str)):
                    continue
                reactions.append({
                    'messageRef': str(item.get('messageRef')),
                    'reaction': str(item.get('reaction')),
                    'messageId': targets.get(str(item.get('messageRef')), '') or '',
                })
            allowed = set(_field(capabilities, 'reactions') or [])
            reactions = [item for item in reactions if item['messageId'] and item['reaction'] in allowed][:1]
        result: Dict[str, Any] = {}
        if reply_message_id:
            result['replyTo'] = {'messageRef': raw_reply_to, 'messageId': reply_message_id}
        result['reactions'] = reactions
        return result

    def normalize_group_visible_reply(raw: Any, interaction: Any, max_characters: Any,
                                      separator: Any = '<sep/>') -> str:
        """上游 7585-7587 ``normalizeGroupVisibleReply``（含 7639-7663 的局部 helper）。"""
        return (_normalize_group_reply(raw, max_characters, separator)
                or _normalize_group_interaction_reply(interaction, max_characters, separator))

    def _normalize_group_reply(raw: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
        """上游 7639-7642 ``normalizeGroupReply``。"""
        if not is_record(raw) or raw.get('mode') != 'immediate':
            return ''
        return _normalize_visible_message_content(raw.get('content'), max_characters, separator)

    def _normalize_group_interaction_reply(raw: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
        """上游 7644-7647 ``normalizeGroupInteractionReply``。"""
        if not is_record(raw) or not is_record(raw.get('reply')) or raw['reply'].get('mode') != 'immediate':
            return ''
        return _normalize_visible_message_content(raw['reply'].get('content'), max_characters, separator)

    def _normalize_visible_message_content(value: Any, max_characters: Any, separator: Any = '<sep/>') -> str:
        """上游 7649-7663 ``normalizeVisibleMessageContent``。"""
        fallback = (separator or '').strip() if isinstance(separator, str) else ''
        text = _js_string(value)
        text = re.sub(r'[<＜]\s*sep\s*/?\s*[>＞]', lambda _match: fallback or '<sep/>', text, flags=re.IGNORECASE)
        text = re.sub(r'</?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*/?>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]', '', text, flags=re.IGNORECASE)
        text = re.sub(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', '', text, flags=re.IGNORECASE)
        text = re.sub(r'[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]', '', text)
        try:
            limit = max(1, int(max_characters))
        except (TypeError, ValueError):
            limit = 1
        return text.strip()[:limit]


class ServiceInboundMixin:
    """上游 InterludeService 1605-1953：私聊/群聊入站与群消息回合。"""

    # ================= 上游 1605-1643：receiveGroup =================

    def receive_group(self, session: InboundSession, received_at: Optional[datetime] = None) -> bool:
        """上游 1605-1643 receiveGroup。

        Entry point for configured OneBot group chats. Group members do not need
        private-message authorization; the group allowlist controls access.
        """
        if received_at is None:
            received_at = now_utc()
        if self.database_resetting or not self.can_handle_group_session(session):
            return False
        group_id = session_group_id(session)
        rule = self.group_rule(group_id)
        if not rule:
            return False
        mentioned = mentions_bot(session)
        quoted = quotes_bot(session)
        if _field(rule, 'responseMode') == 'mention-only' and not mentioned:
            return False
        story = self.find_story(session)
        if not story and _config_get(self.config, 'runtime', 'autoCreate', default=False):
            story = self.create_story(session)
        if not story or story.get('status') != 'active':
            return False
        now = received_at
        sender_id = normalize_account_id(session.userId)
        sender_name = self.group_sender_name(group_id, sender_id, session)
        quote = describe_quoted_message(session, _config_get(story, 'setting', 'character', 'name', default='主角'))
        message_content = describe_group_attachments(session.content)

        def accept_group_message() -> Dict[str, Any]:
            current = self.get_story(story.get('id'))
            entry = self.append_entry(current.get('id'), {
                'kind': 'group-message', 'actor': 'user', 'content': message_content,
                'occurredAt': iso(now),
                'metadata': {
                    'groupId': group_id, 'senderId': sender_id, 'senderName': sender_name,
                    'channelId': session.channelId, 'messageId': session.messageId,
                    **({'quote': quote} if quote else {}),
                },
            }, now)
            self.pause_automatic_advance_after_user_message(current.get('id'), now)
            return entry

        accepted = self.serial(story.get('id'), accept_group_message)
        message_id = targetable_message_id(session.messageId)
        self.buffer_group_message(story, rule, session, {
            'senderId': sender_id, 'senderName': sender_name,
            'speaker': format_group_speaker(sender_name, sender_id),
            **({'messageId': message_id, 'messageRef': group_message_ref(accepted.get('id'))} if message_id else {}),
            **({'quote': quote} if quote else {}),
            'content': message_content, 'occurredAt': now, 'direction': 'user',
        }, mentioned, quoted)
        self.report_operation('summary', 'info', story, 'user-message', '收到群聊消息 群=%s 发送者=%s', group_id, sender_id)
        return True

    # ================= 上游 1645-1718：receive =================

    def receive(self, session: InboundSession, received_at: Optional[datetime] = None) -> bool:
        """上游 1645-1718 receive（私聊入口）。"""
        if received_at is None:
            received_at = now_utc()
        if self.database_resetting:
            return False
        # Check before find/create so an unauthorized QQ can neither trigger the
        # model nor create a persistent story by merely sending a private message.
        if not self.can_handle_session(session):
            return False
        story = self.find_story(session)
        if not story and _config_get(self.config, 'runtime', 'autoCreate', default=False):
            story = self.create_story(session)
        if not story or story.get('status') != 'active':
            self.report_standalone_operation(
                'diagnostic', 'debug', '私聊未处理：故事不存在或已暂停 平台=%s 机器人ID=%s 用户ID=%s',
                session.platform, session.selfId, session.userId,
            )
            return False
        participant = self.find_participant(session, story)
        if participant:
            # A whitelist row can be edited after this QQ first joined the shared
            # story. Refresh the current branch before composing its model context.
            # ensureParticipant performs no write when nothing actually changed.
            participant = self.ensure_participant(story, session, received_at, participant)
        elif (_config_get(self.config, 'runtime', 'autoCreate', default=False)
              or _field(self.shared_story_config, 'autoEnrollParticipants')):
            participant = self.ensure_participant(story, session)
        if not participant or participant.get('status') != 'active':
            self.report_operation('diagnostic', 'debug', story, 'user-message',
                                  '私聊未处理：参与者不存在或已暂停 用户ID=%s', session.userId)
            return False
        if not (session.content or '').strip() and not extract_session_voice_count(session):
            return False
        observed = self.describe_vision_event(session)
        if (not (observed.get('content') or '').strip() and not observed.get('sources')
                and not extract_session_voice_count(session) and not extract_session_file_facts(session)):
            return False
        # Mark the relationship synchronously before waiting for the story queue.
        # This lets an arriving message invalidate a model request that is about
        # to persist, and lets a due split segment stop before transport begins.
        self.signal_incoming_interruption(story, participant)
        user_input = self.describe_user_event(story, session)
        user_content = user_input.get('content') or ''
        image_sources = user_input.get('sources') or []
        audio_sources = user_input.get('audioSources') or []
        user_quote = user_input.get('quote')
        self.report_operation('summary', 'info', story, 'user-message', '收到参与者私聊消息 参与者=%s', participant.get('id'))
        logging_config = _field(self.config, 'logging')
        if _field(logging_config, 'logMessageContent'):
            preview_length = _field(logging_config, 'previewLength')
            preview_length = (int(preview_length)
                              if isinstance(preview_length, (int, float)) and not isinstance(preview_length, bool)
                              else 0)
            self.report_operation('diagnostic', 'info', story, 'user-message',
                                  '用户消息内容：%s', user_content[:preview_length])

        def persist_user_turn() -> Optional[Dict[str, Any]]:
            current = self.get_story(story.get('id'))
            current_participant = self.get_participant(participant.get('id'))
            if not current_participant or current_participant.get('status') != 'active':
                return None
            now = received_at
            incoming_participant = self.record_incoming_message(current_participant, now)
            superseded = self.cancel_pending_outgoing_messages(
                current.get('id'),
                incoming_participant.get('id'),
                now,
                _config_get(self.config, 'runtime', 'cancelDelayedRepliesOnUserMessage', default=False),
            )
            self.append_entry(current.get('id'), {
                'kind': 'user-message', 'actor': 'user', 'content': user_content,
                'occurredAt': iso(now), 'metadata': {
                    'platform': session.platform, 'messageId': session.messageId,
                    'personId': incoming_participant.get('personId'),
                    **({'imageCount': len(image_sources)} if image_sources else {}),
                    **({'audioCount': len(audio_sources)} if audio_sources else {}),
                    **({'quote': user_quote} if user_quote else {}),
                },
            }, now, incoming_participant.get('id'))
            # Messages are persisted at arrival. The model request itself is
            # debounced below, so a burst can become one coherent writing turn.
            self.pause_automatic_advance_after_user_message(current.get('id'), now)
            return {'story': current, 'participant': incoming_participant, 'now': now, 'superseded': superseded}

        accepted = self.serial(story.get('id'), persist_user_turn)
        if not accepted:
            return False
        self.buffer_user_narrative(
            accepted['story'], accepted['participant'], session, accepted['now'], accepted['superseded'],
            user_content, image_sources, audio_sources, user_quote,
        )
        if image_sources:
            self.report_operation(
                'standard', 'info', accepted['story'], 'user-message',
                '当前事件包含图片附件 数量=%d 原生识图=%s', len(image_sources),
                '开启' if _config_get(self.config, 'model', 'vision', 'enabled', default=False) else '关闭',
            )
        if audio_sources:
            self.report_operation(
                'standard', 'info', accepted['story'], 'user-message',
                '当前事件包含语音附件 数量=%d 原生音频=%s', len(audio_sources),
                '开启' if _field(self.audio_config, 'enabled') else '关闭',
            )
        self.report_operation('standard', 'info', accepted['story'], 'user-message',
                              '用户回合已入队 参与者=%s 已取消旧计划=%d',
                              accepted['participant'].get('id'), len(accepted['superseded'] or []))
        return True

    # ================= 上游 1720-1733：groupSenderName =================

    def group_sender_name(self, group_id: str, user_id: str, session: InboundSession) -> str:
        """上游 1720-1733 groupSenderName。

        Koishi ``author.nick`` / ``author.name`` → ``session.sender_name``；
        ``author.username`` / ``session.username`` → ``session.username``。
        """
        account = self.user_account_rule(user_id)
        observed = normalize_group_display_name(
            _field(account, 'label'),
            session.sender_name,
            session.username,
            session.sender_name,
            session.username,
        )
        if observed:
            return observed

        key = '%s:%s' % (normalize_group_id(group_id), user_id)
        cached = self.group_member_name_cache.get(key)
        if cached and cached.get('expiresAt') > now_utc():
            return cached.get('name')
        pending = self.group_member_name_lookups.get(key)
        if pending is None:
            pending = self.lookup_group_member_name(key, group_id, user_id, session.selfId)
        self.group_member_name_lookups[key] = pending
        try:
            return pending or user_id
        finally:
            self.group_member_name_lookups.pop(key, None)

    # ================= 上游 1735-1748：lookupGroupMemberName =================

    def lookup_group_member_name(self, cache_key: str, group_id: str, user_id: str, self_id: str) -> str:
        """上游 1735-1748 lookupGroupMemberName。

        上游在 ``ctx.bots`` 中找 selfId 匹配的 onebot 账号并要求 ``getGuildMember`` 可用；
        本项目 ``ctx.bots`` → ``self.platform.list_bots()``，``getGuildMember`` →
        ``self.platform.group_member_name()``。平台暴露账号列表时仍按上游要求匹配账号
        （没有匹配账号就返回 ''）；未暴露账号列表的适配器（微信侧）直接使用平台的
        群成员名能力，否则该方法永远不会被调用。
        """
        bots = self.platform.list_bots() or []
        self_id_text = _js_string(self_id)
        bot = next((
            item for item in bots
            if _js_string(_field(item, 'selfId')) == self_id_text
            and (_field(item, 'platform') == 'onebot' or is_one_bot_platform(_field(item, 'platform')))
        ), None)
        if bots and bot is None:
            # 平台适配（非上游）：微信等非 OneBot 平台的账号列表不参与 OneBot 白名单校验，
            # 直接使用平台群成员名能力，否则该方法在微信侧永远不会被调用。
            platform_name = _js_string(_field(self.platform, 'platform'))
            if platform_name and not is_one_bot_platform(platform_name):
                bot = bots[0]
        if bots and bot is None:
            return ''
        try:
            name = normalize_group_display_name(
                self.platform.group_member_name(normalize_group_id(group_id), user_id))
            if not name:
                return ''
            self.group_member_name_cache[cache_key] = {
                'name': name,
                'expiresAt': now_utc() + timedelta(hours=12),  # 上游 Date.now() + 12 * Time.hour
            }
            return name
        except Exception:  # noqa: BLE001 - 上游 catch 全部错误并返回 ''
            return ''

    # ================= 上游 1750-1767：bufferGroupMessage =================

    def buffer_group_message(self, story: Dict[str, Any], rule: Dict[str, Any], session: InboundSession,
                             message: Dict[str, Any], mentioned_bot: bool, quoted_bot: bool) -> None:
        """上游 1750-1767 bufferGroupMessage：延迟窗口内合并群消息，revision 取消旧定时器。"""
        key = '%s:%s' % (story.get('id'), normalize_group_id(_field(rule, 'groupId')))
        existing = self.buffered_group_turns.get(key)
        turn: Dict[str, Any] = existing if existing is not None else {
            'storyId': story.get('id'), 'groupId': normalize_group_id(_field(rule, 'groupId')), 'rule': rule,
            'channelId': session.channelId, 'messages': [], 'revision': 0, 'mentionedBot': False, 'quotedBot': False,
        }
        if turn.get('timer'):
            self.ctx.clear_timer(turn['timer'])
        turn['channelId'] = session.channelId
        turn['latestSession'] = session
        turn['messages'].append(message)
        turn['mentionedBot'] = turn['mentionedBot'] or mentioned_bot
        turn['quotedBot'] = turn['quotedBot'] or quoted_bot
        turn['revision'] += 1
        revision = turn['revision']
        debounce = _field(rule, 'debounceSeconds')
        if debounce is None:
            debounce = 1
        delay = max(0, debounce) * 1000  # 上游 Max(0, rule.debounceSeconds ?? 1) * Time.second
        # 先登记再定时：delay=0 时线程可能在赋值前就跑 flush（上游 setTimeout 是宏任务，
        # 当前同步函数必然先跑完；Python threading.Timer(0) 没有这个保证）。
        self.buffered_group_turns[key] = turn
        turn['timer'] = self.ctx.set_timeout(lambda: self.flush_group_turn(key, revision), delay)

    # ================= 上游 1769-1928：flushGroupTurn =================

    def flush_group_turn(self, key: str, revision: int) -> None:
        """上游 1769-1928 flushGroupTurn：把一批群消息交给主叙事并投递结果。"""
        turn = self.buffered_group_turns.get(key)
        if (not turn or turn.get('revision') != revision
                or self.database_resetting or self.desktop_runtime_phase == 'paused'):
            return
        if turn.get('storyId') in self.narrating_stories:
            turn['timer'] = self.ctx.set_timeout(lambda: self.flush_group_turn(key, revision), 250)
            return
        turn['timer'] = None
        # Unlike private turns, a group batch stays deliverable after its model
        # request has started. New group messages form the next batch so a busy
        # conversation cannot permanently starve the protagonist of a reply.
        batch = list(turn.get('messages') or [])
        del turn['messages'][:]
        if not batch:
            self.buffered_group_turns.pop(key, None)
            return
        try:
            story = self.get_story(turn.get('storyId'))
        except Exception as error:  # noqa: BLE001 - 上游 catch(error) 后放弃本批消息
            self.report_standalone('warn', '群聊回合读取剧本失败，已放弃本批消息 故事=%s 错误=%s',
                                   turn.get('storyId'), error)
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        if not story or story.get('status') != 'active':
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        rule = turn.get('rule') or {}
        willingness = evaluate_group_willingness(
            self.group_willingness.get(key),
            _field(rule, 'willingness'),
            {
                'now': _now_ms(), 'messageCount': len(batch),
                'content': '\n'.join(_js_string(_field(message, 'content')) for message in batch),
                'mentionedBot': turn.get('mentionedBot'), 'quotedBot': turn.get('quotedBot'),
            },
        )
        self.group_willingness[key] = willingness['state']
        turn['mentionedBot'] = False
        turn['quotedBot'] = False
        score_text = '%.3f' % willingness['state']['score']
        if not willingness['shouldCall']:
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '群聊意愿未触发模型调用 群=%s 分数=%s 概率=%s 原因=%s', turn.get('groupId'),
                score_text, '%.3f' % willingness['probability'], willingness['reason'],
            )
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        if self.group_cooldown_active(story.get('id'), turn.get('groupId'), _field(rule, 'cooldownSeconds', 0)):
            self.report_operation('diagnostic', 'debug', story, 'user-message',
                                  '群聊仍在冷却期，跳过群发言 群=%s', turn.get('groupId'))
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        self.report_operation('standard', 'info', story, 'user-message',
                              '群聊消息准备进入主叙事 群=%s 模式=%s 意愿=%s',
                              turn.get('groupId'), _field(rule, 'responseMode'), score_text)
        self.narrating_stories.add(turn.get('storyId'))
        try:
            def take_snapshot() -> Dict[str, Any]:
                current = self.get_story(story.get('id'))
                context_messages = self.group_messages(current.get('id'), turn.get('groupId'),
                                                       _field(rule, 'contextLimit', 20))
                moment = now_utc()
                return {
                    'story': current, 'from': narrative_cursor(current, moment), 'now': moment,
                    'contextMessages': context_messages,
                }

            snapshot = self.serial(story.get('id'), take_snapshot)
            group_context: Dict[str, Any] = {
                'groupId': turn.get('groupId'), 'channelId': turn.get('channelId'), 'label': _field(rule, 'label'),
                'purpose': _field(rule, 'purpose'), 'characterRole': _field(rule, 'characterRole'),
                'messages': snapshot.get('contextMessages'),
            }
            chat_capabilities = self.group_chat_capabilities(turn.get('latestSession'), group_context.get('messages'))
            user_message = '\n\n'.join(
                '[群聊连续消息 %d｜%s]\n%s' % (index + 1, _field(message, 'speaker'), _field(message, 'content'))
                for index, message in enumerate(batch)
            )
            turn_query_embedding = None
            if self.semantic_turn_embedding_enabled():
                max_input = _config_get(self.config, 'model', 'embedding', 'maxInputCharacters', default=4_000)
                if max_input is None:
                    max_input = 4_000
                turn_query_embedding = self.embed_text(user_message[:int(max_input)])
            sticker_catalog = self.sticker_catalog_for_session(turn.get('latestSession'), turn_query_embedding)
            decided = self.try_decide(
                snapshot.get('story'), None, 'user-message', snapshot.get('from'), snapshot.get('now'),
                user_message, [], [], group_context, [], [], chat_capabilities, [], sticker_catalog, turn_query_embedding,
            )
            decision = _field(decided, 'decision') or {}
            if not isinstance(decision, dict):
                decision = {}
            succeeded = _field(decided, 'succeeded')
            chat_actions = normalize_group_chat_actions(decision, chat_capabilities, group_context)
            sticker = self.resolve_sticker(decision.get('localMedia'), sticker_catalog)
            native_face = None if sticker else self.resolve_native_face(decision, chat_capabilities)

            def persist_group_turn() -> Dict[str, Any]:
                if self.database_resetting or not succeeded:
                    return {
                        'content': '', 'messages': [],
                        'chatActions': {'reactions': []}, 'commit': None, 'scriptEntry': None,
                    }
                current = self.get_story(story.get('id'))
                reactions = [
                    {'messageRef': _field(reaction, 'messageRef'), 'reaction': _field(reaction, 'reaction')}
                    for reaction in (chat_actions.get('reactions') or [])
                ]
                normalized: Dict[str, Any] = {**decision, 'messageReactions': reactions}
                # 上游显式传 undefined；这里与之一致地写入 None（缺省语义）。
                normalized['localMedia'] = decision.get('localMedia') if sticker else None
                normalized['nativeFace'] = decision.get('nativeFace') if native_face else None
                persisted = self.persist_decision(
                    current, None, normalized, snapshot.get('from'), snapshot.get('now'), False, 'user-message',
                )
                max_characters = _config_get(self.config, 'runtime', 'maxMessageCharacters', default=0)
                separator = _config_get(self.config, 'runtime', 'messageSeparator')
                if separator is None:
                    # 上游传 undefined 时走 helper 默认值 <sep/>。
                    content = normalize_group_visible_reply(
                        decision.get('groupReply'), decision.get('interaction'), max_characters)
                else:
                    content = normalize_group_visible_reply(
                        decision.get('groupReply'), decision.get('interaction'), max_characters, separator)
                self.db_set('interlude_story', {'id': current.get('id')},
                            {'cursorAt': snapshot.get('now'), 'updatedAt': now_utc()})
                if succeeded:
                    self.schedule_conversation_follow_ups_after_turn(
                        current.get('id'), snapshot.get('now'), decision.get('interaction'))
                return {
                    'content': content, 'messages': persisted.get('messages'), 'chatActions': chat_actions,
                    'sticker': sticker, 'nativeFace': native_face,
                    'commit': persisted.get('commit'), 'scriptEntry': persisted.get('scriptEntry'),
                }

            result = self.serial(story.get('id'), persist_group_turn)
            platform_entry_id = _field(result.get('scriptEntry'), 'id')
            completed_reactions = 0
            if _field(_field(result, 'chatActions'), 'reactions') and turn.get('latestSession'):
                completed_reactions = self.execute_group_reactions(
                    snapshot.get('story'),
                    turn.get('latestSession'),
                    turn.get('groupId'),
                    result['chatActions']['reactions'],
                    lambda reaction: platform_action_reference(
                        result.get('commit'), platform_entry_id, 'message-reaction',
                        '%s:%s' % (_field(reaction, 'messageRef'), _field(reaction, 'reaction')),
                    ),
                )
            if result.get('content'):
                reply_to = _field(result.get('chatActions'), 'replyTo') or {}
                group_delivery = self.send_group_message(
                    snapshot.get('story'), turn.get('channelId'), result.get('content'),
                    _field(reply_to, 'messageId'), turn.get('latestSession'),
                )
            else:
                group_delivery = {'deliveredSegments': [], 'complete': False, 'segmentOutcomes': []}
            if group_delivery.get('segmentOutcomes') or group_delivery.get('deliveredSegments'):
                def persist_group_delivery() -> None:
                    current = self.get_story(story.get('id'))
                    moment = now_utc()
                    group_event = find_group_script_event(result.get('commit')) if result.get('commit') else None
                    script_event = message_event_reference(group_event, 0, platform_entry_id) if group_event else None
                    if script_event:
                        for outcome in (group_delivery.get('segmentOutcomes') or []):
                            self.update_script_delivery_outcome(
                                current.get('id'),
                                {**script_event, 'segmentIndex': _field(outcome, 'index')},
                                _field(outcome, 'status'), moment, _field(outcome, 'reason'),
                            )
                    if group_delivery.get('deliveredSegments'):
                        metadata: Dict[str, Any] = {
                            'groupId': turn.get('groupId'), 'channelId': turn.get('channelId'),
                            **(script_event or {}),
                            'deliverySegmentIndexes': [
                                _field(item, 'index') for item in (group_delivery.get('segmentOutcomes') or [])
                                if _field(item, 'status') == 'delivered'
                            ],
                        }
                        if not group_delivery.get('complete'):
                            metadata['partialDelivery'] = True
                            metadata['deliveredSegments'] = len(group_delivery['deliveredSegments'])
                        reply_to = _field(result.get('chatActions'), 'replyTo')
                        if reply_to:
                            metadata['replyTo'] = _field(reply_to, 'messageRef')
                        self.append_entry(current.get('id'), {
                            'kind': 'character-group-message', 'actor': 'character',
                            'content': '<sep/>'.join(group_delivery['deliveredSegments']),
                            'occurredAt': iso(moment),
                            'metadata': metadata,
                        }, moment)

                self.serial(story.get('id'), persist_group_delivery)
            sticker_delivered: Any = False
            if result.get('sticker') and turn.get('latestSession'):
                sticker_delivered = self.send_sticker(
                    snapshot.get('story'), turn.get('latestSession'), turn.get('channelId'), result.get('sticker'),
                    turn.get('groupId'),
                    platform_action_reference(result.get('commit'), platform_entry_id, 'local-media',
                                              _field(result.get('sticker'), 'assetId')),
                )
            native_face_delivered: Any = False
            if result.get('nativeFace') and turn.get('latestSession'):
                native_face_delivered = self.send_native_face(
                    snapshot.get('story'), turn.get('latestSession'), turn.get('channelId'), result.get('nativeFace'),
                    turn.get('groupId'),
                    platform_action_reference(result.get('commit'), platform_entry_id, 'native-face',
                                              result.get('nativeFace')),
                )
            if (group_delivery.get('deliveredSegments') or completed_reactions
                    or sticker_delivered or native_face_delivered):
                self.group_willingness[key] = consume_group_willingness(
                    self.group_willingness.get(key), _field(rule, 'willingness'), _now_ms(),
                )
            self.schedule_compaction(story.get('id'))
        except Exception as error:  # noqa: BLE001 - 上游 catch 后保持静默
            self.report('warn', story, 'user-message', '群聊主叙事失败，保持静默 群=%s 错误=%s',
                        turn.get('groupId'), error)
        finally:
            self.narrating_stories.discard(turn.get('storyId'))
            if not turn.get('messages') and turn.get('revision') == revision:
                # 同 flush_buffered_narrative：清掉已触发的死定时器句柄。
                turn['timer'] = None
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)

    # ================= 上游 1929-1951：groupMessages =================

    def group_messages(self, story_id: str, group_id: str, limit: int) -> List[Dict[str, Any]]:
        """上游 1929-1951 groupMessages：供群上下文使用的最近群消息（时间升序）。"""
        rows = self.db_get('interlude_script_entry', {'storyId': story_id}, {
            'limit': int(max(20, min(200, limit * 8))), 'sort': {'occurredAt': 'desc'},
        })
        normalized_group = normalize_group_id(group_id)
        filtered = [
            entry for entry in rows
            if entry.get('kind') in ('group-message', 'character-group-message')
            and normalize_group_id(_field(entry.get('metadata'), 'groupId')) == normalized_group
        ]
        selected = filtered[:int(max(1, limit))]
        messages: List[Dict[str, Any]] = []
        for entry in reversed(selected):
            metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
            actor_is_character = entry.get('actor') == 'character'
            sender_id_raw = metadata.get('senderId')
            sender_id = _js_string(('character' if actor_is_character else 'unknown')
                                   if sender_id_raw is None else sender_id_raw)
            sender_name_raw = metadata.get('senderName')
            sender_name_default = ('主角' if actor_is_character
                                   else ('群成员' if sender_id_raw is None else _js_string(sender_id_raw)))
            sender_name = _js_string(sender_name_default if sender_name_raw is None else sender_name_raw)
            message: Dict[str, Any] = {
                'senderId': sender_id,
                'senderName': sender_name,
                'speaker': format_group_speaker(sender_name, sender_id),
            }
            message_id = targetable_message_id(metadata.get('messageId'))
            if message_id:
                message['messageId'] = message_id
                message['messageRef'] = group_message_ref(entry.get('id'))
            quote = normalize_quoted_message_context(metadata.get('quote'))
            if quote:
                message['quote'] = quote
            message['content'] = entry.get('content')
            message['occurredAt'] = entry.get('occurredAt')
            message['direction'] = 'character' if actor_is_character else 'user'
            messages.append(message)
        return messages

    # ================= 上游 1953-1961：groupCooldownActive =================

    def group_cooldown_active(self, story_id: str, group_id: str, cooldown_seconds: float) -> bool:
        """上游 1953-1961 groupCooldownActive：同一群最近一次角色发言是否仍在冷却期。"""
        if cooldown_seconds <= 0:
            return False
        rows = self.db_get('interlude_script_entry', {'storyId': story_id}, {
            'limit': 100, 'sort': {'occurredAt': 'desc'},
        })
        normalized_group = normalize_group_id(group_id)
        latest = next((
            entry for entry in rows
            if entry.get('kind') in ('character-group-message', 'character-platform-action')
            and normalize_group_id(_field(entry.get('metadata'), 'groupId')) == normalized_group
        ), None)
        if not latest:
            return False
        occurred_at = parse_time(latest.get('occurredAt'))
        if occurred_at is None:
            return False
        return (now_utc() - occurred_at) < timedelta(seconds=cooldown_seconds)
