# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/continuity-guards.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/continuity-guards.test.ts
移植说明：
- TS 从 '../src/service' 导入 `normalizeScenePresenceDrafts`；Python 对应
  `hdsi.service_helpers.normalize_scene_presence_drafts`（service mixin 转发同一实现）。
  该函数是同步纯函数：入参 (drafts, entries, now)。
- TS `toPromptPayload` → `to_prompt_payload`；payload 里被丢弃的 undefined key 用 `.get(...)` 断言为 None。
- 两个用例都只依赖纯函数 / prompt 组装，全部照搬。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.narrator import to_prompt_payload  # noqa: E402
from hdsi.service_helpers import normalize_scene_presence_drafts  # noqa: E402
from hdsi.types import empty_story_setting, empty_story_state  # noqa: E402


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class ContinuityGuardsTest(unittest.TestCase):

    def setUp(self):
        self.now = _dt('2026-08-25T08:00:00.000Z')

    def entry(self, entry_id: int, content: str) -> dict:
        return {
            'id': entry_id, 'storyId': 'story', 'participantId': '', 'kind': 'script', 'actor': 'narrator',
            'content': content, 'occurredAt': self.now, 'metadata': {}, 'createdAt': self.now,
        }

    def request(self, phase: str) -> dict:
        state = empty_story_state()
        state['automaticDeliverySummaries'] = [{
            'participantId': 'friend', 'summary': '已向对方确认 Gate 的试听结论。',
            'sourceEntryId': 7,
            'deliveredAt': self.now.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (self.now.microsecond // 1000),
        }]
        story = {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': 'global', 'channelId': 'private:global',
            'status': 'active', 'setting': empty_story_setting(), 'state': state,
            'cursorAt': self.now, 'createdAt': self.now, 'updatedAt': self.now,
        }
        return {
            'phase': phase, 'story': story, 'from': self.now, 'now': self.now, 'participant': None,
            'participants': [], 'shareParticipantDetails': False, 'dueIntents': [], 'activeConsequences': [],
            'supersededIntents': [], 'recentEntries': [], 'memories': [],
            'automaticDeliverySummaries': state['automaticDeliverySummaries'],
        }

    def test_scene_presence_accepts_explicit_evidence_and_rejects_inferred_departures(self):
        """scene presence accepts explicit arrival/departure evidence and rejects inferred departures"""
        departure = normalize_scene_presence_drafts([{
            'name': '希绘', 'status': 'off-scene', 'basis': '希绘在电梯口与水濑道别后回家。', 'sourceEntryIds': [1],
        }], [self.entry(1, '希绘在电梯口与水濑道别后回家。')], self.now)
        self.assertEqual(departure[0]['status'], 'off-scene')

        inferred = normalize_scene_presence_drafts([{
            'name': '希绘', 'status': 'off-scene', 'basis': '希绘似乎该离开了。', 'sourceEntryIds': [2],
        }], [self.entry(2, '希绘和水濑一起走进音频馆。')], self.now)
        self.assertEqual(inferred, [])

    def test_automatic_delivery_summaries_stay_on_background_turns_only(self):
        """automatic delivery summaries stay on background turns only"""
        advance = to_prompt_payload(self.request('advance'))
        user = to_prompt_payload(self.request('user-message'))
        self.assertEqual(len(advance['ongoingThreads']['automaticDeliverySummaries']), 1)
        self.assertIsNone(advance['ongoingThreads']['state'].get('automaticDeliverySummaries'))
        self.assertIsNone(user['ongoingThreads'].get('automaticDeliverySummaries'))
        self.assertIsNone(user['ongoingThreads']['state'].get('automaticDeliverySummaries'))


if __name__ == '__main__':
    unittest.main()
