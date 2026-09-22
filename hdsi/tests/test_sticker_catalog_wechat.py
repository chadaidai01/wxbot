# -*- coding: utf-8 -*-
"""回归：微信平台也能拿到本地贴纸目录；偷来的素材能被描述激活并使用。"""

import base64
import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.index import HdsiRuntime, build_config  # noqa: E402
from hdsi.platform.session import InboundSession  # noqa: E402
from hdsi.store import Database  # noqa: E402
from hdsi.time_utils import now_utc  # noqa: E402
from hdsi.tests.smoke_runtime import RecordingAdapter  # noqa: E402

_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


class FakeStickerDescriber:
    def available(self):
        return True

    def describe_sticker(self, data_uri, mime_type, file_name, animated,
                         response_format='json-object', max_tokens=768):
        return {'description': '一只在笑的鲸鱼', 'aliases': ['笑', '开心']}


class StickerCatalogWeChatTest(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='hdsi-sticker-test-')

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _runtime(self, stickers_dir):
        store = Database(os.path.join(self.workdir, 'hdsi.sqlite3'))
        config = build_config({
            'api_key': 'k', 'base_url': 'https://example.invalid/v1', 'model': 'm',
            'stickers': {'enabled': True, 'directory': stickers_dir, 'maxFileSizeMB': 10, 'catalogLimit': 40},
            'runtime': {'autoCreate': True, 'userMessageDebounceSeconds': 0},
            'logging': {'level': 'warn'},
        }, theater_groups=[])
        adapter = RecordingAdapter()
        runtime = HdsiRuntime(config, adapter, store, logging.getLogger('hdsi-sticker-test'), self.workdir)
        runtime.service.sticker_describer = FakeStickerDescriber()
        return runtime, store

    def test_stolen_sticker_activates_and_wechat_catalog_returns_it(self):
        stickers_dir = 'stickers'
        os.makedirs(os.path.join(self.workdir, stickers_dir, 'happy'))
        with open(os.path.join(self.workdir, stickers_dir, 'happy', 'one.png'), 'wb') as handle:
            handle.write(_PNG)

        runtime, store = self._runtime(stickers_dir)
        runtime.service.scan_sticker_library()

        rows = store.get('interlude_sticker', {})
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['status'], 'active')
        self.assertEqual(rows[0]['group'], 'happy')
        self.assertEqual(rows[0]['description'], '一只在笑的鲸鱼')

        wechat = InboundSession(platform='wechat', selfId='bot', userId='u1', username='u1',
                                channelId='u1', isDirect=True, content='', timestamp=now_utc())
        catalog = runtime.service.sticker_catalog_for_session(wechat)
        self.assertTrue(catalog, '微信平台应该能拿到本地贴纸目录')
        self.assertEqual(catalog[0]['description'], '一只在笑的鲸鱼')

        # 非微信、非 OneBot 平台仍按上游语义不暴露目录
        other = InboundSession(platform='discord', selfId='b', userId='u', username='u',
                               channelId='c', isDirect=True, content='', timestamp=now_utc())
        self.assertEqual(runtime.service.sticker_catalog_for_session(other), [])


if __name__ == '__main__':
    unittest.main()
