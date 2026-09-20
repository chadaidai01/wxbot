# -*- coding: utf-8 -*-
"""表结构定义，对应上游 src/database.ts（Koishi/minato 表 → SQLite 表）。

每张表的字段、主键、索引与上游一一对应；未列出的字段不允许写入。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 字段类型：string/text/json/timestamp/unsigned/double/boolean
TABLES: Dict[str, Dict[str, Any]] = {
    'interlude_story': {
        'fields': {
            'id': 'string', 'platform': 'string', 'selfId': 'string', 'userId': 'string',
            'channelId': 'string', 'status': 'string', 'setting': 'json', 'state': 'json',
            'cursorAt': 'timestamp', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': False,
        'indexes': ['platform', 'selfId', 'userId'],
        'unique': [],
    },
    'interlude_participant': {
        'fields': {
            'id': 'string', 'storyId': 'string', 'platform': 'string', 'selfId': 'string',
            'userId': 'string', 'channelId': 'string', 'personId': 'string',
            'displayName': 'string', 'profile': 'text', 'relationship': 'text', 'state': 'json',
            'status': 'string', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': False,
        'indexes': ['storyId', 'status', 'personId', 'userId'],
        'unique': [],
    },
    'interlude_script_entry': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'kind': 'string',
            'actor': 'string', 'content': 'text', 'occurredAt': 'timestamp', 'metadata': 'json',
            'embedding': 'json', 'createdAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'occurredAt'],
        'unique': [],
    },
    'interlude_memory': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'category': 'string',
            'content': 'text', 'importance': 'double', 'status': 'string', 'sourceEntryId': 'unsigned',
            'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'importance'],
        'unique': [],
    },
    'interlude_intent': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'type': 'string',
            'summary': 'text', 'notBefore': 'timestamp', 'status': 'string', 'payload': 'json',
            'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'notBefore'],
        'unique': [],
    },
    'interlude_scene': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'status': 'string', 'startedAt': 'timestamp',
            'endedAt': 'timestamp', 'hook': 'text', 'summary': 'text', 'entryCount': 'unsigned',
            'lastEntryId': 'unsigned', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'startedAt'],
        'unique': [],
    },
    'interlude_arc': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'status': 'string', 'title': 'string',
            'summary': 'text', 'sceneCount': 'unsigned', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'updatedAt'],
        'unique': [],
    },
    'interlude_fact': {
        'fields': {
            'knowledge': 'json',
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'scope': 'string',
            'content': 'text', 'importance': 'double', 'confidence': 'double', 'unresolved': 'boolean',
            'embedding': 'json', 'status': 'string', 'sourceEntryIds': 'json',
            'lastSeenAt': 'timestamp', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'importance'],
        'unique': [],
    },
    'interlude_state_patch': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'target': 'string',
            'path': 'string', 'proposedValue': 'text', 'evidence': 'text', 'confidence': 'double',
            'impact': 'string', 'status': 'string', 'sourceEntryIds': 'json',
            'createdAt': 'timestamp', 'appliedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'confidence'],
        'unique': [],
    },
    'interlude_overlay_snapshot': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'target': 'string',
            'tier': 'string', 'periodStart': 'timestamp', 'periodEnd': 'timestamp',
            'summary': 'text', 'majorEvents': 'json', 'sourcePatchIds': 'json',
            'status': 'string', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'target', 'periodEnd'],
        'unique': [],
    },
    'interlude_sticker': {
        'fields': {
            'id': 'unsigned', 'assetId': 'string', 'filePath': 'string', 'group': 'string',
            'mimeType': 'string', 'animated': 'boolean', 'size': 'unsigned', 'hash': 'string',
            'description': 'text', 'aliases': 'json', 'status': 'string', 'embedding': 'json',
            'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['status', 'group', 'updatedAt'],
        'unique': ['assetId'],
    },
    'interlude_web_observation': {
        'fields': {
            'id': 'unsigned', 'storyId': 'string', 'participantId': 'string', 'intentId': 'unsigned',
            'mode': 'string', 'query': 'text', 'url': 'text', 'title': 'text', 'excerpt': 'text',
            'summary': 'text', 'status': 'string', 'accessedAt': 'timestamp', 'createdAt': 'timestamp',
        },
        'primary': 'id', 'autoInc': True,
        'indexes': ['storyId', 'status', 'accessedAt'],
        'unique': [],
    },
    'interlude_schedule_preplan': {
        'fields': {
            'storyId': 'string', 'revision': 'unsigned', 'timezone': 'string',
            'validFrom': 'string', 'validThrough': 'string', 'lastReviewedLocalDate': 'string',
            'lastEvidenceEntryId': 'unsigned', 'reviewReason': 'text', 'regimes': 'json',
            'exceptions': 'json', 'materializedDays': 'json', 'createdAt': 'timestamp', 'updatedAt': 'timestamp',
        },
        'primary': 'storyId', 'autoInc': False,
        'indexes': ['validThrough', 'lastReviewedLocalDate'],
        'unique': [],
    },
}

# 读出来的 timestamp 字段转 datetime（上游 normalizeDatabaseRow 的 DATABASE_DATE_FIELDS）
DATABASE_DATE_FIELDS: Dict[str, List[str]] = {
    'interlude_story': ['cursorAt', 'createdAt', 'updatedAt'],
    'interlude_participant': ['createdAt', 'updatedAt'],
    'interlude_script_entry': ['occurredAt', 'createdAt'],
    'interlude_memory': ['createdAt', 'updatedAt'],
    'interlude_intent': ['notBefore', 'createdAt', 'updatedAt'],
    'interlude_scene': ['startedAt', 'endedAt', 'createdAt', 'updatedAt'],
    'interlude_arc': ['createdAt', 'updatedAt'],
    'interlude_fact': ['lastSeenAt', 'createdAt', 'updatedAt'],
    'interlude_state_patch': ['createdAt', 'appliedAt'],
    'interlude_overlay_snapshot': ['periodStart', 'periodEnd', 'createdAt', 'updatedAt'],
    'interlude_sticker': ['createdAt', 'updatedAt'],
    'interlude_web_observation': ['accessedAt', 'createdAt'],
    'interlude_schedule_preplan': ['createdAt', 'updatedAt'],
}

JSON_FIELDS: Dict[str, List[str]] = {
    name: [field for field, kind in spec['fields'].items() if kind == 'json']
    for name, spec in TABLES.items()
}

SQLITE_TYPES = {
    'string': 'TEXT', 'text': 'TEXT', 'json': 'TEXT', 'timestamp': 'TEXT',
    'unsigned': 'INTEGER', 'double': 'REAL', 'boolean': 'INTEGER',
}


def table_names() -> List[str]:
    return list(TABLES.keys())


def table_spec(table: str) -> Dict[str, Any]:
    spec = TABLES.get(table)
    if not spec:
        raise KeyError(f'unknown interlude table: {table}')
    return spec


def register_tables(ctx: Any = None) -> None:
    """Upstream registerTables(ctx): Koishi/minato schema registration.

    The Python port creates every table in hdsi/store.py on first open, so this is a
    compatibility no-op kept so call sites mirroring upstream keep working.
    """
    return None
