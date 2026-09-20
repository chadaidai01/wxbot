# -*- coding: utf-8 -*-
"""HDS-Interlude 管理命令（Python 移植）。

上游文件：`.hdsi_reference/src/index.ts`（HDS-Interlude 1.0.1-beta6-rebuild，919 行）
  · 命令注册：`registerCommands()` 第 519-868 行（ctx.command('interlude.xxx') 共 33 条，
    其中根命令 `interlude` 只有描述没有 action）；
  · 命令文案与辅助函数：第 869-919 行
    （askConfirmation / formatStoryStartReadiness / requireStory / changeStatus /
      requireManager / isFactScope / looksLikeInterludeCommand）。

移植约定：
- 上游 `ctx.command(...)` 注册的每条命令在这里对应 COMMANDS 里的一条 CommandSpec：
  命令名、描述、参数形状（`<x:type>` 必填 / `[x:type]` 可选）与 index.ts 逐字一致；
- `parse_command(text)` 只做解析，返回 `(commandName, args)`（args 为原始字符串）；
  `execute_command(service, session, text, confirm=None)` 执行并返回人类可读文本，
  返回值就是上游 action 的返回值（session.send 的文案逐字保留）；
- 上游 `await service.foo(...)` → 同步 `service.foo(...)`；方法名按
  SERVICE_PORTING_SPEC.md 的方法名对照表（snake_case），本文件不 import 平台/数据库代码；
- 上游 `session.send(...)` + `session.prompt(60_000)` 的 y/n 确认收敛成可注入的
  `confirm(text)` 回调（见 ask_confirmation）：平台层负责发出询问并取回答复；
- 上游 Koishi 的“缺参不执行 action、先输出命令用法”改由 execute_command 返回该命令的
  用法行（`_usage(spec)`），这是本文件唯一没有上游文案的兜底路径；
- index.ts 里的管理命令全部能映射到 service 方法（见下面的 UNSUPPORTED_COMMANDS 说明），
  没有需要降级的平台专属命令。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from .schedule_preplan import resolve_schedule_preplan_config, schedule_preplan_window
from .time_utils import ensure_aware, format_log_time, format_story_display_time, now_utc, parse_time

ROOT_COMMAND = 'interlude'
ROOT_DESCRIPTION = 'HDS Interlude：管理与查看命令'
UNSUPPORTED_MESSAGE = '当前平台不支持'

# 上游 index.ts 注册的管理命令全部落在 SERVICE_PORTING_SPEC.md 的方法对照表里，因此这里为空。
# 若将来出现依赖 Koishi Console 等平台专属能力、无法映射到 service 的命令：在 COMMANDS 里
# 保留命令名/描述/参数（解析与文案不动），把命令名登记到这里，execute_command 会返回
# '当前平台不支持'。
UNSUPPORTED_COMMANDS: Dict[str, str] = {}


# ==================== JS 语义小工具（命令文案里有 toFixed/toISOString/JSON.stringify） ====================

_NAN = float('nan')


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _js_number(value: Any) -> float:
    """Koishi `number` 参数：等价 JS `Number(text)`；解析失败返回 NaN。"""
    if value is None:
        return _NAN
    text = str(value).strip()
    if not text:
        return 0.0  # JS Number('') === 0
    try:
        return float(text)
    except (TypeError, ValueError):
        return _NAN


def _number_arg(raw: Optional[str], default: float) -> float:
    """可选 number 参数：没写时用 action 的 JS 默认值（如 limit = 10）。"""
    if raw is None or raw == '':
        return default
    return _js_number(raw)


def _js_number_text(value: Any) -> str:
    """JS 模板字符串里的数字：整数不带小数点，NaN → 'NaN'。"""
    if value is None:
        return 'undefined'
    if _is_nan(value):
        return 'NaN'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _js_min(left: float, right: float) -> float:
    """JS Math.min：任一参数为 NaN 时结果为 NaN。"""
    if _is_nan(left) or _is_nan(right):
        return _NAN
    return left if left < right else right


def _js_max(left: float, right: float) -> float:
    """JS Math.max：任一参数为 NaN 时结果为 NaN。"""
    if _is_nan(left) or _is_nan(right):
        return _NAN
    return left if left > right else right


def _js_bool(value: Any) -> str:
    """JS 模板字符串里的布尔值：`${true}` → 'true'，`${false}` → 'false'。

    SQLite 里的布尔列可能读成 0/1，这里按真值处理，保持与上游 JSON 语义一致。
    """
    if isinstance(value, str):
        return 'true' if value.strip().lower() in ('true', '1', 'yes') else 'false'
    return 'true' if value else 'false'


def _to_fixed(value: Any, digits: int = 2) -> str:
    """TS Number.prototype.toFixed(digits)。"""
    if value is None:
        return 'undefined'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 'NaN'
    if math.isnan(number):
        return 'NaN'
    return f'{number:.{digits}f}'


def _to_iso_string(value: Any) -> str:
    """TS Date.prototype.toISOString()：UTC + 毫秒精度 + 'Z' 结尾。"""
    parsed = value if isinstance(value, datetime) else parse_time(value)
    if parsed is None:
        return 'Invalid Date'  # JS 里 Invalid Date.toISOString() 会抛 RangeError
    aware = ensure_aware(parsed).astimezone(timezone.utc)
    return (
        f'{aware.year:04d}-{aware.month:02d}-{aware.day:02d}'
        f'T{aware.hour:02d}:{aware.minute:02d}:{aware.second:02d}'
        f'.{aware.microsecond // 1000:03d}Z'
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _to_iso_string(value)
    return str(value)


def _json_stringify(value: Any) -> str:
    """JS JSON.stringify：紧凑输出、中文不转义。"""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=_json_default)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ('' if value is None else str(value))


def _json_stringify_or_undefined(value: Any) -> str:
    """JS JSON.stringify(undefined) 返回 undefined，模板串里是 'undefined'。"""
    if value is None:
        return 'undefined'
    return _json_stringify(value)


# ==================== 命令表结构 ====================

class CommandArg(NamedTuple):
    """一个 Koishi 参数：name/kind 对应 `<name:kind>`，required 对应尖括号还是方括号。"""

    name: str
    kind: str       # 'text' | 'string' | 'number'
    required: bool  # `<x>` True；`[x]` False


class CommandContext(NamedTuple):
    """一次命令执行的输入：service 是 InterludeService，session 是 InboundSession。"""

    service: Any
    session: Any
    args: List[str]
    confirm: Optional[Callable[[str], Any]] = None


class CommandSpec(NamedTuple):
    name: str
    description: str
    args: Tuple[CommandArg, ...]
    handler: Callable[[CommandContext], str]


def _arg(name: str, kind: str, required: bool = True) -> CommandArg:
    return CommandArg(name=name, kind=kind, required=required)


def _usage(spec: CommandSpec) -> str:
    """命令用法行：`interlude.memory.forget <id:number>`（等价 Koishi 缺参时输出的用法）。"""
    parts = [spec.name]
    for argument in spec.args:
        left, right = ('<', '>') if argument.required else ('[', ']')
        parts.append(f'{left}{argument.name}:{argument.kind}{right}')
    return ' '.join(parts)


# ==================== 文本解析（Koishi 参数切分） ====================

def _token_spans(text: str) -> List[Tuple[str, int, int]]:
    """Koishi 风格分词：按空白切分，支持单/双引号包裹；返回 (值, 起点, 终点)。"""
    tokens: List[Tuple[str, int, int]] = []
    index, length = 0, len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        if text[index] in ('"', "'"):
            quote = text[index]
            index += 1
            value_start = index
            while index < length and text[index] != quote:
                index += 1
            value = text[value_start:index]
            if index < length:
                index += 1  # 跳过收尾引号
            tokens.append((value, start, index))
        else:
            value_start = index
            while index < length and not text[index].isspace():
                index += 1
            tokens.append((text[value_start:index], start, index))
    return tokens


def _strip_outer_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def _bind_arguments(spec: CommandSpec, rest: str) -> List[str]:
    """按命令参数表切分剩余文本。

    `text` 是 Koishi 的贪婪参数类型：吃掉剩余原文（interlude.setup 的 JSON、memory.add 的
    content 都靠它），`string`/`number` 各取一个 token。缺参时补空串，由 execute_command
    按 required 判定。
    """
    tokens = _token_spans(rest)
    values: List[str] = []
    index = 0
    for argument in spec.args:
        if argument.kind == 'text':
            if index < len(tokens):
                raw = rest[tokens[index][1]:].strip()
                values.append(_strip_outer_quotes(raw))
            else:
                values.append('')
            index = len(tokens)
        else:
            if index < len(tokens):
                values.append(tokens[index][0])
                index += 1
            else:
                values.append('')
    return values


# ==================== session / story 辅助（上游 869-919 行） ====================

def looks_like_interlude_command(content: Any) -> bool:
    r"""上游 looksLikeInterludeCommand(content)（index.ts 917 行）。

    逐字保留上游正则 `/^[!/.]?interlude(?:\s|$)/i`：只匹配根命令 `interlude`
    （后面紧跟空白或行尾），不匹配 `interlude.status` 这类子命令。
    """
    return re.match(r'^[!/.]?interlude(?:\s|$)', _text(content).strip(), re.IGNORECASE) is not None


def is_fact_scope(value: Any) -> bool:
    """上游 isFactScope(value)（index.ts 913 行）。"""
    return value in ('character', 'world', 'relationship', 'event', 'promise')


def _config(service: Any) -> Dict[str, Any]:
    config = getattr(service, 'config', None)
    return config if isinstance(config, dict) else {}


def _story_setting(story: Any) -> Dict[str, Any]:
    setting = story.get('setting') if isinstance(story, dict) else None
    return setting if isinstance(setting, dict) else {}


def _story_state(story: Any) -> Dict[str, Any]:
    state = story.get('state') if isinstance(story, dict) else None
    return state if isinstance(state, dict) else {}


def _character_name(story: Any) -> str:
    character = _story_setting(story).get('character')
    name = character.get('name') if isinstance(character, dict) else None
    return name if isinstance(name, str) else ''


def _story_timezone(story: Any) -> str:
    return _text(_story_setting(story).get('timezone'))


def _story_id(story: Any) -> Any:
    return story.get('id') if isinstance(story, dict) else None


def _session_user_id(session: Any) -> str:
    return _text(getattr(session, 'userId', ''))


def require_manager(service: Any, session: Any) -> bool:
    """上游 requireManager(service, session)（index.ts 911 行）。"""
    return bool(service.can_manage_session(session))


def require_story(service: Any, session: Any) -> Any:
    """上游 requireStory(service, session)（index.ts 899-902 行）。

    返回 story dict，或需要直接回给用户的字符串（上游用 `typeof story === 'string'` 区分）。
    """
    if not service.can_handle_session(session):
        return '当前 QQ 账号未获 HDSI 互动授权。请在 Console 的“NapCat / OneBot QQ 账号控制”中检查机器人 QQ 号、用户 QQ 白名单和启用状态。'
    story = service.find_story(session)
    if story:
        return story
    return '当前私聊还没有故事。请先在 Console 完成档案，然后执行 interlude.doctor；手动启动请使用 interlude.story.start，或开启 runtime.autoCreate 后直接发送第一条私聊。'


def change_status(service: Any, session: Any, status: str) -> str:
    """上游 changeStatus(service, session, status)（index.ts 904-909 行）。"""
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    service.set_status(story, status)
    return '故事已恢复自动处理。' if status == 'active' else '故事已暂停自动处理；已有记录不会删除。'


def ask_confirmation(session: Any, message: str, confirm: Optional[Callable[[str], Any]] = None) -> bool:
    r"""上游 askConfirmation(session, message)（index.ts 869-873 行）。

    上游先 `session.send(`${message}\n请在 60 秒内回复 y 或 n。`)`，再
    `session.prompt(60_000)`，最后用 `/^(?:y|yes)$/i` 判定。Python 侧把交互收敛成可注入的
    confirm(text) 回调：

    - confirm(text) 可以返回 bool，也可以返回原始答复文本（按 y/yes 判定）；
    - 未注入 confirm 时，若平台 session 提供 prompt(text[, timeout]) 就用它，
      提供 send(text) 就先发询问文案；
    - 两者都没有时按“没有答复”处理，等价上游超时（返回 False）。
    """
    prompt_text = f'{message}\n请在 60 秒内回复 y 或 n。'
    answer: Any = None
    send = getattr(session, 'send', None) if session is not None else None
    prompt = getattr(session, 'prompt', None) if session is not None else None
    if callable(confirm):
        answer = confirm(prompt_text)
    else:
        if callable(send):
            send(prompt_text)
        if callable(prompt):
            try:
                answer = prompt(prompt_text, 60000)
            except TypeError:
                answer = prompt(prompt_text)
    if isinstance(answer, bool):
        return answer
    return re.match(r'^(?:y|yes)$', _text(answer).strip(), re.IGNORECASE) is not None


def _format_story_start_readiness(readiness: Any, title: str = 'Console 档案检查') -> str:
    """上游 formatStoryStartReadiness(readiness, title)（index.ts 875-897 行）。"""
    data = readiness if isinstance(readiness, dict) else {}
    preview = data.get('preview') if isinstance(data.get('preview'), dict) else {}
    existing = data.get('existing')
    lines = [
        title,
        f'主角：{preview.get("characterName") or "未填写"}',
        f'角色设定：{"已填写" if preview.get("characterProfile") else "未填写"}',
        f'Perspective：{"已填写" if preview.get("perspective") else "未填写"}',
        f'世界：{"已填写" if preview.get("world") else "未填写"}',
        f'时区：{preview.get("timezone")}',
        f'主模型：{preview.get("model")}',
        f'自动创建：{"开启" if preview.get("autoCreate") else "关闭"}',
    ]
    if existing:
        lines.append(f'运行中故事：{_character_name(existing)}（{existing.get("status")}）')
    else:
        lines.append('运行中故事：尚未创建')
    for blocker in data.get('blockers') or []:
        lines.append(f'阻断：{blocker}')
    for warning in data.get('warnings') or []:
        lines.append(f'提示：{warning}')
    if existing:
        lines.append('结果：已有运行中故事，无需再次启动。')
    elif data.get('ready'):
        lines.append('结果：可以启动。')
    else:
        lines.append('结果：请先完成阻断项。')
    return '\n'.join(lines)


# ==================== 命令实现（顺序与 index.ts registerCommands 一致） ====================

def _cmd_root(context: CommandContext) -> str:
    """根命令 `interlude` 在上游没有 action；Koishi 默认会输出子命令帮助。

    index.ts 没有给出帮助文案，这里按同样语义生成命令清单（本文件唯一自拟的展示文案）。
    """
    lines = [ROOT_DESCRIPTION]
    for spec in COMMANDS:
        if spec.name == ROOT_COMMAND:
            continue
        lines.append(f'{_usage(spec)}  {spec.description}')
    return '\n'.join(lines)


def _cmd_doctor(context: CommandContext) -> str:
    """interlude.doctor（index.ts 552-553）。"""
    return _format_story_start_readiness(context.service.story_start_readiness(context.session))


def _start_story_from_console(context: CommandContext, legacy_name: Optional[str] = None) -> str:
    """上游 startStoryFromConsole(session, legacyName?)（index.ts 523-550）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '无权限：手动启动共享主剧本需要 HDSI 管理员权限。'
    readiness = service.story_start_readiness(session)
    data = readiness if isinstance(readiness, dict) else {}
    existing = data.get('existing')
    if existing:
        if existing.get('status') == 'paused':
            return f'当前已有 {_character_name(existing)} 的主剧本（暂停中）；请使用 interlude.resume 恢复，不要重复启动。'
        return f'当前已有 {_character_name(existing)} 的活动主剧本；请使用 interlude.status 查看状态。'
    if not data.get('ready'):
        return _format_story_start_readiness(data, 'Console 档案尚未适合启动')
    preview = data.get('preview') if isinstance(data.get('preview'), dict) else {}
    legacy_text = _text(legacy_name).strip()
    legacy_note = f'\n已忽略旧 init 的名称参数“{legacy_text}”；角色名称以 Console 为准。' if legacy_text else ''
    lines = [
        '即将从当前 Console 档案启动故事：',
        f'主角：{preview.get("characterName") or ""}',
        f'角色设定：{"已填写" if preview.get("characterProfile") else "未填写"}',
        f'Perspective：{"已填写" if preview.get("perspective") else "未填写"}',
        f'世界与地点：{"已填写" if preview.get("world") else "未填写"}',
        f'时区：{preview.get("timezone")}',
        f'主模型：{preview.get("model")}',
        f'自动创建：{"开启（首次私聊通常无需手动启动）" if preview.get("autoCreate") else "关闭"}',
    ]
    for warning in data.get('warnings') or []:
        lines.append(f'提示：{warning}')
    lines.append(legacy_note)
    message = '\n'.join(line for line in lines if line)
    if not ask_confirmation(session, f'{message}\n确认从此档案启动吗？(y/n)', context.confirm):
        return '操作已取消。'
    story = service.create_story(session)
    participant = service.find_participant(session, story)
    display_name = (participant or {}).get('displayName') if isinstance(participant, dict) else None
    return f'已从 Console 档案启动 {_character_name(story)} 的共享主剧本，并加入 {display_name or _session_user_id(session)}。'


def _cmd_story_start(context: CommandContext) -> str:
    """interlude.story.start（index.ts 555-556）。"""
    return _start_story_from_console(context)


def _cmd_init(context: CommandContext) -> str:
    """interlude.init [legacyName:text]（index.ts 558-559）。"""
    return _start_story_from_console(context, context.args[0] if context.args else None)


def _cmd_setup(context: CommandContext) -> str:
    """interlude.setup <json:text>（index.ts 561-574）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。请在 Console 的 sharedStory.managerAccounts 中添加此 QQ，或留空允许所有获授权账号。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    try:
        patch = json.loads(context.args[0])
        if not isinstance(patch, dict):
            raise ValueError('设定必须是 JSON 对象。普通测试无需使用此命令。')
        updated = service.update_setting(story, patch)
        return f'已保存 {_character_name(updated)} 的当前故事设定。'
    except Exception as error:  # 上游 catch (error)：JSON 解析错误也走这里
        return f'JSON 格式不正确：{error}'


def _cmd_status(context: CommandContext) -> str:
    """interlude.status（index.ts 576-589）。"""
    service, session = context.service, context.session
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    config = _config(service)
    model = config.get('model') if isinstance(config.get('model'), dict) else {}
    providers = model.get('providers') if isinstance(model.get('providers'), list) else []
    main_label = '未指定（按模型配置回退）'
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        # 上游：provider.useForMain && provider.enabled !== false
        if provider.get('useForMain') and provider.get('enabled') is not False:
            main_label = provider.get('label') or main_label
            break
    runtime = config.get('runtime') if isinstance(config.get('runtime'), dict) else {}
    agency = config.get('agency') if isinstance(config.get('agency'), dict) else {}
    state = _story_state(story)
    agency_window = state.get('agencyWindow') if isinstance(state.get('agencyWindow'), dict) else {}
    participants = service.participants(_story_id(story)) or []
    lines = [
        f'主角：{_character_name(story)}',
        f'关系人数：{len(participants)}',
        f'故事状态：{story.get("status")}',
        f'已写到：{_to_iso_string(story.get("cursorAt"))}',
        f'主模型连接：{main_label}',
        f'允许主动可见消息：{"开启" if runtime.get("allowProactiveMessages") else "关闭"}',
        f'Agency Window：{"关闭" if agency.get("enabled") is False else "开启"}（{agency_window.get("activityLoad") or "尚未建立"}）',
    ]
    return '\n'.join(lines)


def _cmd_pause(context: CommandContext) -> str:
    """interlude.pause（index.ts 591-592）。"""
    return change_status(context.service, context.session, 'paused')


def _cmd_resume(context: CommandContext) -> str:
    """interlude.resume（index.ts 594-595）。"""
    return change_status(context.service, context.session, 'active')


def _cmd_advance(context: CommandContext) -> str:
    """interlude.advance（index.ts 597-605）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    messages = service.advance_story(story)
    delivered = service.deliver_messages(story, messages, session)
    if delivered:
        return '剧本已补写到现在，并已发送其中已经发生的可见角色消息。'
    if messages:
        return '剧本已补写到现在；可见消息投递未完成，请查看日志。'
    return '剧本已补写到现在；这次没有发生可见角色消息。'


def _cmd_timeline_rebase(context: CommandContext) -> str:
    """interlude.timeline.rebase（index.ts 607-615）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    question = '将清空当前场景摘要、连续性快照和工作暂存，并从现在重新建立宿主时间线；历史剧本和长期事实会保留。确认执行吗？(y/n)'
    if not ask_confirmation(session, question, context.confirm):
        return '操作已取消。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    result = service.rebase_timeline(story)
    data = result if isinstance(result, dict) else {}
    scene_note = '，活跃场景摘要已重置' if data.get('sceneReset') else ''
    return f'已在 {format_log_time(data.get("at"), _story_timezone(story))} 重建宿主时间线{scene_note}。'


def _cmd_timeline(context: CommandContext) -> str:
    """interlude.timeline [limit:number]（index.ts 617-627）。"""
    service, session = context.service, context.session
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    limit = _number_arg(context.args[0] if context.args else '', 10)
    participant = service.find_participant(session, story)
    participant_id = participant.get('id') if isinstance(participant, dict) else None
    entries = service.recent_entries(_story_id(story), _js_max(1, _js_min(limit * 3, 90))) or []
    visible = [
        entry for entry in entries
        if isinstance(entry, dict) and (not entry.get('participantId') or entry.get('participantId') == participant_id)
    ]
    tail = _js_max(1, _js_min(limit, 30))
    # JS entries.slice(-NaN) === slice(0)（NaN 会被 ToIntegerOrInfinity 变成 0），取全部。
    selected = visible if _is_nan(tail) else visible[-int(tail):]
    if not selected:
        return '当前故事还没有剧本记录。'
    timezone_name = _story_timezone(story)
    return '\n'.join(
        f'[{format_story_display_time(entry.get("occurredAt"), timezone_name)}] '
        f'{entry.get("actor")}/{entry.get("kind")}: {entry.get("content")}'
        for entry in selected
    )


def _cmd_memory(context: CommandContext) -> str:
    """interlude.memory [limit:number]（index.ts 629-637）。"""
    service, session = context.service, context.session
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    limit = _number_arg(context.args[0] if context.args else '', 10)
    participant = service.find_participant(session, story)
    participant_id = participant.get('id') if isinstance(participant, dict) else None
    memories = service.memories(_story_id(story), _js_max(1, _js_min(limit, 30)), participant_id) or []
    if not memories:
        return '暂时还没有提取出耐久记忆；多进行一些对话并等待后台整理后再看。'
    return '\n'.join(
        f'[{memory.get("category")}/{_to_fixed(memory.get("importance"), 2)}] {memory.get("content")}'
        for memory in memories
    )


def _cmd_context(context: CommandContext) -> str:
    """interlude.context（index.ts 639-658）。"""
    service, session = context.service, context.session
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    participant = service.find_participant(session, story)
    participant_id = participant.get('id') if isinstance(participant, dict) else None
    scene = service.active_scene(_story_id(story))
    arc = service.active_arc(_story_id(story))
    facts = service.facts(_story_id(story), 8, '', participant_id) or []
    state = _story_state(story)
    overlay = state.get('settingOverlay') if isinstance(state.get('settingOverlay'), dict) else None
    agency_window = state.get('agencyWindow')
    scene_data = scene if isinstance(scene, dict) else {}
    arc_data = arc if isinstance(arc, dict) else {}
    participant_data = participant if isinstance(participant, dict) else {}
    participant_state = participant_data.get('state') if isinstance(participant_data.get('state'), dict) else {}
    facts_text = ' | '.join(
        f'[{fact.get("scope")}/{_to_fixed(fact.get("importance"), 2)}] {fact.get("content")}'
        for fact in facts if isinstance(fact, dict)
    ) if facts else '暂无'
    lines = [
        f'场景引子：{scene_data.get("hook") or "尚未整理"}',
        f'场景摘要：{scene_data.get("summary") or "尚未整理"}',
        f'剧情弧线：{arc_data.get("title") or "开场"} — {arc_data.get("summary") or "尚未整理"}',
        f'当前关系：{participant_data.get("displayName") or _session_user_id(session)}（{participant_data.get("relationship") or "未填写"}）',
        f'当前关系状态：{_json_stringify(participant_state)}',
        f'主角个体价值观 / 看待世界的方式：{_story_setting(story).get("perspective") or "未填写"}（当前 overlay：{(overlay or {}).get("perspective") or "未形成"}）',
        f'主角全局变化：{_json_stringify(overlay if overlay is not None else {})}',
        f'主体行动窗口：{_json_stringify(agency_window)}',
        f'长期事实：{facts_text}',
    ]
    return '\n'.join(lines)


def _cmd_compact(context: CommandContext) -> str:
    """interlude.compact（index.ts 660-667）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    compacted = service.compact_story(story)
    return '已完成一次连续性记忆整理。' if compacted else '当前还没有达到需要整理的剧本量。'


def _cmd_script(context: CommandContext) -> str:
    """interlude.script [limit:number]（index.ts 669-677）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    limit = _number_arg(context.args[0] if context.args else '', 20)
    entries = service.recent_entries(_story_id(story), _js_max(1, _js_min(limit, 50))) or []
    if not entries:
        return '当前主剧本还没有原始条目。'
    blocks = []
    for entry in entries:
        participant_note = f'/{entry.get("participantId")}' if isinstance(entry, dict) and entry.get('participantId') else ''
        actor = entry.get('actor') if isinstance(entry, dict) else None
        kind = entry.get('kind') if isinstance(entry, dict) else None
        occurred = entry.get('occurredAt') if isinstance(entry, dict) else None
        content = entry.get('content') if isinstance(entry, dict) else None
        entry_id = entry.get('id') if isinstance(entry, dict) else None
        blocks.append(f'#{entry_id} [{_to_iso_string(occurred)}] {actor}/{kind}{participant_note}\n{content}')
    return '\n\n'.join(blocks)


def _cmd_script_note(context: CommandContext) -> str:
    """interlude.script.note <content:text>（index.ts 679-685）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    content = context.args[0] if context.args else ''
    return '已写入管理员注记，后续压缩会将其纳入连续性。' if service.add_admin_script_note(story, content) else '注记为空，未写入。'


def _cmd_memory_facts(context: CommandContext) -> str:
    """interlude.memory.facts [limit:number]（index.ts 687-695）。

    注意：上游这里直接把 limit 传给 service.adminFacts，没有做 Math.max/min 裁剪。
    """
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    limit = _number_arg(context.args[0] if context.args else '', 20)
    facts = service.admin_facts(_story_id(story), limit) or []
    if not facts:
        return '当前没有有效的长期事实。'
    blocks = []
    for fact in facts:
        data = fact if isinstance(fact, dict) else {}
        blocks.append(
            f'#{data.get("id")} [{data.get("scope")}] 重要度={_to_fixed(data.get("importance"), 2)} '
            f'置信度={_to_fixed(data.get("confidence"), 2)} 未解决={_js_bool(data.get("unresolved"))}\n'
            f'{data.get("content")}'
        )
    return '\n\n'.join(blocks)


def _cmd_memory_add(context: CommandContext) -> str:
    """interlude.memory.add <scope:string> <content:text>（index.ts 697-704）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    scope = context.args[0] if context.args else ''
    if not is_fact_scope(scope):
        return 'scope 必须是 character、world、relationship、event 或 promise。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    content = context.args[1] if len(context.args) > 1 else ''
    return '已添加高置信度长期事实。' if service.add_admin_fact(story, scope, content) else '事实内容为空，未添加。'


def _cmd_memory_forget(context: CommandContext) -> str:
    """interlude.memory.forget <id:number>（index.ts 706-712）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    fact_id = _js_number(context.args[0] if context.args else None)
    number_text = _js_number_text(fact_id)
    if service.forget_admin_fact(_story_id(story), fact_id):
        return f'长期事实 #{number_text} 已标记为失效。'
    return f'未找到有效的长期事实 #{number_text}。'


def _cmd_memory_intents(context: CommandContext) -> str:
    """interlude.memory.intents [limit:number]（index.ts 714-728）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    limit = _number_arg(context.args[0] if context.args else '', 20)
    intents = service.admin_pending_intents(_story_id(story), limit) or []
    if not intents:
        return '当前没有等待中的计划、提醒、承诺或剧情余波。'
    blocks = []
    for intent in intents:
        data = intent if isinstance(intent, dict) else {}
        payload = data.get('payload') if isinstance(data.get('payload'), dict) else {}
        active = data.get('type') == 'active-consequence' and payload.get('lifecycle') == 'active'
        if active:
            timing = f'持续影响至={payload.get("expiresAt") or "未设置"}'
        else:
            timing = f'最早执行={_to_iso_string(data.get("notBefore"))}'
        blocks.append(
            f'#{data.get("id")} [{data.get("type")}] 参与者={data.get("participantId") or "全局"} {timing}\n'
            f'{data.get("summary")}'
        )
    return '\n\n'.join(blocks)


def _cmd_memory_cancel(context: CommandContext) -> str:
    """interlude.memory.cancel <id:number>（index.ts 730-736）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    intent_id = _js_number(context.args[0] if context.args else None)
    number_text = _js_number_text(intent_id)
    if service.cancel_admin_intent(_story_id(story), intent_id):
        return f'意图 #{number_text} 已取消。'
    return f'未找到等待中的意图 #{number_text}。'


def _cmd_memory_patches(context: CommandContext) -> str:
    """interlude.memory.patches [limit:number]（index.ts 738-746）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    limit = _number_arg(context.args[0] if context.args else '', 20)
    patches = service.admin_state_patches(_story_id(story), limit) or []
    if not patches:
        return '当前没有设定演化提案。'
    blocks = []
    for patch in patches:
        data = patch if isinstance(patch, dict) else {}
        blocks.append(
            f'#{data.get("id")} [{data.get("status")}/{data.get("target")}/{data.get("impact")}] '
            f'置信度={_to_fixed(data.get("confidence"), 2)}\n'
            f'提案：{data.get("proposedValue")}\n证据：{data.get("evidence")}'
        )
    return '\n\n'.join(blocks)


def _cmd_memory_reject(context: CommandContext) -> str:
    """interlude.memory.reject <id:number>（index.ts 748-754）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    patch_id = _js_number(context.args[0] if context.args else None)
    number_text = _js_number_text(patch_id)
    if service.reject_admin_state_patch(_story_id(story), patch_id):
        return f'设定演化提案 #{number_text} 已拒绝。'
    return f'未找到待审核的设定演化提案 #{number_text}。'


def _cmd_overlay_clear(context: CommandContext) -> str:
    """interlude.overlay.clear <target:string>（index.ts 756-767）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '无权限：当前账号不是 HDSI 管理员。'
    target = context.args[0] if context.args else ''
    normalized = _text(target).strip().lower()
    if normalized not in ('character', 'perspective', 'relationship', 'world', 'all'):
        return 'target 必须是 character、perspective、relationship、world 或 all。'
    question = f'即将清理 {normalized} overlay；剧本和记忆不会删除。确认执行吗？(y/n)'
    if not ask_confirmation(session, question, context.confirm):
        return '操作已取消。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    result = service.clear_setting_overlay(story, normalized)
    data = result if isinstance(result, dict) else {}
    participant_note = ''
    if normalized in ('relationship', 'all'):
        participant_note = f'，已清理 {data.get("participantCount")} 个参与者关系 overlay'
    return f'已清理 {normalized} overlay{participant_note}；剧本、长期事实和普通记忆均未删除。'


def _cmd_overlay_status(context: CommandContext) -> str:
    """interlude.overlay.status（index.ts 769-784）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '无权限：当前账号不是 HDSI 管理员。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    status = service.admin_overlay_status(_story_id(story))
    data = status if isinstance(status, dict) else {}
    overlay = _json_stringify_or_undefined(data.get('state'))
    return '\n'.join([
        f'当前全局 overlay：{"空" if overlay == "{}" else overlay}',
        f'待积累提案：{len(data.get("proposed") or [])} 条（需要跨多个剧本回合和日期后才会应用）',
        f'已应用/已归档提案：{len(data.get("applied") or [])} 条',
        f'已清理提案：{len(data.get("cleared") or [])} 条',
        f'overlay 压缩快照：{len(data.get("snapshots") or [])} 条',
        f'参与者关系 overlay：{len(data.get("participantOverlays") or [])} 个',
    ])


def _cmd_overlay_compact(context: CommandContext) -> str:
    """interlude.overlay.compact（index.ts 786-793）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '无权限：当前账号不是 HDSI 管理员。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    changed = service.compact_overlay(story)
    return 'overlay 合并和压缩完成。' if changed else '没有需要合并或压缩的 overlay。'


def _cmd_schedule(context: CommandContext) -> str:
    """interlude.schedule（index.ts 795-817）。"""
    service, session = context.service, context.session
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    record = service.admin_schedule_preplan(_story_id(story))
    if not record:
        return 'Schedule Preplan 尚未生成；后台会在空闲整理时建立。'
    config = _config(service)
    window = schedule_preplan_window(
        record, now_utc(), _story_timezone(story), 12,
        resolve_schedule_preplan_config(config.get('schedulePreplan')),
    )
    data = record if isinstance(record, dict) else {}
    lines = [
        f'Schedule Preplan：版本 {data.get("revision")}，覆盖 {data.get("validFrom")} → {data.get("validThrough")}',
        f'最后审查：{data.get("lastReviewedLocalDate") or "尚未"}；原因：{data.get("reviewReason") or "无"}',
    ]
    blocks = (window or {}).get('blocks') if isinstance(window, dict) else None
    if blocks:
        lines.append('\n'.join(
            f'{block.get("date")} {block.get("start")}-{block.get("end")} [{block.get("kind")}] {block.get("label")}'
            f'{(" @ " + str(block.get("location"))) if block.get("location") else ""}'
            for block in blocks
        ))
    else:
        lines.append('未来约半天没有已确定的日程块。')
    return '\n'.join(lines)


def _request_schedule_refresh(context: CommandContext) -> str:
    """上游 requestScheduleRefresh(session)（index.ts 807-813），供 refresh/rebuild 共用。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '无权限：当前账号不是 HDSI 管理员。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    marked = service.request_schedule_preplan_rebuild(_story_id(story))
    if marked:
        return 'Schedule Preplan 已标记为重新审查；会在当前前台回合结束后的空闲队列中处理。'
    return '当前还没有 Schedule Preplan；后台会自动建立。'


def _cmd_schedule_refresh(context: CommandContext) -> str:
    """interlude.schedule.refresh（index.ts 819-820）。"""
    return _request_schedule_refresh(context)


def _cmd_schedule_rebuild(context: CommandContext) -> str:
    """interlude.schedule.rebuild（index.ts 822-823）。"""
    return _request_schedule_refresh(context)


def _cmd_database_clear(context: CommandContext) -> str:
    """interlude.database.clear（index.ts 825-831）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '无权限：当前账号不是 HDSI 管理员。'
    question = '即将清空 HDSI 自有数据库，剧本、记忆和状态记录都会删除。确认执行吗？(y/n)'
    if not ask_confirmation(session, question, context.confirm):
        return '操作已取消。'
    result = service.clear_database()
    data = result if isinstance(result, dict) else {}
    cleared = data.get('logicallyCleared')
    extra = f'，其中 {cleared} 条因 SQLite 锁定改为逻辑清空' if cleared else ''
    return f'HDSI 数据库清空完成：处理 {data.get("removed")} 条记录{extra}。'


def _cmd_purge_all(context: CommandContext) -> str:
    """interlude.purge.all（index.ts 833-841）。"""
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    question = '即将删除所有平台的剧本、记忆、事实、意图和状态。确认执行吗？(y/n)'
    if not ask_confirmation(session, question, context.confirm):
        return '操作已取消。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    service.purge_all_data(_story_id(story))
    return '已彻底重置所有平台：旧剧本、场景摘要、剧情弧线、长期事实、记忆、意图、状态演化和参与者关系状态均已清除；当前故事保留为空白的全局主剧本，Canon 已按当前 Console 配置重建。'


def _cmd_purge_platform(context: CommandContext) -> str:
    """interlude.purge.platform <platform:string>（index.ts 843-854）。

    注意：上游确认文案里用的是未规范化的原始 platform 参数。
    """
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    platform = context.args[0] if context.args else ''
    question = f'即将删除平台 {platform} 的全部剧本和记忆。确认执行吗？(y/n)'
    if not ask_confirmation(session, question, context.confirm):
        return '操作已取消。'
    normalized = _text(platform).strip().lower()
    if not normalized:
        return '请填写平台名，例如 sandbox 或 onebot。'
    count = service.purge_platform_data(normalized)
    if count:
        return f'已清空并归档平台 {normalized} 的 {count} 部剧本；其它平台不受影响。'
    return f'没有找到平台 {normalized} 的 HDSI 剧本。'


def _cmd_purge_range(context: CommandContext) -> str:
    """interlude.purge.range <from:string> <to:string>（index.ts 856-867）。

    上游注释：`text` 是 Koishi 的贪婪参数类型，会吞掉后续的结束时间；这里必须使用普通
    string（本文件 CommandArg 的 kind='string' 即对应 Koishi 的 string）。
    """
    service, session = context.service, context.session
    if not require_manager(service, session):
        return '当前 QQ 没有共享主剧本的管理权限。'
    from_value = parse_time(_text(context.args[0] if context.args else '').strip())
    to_value = parse_time(_text(context.args[1] if len(context.args) > 1 else '').strip())
    if from_value is None or to_value is None or from_value > to_value:
        return '时间范围无效，请使用 ISO-8601，例如 2026-08-01T00:00:00+08:00。'
    question = f'即将删除 {_to_iso_string(from_value)} 至 {_to_iso_string(to_value)} 范围内的剧本和关联记忆。确认执行吗？(y/n)'
    if not ask_confirmation(session, question, context.confirm):
        return '操作已取消。'
    story = require_story(service, session)
    if isinstance(story, str):
        return story
    service.purge_story_range(_story_id(story), from_value, to_value)
    return f'已删除 {_to_iso_string(from_value)} 至 {_to_iso_string(to_value)} 范围内的剧本和关联记忆；Canon 与参与者身份未删除。'


# ==================== 命令清单（顺序 = index.ts registerCommands 的注册顺序） ====================

COMMANDS: Tuple[CommandSpec, ...] = (
    CommandSpec(ROOT_COMMAND, ROOT_DESCRIPTION, (), _cmd_root),
    CommandSpec('interlude.doctor', '检查当前 Console 档案、权限与模型是否适合启动故事',
                (), _cmd_doctor),
    CommandSpec('interlude.story.start', '管理员：从当前 Console 档案手动启动第一份运行中故事',
                (), _cmd_story_start),
    CommandSpec('interlude.init', '兼容别名：请改用 interlude.story.start；名称参数已忽略',
                (_arg('legacyName', 'text', False),), _cmd_init),
    CommandSpec('interlude.setup', '高级：用 JSON 单独修改当前故事设定；普通测试请优先在 Console 填 storyDefaults',
                (_arg('json', 'text'),), _cmd_setup),
    CommandSpec('interlude.status', '查看当前故事、游标、主模型连接与主动消息开关',
                (), _cmd_status),
    CommandSpec('interlude.pause', '暂停当前故事的自动处理，不删除任何记录',
                (), _cmd_pause),
    CommandSpec('interlude.resume', '恢复当前故事的自动处理',
                (), _cmd_resume),
    CommandSpec('interlude.advance', '手动把故事补写到现在；用于测试自动生活推进',
                (), _cmd_advance),
    CommandSpec('interlude.timeline.rebase', '管理员：以当前真实时间重建自动推进时间线，不删除历史剧本',
                (), _cmd_timeline_rebase),
    CommandSpec('interlude.timeline', '查看当前账号可见的近期剧本记录；limit 为条数，默认 10',
                (_arg('limit', 'number', False),), _cmd_timeline),
    CommandSpec('interlude.memory', '查看当前账号相关的记忆摘要；limit 为条数，默认 10',
                (_arg('limit', 'number', False),), _cmd_memory),
    CommandSpec('interlude.context', '查看运行上下文摘要：场景、关系、Overlay 与长期事实',
                (), _cmd_context),
    CommandSpec('interlude.compact', '立即整理当前故事的旧剧本；必要时一并审查 Schedule Preplan',
                (), _cmd_compact),
    CommandSpec('interlude.script', '管理员：查看当前主剧本的最近原始条目，默认 20 条',
                (_arg('limit', 'number', False),), _cmd_script),
    CommandSpec('interlude.script.note', '管理员：向剧本写入一条人工注记，不伪装成模型输出',
                (_arg('content', 'text'),), _cmd_script_note),
    CommandSpec('interlude.memory.facts', '管理员：列出长期事实及其编号，默认 20 条',
                (_arg('limit', 'number', False),), _cmd_memory_facts),
    CommandSpec('interlude.memory.add', '管理员：手动添加长期事实；scope 为 character/world/relationship/event/promise',
                (_arg('scope', 'string'), _arg('content', 'text')), _cmd_memory_add),
    CommandSpec('interlude.memory.forget', '管理员：将指定长期事实标记为已失效，可审计且不会物理删除',
                (_arg('id', 'number'),), _cmd_memory_forget),
    CommandSpec('interlude.memory.intents', '管理员：查看等待中的计划、提醒、承诺与剧情余波',
                (_arg('limit', 'number', False),), _cmd_memory_intents),
    CommandSpec('interlude.memory.cancel', '管理员：取消指定的等待中意图或延迟消息',
                (_arg('id', 'number'),), _cmd_memory_cancel),
    CommandSpec('interlude.memory.patches', '管理员：查看人物、关系和世界设定的演化提案',
                (_arg('limit', 'number', False),), _cmd_memory_patches),
    CommandSpec('interlude.memory.reject', '管理员：拒绝一条尚未应用的设定演化提案',
                (_arg('id', 'number'),), _cmd_memory_reject),
    CommandSpec('interlude.overlay.clear', '管理员：只清理指定部分的设定演化 overlay，不删除剧本和记忆；执行前会询问 y/n',
                (_arg('target', 'string'),), _cmd_overlay_clear),
    CommandSpec('interlude.overlay.status', '管理员：查看当前 overlay、待积累提案和压缩归档状态',
                (), _cmd_overlay_status),
    CommandSpec('interlude.overlay.compact', '管理员：只合并和压缩已应用的 overlay，不整理普通剧本记忆',
                (), _cmd_overlay_compact),
    CommandSpec('interlude.schedule', '查看 Schedule Preplan 当前版本与未来约半天的日程',
                (), _cmd_schedule),
    CommandSpec('interlude.schedule.refresh', '管理员：重新审查当前 Schedule Preplan，并保留旧计划作为稳定参考',
                (), _cmd_schedule_refresh),
    CommandSpec('interlude.schedule.rebuild', '管理员：兼容别名，等同于 interlude.schedule.refresh',
                (), _cmd_schedule_rebuild),
    CommandSpec('interlude.database.clear', '管理员：清空 HDSI 自有 SQLite 数据表；不会删除 Koishi 用户和其它插件数据；执行前会询问 y/n',
                (), _cmd_database_clear),
    CommandSpec('interlude.purge.all', '管理员：彻底重置所有平台的剧本、记忆与 Canon；执行前会询问 y/n',
                (), _cmd_purge_all),
    CommandSpec('interlude.purge.platform', '管理员：删除指定平台的全部剧本和记忆；例如 sandbox 或 onebot；执行前会询问 y/n',
                (_arg('platform', 'string'),), _cmd_purge_platform),
    CommandSpec('interlude.purge.range', '管理员：删除时间范围内的剧本和关联记忆；时间使用 ISO-8601；执行前会询问 y/n',
                (_arg('from', 'string'), _arg('to', 'string')), _cmd_purge_range),
)


# 命令名 → 命令表（上游命令名本身就是小写；解析时统一转小写后匹配）。
COMMANDS_BY_NAME: Dict[str, CommandSpec] = {spec.name: spec for spec in COMMANDS}


def parse_command(content: Any) -> Tuple[Optional[str], List[str]]:
    """把命令文本解析成 `(commandName, args)`（等价 Koishi 的命令解析）。

    - 接受可选前缀 `!` / `/` / `.`（与上游 looksLikeInterludeCommand 的前缀一致）；
    - 命令名统一小写后匹配（middleware 的正则也是 /i）；
    - 参数按命令表切分：`text` 贪婪吃掉剩余原文（setup 的 JSON、memory.add 的 content），
      `string`/`number` 各取一个 token，方括号参数缺省时补空串；
    - 不是 interlude 命令时返回 `(None, [])`。
    """
    text = _text(content).strip()
    if not text:
        return None, []
    match = re.match(r'^[!/.]?(\S+)(?:\s+([\s\S]*))?$', text)
    if not match:
        return None, []
    name = match.group(1).lower()
    if name != ROOT_COMMAND and not name.startswith(ROOT_COMMAND + '.'):
        return None, []
    rest = match.group(2) or ''
    spec = COMMANDS_BY_NAME.get(name)
    if spec is None:
        # 未知的 interlude.* 子命令：只做分词，交给调用方决定（上游是 Koishi 的未知命令提示）。
        return name, [token[0] for token in _token_spans(rest)]
    return name, _bind_arguments(spec, rest)


def execute_command(service: Any, session: Any, content: Any,
                    confirm: Optional[Callable[[str], Any]] = None) -> Optional[str]:
    """执行一条管理命令，返回要发给用户的文本（对应上游 `ctx.command(...).action`）。

    返回 None 表示 `content` 不是 HDSI 管理命令（或未知的 interlude.* 子命令），
    调用方应当把它当普通消息处理。缺必填参数时返回命令用法行（上游 Koishi 在缺参时
    也不会执行 action）。
    """
    name, args = parse_command(content)
    if name is None:
        return None
    if name in UNSUPPORTED_COMMANDS:
        return UNSUPPORTED_MESSAGE
    spec = COMMANDS_BY_NAME.get(name)
    if spec is None:
        return None
    for index, argument in enumerate(spec.args):
        if argument.required and not (args[index] if index < len(args) else ''):
            return _usage(spec)
    return spec.handler(CommandContext(service=service, session=session, args=list(args), confirm=confirm))
