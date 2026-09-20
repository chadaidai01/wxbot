# -*- coding: utf-8 -*-
"""分层日志渲染，对应上游 src/logging.ts（1.0.1-beta6-rebuild，226 行）。

上游运行在服务端并直接写入 Koishi logger；本移植只做纯渲染：
`render_log_message` / `format_layered_log` 把结果作为字符串返回，由调用方交给
Python logging。ANSI 颜色码、分隔线、层叠格式、动作识别正则与上游逐字一致，
中文文案原样保留。
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, TypedDict

from .types import NarrativePhase

InterludeLogLevel = Literal['error', 'warn', 'info', 'debug']
InterludeLogFormat = Literal['compact', 'detailed', 'layered']
InterludeLogColorTheme = Literal['dark', 'light']
InterludeLogAction = Literal[
    'receive', 'send', 'processing', 'complete', 'trigger', 'emotion',
    'memory', 'advance', 'agency', 'group', 'error', 'retry', 'warning', 'waiting', 'system',
]


class _LayeredLogInputRequired(TypedDict):
    level: InterludeLogLevel
    message: str


class LayeredLogInput(_LayeredLogInputRequired, total=False):
    phase: NarrativePhase
    protagonist: str
    args: List[Any]
    colors: bool
    colorTheme: InterludeLogColorTheme
    kaomoji: bool
    standalone: bool


KAOMOJI: Dict[str, str] = {
    'receive': '(*^▽^*)',
    'send': '(・ω・)ノ',
    'processing': '(•̀ᴗ•́)و',
    'complete': '(ﾉ´ヮ`)ﾉ*: ･ﾟ',
    'trigger': '(๑•̀ㅂ•́)و✧',
    'emotion': '(*>ω<*)',
    'memory': '₍ᐢ- ˕ -ᐢ₎zzZ',
    'advance': '(⊙ω⊙)',
    'agency': 'ᕙ( •̀ ᗜ •́ )ᕗ',
    'group': '(´▽｀)ノ',
    'error': '(˶ˊᜊˋ˶)',
    'retry': '(ง •̀_•́)ง',
    'warning': '(´･_･`)',
    'waiting': '(っ˘ω˘ς )',
    'system': '(^_^)/',
}

SYMBOLS: Dict[str, str] = {
    'receive': '←', 'send': '→', 'processing': '⋯', 'complete': '✓', 'trigger': '⚡', 'emotion': '★',
    'memory': '◈', 'advance': '⟳', 'agency': '◇', 'group': '◎', 'error': '✗', 'retry': '↻', 'warning': '!', 'waiting': '…', 'system': '•',
}

FIELD_LABELS: Dict[str, str] = {
    '任务': '任务', '模型': '模型', '参与者': '参与者', '时间段': '时间段', '到期计划': '到期计划',
    '耗时': '耗时', '剧本文字': '剧本文字', '回复模式': '回复模式', '成功': '成功', '可见消息': '可见消息',
    '合并消息': '合并消息', '数量': '数量', '数值': '数值', '累计': '累计', '阈值': '阈值', '方向': '方向',
    '强度': '强度', '描述': '描述', '权重': '权重', '错误': '错误', '群': '群聊', '发送者': '发送者',
    '模式': '模式', '条目': '条目', '字符': '字符', '长期事实': '长期事实', '状态变更': '状态变更',
    '时间': '时间', '间隔': '间隔', '等待': '等待', '已投递': '已投递', '原因': '原因', '请求': '请求',
}

# 上游注释：logger 在服务端运行，无法探测 Console 的 CSS 主题，
# 因此提供 dark/light 两套手选 256 色盘。dark 用明亮柔和的粉彩色，
# light 用较深的墨色，保证白底上的对比度。
COLOR_PALETTES: Dict[str, Dict[str, int]] = {
    'dark': {
        'protagonist': 159,
        'detail': 250,
        'body': 255,
        'user': 81,
        'success': 114,
        'alter': 219,
        'memory': 111,
        'warning': 222,
        'error': 210,
    },
    'light': {
        'protagonist': 24,
        'detail': 240,
        'body': 236,
        'user': 25,
        'success': 28,
        'alter': 90,
        'memory': 25,
        'warning': 130,
        'error': 160,
    },
}

_PHASE_LABELS: Dict[str, str] = {
    'user-message': '用户消息',
    'conversation-follow-up': '对话后续',
    'advance': '自动推进',
    'intent-due': '到期意图',
}

# 动作识别正则（与上游 detectLogAction 逐字一致；JS 的 . 默认不匹配换行，Python 相同）
_RE_RETRY = re.compile(r'重试|再次尝试')
_RE_CALL_FAILURE = re.compile(r'模型调用失败|主叙事失败|消息投递失败')
_RE_WARNING = re.compile(r'警告|拦截|不可用|失败')
_RE_TRIGGER = re.compile(r'Alter.*(?:触发|超过阈值)|累积触发')
_RE_COMPLETE_TASK = re.compile(r'(?:模型调用|情绪偏移生成|记忆整理|后台扫描|剧本推进).*完成')
_RE_ALTER_EMOTION = re.compile(r'情绪偏移|Alter')
_RE_AGENCY = re.compile(r'Agency|主动联系判断|主动联系重查')
_RE_MEMORY = re.compile(r'记忆|压缩|Overlay')
_RE_GROUP = re.compile(r'群消息|群聊|群发言')
_RE_SEND = re.compile(r'投递|发送')
_RE_RECEIVE = re.compile(r'收到|接收|入队')
_RE_PROCESSING = re.compile(r'模型调用开始|分析开始|读取开始|整理开始')
_RE_COMPLETE = re.compile(r'完成|成功|已就绪|已启动')
_RE_ADVANCE = re.compile(r'推进|后台扫描')
_RE_WAITING = re.compile(r'等待|计时器|排队')

# 上游 extractFields 的字段正则：(?:^|\s)([\p{L}\p{N}_-]+)=([^=]*?)(?=\s+[\p{L}\p{N}_-]+=|$)
# Python 的 \w 在 str 模式下包含 Unicode 字母/数字/下划线，等价于 [\p{L}\p{N}_]。
_FIELD_PATTERN = re.compile(r'(?:^|\s)([\w-]+)=([^=]*?)(?=\s+[\w-]+=|$)')

_FORMAT_SPECIFIERS = frozenset('sdifjoOc')


def render_log_message(message: str, args: Optional[List[Any]] = None) -> str:
    """上游 renderLogMessage：node:util format（Error 取 message）。"""
    values = list(args) if args else []
    # 上游：value instanceof Error ? value.message : value；Python 侧异常用 str()。
    mapped = [str(value) if isinstance(value, BaseException) else value for value in values]
    return _format_text(message, *mapped)


def detect_log_action(message: str, level: InterludeLogLevel) -> InterludeLogAction:
    """上游 detectLogAction：按固定顺序匹配日志动作。"""
    if level == 'error':
        return 'error'
    if _RE_RETRY.search(message):
        return 'retry'
    if _RE_CALL_FAILURE.search(message):
        return 'error'
    if level == 'warn' or _RE_WARNING.search(message):
        return 'warning'
    if _RE_TRIGGER.search(message):
        return 'trigger'
    if _RE_COMPLETE_TASK.search(message):
        return 'complete'
    if _RE_ALTER_EMOTION.search(message):
        return 'emotion'
    if _RE_AGENCY.search(message):
        return 'agency'
    if _RE_MEMORY.search(message):
        return 'memory'
    if _RE_GROUP.search(message):
        return 'group'
    if _RE_SEND.search(message):
        return 'send'
    if _RE_RECEIVE.search(message):
        return 'receive'
    if _RE_PROCESSING.search(message):
        return 'processing'
    if _RE_COMPLETE.search(message):
        return 'complete'
    if _RE_ADVANCE.search(message):
        return 'advance'
    if _RE_WAITING.search(message):
        return 'waiting'
    return 'system'


def format_layered_log(log_input: LayeredLogInput) -> str:
    """上游 formatLayeredLog：层叠格式（主行 + └─/├─ 字段行）。"""
    text = render_log_message(log_input['message'], log_input.get('args'))
    action = detect_log_action(text, log_input['level'])
    details = _extract_fields(text)
    summary = details['summary'] or text
    standalone = log_input.get('standalone') is True
    root = _is_root_log(summary, action, log_input['level'], standalone)
    branch = '' if root else ('└─' if _is_final_branch(summary, action) else '├─')
    category = _log_category(action, log_input.get('phase'), standalone, text)
    face = SYMBOLS[action] if log_input.get('kaomoji') is False else KAOMOJI[action]
    palette = COLOR_PALETTES[log_input.get('colorTheme') or 'dark']
    # 上游 paint 的 enabled 默认 true：只有显式传入 colors 才按其真值处理。
    colors = log_input['colors'] if 'colors' in log_input else True
    header = (
        f"{_paint(category, _category_color(action, log_input.get('phase'), text, palette), colors)} "
        f"{_paint(log_input.get('protagonist') or 'HDSI', palette['protagonist'], colors)}"
        if root else branch
    )
    main = (
        f"{header}{' ' if header else ''}"
        f"{_paint(face, _action_color(action, palette), colors)} "
        f"{_paint(summary, _summary_color(action, log_input['level'], palette), colors)}"
    ).rstrip()
    if not details['fields']:
        return main
    lines: List[str] = []
    for index, field in enumerate(details['fields']):
        connector = '└─' if index == len(details['fields']) - 1 else '├─'
        lines.append(
            f"{connector if root else '   ' + connector} "
            f"{_paint(field['label'] + ':', palette['detail'], colors)} {field['value']}"
        )
    return '\n'.join([main] + lines)


def phase_label(phase: Optional[NarrativePhase] = None) -> Optional[str]:
    """上游 phaseLabel：阶段中文名；未知阶段与上游一致返回 undefined（None）。"""
    if not phase:
        return '系统'
    return _PHASE_LABELS.get(phase)


def _log_category(action: InterludeLogAction, phase: Optional[NarrativePhase] = None, standalone: bool = False, message: str = '') -> str:
    if action == 'trigger' or action == 'emotion' or _RE_ALTER_EMOTION.search(message):
        return '[情绪追踪]'
    if action == 'agency' or _RE_AGENCY.search(message):
        return '[主体节奏]'
    if action == 'memory' or _RE_MEMORY.search(message):
        return '[记忆整理]'
    if action == 'group' or _RE_GROUP.search(message):
        return '[群聊]'
    if action == 'retry':
        return '[自动重试]'
    if standalone:
        return '[系统]'
    return f'[{phase_label(phase)}]'


def _extract_fields(text: str) -> Dict[str, Any]:
    if '\n' in text:
        return {'summary': text, 'fields': []}
    fields: List[Dict[str, str]] = []
    first = -1
    for match in _FIELD_PATTERN.finditer(text):
        if first < 0:
            first = match.start()
        raw = match.group(1)
        value = match.group(2).strip()
        if not value:
            continue
        fields.append({'label': FIELD_LABELS.get(raw) or raw, 'value': value})
    summary = re.sub(r'[：:，,]+$', '', text[:first].strip()) if first >= 0 else text
    return {'summary': summary, 'fields': fields}


def _is_root_log(summary: str, action: InterludeLogAction, level: InterludeLogLevel, standalone: bool) -> bool:
    if standalone or level == 'error' or action == 'error':
        return True
    if action == 'trigger' or (action == 'memory' and re.search(r'开始', summary)):
        return True
    if action == 'advance' and re.search(r'(?:开始|即将执行)', summary):
        return True
    if action == 'receive' and re.search(r'(?:收到|接收)', summary):
        return True
    if action == 'group' and re.search(r'收到', summary):
        return True
    return False


def _is_final_branch(summary: str, action: InterludeLogAction) -> bool:
    if action == 'send':
        return True
    if action == 'complete' and not re.search(r'模型调用完成', summary):
        return True
    return re.search(r'写作回合完成|扫描完成|整理完成|已注入', summary) is not None


def _category_color(action: InterludeLogAction, phase: Optional[NarrativePhase], message: str, palette: Dict[str, int]) -> int:
    if action == 'error':
        return palette['error']
    if action == 'warning' or action == 'retry':
        return palette['warning']
    if action == 'trigger' or action == 'emotion' or _RE_ALTER_EMOTION.search(message):
        return palette['alter']
    if action == 'agency' or _RE_AGENCY.search(message):
        return palette['user']
    if action == 'memory' or _RE_MEMORY.search(message):
        return palette['memory']
    if action == 'complete':
        return palette['success']
    if phase == 'advance':
        return palette['memory']
    return palette['user']


def _action_color(action: InterludeLogAction, palette: Dict[str, int]) -> int:
    if action == 'error':
        return palette['error']
    if action == 'warning' or action == 'retry':
        return palette['warning']
    if action == 'complete' or action == 'send':
        return palette['success']
    if action == 'trigger' or action == 'emotion':
        return palette['alter']
    if action == 'memory' or action == 'advance':
        return palette['memory']
    if action == 'agency':
        return palette['user']
    return palette['user']


def _summary_color(action: InterludeLogAction, level: InterludeLogLevel, palette: Dict[str, int]) -> int:
    if level == 'error':
        return palette['error']
    if level == 'warn':
        return palette['warning']
    if action == 'complete':
        return palette['success']
    return palette['body']


def _paint(value: str, code: int, enabled: bool = True) -> str:
    # 上游：基本 ANSI（30-37 / 90-97）直接写码，其余按 256 色 38;5;N。
    if not enabled:
        return value
    basic_ansi = (30 <= code <= 37) or (90 <= code <= 97)
    sequence = str(code) if basic_ansi else f'38;5;{code}'
    return f'\u001b[{sequence}m{value}\u001b[0m'


# ---------------------------------------------------------------------------
# node:util format 的等价实现（只服务于 render_log_message）
# ---------------------------------------------------------------------------

def _format_text(template: Any, *args: Any) -> str:
    """node:util.format(message, ...args)。

    与 Node 一致：没有额外参数时原样返回模板；`%%` 不消耗参数；参数用尽后保留
    未填充的占位符；多余参数按 inspect 追加（空格分隔）。
    """
    if not isinstance(template, str):
        return ' '.join(_format_extra(value) for value in (template,) + args)
    if not args:
        return template
    pieces: List[str] = []
    index = 0
    arg_index = 0
    while index < len(template):
        char = template[index]
        if char == '%' and index + 1 < len(template) and arg_index < len(args):
            specifier = template[index + 1]
            if specifier == '%':
                pieces.append('%')
                index += 2
                continue
            if specifier in _FORMAT_SPECIFIERS:
                pieces.append(_format_value(specifier, args[arg_index]))
                arg_index += 1
                index += 2
                continue
        pieces.append(char)
        index += 1
    text = ''.join(pieces)
    if arg_index < len(args):
        remainder = ' '.join(_format_extra(value) for value in args[arg_index:])
        return f'{text} {remainder}' if text else remainder
    return text


def _format_extra(value: Any) -> str:
    """Node 追加多余参数时的规则：字符串原样，其余走 inspect。"""
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, (int, float)):
        return _js_number_text(_js_number(value))
    return _inspect(value)


def _format_value(specifier: str, value: Any) -> str:
    if specifier == 's':
        return _js_string(value)
    if specifier == 'd':
        return _js_number_text(_js_number(value))
    if specifier == 'i':
        return _js_number_text(_js_parse_int(value))
    if specifier == 'f':
        return _js_number_text(_js_parse_float(value))
    if specifier == 'j':
        return _json_stringify(value)
    if specifier == 'o':
        return _inspect(value, 0, 4)
    if specifier == 'O':
        return _inspect(value, 0, 2)
    if specifier == 'c':
        # CSS 占位符：消耗参数但不输出。
        return ''
    return ''


def _js_string(value: Any) -> str:
    """JS String(value) / Node %s（对象走 inspect）。"""
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return _js_number_text(_js_number(value))
    if isinstance(value, (dict, list, tuple)):
        return _inspect(value, 0, 0)
    return str(value)


def _js_number(value: Any) -> float:
    """JS Number(value)：失败返回 NaN。"""
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        if text in ('Infinity', '+Infinity', '-Infinity'):
            return math.inf if text[0] != '-' else -math.inf
        try:
            if re.fullmatch(r'0[xX][0-9a-fA-F]+', text):
                return float(int(text, 16))
            if re.fullmatch(r'0[bB][01]+', text):
                return float(int(text, 2))
            if re.fullmatch(r'0[oO][0-7]+', text):
                return float(int(text, 8))
            if re.fullmatch(r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?', text):
                return float(text)
        except ValueError:
            return math.nan
        return math.nan
    if isinstance(value, (list, tuple)):
        if not value:
            return 0.0
        if len(value) == 1:
            return _js_number(value[0])
        return math.nan
    return math.nan


def _js_number_text(number: float) -> str:
    """JS String(Number)。"""
    if math.isnan(number):
        return 'NaN'
    if math.isinf(number):
        return 'Infinity' if number > 0 else '-Infinity'
    if number == 0:
        return '0'
    if float(number).is_integer() and abs(number) < 1e21:
        return str(int(number))
    text = repr(float(number))
    if 'e' in text:
        if 1e-6 <= abs(number) < 1e21:
            # Python repr 在 1e-6..1e-4 也用指数，JS 用十进制展开。
            return format(Decimal(text), 'f')
        mantissa, _, exponent = text.partition('e')
        sign = '-' if exponent.startswith('-') else '+'
        digits = exponent.lstrip('+-0') or '0'
        return f'{mantissa}e{sign}{digits}'
    return text


def _js_parse_int(value: Any) -> float:
    """JS parseInt(value)（自动识别 0x/0b/0o，否则十进制；失败 NaN）。"""
    if isinstance(value, bool):
        text = 'true' if value else 'false'
    elif value is None:
        text = 'null'
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = _js_number_text(_js_number(value))
    elif isinstance(value, (list, tuple)):
        text = ','.join(_js_string(item) for item in value)
    else:
        return math.nan
    match = re.match(r'^\s*([+-]?)(0[xX][0-9a-fA-F]+|\d+)', text)
    if not match:
        return math.nan
    digits = match.group(2)
    base = 16 if digits[:2].lower() == '0x' else 10
    number = int(digits, base)
    return float(-number if match.group(1) == '-' else number)


def _js_parse_float(value: Any) -> float:
    """JS parseFloat(value)：取最长合法前缀，失败 NaN。"""
    if isinstance(value, bool):
        text = 'true' if value else 'false'
    elif value is None:
        text = 'null'
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = _js_number_text(_js_number(value))
    elif isinstance(value, (list, tuple)):
        text = ','.join(_js_string(item) for item in value)
    else:
        return math.nan
    match = re.match(r'^\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))', text)
    if not match:
        return math.nan
    token = match.group(1)
    if token.endswith('Infinity'):
        return -math.inf if token[0] == '-' else math.inf
    return float(token)


def _json_stringify(value: Any) -> str:
    """JS JSON.stringify（%j）；循环引用/不可序列化按上游返回 '[Circular]'。"""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        number = _js_number(value)
        return 'null' if math.isnan(number) or math.isinf(number) else _js_number_text(number)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(_json_sanitize(value), ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError):
            return '[Circular]'
    return 'undefined'


def _json_sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(key): _json_sanitize(item)
            for key, item in value.items()
            if not callable(item)
        }
    if isinstance(value, (list, tuple)):
        return [None if callable(item) else _json_sanitize(item) for item in value]
    return None


def _inspect(value: Any, depth: int = 0, max_depth: int = 4) -> str:
    """node:util.inspect 的近似实现；对象字面量与数组按 Node 风格输出。"""
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return _inspect_string(value)
    if isinstance(value, (int, float)):
        return _js_number_text(_js_number(value))
    if isinstance(value, dict):
        if depth > max_depth:
            return '[Object]'
        items = ', '.join(
            f'{_inspect_key(key)}: {_inspect(item, depth + 1, max_depth)}'
            for key, item in value.items()
        )
        return f'{{ {items} }}' if items else '{}'
    if isinstance(value, (list, tuple)):
        if depth > max_depth:
            return '[Array]'
        items = ', '.join(_inspect(item, depth + 1, max_depth) for item in value)
        return f'[ {items} ]' if items else '[]'
    return str(value)


def _inspect_string(value: str) -> str:
    escapes = {'\\': '\\\\', "'": "\\'", '\n': '\\n', '\r': '\\r', '\t': '\\t'}
    return "'" + ''.join(escapes.get(char, char) for char in value) + "'"


def _inspect_key(key: Any) -> str:
    if isinstance(key, str) and re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', key):
        return key
    if isinstance(key, str):
        return _inspect_string(key)
    return str(key)
