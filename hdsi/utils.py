# -*- coding: utf-8 -*-
"""hdsi 公共小工具。

对应上游散落在各文件里的 isRecord / clip / clamp / toDate / 数组归一化等局部函数。
移植时统一从这里导入，保证语义与上游一致。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def is_record(value: Any) -> bool:
    """TS isRecord：非空对象才算 record；空 dict 在 JS 里是 truthy，必须返回 True。"""
    return isinstance(value, dict)


def is_array(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def text_or_undefined(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def clipped_text_or_undefined(value: Any, limit: int) -> Optional[str]:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def clip(value: Any, length: int) -> str:
    return value.strip()[:length] if isinstance(value, str) else ''


def clamp(value: float, minimum: float, maximum: float) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return minimum
    return max(minimum, min(maximum, value))


def clamp_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or math.isnan(float(value)):
        return fallback
    return max(minimum, min(maximum, float(value)))


def finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(float(value)) or math.isinf(float(value)):
        return None
    return float(value)


def finite_integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def finite_float(value: Any) -> Optional[float]:
    number = finite_number(value)
    return number


def safe_int(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def integer_array(value: Any, limit: int) -> List[int]:
    if not isinstance(value, (list, tuple)):
        return []
    result: List[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def string_array(value: Any, limit: int, item_limit: int) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    seen = set()
    result: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:item_limit]
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def unique_strings(values: Iterable[Any], limit: int = 0) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if limit and len(result) >= limit:
            break
    return result


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def json_equal(left: Any, right: Any) -> bool:
    return json_dumps(left) == json_dumps(right)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    raise TypeError(f'Object of type {type(value)!r} is not JSON serializable')


def stable_hash(value: Any, length: int = 40) -> str:
    raw = value if isinstance(value, str) else json_dumps(value)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:length]


def normalize_ws(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()


def truncate(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ''
    return text if len(text) <= limit else text[:limit]


def first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def as_number(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    return fallback


def percent(part: float, total: float) -> float:
    return 0.0 if total <= 0 else part / total


def escape_regex(value: str) -> str:
    return re.escape(value)
