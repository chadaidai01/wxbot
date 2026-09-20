# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/agency.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/agency.test.ts
移植说明：
- TS `resolveAgencyConfig` → `resolve_agency_config`，其余同名 snake_case。
- TS `groupDueIntents` 从 '../src/service' 导入；Python 对应 `hdsi.service_helpers.group_due_intents`
  （`hdsi/service_narrative.py` / `hdsi/service_memory.py` 均从该模块转发同一实现）。
- TS `new Set([...])` → Python `set`；`undefined` → `None`。
- 时间字段：上游把窗口/候选草稿的 ISO 字符串交给 `new Date(...)` 比较；Python 实现直接接受 ISO 字符串，
  这五个用例不依赖运行时，全部照搬。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.agency import (  # noqa: E402
    active_agency_window,
    evaluate_agency_capacity,
    normalize_agency_window_draft,
    normalize_proactive_contact,
    proactive_candidate_fingerprint,
    proactive_recheck_at,
    resolve_agency_config,
)
from hdsi.service_helpers import group_due_intents  # noqa: E402
from hdsi.time_utils import iso  # noqa: E402


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class AgencyTest(unittest.TestCase):

    def setUp(self):
        self.config = resolve_agency_config({
            'enabled': True,
            'maxWindowMinutes': 240,
            'minimumProactiveIntervalMinutes': 60,
            'maxCandidateHours': 24,
        })
        self.now = _dt('2026-08-24T08:00:00.000Z')

    def candidate(self, **overrides) -> dict:
        draft = {
            'participantId': 'friend', 'origin': 'life-event', 'motive': '她遇到一件想分享的事。',
            'disclosure': 'ordinary', 'sourceEntryIds': [10], 'willingness': 0.8,
            'outcome': 'send-now', 'expiresAt': '2026-08-25T08:00:00.000Z',
        }
        draft.update(overrides)
        return draft

    def window(self, **overrides) -> dict:
        state = {
            'activityLoad': 'free', 'privacy': 'private', 'deviceAccess': 'available',
            'validUntil': '2026-08-24T12:00:00.000Z', 'basis': '她已经回到自己的房间。',
            'sourceEntryIds': [10], 'updatedAt': iso(self.now),
        }
        state.update(overrides)
        return state

    def test_window_accepts_only_grounded_bounded_practical_state(self):
        """Agency Window accepts only grounded, bounded practical state"""
        normalized = normalize_agency_window_draft({
            'activityLoad': 'occupied', 'privacy': 'public', 'deviceAccess': 'limited',
            'nextOpportunityAt': '2026-08-24T10:00:00.000Z',
            'validUntil': '2026-08-25T10:00:00.000Z',
            'basis': '她还在教室里，周围有人。', 'sourceEntryIds': [10, 999],
        }, self.now, self.config, {10})
        self.assertEqual(normalized['activityLoad'], 'occupied')
        self.assertEqual(normalized['sourceEntryIds'], [10])
        self.assertEqual(normalized['validUntil'], '2026-08-24T12:00:00.000Z')
        self.assertEqual(active_agency_window(normalized, self.now)['privacy'], 'public')

    def test_capacity_rules_separate_schedule_privacy_device_from_emotion(self):
        """capacity rules separate schedule, privacy and device access from emotion"""
        self.assertEqual(evaluate_agency_capacity(self.window(), self.candidate(), self.now, self.config)['allowed'], True)
        self.assertEqual(
            evaluate_agency_capacity(self.window(activityLoad='overloaded'), self.candidate(), self.now, self.config)['reason'],
            'schedule-overloaded')
        self.assertEqual(
            evaluate_agency_capacity(self.window(privacy='public'), self.candidate(disclosure='personal'),
                                     self.now, self.config)['reason'],
            'privacy-insufficient')
        self.assertEqual(
            evaluate_agency_capacity(self.window(deviceAccess='unavailable'), self.candidate(), self.now, self.config)['reason'],
            'device-unavailable')
        self.assertEqual(
            evaluate_agency_capacity(self.window(activityLoad='occupied'), self.candidate(origin='promise'),
                                     self.now, self.config)['allowed'],
            True)

    def test_ordinary_contact_respects_minimum_interval_while_promises_bypass(self):
        """ordinary proactive contact respects the minimum interval while promises bypass it"""
        recent = '2026-08-24T07:30:00.000Z'
        self.assertEqual(
            evaluate_agency_capacity(self.window(), self.candidate(), self.now, self.config, recent)['reason'],
            'minimum-proactive-interval')
        self.assertEqual(
            evaluate_agency_capacity(self.window(), self.candidate(origin='promise'), self.now, self.config, recent)['allowed'],
            True)

    def test_contact_candidates_require_permitted_target_and_real_source_evidence(self):
        """contact candidates require a permitted target and real source evidence"""
        normalized = normalize_proactive_contact({
            'participantId': 'friend', 'origin': 'life-event', 'motive': '她想告诉对方今天发生的事。',
            'disclosure': 'ordinary', 'willingness': 0.8, 'outcome': 'recheck-later',
        }, self.now, self.config, {'friend'}, set(), 42)
        self.assertEqual(normalized['sourceEntryIds'], [42])
        self.assertIsNone(normalize_proactive_contact(
            {**normalized, 'participantId': 'blocked'}, self.now, self.config, {'friend'}, {42}))

    def test_candidate_identity_ignores_wording_and_recheck_time_stays_bounded(self):
        """candidate identity ignores wording changes and recheck time stays bounded"""
        first = self.candidate(motive='第一种措辞')
        second = self.candidate(motive='完全不同的措辞')
        self.assertEqual(proactive_candidate_fingerprint(first), proactive_candidate_fingerprint(second))
        capacity = evaluate_agency_capacity(
            self.window(activityLoad='overloaded', nextOpportunityAt='2026-08-24T09:00:00.000Z'),
            first, self.now, self.config)
        self.assertEqual(
            iso(proactive_recheck_at(
                first, capacity, self.window(nextOpportunityAt='2026-08-24T09:00:00.000Z'), self.now)),
            '2026-08-24T09:00:00.000Z')

    def test_proactive_checks_isolated_from_ordinary_due_messages(self):
        """proactive checks are isolated from ordinary due messages for the same participant"""
        def intent(intent_id: int, intent_type: str) -> dict:
            return {
                'id': intent_id, 'storyId': 'story', 'participantId': 'friend', 'type': intent_type,
                'summary': intent_type, 'notBefore': self.now, 'status': 'pending', 'payload': {},
                'createdAt': self.now, 'updatedAt': self.now,
            }

        batches = group_due_intents([intent(1, 'delayed-reply'), intent(2, 'proactive-check')])
        self.assertEqual(len(batches), 2)
        self.assertEqual(sorted(batch[0]['type'] for batch in batches),
                         ['delayed-reply', 'proactive-check'])


if __name__ == '__main__':
    unittest.main()
