# -*- coding: utf-8 -*-
"""QQ 原生表情语义，对应上游 src/qq-face.ts（HDS-Interlude 1.0.1-beta6-rebuild）。

平台层说明：上游用 npm `qface` 包查询长期系统表情表（ID ≤ 348），并用本文件的扩展表
补齐 QQ 后续新增、但官方没有稳定机器可读表的表情（349–431）。本项目运行在微信侧，
不附带 qface 数据表，因此这里只在运行环境存在同名 Python 模块时调用其 get()；查不到
时遵守上游注释的约定——“Unknown IDs deliberately stay unknown instead of acquiring
guessed meaning”，返回 None，不猜测含义、不臆造名称。

与上游 JS 语义逐处对应的实现：
- `String(id ?? '')` / `String(content ?? '')` 用 _js_string 复刻（bool → 'true'/'false'，
  整数值浮点 → '349'，对象 → '[object Object]'，数组 → 逗号连接，null/undefined → 'null'/'undefined'）。
- `match?.[1] ?? match?.[2] ?? match?.[3] ?? ''` 用“is not None”依次取第一个捕获组：
  匹配到的空串不会被跳过（?? 只看 null/undefined）。
- 正则的 /i 与 `\b`：JS 的单词字符只含 ASCII，故统一加 re.ASCII，避免 Python 的 Unicode
  `\b` 把中文字符当成单词字符而少匹配。
- `<face>` / `[CQ:face,...]` / `<mface>` 三段替换严格按上游顺序串联，前一步的输出会进入后一步。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional

# 上游 npm qface 包（Python 环境通常不存在）。缺失时按“名称未收录”处理，见文件头。
try:  # pragma: no cover - 取决于运行环境
    import qface as _qface  # type: ignore
except Exception:  # noqa: BLE001 - 可选依赖，缺失/损坏都不能影响本模块导入
    _qface = None

# 上游同名模块内常量 QQ_NATIVE_FACE_NAME_EXTENSIONS（未 export），349–431 逐项照抄。
QQ_NATIVE_FACE_NAME_EXTENSIONS: Dict[str, str] = {
    '349': '坚强', '350': '贴贴', '351': '敲敲', '352': '咦', '353': '拜托', '354': '尊嘟假嘟', '355': '耶', '356': '666',
    '357': '裂开', '358': '骰子', '359': '包剪锤', '360': '亲亲', '361': '狗狗笑哭', '362': '好兄弟', '363': '狗狗可怜', '364': '超级赞',
    '365': '狗狗生气', '366': '芒狗', '367': '狗狗疑问', '368': '奥特笑哭', '369': '彩虹', '370': '祝贺', '371': '冒泡', '372': '气呼呼',
    '373': '忙', '374': '波波流泪', '375': '超级鼓掌', '376': '跺脚', '377': '嗨', '378': '企鹅笑哭', '379': '企鹅流泪', '380': '真棒',
    '381': '路过', '382': 'emo', '383': '企鹅爱心', '384': '晚安', '385': '太气了', '386': '呜呜呜', '387': '太好笑', '388': '太头疼',
    '389': '太赞了', '390': '太头秃', '391': '太沧桑', '392': '龙年快乐', '393': '新年中龙', '394': '新年大龙', '395': '略略略', '396': '狼狗',
    '397': '抛媚眼', '398': '超级ok', '399': 'tui', '400': '快乐', '401': '超级转圈', '402': '别说话', '403': '出去玩', '404': '闪亮登场',
    '405': '好运来', '406': '姐是女王', '407': '我听听', '408': '臭美', '409': '送你花花', '410': '么么哒', '411': '一起嗨', '412': '开心',
    '413': '摇起来', '415': '划龙舟', '416': '中龙舟', '417': '大龙舟', '419': '火车', '420': '中火车', '421': '大火车', '424': '续标识',
    '425': '求放过', '426': '玩火', '427': '偷感', '428': '收到', '429': '蛇年快乐', '430': '蛇身', '431': '蛇尾',
}


def qq_native_face_name(face_id: Any) -> Optional[str]:
    """上游 qqNativeFaceName(id)；参数名 id 与 Python 内置冲突，这里改名 face_id。"""
    key = _face_key(face_id)
    if not key:
        return None
    extension = QQ_NATIVE_FACE_NAME_EXTENSIONS.get(key)
    if extension:
        return extension
    face = _qface_lookup(key)
    qdes = _qface_qdes(face)
    if not isinstance(qdes, str):
        return None
    # TS: face?.QDes?.replace(/^\//, '').trim() || undefined
    name = (qdes[1:] if qdes.startswith('/') else qdes).strip()
    return name or None


def describe_qq_native_face(face_id: Any) -> str:
    """上游 describeQQNativeFace(id)；中文与全角标点逐字保留。"""
    key = _face_key(face_id)
    if not key:
        return '[QQ 原生表情（未提供 ID）]'
    name = qq_native_face_name(key)
    return (
        f'[QQ 原生表情：{name}（ID: {key}）]'
        if name
        else f'[QQ 原生表情（ID: {key}；名称未收录）]'
    )


def normalize_qq_native_face_segments(content: Any) -> str:
    """上游 normalizeQQNativeFaceSegments(content)。

    Convert adapter markup into stable narrator-visible meaning without asking a
    model to infer an icon.
    """
    text = '' if content is None else _js_string(content)
    text = re.sub(
        r'<face\b([^>]*)>(?:</face>)?',
        lambda match: describe_qq_native_face(_attribute_value(match.group(1), 'id')),
        text,
        flags=re.IGNORECASE | re.ASCII,
    )
    text = re.sub(
        r'\[CQ:face,([^\]]*)\]',
        lambda match: describe_qq_native_face(_attribute_value(match.group(1), 'id')),
        text,
        flags=re.IGNORECASE | re.ASCII,
    )

    def market_face(match: 're.Match[str]') -> str:
        attributes = match.group(1)
        name = _attribute_value(attributes, 'summary') or _attribute_value(attributes, 'name')
        return f'[QQ 商城表情：{name}]' if name else '[QQ 商城表情]'

    return re.sub(
        r'<mface\b([^>]*)>(?:</mface>)?',
        market_face,
        text,
        flags=re.IGNORECASE | re.ASCII,
    )


# ========== 上游模块内私有函数 ==========

def _attribute_value(attributes: str, key: str) -> str:
    """上游 attributeValue(attributes, key)。

    TS: new RegExp(`(?:^|[\\s,])${key}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, 'i')
        .exec(attributes)，再 (match?.[1] ?? match?.[2] ?? match?.[3] ?? '').trim()
    """
    pattern = r'(?:^|[\s,])' + re.escape(key) + r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
    match = re.search(pattern, attributes or '', re.IGNORECASE)
    if match is None:
        return ''
    for index in (1, 2, 3):
        value = match.group(index)
        if value is not None:
            return value.strip()
    return ''


def _qface_lookup(key: str) -> Any:
    """上游 qface.get(key)；无该可选模块时返回 None（未知 ID 保持未知）。"""
    getter = getattr(_qface, 'get', None) if _qface is not None else None
    return getter(key) if callable(getter) else None


def _qface_qdes(face: Any) -> Any:
    """上游 face?.QDes。"""
    if face is None:
        return None
    if isinstance(face, dict):
        return face.get('QDes')
    return getattr(face, 'QDes', None)


def _face_key(value: Any) -> str:
    """TS: String(id ?? '').trim()"""
    return ('' if value is None else _js_string(value)).strip()


def _js_string(value: Any) -> str:
    """JS String(value) 的等价实现（用于 String(x ?? '') 中的 String 部分）。"""
    if value is None:
        return 'null'
    if isinstance(value, str):
        return value
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        # ECMAScript Number::toString：绝对值 < 1e21 的整数值用完整十进制
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        text = repr(value)
        if 'e' not in text:
            return text
        mantissa, _, exponent = text.partition('e')
        exp = int(exponent)
        if 1e-6 <= abs(value) < 1e21:
            # JS 在这个区间用十进制展开（Python repr 更早切到指数，如 1e-05）
            digits = mantissa.lstrip('-').replace('.', '')
            sign = '-' if mantissa.startswith('-') else ''
            if exp < 0:
                return '%s0.%s%s' % (sign, '0' * (-exp - 1), digits)
            return '%s%s%s' % (sign, digits, '0' * exp)
        # 指数表示：JS 的指数不带前导零（如 1e-7 而不是 1e-07）
        return '%se%s%d' % (mantissa, '+' if exp >= 0 else '-', abs(exp))
    if isinstance(value, dict):
        return '[object Object]'
    if isinstance(value, (list, tuple)):
        # JS: Array.prototype.join 把 null/undefined 视作空串
        return ','.join('' if item is None else _js_string(item) for item in value)
    return str(value)
