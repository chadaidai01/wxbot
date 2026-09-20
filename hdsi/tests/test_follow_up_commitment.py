# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/follow-up-commitment.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/follow-up-commitment.test.ts
移植说明：
- TS `systemPrompt` → `system_prompt`；TS `toPromptPayload` → `to_prompt_payload`。
- 上游 `advance.ongoingThreads.followUpCommitments === undefined`：Python 实现把 undefined 的 key 整个丢弃，
  因此用 `.get(...)` 断言取值是 None（与上游“值为 undefined”等价）。
- 两个用例都只依赖 prompt 组装（无 Koishi session / DB / narrator HTTP），全部照搬。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.narrator import system_prompt, to_prompt_payload  # noqa: E402
from hdsi.types import empty_story_setting, empty_story_state  # noqa: E402


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class FollowUpCommitmentTest(unittest.TestCase):

    def setUp(self):
        self.now = _dt('2026-08-25T08:00:00.000Z')

    def commitment(self) -> dict:
        return {
            'id': 7, 'storyId': 'story', 'participantId': 'friend', 'type': 'follow-up-commitment',
            'summary': '答应想清楚后回复耳机选择。',
            'notBefore': _dt('2026-08-25T08:30:00.000Z'), 'status': 'pending',
            'payload': {'kind': 'thinking', 'sourceEntryIds': [12],
                        'expiresAt': '2026-08-25T20:00:00.000Z', 'requiresVisibleOutcome': True},
            'createdAt': self.now, 'updatedAt': self.now,
        }

    def request(self, phase: str) -> dict:
        story = {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': 'global', 'channelId': 'private:global',
            'status': 'active', 'setting': empty_story_setting(), 'state': empty_story_state(),
            'cursorAt': self.now, 'createdAt': self.now, 'updatedAt': self.now,
        }
        return {
            'phase': phase, 'story': story, 'from': self.now, 'now': self.now, 'participant': None,
            'participants': [], 'shareParticipantDetails': False, 'dueIntents': [], 'activeConsequences': [],
            'supersededIntents': [], 'recentEntries': [], 'memories': [],
            'followUpCommitments': [self.commitment()],
        }

    def test_commitment_instructions_limited_to_live_and_due_turns(self):
        """follow-up commitment instructions are limited to live and due turns"""
        self.assertRegex(system_prompt('user-message', '', '', '', '', ''), r'followUpCommitment')
        self.assertRegex(system_prompt('intent-due', '', '', '', '', ''), r'do not silently finish')
        self.assertNotRegex(system_prompt('advance', '', '', '', '', ''), r'followUpCommitment')

    def test_only_live_and_due_payloads_carry_bounded_commitment_list(self):
        """only live and due prompt payloads carry the bounded commitment list"""
        user = to_prompt_payload(self.request('user-message'))
        due = to_prompt_payload(self.request('intent-due'))
        advance = to_prompt_payload(self.request('advance'))
        self.assertEqual(user['ongoingThreads']['followUpCommitments'][0]['id'], 7)
        self.assertEqual(due['ongoingThreads']['followUpCommitments'][0]['summary'], '答应想清楚后回复耳机选择。')
        self.assertIsNone(advance['ongoingThreads'].get('followUpCommitments'))


if __name__ == '__main__':
    unittest.main()
