# -*- coding: utf-8 -*-
"""回归：主剧本被“单 active 剧本”清理逻辑归档后，收到消息应自动恢复并继续处理。

线上现象：人设文件被编辑后，不同聊天缓存到不同版本 → 生成不同主剧本 key →
全局单剧本规则把其中一个归档 → receive 遇到 archived 直接 return，
表现为“hdsi 直接调不出来”（不写剧本、不回消息）。
"""

import logging
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.index import HdsiRuntime, build_config  # noqa: E402
from hdsi.platform.session import InboundSession  # noqa: E402
from hdsi.store import Database  # noqa: E402
from hdsi.time_utils import now_utc  # noqa: E402
from hdsi.tests.smoke_runtime import FakeNarrator, RecordingAdapter, wait_for  # noqa: E402


class ArchivedStoryRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='hdsi-archive-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _runtime(self):
        store = Database(os.path.join(self.workdir, 'hdsi.sqlite3'))
        config = build_config({
            'api_key': 'test-key', 'base_url': 'https://example.invalid/v1', 'model': 'test-model',
            'story_defaults': {'characterName': '默认名', 'characterProfile': '默认档案'},
            'runtime': {'autoCreate': True, 'userMessageDebounceSeconds': 0, 'sweepIntervalMinutes': 0},
            'logging': {'level': 'info'},
        }, theater_groups=[])
        adapter = RecordingAdapter()
        runtime = HdsiRuntime(config, adapter, store, logging.getLogger('hdsi-archive-test'), self.workdir)
        narrator = FakeNarrator()
        runtime.service.set_narrator(narrator)
        return runtime, adapter, narrator

    def _session(self, user):
        return InboundSession(
            platform='wechat', selfId='bot', userId=user, username=user, channelId=user,
            isDirect=True, content='在吗', timestamp=now_utc(), sender_name=user,
        )

    def test_archived_story_is_reactivated_on_next_message(self):
        runtime, adapter, narrator = self._runtime()
        character = {'key': 'charX', 'name': '茶呆呆', 'profile': '测试人设', 'timezone': 'Asia/Shanghai'}

        runtime.receive(self._session('u1'), character)
        assert wait_for(lambda: bool(adapter.sent)), '首条消息没有投递'
        # 生产路径里 receive 会先给 selfId 加角色后缀，断言也必须用同一个已 scope 的 session。
        scoped = runtime.character_scoped_session(self._session('u1'), character)
        story = runtime.service.find_story(scoped)
        self.assertIsNotNone(story)
        story_id = story['id']

        # 模拟被“单 active 剧本”清理逻辑归档
        runtime.service.set_status(story, 'archived')
        adapter.sent.clear()

        # 下一条消息应自动恢复 active 并正常处理/投递
        runtime.receive(self._session('u2'), character)
        assert wait_for(lambda: bool(adapter.sent)), '归档后的消息没有恢复处理（回归失败）'
        recovered = runtime.service.find_story(runtime.character_scoped_session(self._session('u2'), character))
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered['id'], story_id)
        self.assertEqual(recovered['status'], 'active')
        self.assertEqual(recovered['setting']['character']['name'], '茶呆呆')
        self.assertEqual(recovered['setting']['character']['profile'], '测试人设')


if __name__ == '__main__':
    unittest.main()
