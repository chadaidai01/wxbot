# -*- coding: utf-8 -*-
"""主叙事 JSON 解析失败的行为：只诊断（warn + 落盘原文），不重试。

线上曾出现模型把预算花在推理上、JSON 被 max_tokens 截断（finish_reason=length）。
按用户要求，这里保持上游行为：解析失败即抛错，由外部提高 THEATER_MAX_TOKEN 解决，
不再做“去掉 max_tokens 重试”。
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import hdsi.narrator_providers as np  # noqa: E402


class FakeCtx:
    def __init__(self, responses):
        self.logger = logging.getLogger('hdsi-test')
        self.responses = list(responses)
        self.calls = []

    def http_post(self, url, headers=None, json_body=None, timeout_ms=None, stream=False):
        self.calls.append({'url': url, 'body': dict(json_body or {})})
        return self.responses.pop(0)


def _response(content, finish_reason):
    return {'choices': [{'finish_reason': finish_reason, 'message': {'content': content}}], 'usage': {}}


PROVIDER = {
    'id': 'primary', 'label': 'Primary model', 'enabled': True, 'mode': 'openai-compatible',
    'endpoint': 'https://example.invalid/v1/chat/completions', 'apiKey': 'k', 'model': 'm',
    'temperature': 0.8, 'topP': 1, 'maxTokens': 120, 'timeout': 5000, 'responseFormat': 'json-object',
}
CONFIG = {
    'mainTemperature': 0.8, 'mainTopP': 1, 'mainMaxTokens': 120, 'mainTimeout': 5000,
    'mainResponseFormat': 'json-object', 'mainStreamingMode': 'off', 'mainPayloadOrder': 'legacy',
    'mainPrompt': '', 'formatPrompt': '', 'fixedPrompt': '', 'stylePrompt': '',
    'failover': {'enabled': False},
}
OVERRIDES = {'maxTokens': 120, 'temperature': 0.8, 'topP': 1, 'timeout': 5000,
             'responseFormat': 'json-object', 'model': 'm'}
REQUEST = {
    'phase': 'user-message', 'story': {'setting': {}, 'state': {}}, 'recentEntries': [],
    'from': datetime.now(timezone.utc), 'now': datetime.now(timezone.utc),
    'writingOptions': {'messageSeparator': '<sep/>'},
}


class NarratorInvalidJsonTest(unittest.TestCase):
    def setUp(self):
        self._orig_payload = np.to_prompt_payload
        np.to_prompt_payload = lambda *args, **kwargs: {}

    def tearDown(self):
        np.to_prompt_payload = self._orig_payload

    def _narrator(self, responses):
        ctx = FakeCtx(responses)
        return ctx, np.OpenAICompatibleNarrator(ctx, CONFIG, True, None, {'main': {'available': True}})

    def test_valid_json_returns_decision(self):
        valid = json.dumps({'script': '她看了一眼窗外'}, ensure_ascii=False)
        ctx, narrator = self._narrator([_response(valid, 'stop')])
        decision = narrator._request_provider(PROVIDER, REQUEST, OVERRIDES, [], '主叙事')
        self.assertEqual(decision.get('script'), '她看了一眼窗外')
        self.assertEqual(len(ctx.calls), 1)
        self.assertIn('max_tokens', ctx.calls[0]['body'])

    def test_truncated_json_raises_without_retry(self):
        # finish_reason=length + 截断 JSON：只诊断，不重试（由提高 THEATER_MAX_TOKEN 解决）
        ctx, narrator = self._narrator([_response('{"script": "她看了一眼窗', 'length')])
        with self.assertRaises(RuntimeError) as raised:
            narrator._request_provider(PROVIDER, REQUEST, OVERRIDES, [], '主叙事')
        self.assertIn('invalid JSON', str(raised.exception))
        self.assertEqual(len(ctx.calls), 1, '不再做去掉 max_tokens 的重试')

    def test_broken_json_raises_without_retry(self):
        ctx, narrator = self._narrator([_response('这不是 JSON', 'stop')])
        with self.assertRaises(RuntimeError):
            narrator._request_provider(PROVIDER, REQUEST, OVERRIDES, [], '主叙事')
        self.assertEqual(len(ctx.calls), 1)

    def test_dump_invalid_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            np._dump_invalid_decision('{"a": 1', 'length', 'Expecting value', directory=tmp)
            path = os.path.join(tmp, 'hdsi_invalid_json.txt')
            self.assertTrue(os.path.exists(path))
            with open(path, 'r', encoding='utf-8') as handle:
                content = handle.read()
            self.assertIn('finish_reason=length', content)
            self.assertIn('{"a": 1', content)


if __name__ == '__main__':
    unittest.main()
