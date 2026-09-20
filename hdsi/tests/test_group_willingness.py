# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/group-willingness.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/group-willingness.test.ts
移植说明：
- TS `evaluateGroupWillingness(previous, config, input)` → Python `evaluate_group_willingness(previous, config, input)`；
  input 保持上游字段名（now/messageCount/content/quotedBot/mentionedBot/random）。
- TS `undefined` 状态 → Python `None`。
- 本文件所有测试均不需要上游运行时，全部照搬。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.group_willingness import (  # noqa: E402
    consume_group_willingness,
    evaluate_group_willingness,
)


class GroupWillingnessTest(unittest.TestCase):
    CONFIG = {
        'enabled': True, 'maxScore': 1, 'threshold': 0.24, 'probabilityAmplifier': 1.3,
        'decayHalfLifeSeconds': 180, 'replyCost': 0.55, 'baseGain': 0.12,
        'quoteGain': 0.12, 'keywordGain': 0.18, 'keywords': ['水濑'],
    }

    def test_accumulates_locally_respects_threshold_and_uses_bounded_probability(self):
        """group willingness accumulates locally, respects threshold, and uses a bounded probability"""
        first = evaluate_group_willingness(None, self.CONFIG, {
            'now': 0, 'messageCount': 1, 'content': '大家晚上好',
            'mentionedBot': False, 'quotedBot': False, 'random': 0,
        })
        self.assertEqual(first['shouldCall'], False)
        self.assertEqual(first['reason'], 'below-threshold')

        second = evaluate_group_willingness(first['state'], self.CONFIG, {
            'now': 1_000, 'messageCount': 2, 'content': '水濑你怎么看',
            'mentionedBot': False, 'quotedBot': False, 'random': 0,
        })
        self.assertEqual(second['reason'], 'probability-roll')
        self.assertTrue(0 < second['probability'] <= 1)
        self.assertEqual(second['shouldCall'], True)

    def test_mentions_bypass_while_sent_group_reply_consumes_score(self):
        """mentions bypass group willingness while a sent group reply consumes score"""
        forced = evaluate_group_willingness(None, self.CONFIG, {
            'now': 0, 'messageCount': 1, 'content': '在吗',
            'mentionedBot': True, 'quotedBot': False, 'random': 0.99,
        })
        self.assertEqual(forced['shouldCall'], True)
        self.assertEqual(forced['reason'], 'forced-mention')
        after_reply = consume_group_willingness(forced['state'], self.CONFIG, 1_000)
        self.assertTrue(after_reply['score'] < forced['state']['score'])

    def test_disabled_preserves_always_trigger_behavior(self):
        """disabled group willingness preserves the existing always-trigger behavior"""
        decision = evaluate_group_willingness(None, {'enabled': False}, {
            'now': 0, 'messageCount': 1, 'content': '普通消息',
            'mentionedBot': False, 'quotedBot': False,
        })
        self.assertEqual(decision['shouldCall'], True)
        self.assertEqual(decision['reason'], 'disabled')


if __name__ == '__main__':
    unittest.main()
