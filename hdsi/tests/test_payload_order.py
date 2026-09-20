# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/payload-order.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/payload-order.test.ts
移植说明：
- TS `toPromptPayload` → `to_prompt_payload`；`systemPrompt` → `system_prompt`。
- 上游用 `JSON.stringify` 做前缀比较；这里用 `_js_json`（`ensure_ascii=False` + 紧凑分隔符）复刻
  JS `JSON.stringify` 的字节形状，因此上游的 `],"sceneContext"` 等查找串可以照抄。
- `systemPrompt(...args, false, false, false, false, false, undefined, false, undefined, false, false, true)`
  的第 17 个位置参数是 `cacheFirstPayload`，Python 同名参数为 `cache_first_payload`。
- 十一个用例都是纯函数 / prompt 组装，全部照搬。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.narrator import system_prompt, to_prompt_payload  # noqa: E402
from hdsi.types import empty_story_setting, empty_story_state  # noqa: E402

SEVEN_PART_SCAFFOLD = [
    'storyIdentity', 'relevantEstablishedEpisodes', 'currentSceneEvidence', 'ongoingThreads',
    'availableNearFuture', 'incomingEvent', 'authoringWindow',
]

# 上游 systemPrompt 的 cache-first 调用：17 个位置参数（最后一个是 cacheFirstPayload=true）。
SYSTEM_PROMPT_ARGS = ['user-message', '', '', '', '', '']
CACHE_FIRST_SYSTEM_PROMPT_ARGS = SYSTEM_PROMPT_ARGS + [
    False, False, False, False, False, None, False, None, False, False, True,
]


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _iso(value: datetime) -> str:
    return value.isoformat().replace('+00:00', 'Z')


def _js_json(value) -> str:
    """复刻 JS `JSON.stringify(value)`：不转义非 ASCII，紧凑分隔符。"""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _common_prefix_length(left: str, right: str) -> int:
    index = 0
    limit = min(len(left), len(right))
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class PayloadOrderTest(unittest.TestCase):

    def setUp(self):
        self.now = _dt('2026-08-30T11:22:00.000Z')

    def story(self) -> dict:
        state = empty_story_state()
        state['continuitySnapshot'] = {'current': '在房间', 'next': [], 'recent': [], 'salient': []}
        return {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': empty_story_setting(), 'state': state,
            'cursorAt': self.now, 'createdAt': self.now, 'updatedAt': self.now,
        }

    def entry(self, entry_id: int, kind: str, content: str, offset_minutes: int) -> dict:
        occurred_at = self.now - timedelta(minutes=offset_minutes)
        if kind == 'script':
            actor = 'narrator'
        elif kind == 'intent-cancelled':
            actor = 'system'
        else:
            actor = 'character'
        return {
            'id': entry_id, 'storyId': 'story', 'participantId': 'participant', 'kind': kind, 'actor': actor,
            'content': content, 'occurredAt': occurred_at, 'metadata': {}, 'createdAt': occurred_at,
        }

    def request(self, recent_entries, user_message=None, **overrides) -> dict:
        payload = {
            'phase': 'user-message', 'story': self.story(), 'from': self.now, 'now': self.now,
            'userMessage': user_message, 'participant': None, 'participants': [],
            'shareParticipantDetails': False, 'dueIntents': [], 'activeConsequences': [],
            'supersededIntents': [], 'memories': [], 'recentEntries': recent_entries,
        }
        payload.update(overrides)
        return payload

    def test_both_payload_modes_use_the_seven_part_scaffold(self):
        """both payload modes use the seven-part script continuation scaffold"""
        req = self.request([self.entry(1, 'character-message', '拿到了。确实挺大杯。', 50)], '你健忘吗')
        legacy = to_prompt_payload(req)
        explicit_off = to_prompt_payload(req, {'cacheFirst': False})
        self.assertEqual(list(explicit_off.keys()), list(legacy.keys()))
        self.assertEqual(list(legacy.keys()), SEVEN_PART_SCAFFOLD)
        self.assertNotIn('recentExchange', legacy['relevantEstablishedEpisodes'])

    def test_cache_first_puts_stable_blocks_first_and_per_turn_fields_near_decision(self):
        """cache-first puts stable blocks first and per-turn fields beside the decision point"""
        req = self.request([self.entry(1, 'character-message', '拿到了。确实挺大杯。', 50)], '你健忘吗')
        payload = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual(list(payload.keys()), SEVEN_PART_SCAFFOLD)
        established = payload['relevantEstablishedEpisodes']
        self.assertEqual(list(established.keys())[0], 'recentScript')
        self.assertIn('recentExchange', established)
        self.assertIn('interval', payload['authoringWindow'])

    def test_recent_exchange_anchors_transport_only(self):
        """recentExchange anchors only transport exchanges, excludes the live message and never repeats script prose"""
        req = self.request([
            self.entry(1, 'user-message', '旧的一句', 240),
            self.entry(2, 'character-message', '拿到了。确实挺大杯。', 200),
            self.entry(3, 'script', '剧' * 900, 150),
            self.entry(4, 'character-message', '嗯。', 100),
            self.entry(5, 'user-message', '你健忘吗', 5),
        ], '你健忘吗')
        items = to_prompt_payload(req, {'cacheFirst': True})['relevantEstablishedEpisodes']['recentExchange']
        self.assertEqual(len(items), 3)
        received = next((item for item in items if item['content'] == '拿到了。确实挺大杯。'), None)
        self.assertEqual(received['tag'], 'protagonist')
        self.assertTrue('你健忘吗' not in _js_json(items))
        self.assertTrue('剧' * 20 not in _js_json(items), '剧本文字不应进入 transport 尾部锚点')
        total = sum(len(item['content']) for item in items)
        self.assertTrue(total <= 1_600)
        self.assertTrue(any('旧的一句' in item['content'] for item in items),
                        '跳过剧本文字后，最近三条真实收发消息应补足尾部块')

    def test_consecutive_user_turns_keep_append_only_history_at_cacheable_front(self):
        """consecutive user turns keep append-only history at the cacheable front of the compiled context"""
        long_script = '剧' * 3_000
        turn_one = self.request([
            self.entry(1, 'script', long_script, 60),
            self.entry(2, 'character-message', '拿到了。', 55),
            self.entry(3, 'user-message', '在吗', 50),
        ], '在吗')
        turn_two = self.request([
            self.entry(1, 'script', long_script, 60),
            self.entry(2, 'character-message', '拿到了。', 55),
            self.entry(3, 'user-message', '在吗', 50),
            self.entry(4, 'script', '后续剧情。', 10),
            self.entry(5, 'character-message', '嗯。', 8),
            self.entry(6, 'user-message', '吃饭没', 5),
        ], '吃饭没', **{'from': self.now - timedelta(minutes=6)})

        cache_one = _js_json(to_prompt_payload(turn_one, {'cacheFirst': True}))
        cache_two = _js_json(to_prompt_payload(turn_two, {'cacheFirst': True}))
        cache_prefix = _common_prefix_length(cache_one, cache_two)
        # 分叉点恰好是 recentScript 数组的收口：追加式历史让旧内容全部留在缓存前缀内。
        history_end = cache_one.find('],"sceneContext"')
        # 保护性检查：确保锚点确实存在，避免下面的比较因 find() 返回 -1 而变成空断言。
        self.assertGreater(history_end, 0, '未找到 recentScript 数组收口锚点')
        self.assertGreaterEqual(cache_prefix, history_end - 1,
                                '前缀分叉点不得早于对话史数组结束')
        self.assertGreaterEqual(cache_prefix, len(cache_one) * 0.5,
                                'cache-first 前缀命中过短: %d/%d' % (cache_prefix, len(cache_one)))
        self.assertLess(cache_prefix, cache_one.find('"currentSceneEvidence"'),
                        '分叉点应位于当前场景证据视图之前')

        legacy_one = _js_json(to_prompt_payload(turn_one))
        legacy_two = _js_json(to_prompt_payload(turn_two))
        self.assertGreaterEqual(_common_prefix_length(legacy_one, legacy_two),
                                legacy_one.find('],"sceneContext"') - 1)
        self.assertLess(len(cache_one), len(legacy_one), 'compact tags should keep cache-first payload smaller')

    def test_group_turns_keep_recent_exchange_empty(self):
        """group turns keep recentExchange empty because groupContext already ends near the decision point"""
        req = self.request([self.entry(1, 'group-message', '群里说话', 5)], None, groupContext={
            'groupId': '111', 'channelId': '111', 'label': '群', 'purpose': '闲聊',
            'characterRole': '群友', 'messages': [],
        })
        payload = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual(payload['relevantEstablishedEpisodes']['recentExchange'], [])

    def test_compact_tags_collapse_triples_and_keep_group_action_distinctions(self):
        """compact tags collapse kind/actor triples and keep group and action distinctions"""
        req = self.request([
            self.entry(1, 'character-message', 'a', 50),
            self.entry(2, 'character-group-message', 'b', 49),
            self.entry(3, 'character-platform-action', 'c', 48),
            self.entry(4, 'script', 'd', 47),
            self.entry(5, 'user-message', 'e', 46),
            self.entry(6, 'group-message', 'f', 45),
            self.entry(7, 'intent-cancelled', 'g', 44),
        ], '当前消息')
        payload = to_prompt_payload(req, {'cacheFirst': True})
        recent = payload['relevantEstablishedEpisodes']['recentScript']
        self.assertEqual([item['tag'] for item in recent], [
            'protagonist', 'protagonist(group)', 'protagonist(action)', 'protagonist-narration',
            'user', 'group-member', 'system',
        ])
        self.assertTrue(all('participantId' not in item for item in recent))

    def test_participant_id_survives_only_when_history_spans_several_branches(self):
        """participantId survives only when the history actually spans several branches"""
        same = [self.entry(1, 'user-message', 'a', 50), self.entry(2, 'character-message', 'b', 49)]
        payload_same = to_prompt_payload(self.request(same, 'x'), {'cacheFirst': True})
        self.assertTrue(all('participantId' not in item
                            for item in payload_same['relevantEstablishedEpisodes']['recentScript']))
        mixed = [same[0], {**same[1], 'participantId': 'onebot:bot:222'}]
        payload_mixed = to_prompt_payload(self.request(mixed, 'x'), {'cacheFirst': True})
        self.assertTrue(all(item['participantId'] in ('participant', 'onebot:bot:222')
                            for item in payload_mixed['relevantEstablishedEpisodes']['recentScript']))

    def test_the_ownership_legend_matches_the_payload_mode(self):
        """the ownership legend matches the payload mode"""
        legacy = system_prompt(*SYSTEM_PROMPT_ARGS)
        compact = system_prompt(*CACHE_FIRST_SYSTEM_PROMPT_ARGS)
        self.assertRegex(legacy, r'ownership label is authoritative')
        self.assertNotRegex(legacy, r'compact tag')
        self.assertRegex(compact, r'compact tag that is authoritative')
        self.assertRegex(compact, r'protagonist\(group\)')
        self.assertRegex(compact, r'protagonist\(action\)')

    def test_working_details_recalled_script_and_previous_scenes_ride_right_zones(self):
        """workingDetails, recalledScript and previousScenes ride the payload in the right zones"""
        req = self.request([self.entry(1, 'user-message', '在吗', 5)], '在吗',
            workingDetails=[{
                'label': '奶茶取餐码', 'value': '8914',
                'expiresAt': _iso(self.now + timedelta(hours=1)), 'createdAt': _iso(self.now),
            }],
            recalledHistory=[{
                'id': 99, 'occurredAt': '2026-08-30T10:00:00.000Z', 'content': '拿到了。确实挺大杯。',
            }],
            sceneContext={
                'scene': None, 'arc': None,
                'previousScenes': [{
                    'startedAt': '2026-08-30T09:00:00.000Z', 'endedAt': '2026-08-30T10:30:00.000Z',
                    'summary': '上一场景摘要',
                }],
            })
        legacy = to_prompt_payload(req)
        self.assertEqual(legacy['ongoingThreads']['workingDetails'][0]['value'], '8914')
        self.assertEqual(legacy['relevantEstablishedEpisodes']['recalledScript'][0]['id'], 99)
        self.assertEqual(
            legacy['relevantEstablishedEpisodes']['sceneContext']['previousScenes'][0]['summary'], '上一场景摘要')
        cache = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual(cache['ongoingThreads']['workingDetails'][0]['value'], '8914')
        self.assertEqual(cache['relevantEstablishedEpisodes']['recalledScript'][0]['id'], 99)
        self.assertEqual(cache['incomingEvent']['event']['content'], '在吗')

    def test_the_fixed_contract_documents_the_three_memory_blocks(self):
        """the fixed contract documents the three memory blocks"""
        prompt = system_prompt(*SYSTEM_PROMPT_ARGS)
        self.assertRegex(prompt, r'previousScenes, when supplied')
        self.assertRegex(prompt, r'workingDetails, when supplied')
        self.assertRegex(prompt, r'recalledScript, when supplied')

    def test_the_fixed_contract_explains_recent_exchange_only_for_cache_first(self):
        """the fixed contract explains recentExchange only for cache-first payloads"""
        plain = system_prompt(*SYSTEM_PROMPT_ARGS)
        cache_aware = system_prompt(*CACHE_FIRST_SYSTEM_PROMPT_ARGS)
        self.assertNotRegex(plain, r'recentExchange')
        self.assertRegex(cache_aware, r'recentExchange at the end duplicates the tail of recentScript')


if __name__ == '__main__':
    unittest.main()
