# -*- coding: utf-8 -*-
"""回归测试：模型 JSON 根为数组/标量时的归一化。

对应线上问题：'list' object has no attribute 'get'
（模型把决策对象包在 JSON 数组里返回；上游 JS 读数组字段得到 undefined 会自然降级，
 Python 的 .get 会抛 AttributeError，因此显式归一化。）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.narrator_prompts import normalize_decoded_decision  # noqa: E402
from hdsi.script.authored_actions import resolve_authored_actions  # noqa: E402


class DecodedDecisionTest(unittest.TestCase):
    def test_dict_is_kept_identical(self):
        decision = {'script': '她看了一眼窗外'}
        self.assertIs(normalize_decoded_decision(decision), decision)

    def test_wrapped_object_is_unwrapped(self):
        decision = {'script': '她看了一眼窗外', 'interaction': {'seen': True}}
        self.assertIs(normalize_decoded_decision([decision]), decision)

    def test_first_dict_of_mixed_array_wins(self):
        first = {'script': 'A'}
        self.assertIs(normalize_decoded_decision([None, '噪声', first, {'script': 'B'}]), first)

    def test_empty_array_and_scalars_become_empty_dict(self):
        for value in ([], ['只', '有', '字符串'], '纯文本', 42, 3.5, True, None):
            self.assertEqual(normalize_decoded_decision(value), {})

    def test_resolve_authored_actions_passes_non_dict_through(self):
        # 上游 resolveAuthoredActions 对非对象原样返回；归一化由调用方负责。
        wrapped = [{'script': 'x'}]
        self.assertIs(resolve_authored_actions(wrapped), wrapped)
        self.assertEqual(normalize_decoded_decision(resolve_authored_actions(wrapped)), {'script': 'x'})

    def test_normalize_is_idempotent(self):
        decision = {'script': 'x'}
        self.assertIs(normalize_decoded_decision(normalize_decoded_decision([decision])), decision)


if __name__ == '__main__':
    unittest.main()
