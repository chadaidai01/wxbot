# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/memory-continuity.test.ts（纯逻辑，无需 Koishi session / DB / narrator HTTP）。

上游对应文件：.hdsi_reference/test/memory-continuity.test.ts
移植说明：
- TS `InterludeService.prototype.facts.call(service, ...)` → Python `InterludeService.facts(service, ...)`：
  上游测试自己用桩对象替换了 `dbGet`（及 `dbSet`/`embedText`），不触碰真实数据库；Python 实现是同步的，
  因此这里同样用桩对象直接调用，不需要 asyncio。
- TS `systemPrompt` → `system_prompt`；`toPromptPayload` → `to_prompt_payload`。
- payload 里被丢弃的 undefined key 用 `.get(...)` 断言为 None（与上游“值为 undefined”等价）。
- `shouldRefreshContinuity` 上游用例传 `{}` 作为 this；Python 对应传 `{}` 作为 self。
- 六个用例全部照搬。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.narrator import system_prompt, to_prompt_payload  # noqa: E402
from hdsi.service import InterludeService  # noqa: E402
from hdsi.types import empty_story_setting, empty_story_state  # noqa: E402


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class MemoryContinuityTest(unittest.TestCase):

    def setUp(self):
        self.now = _dt('2026-08-30T11:22:00.000Z')

    def story(self) -> dict:
        state = empty_story_state()
        state['continuitySnapshot'] = {
            'current': '在房间', 'next': ['下楼取已经拿到的奶茶'],
            'recent': ['奶茶已经喝完'], 'salient': [],
        }
        state['lastContinuityUpdateAt'] = '2026-08-30T11:21:00.000Z'
        return {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': empty_story_setting(), 'state': state,
            'cursorAt': self.now, 'createdAt': self.now, 'updatedAt': self.now,
        }

    def entry(self, entry_id: int, kind: str, content: str, occurred_at: datetime) -> dict:
        return {
            'id': entry_id, 'storyId': 'story', 'participantId': 'participant', 'kind': kind,
            'actor': 'narrator' if kind == 'script' else 'character', 'content': content,
            'occurredAt': occurred_at, 'metadata': {}, 'createdAt': occurred_at,
        }

    def fact(self, fact_id: int, scope: str, unresolved: bool, content: str) -> dict:
        return {
            'id': fact_id, 'storyId': 'story', 'participantId': '', 'scope': scope, 'content': content,
            'importance': 0.8, 'confidence': 1, 'unresolved': unresolved, 'embedding': [],
            'status': 'active', 'sourceEntryIds': [], 'lastSeenAt': self.now,
            'createdAt': self.now, 'updatedAt': self.now,
        }

    def test_legacy_free_text_continuity_next_is_hidden_and_upcoming_plans_visible(self):
        """legacy free-text continuity next is hidden and host-owned upcoming plans remain visible"""
        upcoming = {
            'id': 7, 'storyId': 'story', 'participantId': 'participant', 'type': 'follow-up-commitment',
            'summary': '下周完成体检', 'notBefore': _dt('2026-09-02T00:00:00.000Z'), 'status': 'pending',
            'payload': {}, 'createdAt': self.now, 'updatedAt': self.now,
        }
        request = {
            'phase': 'user-message', 'story': self.story(), 'from': self.now, 'now': self.now,
            'userMessage': '然后呢', 'participant': None, 'participants': [], 'shareParticipantDetails': False,
            'dueIntents': [], 'upcomingIntents': [upcoming], 'activeConsequences': [],
            'supersededIntents': [], 'recentEntries': [], 'memories': [],
        }
        payload = to_prompt_payload(request)
        self.assertEqual(payload['relevantEstablishedEpisodes']['continuitySnapshot']['next'], [])
        self.assertIsNone(payload['ongoingThreads']['state'].get('continuitySnapshot'))
        self.assertEqual(payload['availableNearFuture']['upcomingPlans'][0]['summary'], '下周完成体检')
        refresh = system_prompt('user-message', '', '', '', '', '', True)
        self.assertRegex(refresh, r'Do not copy or create free-text future plans')
        self.assertNotRegex(refresh, r'"next":\[')

    def test_raw_messages_survive_large_prose_entry_and_normal_character_budget(self):
        """raw messages inside the time window survive a large prose entry and the normal character budget"""
        request = {
            'phase': 'user-message', 'story': self.story(), 'from': self.now, 'now': self.now,
            'participant': None, 'participants': [], 'shareParticipantDetails': False,
            'dueIntents': [], 'activeConsequences': [], 'supersededIntents': [], 'memories': [],
            'recentProtectionSince': self.now - timedelta(minutes=60),
            'recentEntries': [
                self.entry(1, 'character-message', '拿到了。', self.now - timedelta(minutes=50)),
                self.entry(2, 'script', '长' * 20_000, self.now - timedelta(seconds=1)),
            ],
        }
        payload = to_prompt_payload(request)
        recent_script = payload['relevantEstablishedEpisodes']['recentScript']
        self.assertTrue(any(item['content'] == '拿到了。' for item in recent_script))
        self.assertEqual(next(item for item in recent_script if item['id'] == 2)['content'],
                         request['recentEntries'][1]['content'])

    def test_fact_retrieval_reserves_lanes_for_resolved_events_and_open_promises(self):
        """fact retrieval reserves lanes for recently resolved events and open promises"""
        resolved = self.fact(100, 'event', False, '奶茶已经取回并喝完')
        promise = self.fact(101, 'promise', True, '下周完成体检')
        crowded = [self.fact(index + 1, 'event', True, '旧的未完成事件 %d' % index) for index in range(30)]

        class Service:
            memory_config = {
                'factLimit': 20, 'maxFactsPerStory': 200, 'factImportanceWeight': 0.5,
                'factConfidenceWeight': 0.35, 'factRecencyWeight': 0.15, 'semanticWeight': 0.55,
                'unresolvedWeight': 0.2,
            }
            config = {'model': {'embedding': {'liveQuery': False}}}

            def db_get(self, _table, query, options=None):
                if query.get('scope') == 'event' and query.get('unresolved') is False:
                    return [resolved]
                if query.get('scope') == 'promise':
                    return [promise]
                return crowded

        selected = InterludeService.facts(Service(), 'story', 20, '', None)
        self.assertTrue(any(item['id'] == resolved['id'] for item in selected))
        self.assertTrue(any(item['id'] == promise['id'] for item in selected))

    def test_fact_retrieval_scans_broad_bounded_pool_and_lets_old_literal_evidence_win(self):
        """fact retrieval scans a broad bounded pool and lets literal old evidence outrank generic rows"""
        target = {**self.fact(999, 'event', False, '星期二男孩是 13 号，不是 27 号'), 'importance': 0.1}
        crowded = [self.fact(index + 1, 'event', True, '普通高重要度事件 %d' % index) for index in range(220)]

        class Service:
            memory_config = {
                'factLimit': 5, 'maxFactsPerStory': 100, 'factImportanceWeight': 0.5,
                'factConfidenceWeight': 0.35, 'factRecencyWeight': 0.15, 'semanticWeight': 0.55,
                'unresolvedWeight': 0.2,
            }
            config = {'model': {'embedding': {'liveQuery': False}}}

            def __init__(self):
                self.general_limit = 0

            def db_get(self, _table, query, options=None):
                if query.get('scope'):
                    return []
                self.general_limit = options['limit']
                return [*crowded, target]

        service = Service()
        selected = InterludeService.facts(service, 'story', 5, '星期二男孩 13 号', None)
        self.assertGreaterEqual(service.general_limit, 300)
        self.assertEqual(selected[0]['id'], target['id'])

    def test_explicit_resolved_fact_can_close_the_old_unresolved_row(self):
        """an explicit resolved fact can close the old unresolved row"""
        existing = self.fact(12, 'promise', True, '奶茶配送事项')

        class Service:
            memory_config = {'factContentCharacters': 4_000, 'maxFactsPerStory': 200}
            patch = None

            def db_get(self, _table, _query=None, _options=None):
                return [existing]

            def db_set(self, _table, _query, value):
                self.patch = value

            def embed_text(self, _content):
                return []

        service = Service()
        source = self.entry(1, 'script', '奶茶已经取回', self.now)
        existing['participantId'] = source['participantId']
        resolved = InterludeService.persist_fact(service, 'story', {
            'scope': 'promise', 'content': '奶茶配送事项', 'unresolved': False, 'sourceEntryIds': [1],
            'knowledge': {'mode': 'observed', 'clauses': [
                {'role': 'observation', 'sourceEntryId': 1, 'quote': '奶茶已经取回'}]},
        }, [source], self.now)
        self.assertEqual(resolved, True)
        self.assertEqual(service.patch['unresolved'], False)

    def test_legacy_continuity_dirty_state_does_not_restart_realtime_summary(self):
        """legacy continuity dirty state does not restart an independent real-time summary"""
        current = self.story()
        current['state']['continuityDirty'] = True
        current['state']['narrativeUpdateCount'] = 3
        self.assertEqual(
            InterludeService.should_refresh_continuity({}, current, 'user-message'), False)


if __name__ == '__main__':
    unittest.main()
