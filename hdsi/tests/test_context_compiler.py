# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/context-compiler.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/context-compiler.test.ts
移植说明：
- TS `compileNarrativeContext` → `compile_narrative_context`；`compiledContextConflicts` → `compiled_context_conflicts`。
- TS `assert.strictEqual`（引用相等）→ Python `assertIs`。
- 两个用例都是同步纯函数，全部照搬。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.script.context_compiler import (  # noqa: E402
    compile_narrative_context,
    compiled_context_conflicts,
)


class ContextCompilerTest(unittest.TestCase):

    def test_creates_one_seven_part_scaffold_without_recomputing_shared_facts(self):
        """context compiler creates one seven-part scaffold without recomputing shared facts"""
        payload = {
            'setting': {'character': {'name': '水濑'}}, 'state': {}, 'currentParticipant': None, 'participants': [],
            'recentScript': [{'id': 1, 'content': '她还坐在窗边。'}], 'sceneContext': {'scene': None},
            'continuitySnapshot': None, 'durableFacts': [], 'memories': [], 'overlayEvolution': [],
            'currentEvent': {'type': 'private-message-batch', 'content': '在吗'},
            'interval': {'from': 'a', 'now': 'b'}, 'timelinePlan': None, 'activeConsequences': [],
            'dueIntents': [], 'upcomingPlans': [], 'phase': 'user-message', 'refreshContinuity': False,
            'outputRecovery': False,
        }
        compiled = compile_narrative_context(payload, None, None)
        self.assertEqual(list(compiled.keys()), [
            'storyIdentity', 'relevantEstablishedEpisodes', 'currentSceneEvidence', 'ongoingThreads',
            'availableNearFuture', 'incomingEvent', 'authoringWindow',
        ])
        self.assertEqual(compiled_context_conflicts(payload, compiled), [])
        self.assertIs(compiled['relevantEstablishedEpisodes']['recentScript'], payload['recentScript'])
        self.assertIs(compiled['incomingEvent']['event'], payload['currentEvent'])

    def test_scene_evidence_visible_in_recent_script_is_not_injected_twice(self):
        """scene evidence already visible in recentScript is not injected twice"""
        payload = {
            'setting': {}, 'state': {}, 'recentScript': [{'id': 7, 'content': '她仍在书桌前。'}],
            'currentEvent': {}, 'interval': {},
        }
        compiled = compile_narrative_context(payload, {
            'id': 'frame', 'presentPeople': [], 'openMotions': [], 'openTopics': [], 'sourceEntryIds': [7],
            'sources': {'place': [7]}, 'place': '书桌前', 'updatedAt': 'now',
        }, None)
        self.assertIsNone(compiled['currentSceneEvidence'].get('place'))
        self.assertIsNone(compiled['currentSceneEvidence'].get('sceneId'))
        self.assertEqual(compiled['relevantEstablishedEpisodes']['recentScript'][0]['content'], '她仍在书桌前。')


if __name__ == '__main__':
    unittest.main()
