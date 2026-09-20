# -*- coding: utf-8 -*-
"""平台会话对象（非上游文件）。

上游使用 Koishi 的 Session；本项目把它抽象成本数据类，字段与 service.ts 实际用到的
Session 子集一一对应，供 hdsi/** 内部使用。微信侧由 hdsi/platform/wxbot.py 构造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..time_utils import now_utc


@dataclass
class QuotedMessage:
    """被引用消息的快照（上游 QuotedMessageContext）。"""

    senderId: str = ''
    senderName: str = ''
    speaker: str = ''
    content: str = ''
    messageId: str = ''


@dataclass
class MessageElement:
    """入站附件元素；type: image/audio/file/video/record/face/at/text。"""

    type: str
    value: str = ''
    name: str = ''
    url: str = ''
    size: int = 0
    mimeType: str = ''
    raw: Any = None


@dataclass
class InboundSession:
    """一次入站事件；私聊与群聊共用。"""

    platform: str = 'wechat'
    selfId: str = ''
    userId: str = ''
    username: str = ''
    channelId: str = ''          # 私聊=对端窗口，群聊=群 id
    guildId: str = ''            # 群聊时与 channelId 相同；私聊为空
    isDirect: bool = True
    content: str = ''
    messageId: str = ''
    quote: Optional[QuotedMessage] = None
    elements: List[MessageElement] = field(default_factory=list)
    timestamp: datetime = field(default_factory=now_utc)
    sender_name: str = ''
    channel_name: str = ''
    mentioned_bot: bool = False
    quoted_bot: bool = False
    raw: Any = None

    # ---- 上游 service.ts 用到的 session.* 便捷属性 ----
    @property
    def bot(self) -> Any:
        return self.raw

    def quote_ref(self) -> Optional[Dict[str, str]]:
        if not self.quote:
            return None
        return {
            'senderId': self.quote.senderId,
            'senderName': self.quote.senderName,
            'speaker': self.quote.speaker,
            'content': self.quote.content,
            'messageId': self.quote.messageId,
        }

    def image_sources(self) -> List[str]:
        return [element.value or element.url for element in self.elements if element.type == 'image' and (element.value or element.url)]

    def audio_sources(self) -> List[str]:
        return [element.value or element.url for element in self.elements if element.type in ('audio', 'record') and (element.value or element.url)]

    def voice_count(self) -> int:
        return len([element for element in self.elements if element.type in ('audio', 'record')])

    def file_facts(self) -> List[Dict[str, Any]]:
        facts: List[Dict[str, Any]] = []
        for element in self.elements:
            if element.type not in ('file', 'video'):
                continue
            facts.append({
                'name': element.name or element.value or '',
                'url': element.url or element.value or '',
                'size': element.size,
                'audio': element.type == 'file' and element.name.lower().endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.amr', '.silk')),
            })
        return facts
