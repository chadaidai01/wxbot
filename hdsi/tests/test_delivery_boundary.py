# -*- coding: utf-8 -*-
"""移植自上游 .hdsi_reference/test/delivery-boundary.test.ts（纯逻辑，无需 Koishi session / DB / narrator）。

上游对应文件：.hdsi_reference/test/delivery-boundary.test.ts
移植说明：
- TS `attachMessageEvent` → `attach_message_event`，其余同名 snake_case。
- TS `{ content: '第二句', ...scriptEventPayload(prepared!, 1) }` → Python `{'content': '第二句', **script_event_payload(prepared, 1)}`
  （Python 字典展开语义与 JS 对象展开一致：后面的键覆盖同名前值）。
- 唯一用例不依赖上游运行时，全部照搬。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from hdsi.delivery import (  # noqa: E402
    attach_message_event,
    delivery_entry_metadata,
    prepare_outgoing_delivery,
    restore_message_event,
    script_event_payload,
)


class DeliveryBoundaryTest(unittest.TestCase):

    def test_all_bubbles_retain_script_event_identity_across_intents_and_receipts(self):
        """all bubbles retain the script event identity across delivery intents and receipts"""
        event = {
            'commitId': 'commit:x', 'eventId': 'commit:x:e2', 'kind': 'outgoing-message', 'actor': 'protagonist',
            'occurredAt': '2026-09-04T00:00:00.000Z', 'causedByEventIds': ['commit:x:e1'], 'participantId': 'alice',
            'content': '第一句<sep/>第二句', 'bubbles': ['第一句', '第二句'], 'deliveryMode': 'immediate',
        }
        attached = attach_message_event({'participantId': 'alice', 'content': event['content']}, event)
        prepared = prepare_outgoing_delivery(attached, event['bubbles'])
        self.assertEqual(prepared['content'], '第一句')
        self.assertEqual(prepared['laterSegments'], ['第二句'])
        self.assertEqual(prepared['scriptEvent']['bubbleCount'], 2)

        payload = {'content': '第二句', **script_event_payload(prepared, 1)}
        restored = restore_message_event(payload, '第二句')
        self.assertEqual(restored['eventId'], event['eventId'])
        self.assertEqual(restored['bubbleIndex'], 1)
        self.assertEqual(restored['fullContent'], event['content'])
        self.assertEqual(
            delivery_entry_metadata({'participantId': 'alice', 'content': '第二句', 'scriptEvent': restored})['eventId'],
            event['eventId'])


if __name__ == '__main__':
    unittest.main()
