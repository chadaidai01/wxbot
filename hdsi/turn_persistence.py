# -*- coding: utf-8 -*-
"""剧本条目落库草稿，对应上游 src/turn-persistence.ts（1.0.1-beta6-rebuild）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .script.delivery_ledger import create_script_delivery_actions
from .script.life_handoff import normalize_life_handoff
from .utils import is_record


def script_entry_draft_for_commit(
    commit: Dict[str, Any],
    interaction: Optional[Dict[str, Any]],
    timeline_plan: Optional[Dict[str, Any]] = None,
    life_handoff: Any = None,
) -> Dict[str, Any]:
    """上游 scriptEntryDraftForCommit：把一次 ScriptCommitDraft 变成可落库的条目草稿。"""
    window = commit.get('window') if is_record(commit.get('window')) else {}
    script_delta = commit.get('sceneDelta') if is_record(commit.get('sceneDelta')) else {}
    metadata: Dict[str, Any] = {
        'phase': commit.get('phase'),
        'narrativeAuthority': 'original-v2',
        'lifeHandoff': normalize_life_handoff(life_handoff, commit.get('prose')),
        'commitId': commit.get('commitId'),
        'frameId': script_delta.get('frameId'),
        'burstId': script_delta.get('burstId'),
        'interaction': interaction,
        'scriptCommit': {
            'commitId': commit.get('commitId'),
            'sourceFormat': commit.get('sourceFormat'),
            'from': window.get('from'),
            'to': window.get('to'),
            'eventCount': len(commit.get('events') or []),
        },
        'scriptEvents': commit.get('events'),
        'deliveryActions': create_script_delivery_actions(commit),
        'sceneDelta': commit.get('sceneDelta'),
    }
    if timeline_plan is not None:
        metadata['timelinePlan'] = timeline_plan
        metadata['timelineWindow'] = {'from': window.get('from'), 'to': window.get('to')}
    return {
        'kind': 'script',
        'actor': 'narrator',
        'content': commit.get('prose'),
        'occurredAt': window.get('to'),
        'metadata': metadata,
    }
