# -*- coding: utf-8 -*-
"""平台能力接口（非上游文件）。

上游 service.ts 通过 Koishi 的 bot/adapter/ctx.http/ctx.puppeteer/node:fs 直接访问平台。
Python 里统一收敛到这个接口：hdsi/** 只调用 PlatformAdapter，具体实现见 wxbot.py。
没有对应能力的平台返回 None/False，走上游既有的降级分支。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .session import InboundSession


class PlatformAdapter:
    # ---- 目录 / 文件 ----
    base_dir: str = '.'

    def read_file(self, path: str) -> Optional[bytes]:
        return None

    def file_size(self, path: str) -> int:
        return 0

    def list_files(self, directory: str) -> List[Dict[str, Any]]:
        """返回 [{path, name, size, mtime}]；目录不存在返回 []。"""
        return []

    # ---- 发送 ----
    def send_private(self, participant: Dict[str, Any], content: str, quote_message_id: Optional[str] = None) -> Optional[str]:
        """私聊投递，成功返回 messageId。"""
        return None

    def send_group(self, story: Dict[str, Any], channel_id: str, content: str,
                   reply_to_message_id: Optional[str] = None, session: Optional[InboundSession] = None) -> Optional[str]:
        """群聊投递，成功返回 messageId。"""
        return None

    def send_sticker(self, story: Dict[str, Any], channel_id: str, file_path: str,
                     session: Optional[InboundSession] = None) -> Optional[str]:
        return None

    def send_native_face(self, story: Dict[str, Any], channel_id: str, face_id: Any,
                         session: Optional[InboundSession] = None) -> Optional[str]:
        return None

    def send_reaction(self, story: Dict[str, Any], channel_id: str, message_id: str,
                      reaction: str, direction: str = 'add', session: Optional[InboundSession] = None) -> bool:
        return False

    # ---- 媒体获取 ----
    def fetch_image(self, source: str, session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        """返回 {mimeType, dataUri, bytes?}；失败返回 None。"""
        return None

    def fetch_audio(self, source: str, session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        """返回 {format, base64}；失败返回 None。"""
        return None

    def downscale_image(self, mime_type: str, data_uri: str) -> Optional[str]:
        """可选：返回压缩后的 dataUri；无 Puppeteer 能力时返回 None。"""
        return None

    def render_animated_frame(self, data_uri: str) -> Optional[str]:
        return None

    # ---- 群成员 / 机器人 ----
    def group_member_name(self, group_id: str, user_id: str) -> str:
        return ''

    def find_bot(self, platform: str, self_id: str) -> Any:
        return None

    def list_bots(self) -> List[Any]:
        return []

    # ---- 网络观察（可选） ----
    def http_get(self, url: str) -> Optional[str]:
        return None

    def search_web(self, query: str) -> Optional[str]:
        return None

    def render_page(self, url: str) -> Optional[str]:
        return None

    # ---- 能力声明 ----
    def chat_capabilities(self, kind: str, session: Optional[InboundSession] = None) -> Optional[Dict[str, Any]]:
        """kind: private | group；返回上游 ChatActionCapabilities 结构，未启用返回 None。"""
        return None

    def capabilities_for_participant(self, participant: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None
