# -*- coding: utf-8 -*-
"""HDS-Interlude 移植：上游 src/service.ts 行号 932-1221、1222-1605、5589-5700、5683-5866。

ServiceStoryMixin —— 会话过滤 / 故事与参与者 CRUD / 设定与状态更新 / 管理命令 /
purge / legacy 迁移 / continuity 初始化 / 参与者消息时间戳记录。

本类是 InterludeService 的一个 mixin（不定义 __init__），字段与基础设施由
hdsi/service_base.py 提供：self.config / self.store / self.platform / self.logger /
self.ctx / self.db_get / self.db_create / self.db_set / self.db_remove / self.purge_table /
self.report_operation / self.report_standalone / self.report_standalone_operation /
self.allows_verbosity / self.serial / self.get_story / self.shared_story_config /
self.memory_config / self.model_routing / self.database_resetting 等。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .platform.session import InboundSession
from .story_state import decode_story_state, encode_story_state, normalize_participant_state
from .time_utils import format_log_time, iso, now_utc, resolve_timezone
from .types import empty_participant_state, empty_story_setting, empty_story_state
from .utils import clip


def _trim(value: Any) -> str:
    """TS `value?.trim()`：非字符串（含 undefined/null）返回空串。"""
    return value.strip() if isinstance(value, str) else ''


def _valid_timezone(name: Any) -> bool:
    """对应 `new Intl.DateTimeFormat('en-US', { timeZone: setting.timezone })` 的校验。

    time_utils.resolve_timezone 对无效时区返回 'UTC'，对可用时区（含无 tzdata 环境下的
    固定偏移降级表）原样返回，因此用它判定。
    """
    candidate = (name or '').strip() if isinstance(name, str) else ''
    if not candidate:
        return False
    return resolve_timezone(candidate) == candidate


# 上游 service.ts 7009-8100 的顶层 helper 由 hdsi/service_helpers.py 提供（并行移植）。
try:
    from .service_helpers import (
        is_enabled_account,
        is_one_bot_platform,
        legacy_story_id_for,
        merge_participant_state,
        merge_setting,
        normalize_account_id,
        normalize_group_id,
        participant_id_for,
        participant_id_for_story,
        same_participant_endpoint,
        same_platform_family,
        session_group_id,
        story_id_for_character,
    )
except ImportError:  # TODO(并行移植): service_helpers 暂缺这些名字时回退到本文件内联副本（语义与上游一致）
    def story_id_for_character(platform: str, self_id: str) -> str:
        return f'character:{platform}:{self_id}'

    def legacy_story_id_for(platform: str, self_id: str, user_id: str) -> str:
        return f'{platform}:{self_id}:{user_id}'

    def participant_id_for(platform: str, self_id: str, user_id: str) -> str:
        return f'{platform}:{self_id}:{user_id}'

    def participant_id_for_story(story_id: str, platform: str, self_id: str, user_id: str) -> str:
        return f'{participant_id_for(platform, self_id, user_id)}:{story_id}'[:255]

    def same_participant_endpoint(participant: Dict[str, Any], session: InboundSession) -> bool:
        onebot_pair = is_one_bot_platform(participant.get('platform')) and is_one_bot_platform(session.platform)
        return (
            (participant.get('platform') == session.platform or onebot_pair)
            and normalize_account_id(participant.get('selfId')) == normalize_account_id(session.selfId)
            and normalize_account_id(participant.get('userId')) == normalize_account_id(session.userId)
        )

    def is_one_bot_platform(platform: Any) -> bool:
        value = str(platform if platform is not None else '').lower()
        return (
            value == 'onebot' or value.startswith('onebot:')
            or value == 'napcat' or value.startswith('napcat:')
            or value == 'qq:onebot' or value.startswith('qq:onebot:')
        )

    def session_group_id(session: InboundSession) -> str:
        raw = str(session.guildId or session.channelId or '')
        return normalize_group_id(raw)

    def normalize_group_id(value: Any) -> str:
        return re.sub(r'^(?:group|guild):', '', str(value if value is not None else '').strip(), count=1, flags=re.IGNORECASE)

    def normalize_account_id(value: Any) -> str:
        normalized = str(value if value is not None else '').strip().lower()
        for _ in range(3):
            next_value = re.sub(r'^(?:private|user|onebot|napcat|qq):', '', normalized, count=1, flags=re.IGNORECASE).strip()
            if next_value == normalized:
                break
            normalized = next_value
        return normalized

    def is_enabled_account(accounts: Any, qq: Any) -> bool:
        normalized = normalize_account_id(qq)
        if not normalized:
            return False
        return any(
            (account or {}).get('enabled') is not False and normalize_account_id((account or {}).get('qq')) == normalized
            for account in (accounts or [])
        )

    def same_platform_family(left: Any, right: Any) -> bool:
        if is_one_bot_platform(left) and is_one_bot_platform(right):
            return True
        return str(left if left is not None else '').strip().lower() == str(right if right is not None else '').strip().lower()

    def merge_setting(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        patch = patch or {}
        return {
            **base, **patch,
            'character': {**(base.get('character') or {}), **(patch.get('character') or {})},
            'user': {**(base.get('user') or {}), **(patch.get('user') or {})},
        }

    def merge_participant_state(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        patch = patch or {}
        merged = {**(base or {}), **patch}
        merged['openThreads'] = patch['openThreads'] if isinstance(patch.get('openThreads'), list) else (base or {}).get('openThreads')
        merged['relationshipNotes'] = patch['relationshipNotes'] if isinstance(patch.get('relationshipNotes'), list) else (base or {}).get('relationshipNotes')
        return merged


class ServiceStoryMixin:
    """会话过滤 / 故事与参与者 CRUD / 设定与状态更新 / 管理命令 / purge / 迁移 / continuity。"""

    # ================= 上游 932-996：会话与账号白名单 =================

    def can_handle_session(self, session: InboundSession) -> bool:
        if not is_one_bot_platform(session.platform):
            return True
        config = self.config.get('onebot') or {}
        # Backwards compatibility: an absent/disabled gate does not change old
        # installations. Once enabled, an empty list is intentionally deny-all.
        if not config.get('enabled'):
            return True
        self_id = normalize_account_id(session.selfId)
        user_id = normalize_account_id(session.userId)
        if config.get('ignoreSelfMessages') and self_id and self_id == user_id:
            return False
        if not is_enabled_account(config.get('botAccounts'), self_id):
            self.report_standalone_operation('diagnostic', 'debug', 'OneBot 白名单拒绝机器人账号 平台=%s 原始机器人ID=%s 规范化ID=%s', session.platform, session.selfId, self_id)
            return False
        # HDSI deliberately uses an explicit allowlist.  A legacy userMode field
        # is ignored so an old `blocklist` value cannot silently open the bot to
        # every QQ account after an upgrade.
        allowed = is_enabled_account(config.get('userAccounts'), user_id)
        if not allowed:
            self.report_standalone_operation('diagnostic', 'debug', 'OneBot 白名单拒绝用户账号 原始用户ID=%s 规范化ID=%s', session.userId, user_id)
        return allowed

    # 上游 953-965
    def can_handle_group_session(self, session: InboundSession) -> bool:
        """Group access uses an explicit group allowlist; group members do not need
        to be present in the private-message user whitelist.

        平台适配（非上游）：上游只把群聊接到 OneBot；本项目微信侧同样使用
        onebot.groupChats 规则表（groupId 为群窗口名），所以非 OneBot 平台也按
        群规则白名单判定，而不是一律拒绝。
        """
        if not is_one_bot_platform(session.platform):
            group = self.group_rule(session_group_id(session))
            return bool(group and group.get('enabled'))
        config = self.config.get('onebot') or {}
        if not config.get('enabled'):
            return False
        self_id = normalize_account_id(session.selfId)
        user_id = normalize_account_id(session.userId)
        if config.get('ignoreSelfMessages') and self_id and self_id == user_id:
            return False
        if not is_enabled_account(config.get('botAccounts'), self_id):
            return False
        group = self.group_rule(session_group_id(session))
        return bool(group and group.get('enabled'))

    # 上游 967-970
    def group_rule(self, group_id: str) -> Optional[Dict[str, Any]]:
        normalized = normalize_group_id(group_id)
        groups = (self.config.get('onebot') or {}).get('groupChats') or []
        return next((
            group for group in groups
            if group.get('enabled') is not False and normalize_group_id(group.get('groupId')) == normalized
        ), None)

    # 上游 972-979
    def can_handle_participant(self, participant: Dict[str, Any]) -> bool:
        """Same account gate for direct-message work that already has a participant."""
        if not is_one_bot_platform(participant.get('platform')):
            return True
        config = self.config.get('onebot') or {}
        if not config.get('enabled'):
            return True
        if not is_enabled_account(config.get('botAccounts'), normalize_account_id(participant.get('selfId'))):
            return False
        return is_enabled_account(config.get('userAccounts'), normalize_account_id(participant.get('userId')))

    # 上游 981-988
    def can_manage_session(self, session: InboundSession) -> bool:
        if not self.can_handle_session(session):
            self.report_standalone_operation('diagnostic', 'debug', '私聊被 OneBot 白名单拦截 平台=%s 机器人ID=%s 用户ID=%s', session.platform, session.selfId, session.userId)
            return False
        managers = [
            str(value if value is not None else '').strip()
            for value in (self.shared_story_config.get('managerAccounts') or [])
        ]
        managers = [value for value in managers if value]
        return not managers or any(normalize_account_id(value) == normalize_account_id(session.userId) for value in managers)

    # 上游 990-996
    def can_handle_story(self, story: Dict[str, Any]) -> bool:
        """Background life updates only require the bot account to remain enabled."""
        if not is_one_bot_platform(story.get('platform')):
            return True
        config = self.config.get('onebot') or {}
        if not config.get('enabled'):
            return True
        return is_enabled_account(config.get('botAccounts'), normalize_account_id(story.get('selfId')))

    # ================= 上游 998-1080：故事/参与者查询 =================

    def find_story(self, session: InboundSession) -> Optional[Dict[str, Any]]:
        # 上游 998-1029
        if self.shared_story_config.get('enabled'):
            # Shared mode deliberately has one canonical active story in the whole
            # Koishi instance. Sandbox and OneBot must not run parallel lives.
            existing = self.get_canonical_story(story_id_for_character(session.platform, session.selfId))
            if not existing:
                # 暂停中的故事对 active-only 的主查询不可见，但管理命令（resume/
                # status/pause）必须仍能找到它——否则 pause 之后 resume 永远报
                # "没有故事"，story.start 还会撞主键领回暂停旧故事，形成死锁。
                # 只补 paused，不采纳 archived；调度路径（getCanonicalStory）
                # 保持 active-only，暂停语义不变。
                existing = self.get_paused_story(story_id_for_character(session.platform, session.selfId))
            if existing:
                existing = self.repair_canonical_one_bot_story_transport(existing, session)
                shared_id = story_id_for_character(session.platform, session.selfId)
                if existing.get('platform') == session.platform and existing.get('id') != shared_id:
                    return self.migrate_legacy_story(existing, session)
                self.migrate_legacy_branch_into_shared(existing, session)
                return existing
        story_id = legacy_story_id_for(session.platform, session.selfId, session.userId)
        rows = self.db_get('interlude_story', {'id': story_id})
        existing = rows[0] if rows else None
        if existing or not self.shared_story_config.get('enabled'):
            return existing

        # Old beta versions used one story id per QQ. Migrate lazily when that QQ
        # first returns, so existing scripts become the first relationship branch
        # of the new shared story instead of silently disappearing.
        legacy_id = legacy_story_id_for(session.platform, session.selfId, session.userId)
        legacy_rows = self.db_get('interlude_story', {'id': legacy_id})
        legacy = legacy_rows[0] if legacy_rows else None
        return self.migrate_legacy_story(legacy, session) if legacy else None

    # 上游 1031-1039
    def get_paused_story(self, preferred_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Paused stories stay invisible to scheduling but reachable by management
        commands. No archiving here: this lookup never resolves conflicts."""
        paused = self.db_get('interlude_story', {'status': 'paused'}, {'sort': {'updatedAt': 'desc'}})
        if not paused:
            return None
        if preferred_id:
            found = next((story for story in paused if story.get('id') == preferred_id), None)
            if found is not None:
                return found
        found = next((story for story in paused if str(story.get('id') or '').startswith('character:')), None)
        return found if found is not None else paused[0]

    # 上游 1041-1061
    def get_canonical_story(self, preferred_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resolve and enforce the one global active story. The preferred id wins
        when present; otherwise the most recently updated row is retained and
        every other active row is archived immediately."""
        active = self.db_get('interlude_story', {'status': 'active'}, {'sort': {'updatedAt': 'desc'}})
        if not active:
            return None
        canonical = None
        if preferred_id:
            canonical = next((story for story in active if story.get('id') == preferred_id), None)
        if canonical is None:
            canonical = next((story for story in active if str(story.get('id') or '').startswith('character:')), None)
        if canonical is None:
            canonical = active[0]
        now = now_utc()
        for story in active:
            if story.get('id') == canonical.get('id'):
                continue
            self.db_set('interlude_story', {'id': story.get('id')}, {'status': 'archived', 'updatedAt': now})
            self.report_standalone('warn', '主剧本归档完成 原因=检测到多个活动故事 保留=%s 已归档=%s 范围=%s', canonical.get('id'), story.get('id'), '全局')
        return canonical

    # 上游 1063-1073
    def find_participant(self, session: InboundSession, story: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        resolved = story if story is not None else self.find_story(session)
        if not resolved:
            return None
        # Participant ids from older betas were global to a bot/user pair.  Do
        # not trust that id alone: when shared mode is toggled or a legacy branch
        # is being migrated, the same pair can temporarily exist under another
        # story.  The story-bound lookup prevents accidentally moving or exposing
        # the wrong relationship branch.
        rows = self.db_get('interlude_participant', {'storyId': resolved.get('id')})
        return next((item for item in rows if same_participant_endpoint(item, session)), None)

    # 上游 1075-1080
    def participants(self, story_id: str, include_paused: bool = False) -> List[Dict[str, Any]]:
        rows = self.db_get('interlude_participant', {'storyId': story_id})
        filtered = [participant for participant in rows if include_paused or participant.get('status') == 'active']
        return sorted(filtered, key=lambda participant: participant.get('updatedAt'), reverse=True)

    # ================= 上游 1082-1219：故事与参与者创建 =================

    def create_story(self, session: InboundSession, name: Optional[str] = None) -> Dict[str, Any]:
        if not self.can_handle_session(session) and not self.can_handle_group_session(session):
            raise RuntimeError('This session is not allowed to use HDS Interlude.')
        existing = self.find_story(session)
        if existing:
            if session.isDirect:
                self.ensure_participant(existing, session)
            return existing
        now = now_utc()
        setting = self.initial_story_setting(name)
        story: Dict[str, Any] = {
            'id': story_id_for_character(session.platform, session.selfId)
            if self.shared_story_config.get('enabled')
            else legacy_story_id_for(session.platform, session.selfId, session.userId),
            'platform': session.platform, 'selfId': session.selfId, 'userId': '',
            'channelId': '', 'status': 'active', 'setting': setting, 'state': empty_story_state(),
            'cursorAt': now, 'createdAt': now, 'updatedAt': now,
        }
        try:
            self.db_create('interlude_story', story)
        except Exception as error:
            # Two accounts can DM a newly started bot at almost the same time. The
            # database primary key is the final arbiter; join the winner instead of
            # failing one participant's first message.
            raced_rows = self.db_get('interlude_story', {'id': story['id']})
            raced = raced_rows[0] if raced_rows else None
            if not raced:
                raise error
            self.ensure_continuity(raced, now)
            self.ensure_participant(raced, session, now)
            return raced
        self.ensure_continuity(story, now)
        if session.isDirect:
            self.ensure_participant(story, session, now)
        self.append_entry(story['id'], {
            'kind': 'setup', 'actor': 'system', 'content': f"The story begins with {setting['character']['name']}.",
            'occurredAt': iso(now), 'metadata': {},
        }, now)
        self.schedule_next_automatic_advance(story['id'], now)
        return story

    # 上游 1121-1151
    def story_start_readiness(self, session: InboundSession) -> Dict[str, Any]:
        """Read-only preflight for manually starting a runtime story from Console defaults."""
        setting = self.initial_story_setting()
        blockers: List[str] = []
        warnings: List[str] = []
        if not self.can_handle_session(session):
            blockers.append('当前机器人账号或用户账号未通过 OneBot 白名单。')
        if not (setting['character'].get('name') or '').strip():
            blockers.append('storyDefaults.characterName 为空。')
        if not (setting['character'].get('profile') or '').strip():
            blockers.append('storyDefaults.characterProfile 尚未填写。')
        if not _valid_timezone(setting.get('timezone')):
            blockers.append(f"时区无效：{setting.get('timezone')}")
        providers = self.model_routing.get('providers') or []
        if any((provider or {}).get('enabled') and (provider or {}).get('endpoint') for provider in providers):
            if not (self.model_routing.get('main') or {}).get('available'):
                blockers.append('没有可用的主叙事模型：请在模型中心勾选一条“用作主叙事模型”。')
        else:
            warnings.append('尚未配置启用的模型连接：可用于安装验证，但不会生成远程叙事。')
        if not (setting.get('perspective') or '').strip():
            warnings.append('Perspective 尚未填写；主角将仅使用 Canon 与已有 Overlay。')
        if not (setting.get('world') or '').strip():
            warnings.append('world 尚未填写；建议在 Console 补充现实边界与地点背景。')
        existing = self.find_story(session)
        return {
            'ready': len(blockers) == 0,
            'existing': existing, 'blockers': blockers, 'warnings': warnings,
            'preview': {
                'characterName': setting['character'].get('name'),
                'characterProfile': bool((setting['character'].get('profile') or '').strip()),
                'perspective': bool((setting.get('perspective') or '').strip()),
                'world': bool((setting.get('world') or '').strip()),
                'timezone': setting.get('timezone'),
                'model': self.main_model_label(),
                'autoCreate': (self.config.get('runtime') or {}).get('autoCreate', False) is not False,
            },
        }

    # 上游 1153-1220
    def ensure_participant(self, story: Dict[str, Any], session: InboundSession,
                           now: Optional[datetime] = None,
                           known_existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Enrolls a QQ account as a relationship branch and synchronizes its Console
        identity fields. Callers that already resolved the participant can pass it
        in to avoid a second database read."""
        now = now if now is not None else now_utc()
        account = self.user_account_rule(session.userId)
        preset = self.participant_preset(session.userId)
        existing = known_existing if known_existing is not None else self.find_participant(session, story)
        if existing:
            # Console edits to the whitelist are intentional identity changes.
            # Keep relationship evolution in participant.state.relationshipOverlay;
            # only this base profile/relationship is refreshed here.
            person_id = _trim((account or {}).get('personId')) or _trim((preset or {}).get('personId')) or existing.get('personId') or session.userId
            display_name = _trim((account or {}).get('label')) or _trim((preset or {}).get('label')) or existing.get('displayName') or session.username or session.userId
            profile = _trim((account or {}).get('profile')) or _trim((preset or {}).get('profile')) or existing.get('profile') or (self.config.get('storyDefaults') or {}).get('userProfile', '')
            relationship = _trim((account or {}).get('relationship')) or _trim((preset or {}).get('relationship')) or existing.get('relationship') or (self.config.get('storyDefaults') or {}).get('relationship', '')
            changed = (
                existing.get('storyId') != story.get('id')
                or existing.get('channelId') != session.channelId
                or existing.get('personId') != person_id
                or existing.get('displayName') != display_name
                or existing.get('profile') != profile
                or existing.get('relationship') != relationship
            )
            if changed:
                self.db_set('interlude_participant', {'id': existing.get('id')}, {
                    'storyId': story.get('id'), 'channelId': session.channelId, 'personId': person_id,
                    'displayName': display_name, 'profile': profile, 'relationship': relationship, 'updatedAt': now,
                })
                self.report_operation('diagnostic', 'debug', story, 'user-message', '参与者资料已从 Console 同步 参与者=%s', existing.get('id'))
            return {
                **existing, 'storyId': story.get('id'), 'channelId': session.channelId, 'personId': person_id,
                'displayName': display_name, 'profile': profile, 'relationship': relationship,
                'updatedAt': now if changed else existing.get('updatedAt'),
            }
        base_id = participant_id_for(session.platform, session.selfId, session.userId)
        # Keep the historical id whenever it is free.  If an old per-account
        # story still owns it, use a deterministic story suffix instead of
        # stealing that branch's primary key during rollback/migration.
        globally_existing = self.get_participant(base_id)
        participant_id = base_id if (
            not globally_existing or globally_existing.get('storyId') == story.get('id')
        ) else participant_id_for_story(story.get('id'), session.platform, session.selfId, session.userId)
        participant: Dict[str, Any] = {
            'id': participant_id, 'storyId': story.get('id'), 'platform': session.platform, 'selfId': session.selfId,
            'userId': session.userId, 'channelId': session.channelId,
            'personId': _trim((account or {}).get('personId')) or _trim((preset or {}).get('personId')) or session.userId,
            'displayName': _trim((account or {}).get('label')) or _trim((preset or {}).get('label')) or session.username or session.userId,
            'profile': _trim((account or {}).get('profile')) or _trim((preset or {}).get('profile')) or (self.config.get('storyDefaults') or {}).get('userProfile', ''),
            'relationship': _trim((account or {}).get('relationship')) or _trim((preset or {}).get('relationship')) or (self.config.get('storyDefaults') or {}).get('relationship', ''),
            'state': empty_participant_state(), 'status': 'active', 'createdAt': now, 'updatedAt': now,
        }
        try:
            self.db_create('interlude_participant', participant)
        except Exception as error:
            # Two first private messages can arrive before either request enters the
            # story queue.  The primary key resolves that race; return the branch
            # created by the other request instead of failing one message.
            raced = self.find_participant(session, story)
            if not raced:
                raise error
            return raced
        self.append_entry(story.get('id'), {
            'kind': 'participant-joined', 'actor': 'system',
            'content': f"{participant['displayName']} entered the character's relationship network.",
            'occurredAt': iso(now), 'metadata': {'personId': participant['personId']},
        }, now, participant['id'])
        return participant

    # ================= 上游 1222-1412：设定/状态与管理员命令 =================

    # 上游 1222-1227
    def update_setting(self, story: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        setting = merge_setting(story.get('setting') or {}, patch)
        now = now_utc()
        self.db_set('interlude_story', {'id': story.get('id')}, {'setting': setting, 'updatedAt': now})
        return {**story, 'setting': setting, 'updatedAt': now}

    # 上游 1229-1233
    def set_status(self, story: Dict[str, Any], status: str) -> Dict[str, Any]:
        now = now_utc()
        self.db_set('interlude_story', {'id': story.get('id')}, {'status': status, 'updatedAt': now})
        return {**story, 'status': status, 'updatedAt': now}

    # 上游 1235-1246
    def recent_entries(self, story_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if limit is None:
            limit = (self.config.get('runtime') or {}).get('contextEntryLimit', 50)
        bounded = max(1, min(limit, 200))
        rows = self.db_get('interlude_script_entry', {'storyId': story_id}, {
            'limit': bounded,
            'sort': {'occurredAt': 'desc'},
        })
        # 批次 4：purge fallback 墓碑不进 prompt（物理删除通常无残留，此为软删兜底过滤）。
        return [entry for entry in rows if entry.get('kind') != 'redacted'][::-1]

    # 上游 1248-1262
    def recent_entries_for_prompt(self, story_id: str, now: datetime) -> List[Dict[str, Any]]:
        """Live narration keeps both a count floor and a recent wall-clock window.
        A burst of conversation can therefore exceed the nominal turn count
        without immediately erasing everything said earlier in the same hour."""
        # M4.1 restores a substantial raw-script continuation window. High-density
        # chat may add dozens of entries without demoting yesterday's causal tail.
        context_entry_limit = (self.config.get('runtime') or {}).get('contextEntryLimit')
        count = max(50, min(context_entry_limit if context_entry_limit is not None else 20, 200))
        context_window = (self.config.get('runtime') or {}).get('contextTimeWindowMinutes')
        minutes = max(0, min(context_window if context_window is not None else 60, 1_440))
        count_rows = self.db_get('interlude_script_entry', {'storyId': story_id}, {'limit': count, 'sort': {'occurredAt': 'desc'}})
        if minutes > 0:
            time_rows = self.db_get('interlude_script_entry', {
                'storyId': story_id,
                'occurredAt': {'$gte': now - timedelta(minutes=minutes)},
            }, {'limit': 500, 'sort': {'occurredAt': 'desc'}})
        else:
            time_rows: List[Dict[str, Any]] = []
        by_id: Dict[Any, Dict[str, Any]] = {}
        for entry in [*count_rows, *time_rows]:
            by_id[entry.get('id')] = entry
        return sorted(by_id.values(), key=lambda entry: (entry.get('occurredAt'), entry.get('id')))

    # 上游 1264-1275
    def memories(self, story_id: str, limit: Optional[int] = None,
                 participant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if limit is None:
            limit = (self.config.get('runtime') or {}).get('memoryLimit', 20)
        bounded = max(1, min(limit * 4, 500))
        rows = self.db_get('interlude_memory', {'storyId': story_id, 'status': 'active'}, {
            'limit': bounded,
            'sort': {'importance': 'desc', 'updatedAt': 'desc'},
        })
        filtered = [
            memory for memory in rows
            if participant_id is None or not memory.get('participantId') or memory.get('participantId') == participant_id
        ]
        ordered = sorted(filtered, key=lambda memory: ((memory.get('importance') or 0), memory.get('updatedAt')), reverse=True)
        return ordered[:limit]

    # 上游 1277-1282
    def admin_facts(self, story_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Administrative view: includes global and participant-specific durable facts."""
        return self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'}, {
            'limit': max(1, min(limit, 100)),
            'sort': {'updatedAt': 'desc'},
        })

    # 上游 1284-1289
    def admin_pending_intents(self, story_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending'}, {
            'limit': max(1, min(limit, 100)),
            'sort': {'notBefore': 'asc'},
        })

    # 上游 1291-1297
    def admin_state_patches(self, story_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db_get('interlude_state_patch', {'storyId': story_id}, {
            'limit': max(1, min(limit, 100)),
            'sort': {'createdAt': 'desc'},
        })

    # 上游 1299-1310
    def add_admin_script_note(self, story: Dict[str, Any], content: str) -> bool:
        """Adds an audit-visible system note without pretending it came from the model."""
        text = clip(content, (self.config.get('runtime') or {}).get('maxScriptCharacters', 8000))
        if not text:
            return False
        now = now_utc()
        self.append_entry(story.get('id'), {
            'kind': 'admin-note', 'actor': 'system', 'content': f'[管理员注记] {text}',
            'occurredAt': iso(now), 'metadata': {'source': 'administrator'},
        }, now)
        self.schedule_compaction(story.get('id'))
        return True

    # 上游 1312-1323
    def add_admin_fact(self, story: Dict[str, Any], scope: str, content: str) -> bool:
        """Adds a high-confidence fact for corrections that must survive compaction."""
        text = clip(content, self.memory_config.get('factContentCharacters', 4000))
        if not text:
            return False
        now = now_utc()
        self.db_create('interlude_fact', {
            'storyId': story.get('id'), 'participantId': '', 'scope': scope, 'content': text,
            'importance': 0.8, 'confidence': 1, 'unresolved': False, 'embedding': self.embed_text(text),
            'status': 'active', 'sourceEntryIds': [], 'lastSeenAt': now, 'createdAt': now, 'updatedAt': now,
        })
        return True

    # 上游 1325-1330
    def forget_admin_fact(self, story_id: str, id: int) -> bool:
        """Reversible deletion: facts are retained as superseded rows for audit."""
        rows = self.db_get('interlude_fact', {'id': id, 'storyId': story_id, 'status': 'active'})
        if not rows:
            return False
        self.db_set('interlude_fact', {'id': id}, {'status': 'superseded', 'updatedAt': now_utc()})
        return True

    # 上游 1332-1337
    def cancel_admin_intent(self, story_id: str, id: int) -> bool:
        rows = self.db_get('interlude_intent', {'id': id, 'storyId': story_id, 'status': 'pending'})
        if not rows:
            return False
        self.db_set('interlude_intent', {'id': id}, {'status': 'cancelled', 'updatedAt': now_utc()})
        return True

    # 上游 1339-1345
    def reject_admin_state_patch(self, story_id: str, id: int) -> bool:
        rows = self.db_get('interlude_state_patch', {'id': id, 'storyId': story_id, 'status': 'proposed'})
        if not rows:
            return False
        self.db_set('interlude_state_patch', {'id': id}, {'status': 'rejected'})
        return True

    # 上游 1347-1353
    def clear_setting_overlay(self, story: Dict[str, Any], target: str) -> Dict[str, Any]:
        """Clear only the evolving setting overlay; keep Canon, script and memories."""
        self.invalidate_buffered_narratives(story.get('id'))

        def run() -> Dict[str, Any]:
            return self.clear_setting_overlay_unlocked(self.get_story(story.get('id')), target)

        return self.serial(story.get('id'), run)

    # 上游 1355-1381
    def rebase_timeline(self, story: Dict[str, Any]) -> Dict[str, Any]:
        """Start a clean host-owned timeline without deleting the historical archive.
        This is intended once after upgrading from prose-authoritative releases
        whose active scene or scratchpad may already contain future contamination."""
        self.invalidate_buffered_narratives(story.get('id'))

        def run() -> Dict[str, Any]:
            current = self.get_story(story.get('id'))
            now = now_utc()
            latest = self.db_get('interlude_script_entry', {'storyId': current.get('id')}, {'limit': 1, 'sort': {'id': 'desc'}})
            active_scene = self.active_scene(current.get('id'))
            if active_scene:
                self.db_set('interlude_scene', {'id': active_scene.get('id')}, {
                    'hook': f"Host timeline rebased at {format_log_time(now, (current.get('setting') or {}).get('timezone'))}.",
                    'summary': 'The host resumed the current timeline here. Earlier script remains archived context; no future statement from it is an event after this point.',
                    'lastEntryId': latest[0].get('id') if latest else active_scene.get('lastEntryId'),
                    'entryCount': 0,
                    'updatedAt': now,
                })
            state = decode_story_state(current.get('state'))
            rebased_state: Dict[str, Any] = {
                **state, 'workingDetails': [], 'timelineCarry': [], 'continuityDirty': True,
            }
            rebased_state.pop('continuitySnapshot', None)
            self.db_set('interlude_story', {'id': current.get('id')}, {
                'state': encode_story_state(rebased_state), 'cursorAt': now, 'updatedAt': now,
            })
            self.append_entry(current.get('id'), {
                'kind': 'timeline-rebase', 'actor': 'system',
                'content': 'Host timeline rebased. Earlier narrative prose remains an archive and no longer defines future events.',
                'occurredAt': iso(now), 'metadata': {'timelineRebase': True},
            }, now)
            return {'at': now, 'sceneReset': bool(active_scene)}

        return self.serial(story.get('id'), run)

    # 上游 1383-1429
    def clear_setting_overlay_unlocked(self, story: Dict[str, Any], target: str) -> Dict[str, Any]:
        now = now_utc()
        overlay: Dict[str, Any] = {**((story.get('state') or {}).get('settingOverlay') or {})}
        if target == 'character' or target == 'all':
            overlay.pop('characterProfile', None)
            overlay['characterTraits'] = []
        if target == 'perspective' or target == 'all':
            overlay.pop('perspective', None)
        if target == 'relationship' or target == 'all':
            overlay.pop('relationship', None)
        if target == 'world' or target == 'all':
            overlay.pop('world', None)
        self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state({**decode_story_state(story.get('state')), 'settingOverlay': overlay}), 'updatedAt': now,
        })

        participant_count = 0
        if target == 'relationship' or target == 'all':
            participants = self.participants(story.get('id'), True)
            for participant in participants:
                state = normalize_participant_state(participant.get('state'))
                if not state.get('relationshipOverlay'):
                    continue
                participant_count += 1
                # 上游传 relationshipOverlay: undefined；这里从 state 中删除该 key。
                next_state = {key: value for key, value in state.items() if key != 'relationshipOverlay'}
                self.db_set('interlude_participant', {'id': participant.get('id')}, {
                    'state': next_state, 'updatedAt': now,
                })

        # Preserve proposals for audit, but invalidate both active overlay rows and
        # pending candidates. Otherwise a candidate created before the clear could
        # be applied later and silently resurrect the old personality/relationship.
        patches = self.db_get('interlude_state_patch', {'storyId': story.get('id')})
        for patch in patches:
            if patch.get('status') not in ('proposed', 'applied', 'compacted') or (target != 'all' and patch.get('target') != target):
                continue
            self.db_set('interlude_state_patch', {'id': patch.get('id')}, {'status': 'cleared'})
        snapshots = self.db_get('interlude_overlay_snapshot', {'storyId': story.get('id'), 'status': 'active'})
        for snapshot in snapshots:
            if target != 'all' and snapshot.get('target') != target:
                continue
            self.db_set('interlude_overlay_snapshot', {'id': snapshot.get('id')}, {'status': 'superseded', 'updatedAt': now})
        return {'participantCount': participant_count}

    # ================= 上游 1431-1603：purge / 清库 =================

    def purge_all_story_data(self, story_id: str) -> None:
        """Destructive administrative operation. The caller must validate the
        confirmation phrase. A full purge also rebuilds Canon from the current
        Console configuration, so an old profile cannot survive in later prompts."""
        self.invalidate_buffered_narratives(story_id)
        self.purge_table('interlude_script_entry', {'storyId': story_id}, {
            'kind': 'redacted', 'actor': 'system', 'content': '[管理员已删除剧本内容]', 'metadata': {'redacted': True},
        })
        self.purge_table('interlude_memory', {'storyId': story_id}, {'status': 'deleted', 'content': '[管理员已删除记忆]'})
        self.purge_table('interlude_intent', {'storyId': story_id}, {'status': 'cancelled', 'summary': '[管理员已取消意图]'})
        self.purge_table('interlude_scene', {'storyId': story_id}, {'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0})
        self.purge_table('interlude_arc', {'storyId': story_id}, {'status': 'closed', 'summary': '', 'sceneCount': 0})
        self.purge_table('interlude_fact', {'storyId': story_id}, {'status': 'superseded', 'content': '[管理员已删除事实]'})
        self.purge_table('interlude_state_patch', {'storyId': story_id}, {'status': 'rejected', 'proposedValue': '[管理员已删除提案]', 'evidence': ''})
        self.purge_table('interlude_overlay_snapshot', {'storyId': story_id}, {'status': 'superseded', 'summary': '[管理员已删除 overlay 归档]', 'majorEvents': [], 'sourcePatchIds': []})
        self.purge_table('interlude_web_observation', {'storyId': story_id}, {'status': 'deleted', 'url': '', 'title': '', 'excerpt': '', 'summary': '[管理员已删除网页观察]'})
        self.purge_table('interlude_schedule_preplan', {'storyId': story_id}, {'regimes': [], 'exceptions': [], 'materializedDays': [], 'validFrom': '1970-01-01', 'validThrough': '1970-01-01', 'lastReviewedLocalDate': '', 'reviewReason': '[管理员已删除 Schedule Preplan]'})
        now = now_utc()
        story = self.get_story(story_id)
        setting = self.initial_story_setting()
        self.db_set('interlude_story', {'id': story_id}, {
            'setting': setting, 'state': empty_story_state(), 'cursorAt': now, 'updatedAt': now,
        })
        self.reset_participant_canon(story_id, now)
        self.ensure_continuity({**story, 'setting': setting, 'state': empty_story_state(), 'cursorAt': now}, now)

    # 上游 1455-1469
    def purge_all_data(self, preferred_story_id: Optional[str] = None) -> Optional[str]:
        """Reset all platforms, retaining exactly one empty global canonical story."""
        all_stories = self.db_get('interlude_story', {}, {'sort': {'updatedAt': 'desc'}})
        active = [story for story in all_stories if story.get('status') == 'active']
        if not active:
            return None
        canonical = None
        if preferred_story_id:
            canonical = next((story for story in active if story.get('id') == preferred_story_id), None)
        # 上游 (preferredStoryId && find(...)) ?? active[0]：空串边界按 or 语义处理。
        canonical = canonical if canonical is not None else active[0]
        for story in all_stories:
            self.purge_all_story_data(story.get('id'))
        now = now_utc()
        for story in all_stories:
            if story.get('id') == canonical.get('id'):
                continue
            self.db_set('interlude_story', {'id': story.get('id')}, {'status': 'archived', 'updatedAt': now})
        return canonical.get('id')

    # 上游 1471-1484
    def purge_platform_data(self, platform: str) -> int:
        """Delete one adapter/platform's records without touching other platforms."""
        all_stories = self.db_get('interlude_story', {}, {'sort': {'updatedAt': 'desc'}})
        targets = [story for story in all_stories if same_platform_family(story.get('platform'), platform)]
        for story in targets:
            self.purge_all_story_data(story.get('id'))
            self.db_set('interlude_story', {'id': story.get('id')}, {'status': 'archived', 'updatedAt': now_utc()})
        return len(targets)

    # 上游 1486-1541
    def clear_database(self) -> Dict[str, int]:
        """Clear only HDSI-owned tables. Koishi's users/channels and other plugins
        are intentionally untouched; deleting the physical SQLite file from a
        command would be unsafe while the driver is open."""
        if self.database_resetting:
            raise RuntimeError('HDSI 数据库清空已经在进行中。')
        self.database_resetting = True
        self.invalidate_buffered_narratives()
        self.invalidate_history_vectors()
        try:
            tables = [
                'interlude_script_entry', 'interlude_memory', 'interlude_intent',
                'interlude_scene', 'interlude_arc', 'interlude_fact', 'interlude_state_patch', 'interlude_overlay_snapshot', 'interlude_web_observation', 'interlude_schedule_preplan',
                'interlude_participant', 'interlude_story',
            ]
            removed = 0
            logically_cleared = 0
            for table in tables:
                rows = self.db_get(table, {})
                if not rows:
                    continue
                removed += len(rows)
                try:
                    self.db_remove(table, {})
                except Exception as error:
                    # Preserve the established disk-I/O fallback: content is redacted and
                    # stories are archived so a locked sql.js file cannot revive a story.
                    self.report_standalone('warn', 'SQLite 清空表失败，改用逻辑清空 表=%s 错误=%s', table, error)
                    for row in rows:
                        key = {'storyId': row.get('storyId')} if table == 'interlude_schedule_preplan' else {'id': row.get('id')}
                        if table == 'interlude_story':
                            fallback: Dict[str, Any] = {'status': 'archived', 'setting': self.initial_story_setting(), 'state': empty_story_state()}
                        elif table == 'interlude_participant':
                            fallback = {'status': 'paused', 'profile': '', 'relationship': '', 'state': empty_participant_state()}
                        elif table == 'interlude_script_entry':
                            fallback = {'kind': 'redacted', 'actor': 'system', 'content': '[HDSI 数据库已清空]', 'metadata': {'redacted': True}}
                        elif table == 'interlude_memory':
                            fallback = {'status': 'deleted', 'content': '[HDSI 数据库已清空]'}
                        elif table == 'interlude_intent':
                            fallback = {'status': 'cancelled', 'summary': '[HDSI 数据库已清空]'}
                        elif table == 'interlude_scene' or table == 'interlude_arc':
                            fallback = {'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0, 'sceneCount': 0}
                        elif table == 'interlude_fact':
                            fallback = {'status': 'superseded', 'content': '[HDSI 数据库已清空]'}
                        elif table == 'interlude_web_observation':
                            fallback = {'status': 'deleted', 'url': '', 'title': '', 'excerpt': '', 'summary': '[HDSI 数据库已清空]'}
                        elif table == 'interlude_schedule_preplan':
                            fallback = {'regimes': [], 'exceptions': [], 'materializedDays': [], 'validFrom': '1970-01-01', 'validThrough': '1970-01-01', 'lastReviewedLocalDate': '', 'reviewReason': '[HDSI 数据库已清空]'}
                        else:
                            fallback = {'status': 'rejected', 'proposedValue': '[HDSI 数据库已清空]', 'evidence': ''}
                        self.db_set(table, key, fallback)
                        logically_cleared += 1
            return {'removed': removed, 'logicallyCleared': logically_cleared}
        finally:
            self.database_resetting = False

    # 上游 1543-1603
    def purge_story_range(self, story_id: str, from_: datetime, to: datetime) -> None:
        """Remove script and derived memory records whose timestamps overlap a range.

        上游签名 `purgeStoryRange(storyId, from, to)`；`from` 是 Python 保留字，形参改 `from_`。
        """
        self.invalidate_buffered_narratives(story_id)
        self.invalidate_history_vectors(story_id)

        def in_range(value: Any) -> bool:
            return bool(value) and from_ <= value <= to

        entries = self.db_get('interlude_script_entry', {'storyId': story_id})
        entry_ids = {entry.get('id') for entry in entries if in_range(entry.get('occurredAt'))}
        for entry in entries:
            if entry.get('id') in entry_ids:
                self.purge_table('interlude_script_entry', {'id': entry.get('id')}, {
                    'kind': 'redacted', 'actor': 'system', 'content': '[管理员已删除剧本内容]', 'metadata': {'redacted': True},
                })

        memories = self.db_get('interlude_memory', {'storyId': story_id})
        for memory in memories:
            if in_range(memory.get('createdAt')) or (memory.get('sourceEntryId') is not None and memory.get('sourceEntryId') in entry_ids):
                self.purge_table('interlude_memory', {'id': memory.get('id')}, {'status': 'deleted', 'content': '[管理员已删除记忆]'})

        facts = self.db_get('interlude_fact', {'storyId': story_id})
        for fact in facts:
            sourced = any(item in entry_ids for item in (fact.get('sourceEntryIds') or []))
            if in_range(fact.get('createdAt')) or in_range(fact.get('updatedAt')) or in_range(fact.get('lastSeenAt')) or sourced:
                self.purge_table('interlude_fact', {'id': fact.get('id')}, {'status': 'superseded', 'content': '[管理员已删除事实]'})

        intents = self.db_get('interlude_intent', {'storyId': story_id})
        for intent in intents:
            if in_range(intent.get('createdAt')) or in_range(intent.get('notBefore')) or in_range(intent.get('updatedAt')):
                self.purge_table('interlude_intent', {'id': intent.get('id')}, {'status': 'cancelled', 'summary': '[管理员已取消意图]'})

        scenes = self.db_get('interlude_scene', {'storyId': story_id})
        for scene in scenes:
            overlaps = bool(scene.get('startedAt')) and scene.get('startedAt') <= to and (not scene.get('endedAt') or scene.get('endedAt') >= from_)
            if overlaps:
                self.purge_table('interlude_scene', {'id': scene.get('id')}, {'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0})
        arcs = self.db_get('interlude_arc', {'storyId': story_id})
        for arc in arcs:
            if in_range(arc.get('createdAt')) or in_range(arc.get('updatedAt')):
                self.purge_table('interlude_arc', {'id': arc.get('id')}, {'status': 'closed', 'summary': '', 'sceneCount': 0})

        patches = self.db_get('interlude_state_patch', {'storyId': story_id})
        for patch in patches:
            if in_range(patch.get('createdAt')) or in_range(patch.get('appliedAt')):
                self.purge_table('interlude_state_patch', {'id': patch.get('id')}, {'status': 'rejected', 'proposedValue': '[管理员已删除提案]', 'evidence': ''})

        observations = self.db_get('interlude_web_observation', {'storyId': story_id})
        for observation in observations:
            if in_range(observation.get('createdAt')) or in_range(observation.get('accessedAt')):
                self.purge_table('interlude_web_observation', {'id': observation.get('id')}, {'status': 'deleted', 'url': '', 'title': '', 'excerpt': '', 'summary': '[管理员已删除网页观察]'})

        if entry_ids:
            self.db_set('interlude_schedule_preplan', {'storyId': story_id}, {
                'lastReviewedLocalDate': '', 'validThrough': '1970-01-01', 'reviewReason': 'Source range was purged; Schedule Preplan requires review.', 'updatedAt': now_utc(),
            })

        story = self.get_story(story_id)
        self.ensure_continuity(story, now_utc())

    # ================= 上游 5589-5865：Canon / 参与者状态 / legacy 迁移 / continuity =================

    # 上游 5589-5595
    def main_model_label(self) -> Any:
        route = self.model_routing.get('main') or {}
        providers = route.get('providers') or []
        provider = providers[0] if providers else None
        provider_label = _trim((provider or {}).get('label')) or (provider or {}).get('id') or ''
        if route.get('assigned'):
            model = (provider or {}).get('model')
        else:
            model = (route.get('target') or {}).get('model') or (provider or {}).get('model') or '未配置'
        return f'{provider_label}/{model}' if provider_label else model

    # 上游 5597-5601
    def participant_preset(self, user_id: str) -> Optional[Dict[str, Any]]:
        presets = self.shared_story_config.get('participantPresets') or []
        return next((
            preset for preset in presets
            if preset.get('enabled') is not False and normalize_account_id(preset.get('qq')) == normalize_account_id(user_id)
        ), None)

    # 上游 5603-5619
    def initial_story_setting(self, name: Optional[str] = None) -> Dict[str, Any]:
        """The clean Canon used both by story creation and a full administrative reset."""
        setting = empty_story_setting()
        defaults = self.config.get('storyDefaults') or {}
        setting['character']['name'] = (name or '').strip() or defaults.get('characterName', '') or setting['character']['name']
        setting['character']['profile'] = defaults.get('characterProfile', '')
        setting['user']['displayName'] = 'Multiple participants'
        setting['user']['profile'] = defaults.get('userProfile', '')
        setting['relationship'] = defaults.get('relationship', '')
        setting['world'] = defaults.get('world', '')
        setting['perspective'] = clip(defaults.get('perspective'), 1_200)
        setting['supportingCast'] = defaults.get('supportingCast', '')
        setting['location'] = defaults.get('location', '')
        setting['style'] = defaults.get('style', '') or setting['style']
        setting['timezone'] = defaults.get('timezone', '') or setting['timezone']
        return setting

    # 上游 5621-5635
    def reset_participant_canon(self, story_id: str, now: datetime) -> None:
        """Rebuild per-account relationship baselines and discard evolving state."""
        participants = self.db_get('interlude_participant', {'storyId': story_id})
        for participant in participants:
            account = self.user_account_rule(participant.get('userId'))
            preset = self.participant_preset(participant.get('userId'))
            self.db_set('interlude_participant', {'id': participant.get('id')}, {
                'personId': _trim((account or {}).get('personId')) or _trim((preset or {}).get('personId')) or participant.get('personId') or participant.get('userId'),
                'displayName': _trim((account or {}).get('label')) or _trim((preset or {}).get('label')) or participant.get('displayName') or participant.get('userId'),
                'profile': _trim((account or {}).get('profile')) or _trim((preset or {}).get('profile')) or (self.config.get('storyDefaults') or {}).get('userProfile', ''),
                'relationship': _trim((account or {}).get('relationship')) or _trim((preset or {}).get('relationship')) or (self.config.get('storyDefaults') or {}).get('relationship', ''),
                'state': empty_participant_state(),
                'updatedAt': now,
            })

    # 上游 5637-5641
    def user_account_rule(self, user_id: str) -> Optional[Dict[str, Any]]:
        accounts = (self.config.get('onebot') or {}).get('userAccounts') or []
        normalized = normalize_account_id(user_id)
        return next((
            account for account in accounts
            if account.get('enabled') is not False and normalize_account_id(account.get('qq')) == normalized
        ), None)

    # 上游 5643-5645
    def get_participant(self, id: str) -> Optional[Dict[str, Any]]:
        rows = self.db_get('interlude_participant', {'id': id})
        return rows[0] if rows else None

    # 上游 5647-5657
    def record_incoming_message(self, participant: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        current = normalize_participant_state(participant.get('state'))
        state: Dict[str, Any] = {
            **current,
            'unreadMessageCount': current['unreadMessageCount'] + 1,
            'pendingReplyCount': current['pendingReplyCount'] + 1,
            'lastUserMessageAt': iso(now),
        }
        self.db_set('interlude_participant', {'id': participant.get('id')}, {'state': state, 'updatedAt': now})
        return {**participant, 'state': state, 'updatedAt': now}

    # 上游 5659-5664
    def mark_participant_seen(self, participant: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        current = normalize_participant_state(participant.get('state'))
        state: Dict[str, Any] = {**current, 'unreadMessageCount': 0}
        self.db_set('interlude_participant', {'id': participant.get('id')}, {'state': state, 'updatedAt': now})
        return {**participant, 'state': state, 'updatedAt': now}

    # 上游 5666-5674
    def record_character_message(self, participant: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        current = normalize_participant_state(participant.get('state'))
        state: Dict[str, Any] = {
            **current, 'unreadMessageCount': 0, 'pendingReplyCount': 0,
            'lastCharacterMessageAt': iso(now),
        }
        self.db_set('interlude_participant', {'id': participant.get('id')}, {'state': state, 'updatedAt': now})
        return {**participant, 'state': state, 'updatedAt': now}

    # 上游 5676-5681
    def update_participant_state(self, participant: Dict[str, Any], patch: Dict[str, Any],
                                 now: datetime) -> Dict[str, Any]:
        state = merge_participant_state(normalize_participant_state(participant.get('state')), patch)
        self.db_set('interlude_participant', {'id': participant.get('id')}, {'state': state, 'updatedAt': now})
        return {**participant, 'state': state, 'updatedAt': now}

    # 上游 5683-5734
    def migrate_legacy_story(self, legacy: Dict[str, Any], session: InboundSession) -> Dict[str, Any]:
        """Converts one old account-bound story into a bot-bound shared story once."""
        now = now_utc()
        story_id = story_id_for_character(session.platform, session.selfId)
        rows = self.db_get('interlude_story', {'id': story_id})
        existing = rows[0] if rows else None
        if existing:
            self.migrate_legacy_branch_into_shared(existing, session)
            self.ensure_continuity(existing, now)
            return existing
        story: Dict[str, Any] = {
            **legacy,
            'id': story_id,
            'platform': session.platform,
            'selfId': session.selfId,
            'userId': '',
            'channelId': '',
            'state': decode_story_state(legacy.get('state')),
            'updatedAt': now,
        }
        try:
            self.db_create('interlude_story', story)
        except Exception as error:
            # Concurrent first visits from two legacy accounts can both decide that
            # no shared row exists.  Join the row that won the primary-key race and
            # merge this branch into it instead of leaving an active legacy copy.
            raced_rows = self.db_get('interlude_story', {'id': story_id})
            raced = raced_rows[0] if raced_rows else None
            if not raced:
                raise error
            self.migrate_legacy_branch_into_shared(raced, session)
            self.ensure_continuity(raced, now)
            return raced
        participant = self.ensure_participant(story, session, now)
        tables = [
            'interlude_script_entry', 'interlude_memory', 'interlude_intent',
            'interlude_scene', 'interlude_arc', 'interlude_fact', 'interlude_state_patch', 'interlude_overlay_snapshot', 'interlude_web_observation', 'interlude_schedule_preplan',
        ]
        for table in tables:
            self.db_set(table, {'storyId': legacy.get('id')}, {'storyId': story.get('id')})
        # The old story only had one user, so account-bound records can safely be
        # attached to that initial relationship branch during migration.
        for table in ['interlude_script_entry', 'interlude_memory', 'interlude_intent', 'interlude_fact', 'interlude_state_patch', 'interlude_overlay_snapshot', 'interlude_web_observation']:
            self.db_set(table, {'storyId': story.get('id')}, {'participantId': participant.get('id')})
        self.db_set('interlude_story', {'id': legacy.get('id')}, {'status': 'archived', 'updatedAt': now})
        self.ensure_continuity(story, now)
        return story

    # 上游 5736-5753
    def migrate_legacy_branch_into_shared(self, story: Dict[str, Any], session: InboundSession) -> None:
        """A deployment can contain several old per-account stories. Once the first
        one created the shared story, fold later legacy branches into it as their
        users return; otherwise their old active rows would keep being swept in
        parallel and create a second life for the same character."""
        legacy_id = legacy_story_id_for(session.platform, session.selfId, session.userId)
        if legacy_id == story.get('id'):
            return
        rows = self.db_get('interlude_story', {'id': legacy_id})
        legacy = rows[0] if rows else None
        if not legacy or legacy.get('status') == 'archived':
            return
        now = now_utc()
        participant = self.ensure_participant(story, session, now)
        for table in ['interlude_script_entry', 'interlude_memory', 'interlude_intent', 'interlude_fact', 'interlude_state_patch', 'interlude_overlay_snapshot', 'interlude_web_observation']:
            self.db_set(table, {'storyId': legacy.get('id')}, {'storyId': story.get('id'), 'participantId': participant.get('id')})
        self.db_set('interlude_story', {'id': legacy.get('id')}, {'status': 'archived', 'updatedAt': now})
        self.append_entry(story.get('id'), {
            'kind': 'legacy-branch-merged', 'actor': 'system',
            'content': f"Earlier account-specific history for {participant.get('displayName')} was merged into the shared story.",
            'occurredAt': iso(now), 'metadata': {'legacyStoryId': legacy.get('id')},
        }, now, participant.get('id'))
        self.ensure_continuity(story, now)

    # 上游 5840-5864
    def ensure_continuity(self, story: Dict[str, Any], now: datetime) -> None:
        # 每个故事始终应有一个活动场景和一个活动弧线。旧数据升级或手动关闭场景后，
        # 此方法负责补齐它们，并把 id 缓存在 story.state 供 Console/外部工具查看。
        arc = self.active_arc(story.get('id'))
        if not arc:
            self.db_create('interlude_arc', {
                'storyId': story.get('id'), 'status': 'active', 'title': 'Beginning', 'summary': '', 'sceneCount': 0,
                'createdAt': now, 'updatedAt': now,
            })
            arc = self.active_arc(story.get('id'))
        scene = self.active_scene(story.get('id'))
        if not scene:
            self.db_create('interlude_scene', {
                'storyId': story.get('id'), 'status': 'active', 'startedAt': now, 'endedAt': None,
                'hook': '', 'summary': '', 'entryCount': 0, 'lastEntryId': None, 'createdAt': now, 'updatedAt': now,
            })
            scene = self.active_scene(story.get('id'))
            if arc:
                self.db_set('interlude_arc', {'id': arc.get('id')}, {'sceneCount': (arc.get('sceneCount') or 0) + 1, 'updatedAt': now})
        state = story.get('state') or {}
        if arc and scene and (state.get('activeArcId') != arc.get('id') or state.get('activeSceneId') != scene.get('id')):
            next_state = encode_story_state({
                **decode_story_state(story.get('state')), 'activeArcId': arc.get('id'), 'activeSceneId': scene.get('id'),
            })
            self.db_set('interlude_story', {'id': story.get('id')}, {'state': next_state, 'updatedAt': now})


__all__ = ['ServiceStoryMixin']
