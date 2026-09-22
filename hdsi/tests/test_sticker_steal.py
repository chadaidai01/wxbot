# -*- coding: utf-8 -*-
"""偷表情包：心情解析 / 去重索引 / 落库 的单元测试。"""

import base64
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import sticker_steal  # noqa: E402

_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


class PickMoodTest(unittest.TestCase):
    CATS = ['angry', 'confused', 'evasive', 'happy', 'loved', 'reminded', 'sad', 'surprised', 'tired']

    def test_plain_category_word(self):
        self.assertEqual(sticker_steal.pick_mood('happy', self.CATS), 'happy')

    def test_category_inside_sentence_and_case(self):
        self.assertEqual(sticker_steal.pick_mood('我觉得这张是 SAD 吧', self.CATS), 'sad')

    def test_keyword_fallback_from_description(self):
        self.assertEqual(sticker_steal.pick_mood('哭得稀里哗啦', self.CATS), 'sad')
        self.assertEqual(sticker_steal.pick_mood('一只在笑的鲸鱼', self.CATS), 'happy')

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(sticker_steal.pick_mood('', self.CATS), 'misc')
        self.assertEqual(sticker_steal.pick_mood('完全无关的内容', self.CATS), 'misc')
        self.assertEqual(sticker_steal.pick_mood('whatever', [], default='happy'), 'happy')


class StickerFilesTest(unittest.TestCase):
    def test_sanitize_mood_blocks_path_tricks(self):
        self.assertEqual(sticker_steal.sanitize_mood('../../evil'), 'evil')
        self.assertEqual(sticker_steal.sanitize_mood(''), 'misc')
        self.assertEqual(sticker_steal.sanitize_mood('happy'), 'happy')

    def test_save_and_index_and_discover(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'happy'))
            os.makedirs(os.path.join(tmp, 'sad'))
            target = sticker_steal.save_stolen(_PNG, '.png', 'happy', tmp, 'a' * 64, now_token='20260922')
            self.assertTrue(os.path.exists(target))
            self.assertTrue(target.replace('\\', '/').endswith('happy/stolen_20260922_aaaaaaaa.png'))
            with open(target, 'rb') as handle:
                self.assertEqual(handle.read(), _PNG)

            categories = sticker_steal.discover_categories(tmp, default='happy')
            self.assertEqual(categories, ['happy', 'sad'])

            index = sticker_steal.hash_index(tmp)
            self.assertIn(hashlib.sha256(_PNG).hexdigest(), index)

    def test_save_stolen_defaults_bad_extension_to_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = sticker_steal.save_stolen(_PNG, '.exe', 'misc', tmp, 'b' * 64)
            self.assertTrue(target.endswith('.png'))


if __name__ == '__main__':
    unittest.main()


class DhashTest(unittest.TestCase):
    @staticmethod
    def _gradient_png():
        from PIL import Image
        image = Image.new("L", (64, 64))
        for x in range(64):
            for y in range(64):
                image.putpixel((x, y), (x * 4 + y * 3) % 256)
        buffer = __import__("io").BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_gradient_has_hash_and_solid_is_skipped(self):
        from PIL import Image
        gradient = self._gradient_png()
        self.assertEqual(len(sticker_steal.dhash(gradient)), 16)
        solid_buffer = __import__("io").BytesIO()
        Image.new("L", (64, 64), 128).save(solid_buffer, format="PNG")
        self.assertEqual(sticker_steal.dhash(solid_buffer.getvalue()), '')


class StickerMemoryTest(unittest.TestCase):
    def test_add_match_persist_and_rebuild(self):
        import tempfile
        gradient = DhashTest._gradient_png()
        digest = hashlib.sha256(gradient).hexdigest()
        dhash_value = sticker_steal.dhash(gradient)
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = os.path.join(tmp, "memory.json")
            memory = sticker_steal.StickerStealMemory(memory_path)
            self.assertTrue(memory.empty())
            self.assertTrue(memory.add({
                "sha256": digest, "source": "abc123", "dhash": dhash_value,
                "path": "happy/stolen.png",
            }))
            self.assertEqual(len(memory), 1)
            self.assertFalse(memory.add({"sha256": digest}))
            self.assertIsNotNone(memory.match(sha256=digest)[0])
            self.assertEqual(memory.match(source="abc123")[1], "source")
            self.assertEqual(memory.match(dhash_value=dhash_value)[1], "dhash")
            self.assertIsNone(memory.match(sha256="nope")[0])
            reloaded = sticker_steal.StickerStealMemory(memory_path)
            self.assertEqual(len(reloaded), 1)
            self.assertIsNotNone(reloaded.match(source="abc123")[0])

    def test_rebuild_from_existing_library(self):
        import tempfile
        gradient = DhashTest._gradient_png()
        with tempfile.TemporaryDirectory() as tmp:
            library = os.path.join(tmp, "emojis")
            os.makedirs(os.path.join(library, "sad"))
            with open(os.path.join(library, "sad", "one.png"), "wb") as handle:
                handle.write(gradient)
            memory = sticker_steal.StickerStealMemory(os.path.join(tmp, "m.json"))
            self.assertEqual(memory.rebuild_from_files(library), 1)
            memory.save()
            digest = hashlib.sha256(gradient).hexdigest()
            self.assertIsNotNone(memory.match(sha256=digest)[0])
