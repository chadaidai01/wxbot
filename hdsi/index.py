# -*- coding: utf-8 -*-
"""HDS-Interlude 运行时引导（对应上游 src/index.ts 的 Config/apply 部分 + 本项目平台接入）。

上游是 Koishi 插件，由 ctx.database/ctx.http/bots 提供平台能力；本项目改为：
    RuntimeContext(store, platform, logger, base_dir)  +  InterludeService(ctx, config)
hdsi/** 不 import bot.py，所有微信能力经 hdsi/platform/wxbot.py 注入。
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

from .config_model import DEFAULT_CONFIG, Config, resolve_config
from .meta import HDS_INTERLUDE_VERSION
from .platform.adapter import PlatformAdapter
from .platform.session import InboundSession
from .runtime import RuntimeContext
from .service import InterludeService
from .time_utils import now_utc
from .utils import deep_copy, stable_hash


name = 'hds-interlude'
version = HDS_INTERLUDE_VERSION
inject = ['database', 'http']


def apply(ctx: Any, config: Optional[Dict[str, Any]] = None) -> 'HdsiRuntime':
    """Upstream Koishi plugin apply().

    The Python port has no Koishi plugin lifecycle: the platform layer builds a
    RuntimeContext-like object (store/platform/logger/base_dir) and this factory
    returns the ready InterludeService wrapper. Admin commands live in hdsi/commands.py.
    """
    resolved = resolve_config(config or {})
    store = getattr(ctx, 'store', None)
    adapter = getattr(ctx, 'platform', None)
    logger = getattr(ctx, 'logger', None)
    base_dir = getattr(ctx, 'base_dir', '.')
    return HdsiRuntime(resolved, adapter, store, logger, base_dir)


def normalize_chat_completion_endpoint(base_url: str) -> str:
    """把用户填的 base url 归一成 chat/completions 完整地址（对齐上游 endpoint 语义）。"""
    value = (base_url or '').strip().rstrip('/')
    if not value:
        return ''
    if value.endswith('/chat/completions'):
        return value
    return value + '/chat/completions'


def build_model_provider(*, api_key: str, base_url: str, model: str, label: str = 'Primary model',
                         temperature: float = 0.8, max_tokens: int = 4096, timeout_ms: int = 60000,
                         response_format: str = 'json-object') -> Dict[str, Any]:
    """把本项目的 DeepSeek/OpenAI 兼容配置映射成上游 ProviderConfig。"""
    provider = deep_copy(DEFAULT_CONFIG['model']['providers'][0])
    provider.update({
        'label': label,
        'enabled': bool(api_key),
        'mode': 'openai-compatible',
        'apiKey': api_key or '',
        'endpoint': normalize_chat_completion_endpoint(base_url),
        'model': model or '',
        'useForMain': True,
        'useForCompaction': True,
        'useForAlter': True,
        'useForEmbedding': bool(api_key),
        'useForStickers': True,  # 本地表情包库要靠带视觉的贴纸描述器把新素材激活
        'useForVision': False,
    })
    return provider


def build_config(local: Dict[str, Any], *, theater_groups: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """从本项目 config.py 的值构造上游 Config。

    local 字段（缺省时用 DEFAULT_CONFIG 兜底）：
      api_key/base_url/model/main_temperature/main_max_tokens/main_timeout/response_format
      theater: dict（THEATER_* 语义，见下方映射）
    theater_groups: [{'groupId','label','purpose','characterRole','responseMode','contextLimit',
                      'debounceSeconds','cooldownSeconds','willingness': {...}}]
    """
    api_key = str(local.get('api_key') or '')
    base_url = str(local.get('base_url') or '')
    model = str(local.get('model') or '')
    overrides: Dict[str, Any] = {
        'model': {
            'providers': [build_model_provider(
                api_key=api_key,
                base_url=base_url,
                model=model,
                temperature=float(local.get('main_temperature') or 0.8),
                max_tokens=int(local.get('main_max_tokens') or 4096),
                timeout_ms=int(local.get('main_timeout_ms') or 60000),
                response_format=str(local.get('response_format') or 'json-object'),
            )],
            'mainTemperature': float(local.get('main_temperature') or 0.8),
            'mainMaxTokens': int(local.get('main_max_tokens') or 4096),
            'mainTimeout': int(local.get('main_timeout_ms') or 60000),
            'mainResponseFormat': str(local.get('response_format') or 'json-object'),
        },
        'onebot': {
            'enabled': False,  # 本项目由 bot.py 的 LISTEN_LIST 控制入口，不再做账号白名单过滤
            'groupChats': list(theater_groups or []),
        },
    }
    story_defaults = dict(local.get('story_defaults') or {})
    if story_defaults:
        overrides['storyDefaults'] = story_defaults
    runtime = dict(local.get('runtime') or {})
    if runtime:
        overrides['runtime'] = runtime
    for key in ('memory', 'alterSystem', 'schedulePreplan', 'agency', 'chatRhythm', 'browser',
                'stickers', 'chatActions', 'logging', 'urge', 'blindMode', 'sharedStory'):
        value = local.get(key)
        if isinstance(value, dict):
            overrides[key] = value
    return resolve_config(overrides)


class HdsiRuntime:
    """wxbot 侧的 HDSI 运行时门面。

    - receive(session, character)：私聊/群聊统一入口（内部按上游 receive/receiveGroup 分流）。
    - character：可选的每聊天角色设定（prompt 文件解析结果）。提供时用哈希后缀隔离成独立主剧本，
      字段：{'key','name','profile','perspective','relationship','world','supportingCast','location','style','timezone'}
    - sweep()：手动触发一次后台扫描（正常由 service 自己的定时器驱动）。
    """

    def __init__(self, config: Dict[str, Any], adapter: PlatformAdapter, store: Any,
                 logger: Optional[logging.Logger] = None, base_dir: str = '.'):
        self.config = config
        self.adapter = adapter
        self.store = store
        self.logger = logger or logging.getLogger('hdsi')
        self.ctx = RuntimeContext(store, adapter, self.logger, base_dir)
        self.service = InterludeService(self.ctx, config)
        self._closed = False

    # ---- 角色 → 主剧本 ----
    def character_scoped_session(self, session: InboundSession, character: Optional[Dict[str, Any]]) -> InboundSession:
        if not character:
            return session
        key = str(character.get('key') or '').strip()
        if not key:
            canon = '|'.join(str(character.get(field) or '') for field in
                              ('name', 'profile', 'perspective', 'world', 'relationship', 'supportingCast', 'location', 'style'))
            key = stable_hash(canon, 12)
        session.selfId = f"{session.selfId}~{key}"
        return session

    def ensure_story(self, session: InboundSession, character: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        story = self.service.find_story(session)
        if story is None:
            try:
                story = self.service.create_story(session, (character or {}).get('name'))
            except Exception as error:  # noqa: BLE001
                self.logger.warning('HDSI 创建主剧本失败：%s', error)
                return None
        if character and story:
            # 保险：主剧本若被“单 active 剧本”清理逻辑归档，收到消息时自动恢复，
            # 否则 receive 会因为 status != 'active' 静默返回（表现为“hdsi 调不出来”）。
            if str(story.get('status') or '') == 'archived':
                try:
                    story = self.service.set_status(story, 'active')
                    self.logger.warning('HDSI 主剧本曾被归档，收到消息后已自动恢复 active：%s', story.get('id'))
                except Exception as error:  # noqa: BLE001
                    self.logger.warning('HDSI 恢复归档主剧本失败：%s', error)
            patch = {
                'character': {
                    'name': str(character.get('name') or story['setting']['character']['name']),
                    'profile': str(character.get('profile') or ''),
                },
            }
            for field in ('perspective', 'relationship', 'world', 'supportingCast', 'location', 'style'):
                if character.get(field):
                    patch[field] = character[field]
            if character.get('timezone'):
                patch['timezone'] = character['timezone']
            try:
                self.service.update_setting(story, patch)
            except Exception as error:  # noqa: BLE001
                self.logger.warning('HDSI 更新角色设定失败：%s', error)
        return story

    def refresh_stickers(self) -> None:
        """重新扫描本地贴纸库，登记新素材（偷来的表情包靠这个进入模型目录）。"""
        try:
            self.service.scan_sticker_library()
        except Exception:
            self.logger.exception('HDSI 贴纸库扫描失败')

    # ---- 入口 ----
    def receive(self, session: InboundSession, character: Optional[Dict[str, Any]] = None) -> None:
        if self._closed:
            return
        session = self.character_scoped_session(session, character)
        # 把每个聊天的角色 Prompt（名字/profile）写入或更新到对应主剧本。
        # 之前这里遗漏调用，故事一直是默认的 'Unnamed character' + 空 profile，
        # 模型因此既没有人设、也不认识自己的微信昵称，被 @ 也只会沉默。
        self.ensure_story(session, character)
        try:
            if session.isDirect:
                self.service.receive(session, session.timestamp)
            else:
                self.service.receive_group(session, session.timestamp)
        except Exception:
            self.logger.exception('HDSI 处理入站消息失败 会话=%s', session.channelId or session.userId)

    def sweep(self) -> None:
        if self._closed:
            return
        try:
            self.service.sweep()
        except Exception:
            self.logger.exception('HDSI 后台扫描失败')

    def close(self) -> None:
        self._closed = True
        self.ctx.stop()

    # ---- 管理命令入口（供 hdsi/commands.py / bot.py 使用） ----
    def command_target_story(self, session: InboundSession):
        return self.service.find_story(session)
