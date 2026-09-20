# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/alter-system.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/alter-system.test.ts
移植说明：
- TS `Date` → Python aware `datetime`（UTC）；ISO 字符串保持与上游逐字相同。
- TS `undefined` → Python `None`；`'weight' in obj` 用 Python `in` 逐字复刻。
- 本文件所有测试均不需要上游运行时，全部照搬。
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.alter import (  # noqa: E402
    adjust_alter_weight,
    advance_alter_system,
    alter_history_for_scope,
    alter_scope_value,
    calculate_alter_threshold,
    complete_alter_analysis,
    create_alter_system_state,
    emotional_offset_for_prompt,
    normalize_alter_system_state,
    normalize_alter_value,
)

CONFIG = {
    'enabled': True,
    'baseThreshold': 10,
    'densityFactor': 0.3,
    'sameDirectionBoost': 0.05,
    'oppositeDecay': 0.15,
    'minWeight': 0.2,
    'maxIntensity': 2,
}


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _iso(value: datetime) -> str:
    aware = value.astimezone(timezone.utc)
    return aware.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (aware.microsecond // 1000)


class AlterSystemTest(unittest.TestCase):

    def test_model_alter_values_are_bounded_integers_and_invalid_values_ignored(self):
        """model Alter values are bounded integers and invalid values are ignored"""
        self.assertEqual(normalize_alter_value(3.4), 3)
        self.assertEqual(normalize_alter_value(20), 5)
        self.assertEqual(normalize_alter_value(-20), -5)
        self.assertIsNone(normalize_alter_value(float('nan')))
        self.assertIsNone(normalize_alter_value('3'))

    def test_dynamic_threshold_moves_from_ten_to_seven_under_dense_narration(self):
        """dynamic threshold moves gradually from ten to seven under dense narration"""
        now = _dt('2026-08-22T12:00:00.000Z')

        def entry(turn: int) -> dict:
            return {
                'turn': turn,
                'phase': 'user-message',
                'alter': 1,
                'alterValue': turn,
                'timestamp': _iso(now - timedelta(seconds=turn)),
            }

        self.assertEqual(calculate_alter_threshold([], CONFIG, now), 10)
        self.assertEqual(calculate_alter_threshold([entry(i) for i in range(1, 6)], CONFIG, now), 8.5)
        self.assertEqual(calculate_alter_threshold([entry(i) for i in range(1, 21)], CONFIG, now), 7)

    def test_same_direction_strengthens_while_opposite_movement_decays(self):
        """same-direction movement strengthens while opposite movement decays"""
        self.assertEqual(adjust_alter_weight(0.6, True, 2, CONFIG), 0.7)
        self.assertTrue(math.fabs(adjust_alter_weight(0.6, False, 2, CONFIG) - 0.3) < 1e-9)
        self.assertEqual(adjust_alter_weight(0.95, True, 5, CONFIG), 1)
        self.assertEqual(adjust_alter_weight(0.2, False, 5, CONFIG), 0)

    def test_trigger_completed_only_after_valid_side_model_description(self):
        """a trigger is completed only after a valid side-model description"""
        now = _dt('2026-08-22T12:00:00.000Z')
        state = create_alter_system_state(now)
        state['alterValue'] = 8
        turn = advance_alter_system(state, 3, 'user-message', now, CONFIG)
        self.assertEqual(turn['thresholdReached'], True)
        self.assertEqual(turn['state']['alterValue'], 11)

        completed = complete_alter_analysis(
            turn['state'], '氛围开始转向更谨慎、私密的交流。', turn['threshold'], now, CONFIG)
        self.assertEqual(completed['alterValue'], 0)
        self.assertEqual(completed['alterWeight'], 1)
        self.assertEqual(completed['lastTriggerDirection'], 1)
        self.assertEqual(completed['emotionalOffset']['direction'], 'serious')
        self.assertTrue(completed['emotionalOffset']['intensity'] >= 1)

    def test_legacy_persisted_state_normalized_without_duplicate_weight(self):
        """legacy persisted Alter state is normalized without exposing duplicate weight"""
        normalized = normalize_alter_system_state({
            'alterValue': -4,
            'alterWeight': 0.75,
            'lastTriggerAlter': -12,
            'emotionalOffset': {
                'direction': 'relaxed', 'description': '较轻松', 'intensity': 1.2,
                'generatedAt': 1787400000000, 'weight': 0.1,
            },
            'history': [],
            'lastUpdatedAt': 1787400000000,
        })
        self.assertEqual(normalized['lastTriggerDirection'], -1)
        self.assertEqual(normalized['alterWeight'], 0.75)
        prompt = emotional_offset_for_prompt(normalized, CONFIG)
        self.assertEqual(prompt['weight'], 0.75)
        self.assertEqual('weight' in normalized['emotionalOffset'], False)

    def test_relationship_local_accumulation_and_analysis_evidence_same_bucket(self):
        """Alter keeps relationship-local accumulation and analysis evidence in the same source bucket"""
        now = _dt('2026-09-06T12:00:00.000Z')
        state = create_alter_system_state(now)
        alice_first = advance_alter_system(state, 6, 'user-message', now, CONFIG, 'alice')
        state = alice_first['state']
        bob_first = advance_alter_system(
            state, 6, 'user-message', now + timedelta(seconds=1), CONFIG, 'bob')
        state = bob_first['state']

        # Story-level diagnostics still see twelve points, but neither private
        # relationship may borrow the other's evidence to trigger an analysis.
        self.assertEqual(state['alterValue'], 12)
        self.assertEqual(alice_first['thresholdReached'], False)
        self.assertEqual(bob_first['thresholdReached'], False)
        self.assertEqual(alter_scope_value(state, 'alice'), 6)
        self.assertEqual(alter_scope_value(state, 'bob'), 6)

        alice_trigger = advance_alter_system(
            state, 4, 'conversation-follow-up', now + timedelta(seconds=2), CONFIG, 'alice')
        self.assertEqual(alice_trigger['thresholdReached'], True)
        self.assertEqual(alice_trigger['sourceParticipantId'], 'alice')
        self.assertEqual(alice_trigger['triggerValue'], 10)
        self.assertEqual(len(alter_history_for_scope(alice_trigger['state']['history'], 'alice')), 2)
        self.assertEqual(len(alter_history_for_scope(alice_trigger['state']['history'], 'bob')), 1)

        completed = complete_alter_analysis(
            alice_trigger['state'], '她在这段关系里变得更谨慎。', alice_trigger['threshold'],
            now, CONFIG, 'alice')
        self.assertEqual(alter_scope_value(completed, 'alice'), 0)
        self.assertEqual(alter_scope_value(completed, 'bob'), 6)
        self.assertEqual(completed['alterValue'], 6)


if __name__ == '__main__':
    unittest.main()
