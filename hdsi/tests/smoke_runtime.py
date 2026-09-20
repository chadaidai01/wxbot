# -*- coding: utf-8 -*-
"""hdsi 端到端冒烟测试（不需要微信、不需要真实模型）。

用法：python hdsi/tests/smoke_runtime.py
覆盖：建主剧本 → 参与者入册 → 私聊回合 → 叙事决策落库 → 平台投递 → 群聊缓冲 →
      后台自动推进（advance 阶段）→ sweep 稳定性。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.index import HdsiRuntime, build_config  # noqa: E402
from hdsi.platform.session import InboundSession  # noqa: E402
from hdsi.store import Database  # noqa: E402
from hdsi.time_utils import iso, now_utc  # noqa: E402


class RecordingAdapter:
    """实现 PlatformAdapter 的最小可用子集，记录所有出站投递。"""

    base_dir = '.'
    platform = 'wechat'
    selfId = 'bot'

    def __init__(self):
        self.sent = []
        self.files = []

    def send_private(self, participant, content, quote_message_id=None):
        self.sent.append({'kind': 'private', 'to': participant.get('channelId'), 'content': content})
        return 'msg-%d' % len(self.sent)

    def send_group(self, story, channel_id, content, reply_to_message_id=None, session=None):
        self.sent.append({'kind': 'group', 'to': channel_id, 'content': content})
        return 'msg-%d' % len(self.sent)

    def send_sticker(self, story, channel_id, file_path, session=None):
        self.files.append(file_path)
        return 'file-%d' % len(self.files)

    def send_reaction(self, story, channel_id, message_id, reaction, direction='add', session=None):
        return False

    def read_file(self, path):
        try:
            with open(path, 'rb') as handle:
                return handle.read()
        except OSError:
            return None

    def file_size(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def list_files(self, directory):
        if not directory or not os.path.isdir(directory):
            return []
        entries = []
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                entries.append({'path': path, 'name': name, 'size': os.path.getsize(path), 'mtime': 0})
        return entries

    def group_member_name(self, group_id, user_id):
        return user_id

    def find_bot(self, platform, self_id):
        return self

    def list_bots(self):
        return [self]

    def chat_capabilities(self, kind, session=None):
        return {'platform': 'wechat', 'quoteReply': False, 'reactions': [], 'nativeFaces': []}


class FakeNarrator:
    """按上游协议返回合法决策：私聊用 interaction，群聊用 groupReply，后台推进只写剧本。"""

    def __init__(self):
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        phase = request.get('phase')
        if request.get('groupContext') is not None:
            return {
                'script': '她把手机扣在桌上，听群里热闹了一会儿，才拿起键盘。',
                'groupReply': {'mode': 'immediate', 'content': '大家晚上好呀。'},
            }
        if phase in ('advance', 'conversation-follow-up', 'intent-due'):
            return {
                'script': '她慢慢收拾好桌上的东西，窗外的路灯一盏盏亮起来。\n\n她给自己倒了杯热水。',
            }
        return {
            'script': '她放下手里的杯子，看了一眼消息。\n\n窗外的天已经暗下来。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在的，怎么啦？'}},
            'memories': [{'category': 'event', 'content': '用户在傍晚发来消息问候。', 'importance': 0.3}],
        }

    def analyze_alter(self, request, config):
        return {'description': '心情平静。'}


class WrappedListNarrator(FakeNarrator):
    """回归用：把决策对象包在 JSON 数组里返回（线上触发过 'list' object has no attribute 'get'）。"""

    def decide(self, request):
        return [super().decide(request)]


class FakeCompactor:
    def compact(self, request):
        return {}

    def compact_overlay(self, request):
        return {'summary': ''}

    def plan_timeline(self, request):
        return {'beats': [{'at': 0.5, 'kind': 'activity', 'summary': '她起身去接了杯水。'}], 'carry': []}


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    workdir = tempfile.mkdtemp(prefix='hdsi-smoke-')
    try:
        store = Database(os.path.join(workdir, 'hdsi.sqlite3'))
        adapter = RecordingAdapter()
        config = build_config({
            'api_key': 'test-key',
            'base_url': 'https://example.invalid/v1',
            'model': 'test-model',
            'story_defaults': {'characterName': '默认名', 'characterProfile': '默认档案', 'timezone': 'Asia/Shanghai'},
            'runtime': {'autoCreate': True, 'autoAdvanceEnabled': True, 'autoAdvanceIntervalMinutes': 60,
                        'sweepIntervalMinutes': 60, 'userMessageDebounceSeconds': 0},
            'logging': {'level': 'info'},
        }, theater_groups=[{
            'groupId': '测试群', 'enabled': True, 'responseMode': 'always',
            'contextLimit': 20, 'debounceSeconds': 0, 'cooldownSeconds': 0,
            'willingness': {'enabled': False},
        }])
        runtime = HdsiRuntime(config, adapter, store, logging.getLogger('hdsi-smoke'), workdir)
        narrator = FakeNarrator()
        runtime.service.set_narrator(narrator)
        runtime.service.set_compactor(FakeCompactor())
        character = {'key': 'char1', 'name': '小茶', 'profile': '测试角色', 'timezone': 'Asia/Shanghai'}

        # 1) 私聊：创建主剧本 + 参与者，并投递回复
        session = InboundSession(
            platform='wechat', selfId='bot', userId='u1', username='u1', channelId='u1',
            isDirect=True, content='在吗', timestamp=now_utc(), sender_name='u1',
        )
        runtime.receive(session, character)
        assert wait_for(lambda: bool(adapter.sent)), '私聊回合没有投递任何消息'
        assert narrator.requests, '主叙事决策没有被调用'
        print('[smoke] 私聊投递:', adapter.sent[-1])

        stories = store.get('interlude_story', {})
        assert len(stories) == 1, stories
        story_id = stories[0]['id']
        assert story_id.startswith('character:wechat:bot'), story_id
        entries = store.get('interlude_script_entry', {'storyId': story_id}, {'sort': {'id': 'asc'}})
        assert any(entry.get('kind') == 'user-message' for entry in entries), [e.get('kind') for e in entries]
        assert any(entry.get('kind') == 'script' for entry in entries), [e.get('kind') for e in entries]
        participants = store.get('interlude_participant', {'storyId': story_id})
        assert len(participants) == 1, participants
        print('[smoke] 剧本条目:', [e.get('kind') for e in entries])

        # 回归：HdsiRuntime.receive 必须把每聊天角色设定写进主剧本
        # （storyDefaults 用的是"默认名/默认档案"，这里应被 character 覆盖）
        applied = store.get('interlude_story', {'id': story_id})[0]['setting']['character']
        assert applied.get('name') == '小茶', applied
        assert applied.get('profile') == '测试角色', applied
        print('[smoke] 角色设定已写入故事:', applied.get('name'), '/', applied.get('profile'))

        # 1b) 回归：模型把决策对象包在 JSON 数组里，也应正常落库与投递
        adapter.sent.clear()
        runtime.service.set_narrator(WrappedListNarrator())
        session2 = InboundSession(
            platform='wechat', selfId='bot', userId='u2', username='u2', channelId='u2',
            isDirect=True, content='在吗', timestamp=now_utc(), sender_name='u2',
        )
        # 1c) 回归：历史遗留的空人设剧本（Unnamed character / 空 profile）应在下次消息被补写
        runtime.service.update_setting(runtime.service.find_story(session), {
            'character': {'name': 'Unnamed character', 'profile': ''}})
        runtime.receive(session2, character)
        assert wait_for(lambda: bool(adapter.sent)), '数组根决策没有正常投递（回归失败）'
        print('[smoke] 数组根决策已恢复并投递:', adapter.sent[-1])
        repaired = runtime.service.find_story(session2)['setting']['character']
        assert repaired.get('name') == '小茶' and repaired.get('profile') == '测试角色', repaired
        print('[smoke] 遗留空人设已自动补写:', repaired.get('name'), '/', repaired.get('profile'))
        runtime.service.set_narrator(narrator)

        # 2) 群聊：进入群缓冲并触发一次群叙事（groupReply 协议）
        group_session = InboundSession(
            platform='wechat', selfId='bot', userId='member-a', username='member-a', channelId='测试群',
            guildId='测试群', isDirect=False, content='大家晚上好', timestamp=now_utc(),
            sender_name='成员A', mentioned_bot=True,
        )
        runtime.receive(group_session, character)
        assert wait_for(lambda: any(entry.get('kind') == 'group-message'
                                    for entry in store.get('interlude_script_entry', {'storyId': story_id}))), \
            '群聊消息没有入库'
        assert wait_for(lambda: any(item['kind'] == 'group' for item in adapter.sent)), '群聊回复没有投递'
        assert wait_for(lambda: not runtime.service.has_pending_narrative(story_id)), '群聊回合未结算'
        print('[smoke] 群聊回复:', [item for item in adapter.sent if item['kind'] == 'group'][-1])
        # 回归：微信平台层会把 @昵称 从正文删掉，引擎必须显式提示模型“被点名了”
        group_request = narrator.requests[-1]
        assert '系统提示：这批群消息 @ 了你' in (group_request.get('userMessage') or ''), group_request.get('userMessage')
        print('[smoke] 群聊被 @ 提示已注入模型请求')

        # 3) 后台自动推进：把 nextAdvanceAt 拨到过去，sweep 应补写一段生活
        from datetime import timedelta
        fresh = store.get('interlude_story', {'id': story_id})[0]
        state = dict(fresh['state'])
        state['automation'] = {'nextAdvanceAt': iso(now_utc() - timedelta(hours=2))}
        store.set('interlude_story', {'id': story_id}, {'state': state})
        before = len(store.get('interlude_script_entry', {'storyId': story_id}))
        runtime.sweep()
        assert wait_for(lambda: len(store.get('interlude_script_entry', {'storyId': story_id})) > before), \
            '后台自动推进没有补写剧本条目'
        after_entries = store.get('interlude_script_entry', {'storyId': story_id}, {'sort': {'id': 'asc'}})
        phases = [entry.get('metadata', {}).get('phase') for entry in after_entries]
        assert 'advance' in phases, phases
        print('[smoke] 后台推进后阶段:', phases)

        # 4) 再扫一次不应抛异常
        runtime.sweep()
        print('[smoke] 投递总数 =', len(adapter.sent))
        runtime.close()
        print('SMOKE OK')
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
