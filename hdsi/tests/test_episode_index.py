# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/episode-index.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/episode-index.test.ts
移植说明：
- TS `buildEpisodeIndex` → `build_episode_index`；`Map` → Python `dict`（`.get(id)` 语义一致）。
- TS `episodeExcerpt` → `episode_excerpt`；`deliveryReality` → `delivery_reality`；
  `compileNarrativeContext` → `compile_narrative_context`。
- TS `Array<[number, EpisodeSource]>` → Python `[id, {...}]` 列表。
- 六个用例全是同步纯函数，全部照搬。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.script.context_compiler import compile_narrative_context  # noqa: E402
from hdsi.script.delivery_reality import delivery_reality  # noqa: E402
from hdsi.script.episode_index import build_episode_index, episode_excerpt  # noqa: E402


def row(content: str, participant_id: str = 'a', frame_id: str = 'scene:1') -> dict:
    return {
        'content': content, 'participantId': participant_id, 'frameId': frame_id,
        'occurredAt': '2026-09-05T00:00:00Z', 'kind': 'script',
    }


class EpisodeIndexTest(unittest.TestCase):

    def test_navigation_rebuilds_identically_and_never_groups_scenes_or_relationships(self):
        """episode navigation rebuilds identically and never groups separate relationships or scenes"""
        rows = [[1, row('出发')], [2, row('抵达')], [3, row('秘密', 'b')], [4, row('新场景', 'a', 'scene:2')]]
        self.assertEqual(build_episode_index(rows), build_episode_index(json.loads(json.dumps(rows))))
        self.assertEqual(build_episode_index(rows).get(1), [1, 2])
        self.assertEqual(build_episode_index(rows).get(3), [3])

    def test_long_preceding_prose_cannot_displace_recall_hit_and_source_ids_reflect_text(self):
        """long preceding prose cannot displace the recall hit and source ids reflect included text"""
        rows = [[1, row('旧' * 5000)], [2, row('取餐码8914')], [3, row('取走奶茶')]]
        excerpt = episode_excerpt(rows, 2, 200)
        self.assertRegex(excerpt['content'], r'取餐码8914')
        self.assertIn(2, excerpt['sourceEntryIds'])
        self.assertIn(3, excerpt['sourceEntryIds'])
        self.assertNotIn(1, excerpt['sourceEntryIds'])
        self.assertEqual(len(rows[0][1]['content']), 5000)

    def test_delivery_reality_preserves_prose_and_distinguishes_outcomes(self):
        """delivery reality preserves prose and distinguishes unconfirmed receipt from cancellation"""
        entry = {
            'id': 1, 'kind': 'script', 'content': '她发了两句话。',
            'metadata': {'commitId': 'c', 'deliveryActions': [{
                'commitId': 'c', 'eventId': 'e', 'segments': [
                    {'kind': 'message', 'content': '第一句', 'status': 'delivered'},
                    {'kind': 'message', 'content': '第二句', 'status': 'pending'},
                ],
            }]},
        }
        before = json.dumps(entry)
        result = delivery_reality([entry])
        self.assertEqual(result[0]['segments'][1]['outcome'], 'not-confirmed')
        self.assertEqual(json.dumps(entry), before)
        self.assertEqual(len(delivery_reality([{**entry, 'kind': 'user-message'}])), 0)

    def test_compiled_context_excludes_legacy_rhythm_and_carries_execution_evidence(self):
        """compiled writing context excludes legacy rhythm directives and carries execution evidence"""
        result = compile_narrative_context(
            {'chatRhythm': {'drift': '强制三段'}, 'deliveryReality': [{'sourceEntryId': 1}]}, None, None)
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r'强制三段|chatRhythm')
        self.assertEqual(result['ongoingThreads']['deliveryReality'], [{'sourceEntryId': 1}])

    def test_checkpoint_provenance_groups_legacy_entries_without_merging_relationships(self):
        """checkpoint provenance groups legacy original entries without merging relationships"""
        rows = [
            [1, {**row('起点'), 'frameId': None}],
            [2, {**row('结束'), 'frameId': None,
                 'checkpoint': {'sceneId': 7, 'firstEntryId': 1, 'lastEntryId': 3}}],
            [3, {**row('另一关系', 'b'), 'frameId': None}],
        ]
        self.assertEqual(build_episode_index(rows).get(1), [1, 2])
        self.assertEqual(build_episode_index(rows).get(3), [3])

    def test_cross_relationship_action_text_stays_out_of_delivery_reality(self):
        """cross-relationship action text stays out of delivery reality unless sharing is enabled"""
        entry = {
            'id': 1, 'kind': 'script',
            'metadata': {'commitId': 'c', 'deliveryActions': [{
                'commitId': 'c', 'participantId': 'b', 'eventId': 'e',
                'segments': [{'kind': 'message', 'content': '私密行动', 'status': 'pending'}],
            }]},
        }
        self.assertEqual(delivery_reality([entry], 'a', False), [])
        self.assertEqual(len(delivery_reality([entry], 'b', False)), 1)


if __name__ == '__main__':
    unittest.main()
