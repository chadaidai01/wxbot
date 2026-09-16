# -*- coding: utf-8 -*-
"""
剧场系统（Theater / 幕间），参考 HDS-Interlude 的叙事驱动对话思路实现。

核心概念：
- 每个用户/群聊单独维护一部持续上演的"生活剧本"（一个剧场）。
- 每次消息 = 一次"主叙事写作"：先补写幕间生活 → 处理当前事件 → 决定看见/沉默/立即/延迟
  → 更新场景、引子、长期事实、关系、Overlay、个体视角、剧情余波、意图、情绪偏移。
- 后台自动推进：没有消息时角色也在"幕间生活"，主动联系必须有真实生活依据。

"像人"的关键机制：
- 主角中心：聊天只是生活里的一个事件；先写生活再让事件进入。
- 权威时钟 + 时段/日照，角色按真实时间过日子。
- 看见(seen=false/true) / 沉默 / 立即 / 延迟，三态分离。
- 被打断的草稿：上次"还在打字没发完"的片段作为"未说完的念头"重新决定。
- 记忆分层 + 渐进演化：单一事件不改变人格，Overlay/Perspective 需反复证据。
- Alter 心情惯性：氛围净变化累计，超过动态阈值后由侧端模型生成情绪偏移描述并注入。
- 主动联系要有生活依据：先写生活→真实理由→(立即/稍后重查/自然放下)，带 willingness+reason，
  并受最小间隔限流；"用户很久没说话"本身不能成为理由。
- 现实约束：禁止虚构收到的消息/通知/引用；禁止 [表情]/[图片] 占位文本。

所有状态持久化为 JSON（Theater/<聊天名>.json），下次对话无缝续演。
"""

import os
import re
import json
import time
import random
import threading
import logging

logger = logging.getLogger('theater')

MODE_REPLY = 'reply'
MODE_SILENT = 'silent'
MODE_DELAYED = 'delayed'

INTENT_DELAYED_REPLY = 'delayed_reply'
INTENT_REMINDER = 'reminder'
INTENT_FOLLOWUP = 'followup'
INTENT_PROACTIVE = 'proactive'

# Alter System 参数（参考 HDS-Interlude）
ALTER_BASE_THRESHOLD = 10.0
ALTER_DENSITY_FACTOR = 0.3
ALTER_SAME_BOOST = 0.05
ALTER_OPPOSITE_DECAY = 0.15
ALTER_MIN_WEIGHT = 0.2
ALTER_MAX_INTENSITY = 2.0
ALTER_RETRY_SECONDS = 300


def _sanitize(name):
    return re.sub(r'[\\/:*?"<>|\s]', '_', str(name))


class Theater:
    """一个聊天/群独立的生活剧本（剧场）。"""

    def __init__(self, theater_id, data_dir='Theater', character_canon='',
                 llm_call=None, script_budget=40, proactive_threshold=60,
                 respect_quiet=True, recent_context=12,
                 alter_enabled=True, alter_base_threshold=ALTER_BASE_THRESHOLD,
                 min_proactive_interval_seconds=1800,
                 continuity_refresh_turns=5, active_consequence_decay=0.1,
                 image_recognizer=None, now=None):
        self.theater_id = str(theater_id)
        self.data_dir = data_dir
        self.character_canon = character_canon or ''
        self.llm_call = llm_call                # callable(prompt:str) -> str
        self.script_budget = script_budget
        self.proactive_threshold = proactive_threshold
        self.respect_quiet = respect_quiet
        self.recent_context = recent_context
        self.alter_enabled = alter_enabled
        self.alter_base_threshold = float(alter_base_threshold or ALTER_BASE_THRESHOLD)
        self.min_proactive_interval_seconds = min_proactive_interval_seconds
        self.continuity_refresh_turns = continuity_refresh_turns
        self.active_consequence_decay = active_consequence_decay
        self.image_recognizer = image_recognizer   # callable(image_path)->描述文字，用于识图/表情
        self.is_group = False                      # 是否为群聊（由 bot 注入）
        self.group_reply_probability = 1.0         # 群聊发送闸门（bot 注入，默认全放行）
        self._now = now or (lambda: time.time())
        self._alter_lock = threading.Lock()
        self._op_lock = threading.Lock()   # 串行化本剧场的读-写-存，并避免与网页编辑竞态
        self.path = os.path.join(data_dir, f'{_sanitize(self.theater_id)}.json')
        self.state = self._load()

    # ---- 持久化 ----

    def _new_state(self):
        return {
            'theater_id': self.theater_id,
            'canon': {
                'character': self.character_canon,
                'world': '',
                'scene': '',
                'script_brief': '',
            },
            'participants': {},      # user -> {profile, relation, relation_evolution, arc, last_seen_ts}
            'script': [],            # [{time, type:user|char|advance|scene, who, content, visible}]
            'threads': [],           # 剧情引子/近期事实
            'long_term': [],         # 长期事实
            'overlays': [],          # Overlay：性格/世界/关系随剧情渐进形成的非破坏性变化
            'perspective': '',       # 角色个体视角：独立于 Canon 的个体价值观
            'consequences': [],      # activeConsequence：短期剧情余波，自然消退
            'continuity': {},        # 低频状态摘要 {current, next, recent, salient}
            'intents': [],           # {type, target_ts, target_user, content, done}
            'mood': {'alter': 0, 'cumulative': 0, 'description': '', 'weight': 0,
                     'direction': None, 'recent': [], 'last_analysis_ts': 0},
            'last_proactive': {},    # target -> 最近一次主动发送时间戳（限流用）
            'turns_since_refresh': 0,
            'last_advance': 0.0,
            'updated_at': 0.0,
        }

    def _load(self):
        base = self._new_state()
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for k, v in data.items():
                    base[k] = v
        except Exception as e:
            logger.error(f'加载剧场 {self.theater_id} 失败: {e}，将重建。')
        # 归一化，兼容旧版本文件
        m = base['mood']
        m.setdefault('cumulative', 0)
        m.setdefault('weight', 0)
        m.setdefault('direction', None)
        m.setdefault('recent', [])
        m.setdefault('last_analysis_ts', 0)
        base.setdefault('consequences', [])
        base.setdefault('continuity', {})
        base.setdefault('last_proactive', {})
        base.setdefault('turns_since_refresh', 0)
        return base

    def save(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            self.state['updated_at'] = self._now()
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f'保存剧场 {self.theater_id} 失败: {e}')

    # ---- 参与者 ----

    def ensure_participant(self, user):
        return self.state['participants'].setdefault(user, {
            'profile': '', 'relation': '', 'relation_evolution': '', 'arc': '', 'last_seen_ts': 0.0,
        })

    # ---- 上下文构建 ----

    def _fmt_script(self):
        lines = []
        for it in self.state['script'][-self.script_budget:]:
            t = it.get('type')
            who = it.get('who', '')
            content = it.get('content', '')
            ts = it.get('time', 0)
            local = time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else '?'
            if t == 'user':
                lines.append(f"[{local}] {who}：{content}")
            elif t == 'char':
                lines.append(f"[{local}] 你：{content}")
            elif t == 'advance':
                lines.append(f"[{local}] (幕间) {content}")
            elif t == 'scene':
                lines.append(f"[{local}] (场景) {content}")
        return '\n'.join(lines) if lines else '（还没有发生过什么）'

    def _fmt_recent(self):
        lines = []
        for it in self.state['script'][-self.recent_context:]:
            t = it.get('type')
            if t not in ('user', 'char'):
                continue
            who = it.get('who', '')
            content = it.get('content', '')
            lines.append(f"{who}：{content}" if t == 'user' else f"你：{content}")
        return '\n'.join(lines) if lines else ''

    def _fmt_participants(self):
        parts = []
        for user, p in self.state['participants'].items():
            rel = p.get('relation') or p.get('relation_evolution') or ''
            arc = p.get('arc') or ''
            parts.append(f"- {user}（身份：{p.get('profile') or '未知'}；关系：{rel}；剧情线：{arc}）")
        return '\n'.join(parts) if parts else '（暂无参与者）'

    def _fmt_threads(self):
        return '\n'.join('- ' + t for t in self.state['threads']) if self.state['threads'] else '（无）'

    def _fmt_long_term(self):
        return '\n'.join('- ' + t for t in self.state['long_term']) if self.state['long_term'] else '（无）'

    def _fmt_overlays(self):
        lines = []
        for o in self.state['overlays'][-10:]:
            layer = o.get('layer', '')
            target = o.get('target', '')
            desc = o.get('description', '')
            lines.append(f"- [{layer}]{'(' + target + ')' if target else ''} {desc}")
        return '\n'.join(lines) if lines else '（无）'

    def _fmt_consequences(self):
        lines = []
        now = self._now()
        for c in self.state['consequences']:
            left = int(c.get('expires_ts', 0) - now) if c.get('expires_ts') else 0
            lines.append(f"- {c.get('effect', '')}（强度 {c.get('strength', 0):.1f}，剩余约{max(left, 0)}s）")
        return '\n'.join(lines) if lines else '（无）'

    def _fmt_continuity(self):
        c = self.state['continuity']
        if not c:
            return '（无）'
        out = []
        if c.get('current'):
            out.append(f"当前状态：{c['current']}")
        if c.get('next'):
            out.append('下一步：' + '；'.join(str(x) for x in c['next'][:4]))
        if c.get('recent'):
            out.append('近期事实：' + '；'.join(str(x) for x in c['recent'][:5]))
        if c.get('salient'):
            out.append('重要事项：' + '；'.join(str(x) for x in c['salient'][:5]))
        return '\n'.join(out) if out else '（无）'

    def _fmt_intents(self):
        out = []
        now = self._now()
        for it in self.state['intents']:
            if it.get('done'):
                continue
            left = int(it.get('target_ts', 0) - now)
            out.append(f"- {it.get('type')} 目标:{it.get('target_user') or '自己'} 剩余约{left}s 内容:{it.get('content')}")
        return '\n'.join(out) if out else ''

    def _fmt_mood_offset(self):
        m = self.state['mood']
        if m.get('description') and m.get('weight', 0) > 0:
            direction = m.get('direction') or ('serious' if m.get('cumulative', 0) >= 0 else 'relaxed')
            return (f"（情绪偏移：方向={direction}，描述：{m['description']}，"
                    f"强度={min(abs(m.get('cumulative', 0)) / max(self._alter_threshold(), 1e-9), ALTER_MAX_INTENSITY):.2f}，"
                    f"权重={m.get('weight', 0):.2f}——这只是临时氛围惯性，不是固定人设）")
        return ''

    def _fmt_drafts(self, drafts):
        if not drafts:
            return ''
        return '\n'.join(f"- 「{d}」" for d in drafts)

    def _alter_threshold(self):
        # 动态阈值：density=最近一小时有效回合数/10，阈值由 base 平滑下降（最大降到一半）
        now = self._now()
        recent = [s for s in self.state['mood'].get('recent', []) if now - s < 3600]
        density = min(len(recent) / 10.0, 1.0)
        base = self.alter_base_threshold
        return max(base * 0.5, base * (1.0 - density * ALTER_DENSITY_FACTOR))

    def _build_prompt(self, current_event=None, advance_only=False, drafts=None,
                      refresh_continuity=False, force_reply=False):
        c = self.state['canon']
        now_local = time.strftime('%Y-%m-%d %H:%M:%S %A', time.localtime(self._now()))
        hour = time.localtime(self._now()).tm_hour
        if 5 <= hour < 11:
            period = '早晨/上午'
        elif 11 <= hour < 14:
            period = '中午'
        elif 14 <= hour < 18:
            period = '下午'
        elif 18 <= hour < 23:
            period = '傍晚/晚上'
        else:
            period = '深夜'
        now_local_context = f"现在是{period}（本地时间 {now_local}）"

        p = f"""【权威现实时间】现在是 {now_local}（{period}）。这是绝对基准。
【时间铁律】剧本必须严格以现在的时间为准推进：
- 现在几点、星期几、什么时段（早晨/上午/中午/下午/傍晚/晚上/深夜），就以它为准。
- 最近剧本/记忆/旧场景里写的旧时间（如"深夜/半夜/天黑/凌晨"）已经过去，必须推进到当前时段；
  例如现在是白天/下午，就绝不能在剧本里写"深夜/半夜/天黑了"。
- elapsed_script 从上次游标补写到现在，结尾一定落在当前时刻。

你是一个"剧场"里的演员兼导演，正在维护一部持续上演的生活剧本《{self.theater_id}》。你的中心永远是"你"这个角色自己正在过的生活；聊天只是生活里可能出现的一个事件。

【角色设定（Canon）】
{c.get('character') or '（未设定，请按剧情需要自然塑造）'}

【世界观 / 舞台】
{c.get('world') or '（尚未展开）'}

【当前舞台场景】
{c.get('scene') or '（尚未确立）'}

【剧情梗概 / 初始信息】
{c.get('script_brief') or '（尚未展开）'}

【当前状态摘要（低频，仅作衔接参考，可能过时）】
{self._fmt_continuity()}

【参与者】
{self._fmt_participants()}

【近期剧本（时间线，最新的在后）】
{self._fmt_script()}

【最近聊天记录（最近 {self.recent_context} 条，回复请据此自然接话）】
{self._fmt_recent() or '（无）'}

【剧情引子 / 近期事实】
{self._fmt_threads()}

【长期事实】
{self._fmt_long_term()}

【剧情余波（active consequence，短期影响、会自然消退）】
{self._fmt_consequences()}

【角色个体视角（看待世界/他人/自己的方式，独立于设定）】
{self.state['perspective'] or '（尚未形成明确个体视角）'}

【Overlay 渐进变化（长期剧情中形成的性格/世界/关系微调，非破坏性）】
{self._fmt_overlays()}

【情绪氛围】{self._fmt_mood_offset()}

【待办意图（到期需兑现，不能默默消失）】
{self._fmt_intents() or '（无）'}

【时间】{now_local_context}；{period}是权威判断依据——旧剧本写的时段已过，就把生活推进到现在的时段。
"""
        if drafts:
            p += f"""
【被打断的草稿（角色上次还在打字、没发出去的"未说完的念头"，用户新消息打断了它）】
{self._fmt_drafts(drafts)}
——这些不是已发送的对话，只是被打断的念头。不要把它当作对方收到的话，也不要自动发送；让它自然地影响这次的新剧本，然后重新做一个全新的回复决定。
"""
        if not advance_only:
            p += f"""
【当前事件】{current_event}
"""
            if force_reply:
                p += "【强制回复】对方 @ 了你：本回合必须给出可见回复（decision.mode=reply 且 content 非空），禁止 silent / delayed / seen=false。\n"
            p += f"""
请以演员身份完成一次"幕间写作"，只输出一个 JSON 对象（不要任何多余文字、不要 markdown 代码块）：
{{
  "elapsed_script": "从上次游标到现在，角色在幕间经历的事（一段叙述，时间很短可为空字符串）",
  "decision": {{
    "seen": true,
    "mode": "reply | silent | delayed",
    "content": "要回复的话（可用 <sep/> 分隔多个气泡；mode 不是 reply 时可为空）",
    "delay_minutes": 0
  }},
  "scene_update": "当前舞台场景一句话更新（可为空字符串）",
  "threads": ["保留或新增的剧情引子，最多5条"],
  "long_term": ["新增的长期事实，无则空数组"],
  "perspective_update": "角色个体视角/价值观的一句话更新（无则空字符串）",
  "overlay_candidates": [{{"layer": "character|world|relationship", "target": "目标（人/物/关系）", "description": "长期剧情中渐进形成的非破坏性变化描述"}}],
  "active_consequences": [{{"effect": "持续影响角色的一件事", "strength": 0.0, "minutes": 0}}],
  "participant_update": {{"<参与者名>": {{"relation_evolution": "关系变化", "arc": "剧情线/待办"}}}},
  "intents": [{{"type": "reminder|delayed_reply|followup", "target_user": "<参与者名或空>", "when": "10分钟后", "content": "..."}}],
  "alter": 0,
  "proactive_candidate": {{"willingness": 0, "content": "", "target_user": "", "reason": "", "origin": "life-event|promise|practical-update|relationship-follow-up", "outcome": "send-now|recheck-later|let-go", "notBefore": "0分钟后"}}
}}
规则：
- 完全代入角色，像真实的人一样生活、思考、说话，禁止出现"作为AI/我是机器人"等字眼。
- 【现实约束】禁止虚构角色"收到的消息/电话/通知/对方回复/引用"——剧本只能写观察到的现实；禁止用 [表情]、[图片]、引用：原句 这类占位文本假装平台功能。
- 【人格稳定】单次的情绪、一次回复、一个异常事件不改变 Canon/Overlay/Perspective；只有跨回合反复出现的行为才写成 overlay_candidates/perspective_update。
- 【时间】剧本必须严格以"权威现实时间"（当前几点/时段）为准，把生活推进到当前时段；当前是白天就绝不能写深夜/半夜/天黑。
- 【三态】seen=false 表示没看到；seen=true 且 mode=silent 表示看到了但不回（沉默本身也是剧情）；mode=reply 立即回；mode=delayed 晚点回并写进 intents。
- 【alter】为-5..+5，只衡量本轮新增的氛围净变化：正=更严肃/紧张/正式，负=更轻松/随意；不要把已有氛围当作打分依据。
- 【主动联系】只有当角色有具体、真实的生活理由（origin 之一）才填 proactive_candidate；"对方很久没说话"本身不是理由。outcome=send-now 现在发，recheck-later 稍后再查（写 notBefore 分钟数），let-go 不发。willingness 0-100。
- 【承诺】若回复里说"想想再回/晚点回"，必须写进 intents 并在之后兑现，不能默默消失。
- 每次写作都要让"故事"更丰满，让下一次对话能自然延续。
"""
        else:
            p += f"""
现在是"幕间"自动推进（没有用户消息）。请补写角色在幕间的生活，只输出一个 JSON 对象：
{{
  "elapsed_script": "角色在幕间经历的事（一段叙述，可长可短）",
  "scene_update": "当前舞台场景一句话更新（可为空）",
  "threads": ["保留或新增的剧情引子，最多5条"],
  "long_term": ["新增的长期事实，无则空数组"],
  "perspective_update": "角色个体视角/价值观的一句话更新（无则空字符串）",
  "overlay_candidates": [{{"layer": "character|world|relationship", "target": "目标（人/物/关系）", "description": "长期剧情中渐进形成的非破坏性变化描述"}}],
  "active_consequences": [{{"effect": "持续影响角色的一件事", "strength": 0.0, "minutes": 0}}],
  "intents": [{{"type": "reminder|delayed_reply|followup|proactive", "target_user": "<参与者名或空>", "when": "20分钟后", "content": "..."}}],
  "alter": 0,
  "proactive_candidate": {{"willingness": 0, "content": "", "target_user": "", "reason": "", "origin": "life-event|promise|practical-update|relationship-follow-up", "outcome": "send-now|recheck-later|let-go", "notBefore": "0分钟后"}}
}}
规则：
- 角色在无人打扰时也有自己的生活（作息、想法、办事、思念某位参与者等），这些都会沉淀进剧本。
- 若角色有具体生活动机想主动找某位参与者，填 proactive_candidate（必须有 reason 和 origin，outcome 三选一，willingness 0-100）；"对方很久没说话"不是理由。
- overlay_candidates 只记录"长期稳定"的变化，一次 0-2 条；perspective_update 一句话即可。
- 【现实约束】禁止虚构收到的消息/电话/通知；禁止 [表情]/[图片] 占位文本。
- 只输出 JSON 对象，不要多余文字。
"""
        if refresh_continuity:
            p += '\n【本回合请额外输出 "continuity": {"current":"当前状态一句话","next":["未来计划1-3条"],"recent":["近期事实1-4条"],"salient":["长期重要事项1-3条"]}】\n'
        return p

    # ---- LLM 调用与解析 ----

    def _llm_json(self, prompt):
        if not self.llm_call:
            raise RuntimeError('剧场未注入 llm_call')
        text = self.llm_call(prompt) or ''
        data = self._extract_json(text)
        if data is None:
            raise ValueError('模型未返回有效 JSON')
        return data

    @staticmethod
    def _extract_json(text):
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        start = text.find('{')
        end = text.rfind('}')
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        return None
        return None

    @staticmethod
    def _parse_minutes(when):
        if not when:
            return None
        m = re.search(r'(\d+)', str(when))
        return int(m.group(1)) if m else None

    @staticmethod
    def _norm(content):
        return str(content or '').replace('<sep/>', '$')

    def _group_gate_ok(self, force=False):
        """剧场群聊的发送闸门：返回是否放行（非群聊或 force 时直接放行）。
        用于延迟回复/主动联系/推进投递，让群聊所有发送统一受 1% 限制。"""
        if force or not self.is_group:
            return True
        return random.random() <= max(0.0, min(1.0, self.group_reply_probability))

    # ---- 应用一次写作结果 ----

    def _apply_turn(self, data, user=None, current_message=None, now=None,
                    delivered=None, is_advance=False, reply_probability=1.0, force_reply=False):
        now = now if now is not None else self._now()
        delivered = delivered if delivered is not None else []
        st = self.state
        c = st['canon']

        elapsed = (data.get('elapsed_script') or '').strip()
        if elapsed:
            st['script'].append({'time': now, 'type': 'advance', 'who': user or '', 'content': elapsed, 'visible': False})

        if current_message is not None:
            st['script'].append({'time': now, 'type': 'user', 'who': user or '?', 'content': current_message, 'visible': True})

        decision = data.get('decision') or {}
        mode = decision.get('mode', 'reply')
        content = (decision.get('content') or '').strip()
        delay_minutes = int(decision.get('delay_minutes') or 0)

        if mode == MODE_REPLY and content:
            if reply_probability < 1.0 and random.random() > reply_probability:
                # 概率没中：角色最终没把话发出去，保持沉默（故事里体现"想说没说"）
                st['script'].append({'time': now, 'type': 'advance', 'who': user or '',
                                     'content': f'你本想回复「{content[:40]}…」，但最后没有发出去，保持了沉默。', 'visible': False})
            else:
                st['script'].append({'time': now, 'type': 'char', 'who': user or '', 'content': content, 'visible': True})
                delivered.append({'target_user': user, 'content': self._norm(content)})
        elif mode == MODE_DELAYED:
            if force_reply:
                # 被 @ 了：必须现在回，不能延迟 → 转成即时回复
                st['script'].append({'time': now, 'type': 'char', 'who': user or '', 'content': content, 'visible': True})
                delivered.append({'target_user': user, 'content': self._norm(content)})
            elif not self._group_gate_ok(force=(reply_probability >= 1.0)):
                # 群聊闸门没放行：晚点回也不发，记一句"想说没说"
                st['script'].append({'time': now, 'type': 'advance', 'who': user or '',
                                     'content': '你本想晚点再回，但最终没说出口。', 'visible': False})
            else:
                ts = now + max(delay_minutes, 1) * 60
                st['intents'].append({'type': INTENT_DELAYED_REPLY, 'target_ts': ts, 'target_user': user,
                                      'content': self._norm(content), 'done': False})

        scene = (data.get('scene_update') or '').strip()
        if scene:
            c['scene'] = scene
            st['script'].append({'time': now, 'type': 'scene', 'who': '', 'content': scene, 'visible': False})

        threads = data.get('threads') or []
        if isinstance(threads, list):
            st['threads'] = [str(t).strip() for t in threads if str(t).strip()][:5]

        lt = data.get('long_term') or []
        if isinstance(lt, list):
            for item in lt:
                s = str(item).strip()
                if s and s not in st['long_term']:
                    st['long_term'].append(s)
            st['long_term'] = st['long_term'][-50:]

        pu = data.get('perspective_update')
        if isinstance(pu, str) and pu.strip():
            st['perspective'] = pu.strip()

        ov = data.get('overlay_candidates') or []
        if isinstance(ov, list):
            for item in ov:
                if not isinstance(item, dict):
                    continue
                layer = item.get('layer')
                if layer not in ('character', 'world', 'relationship'):
                    continue
                desc = str(item.get('description') or '').strip()
                if not desc:
                    continue
                st['overlays'].append({'layer': layer, 'target': str(item.get('target') or '').strip(),
                                       'description': desc, 'ts': now})
            st['overlays'] = st['overlays'][-30:]

        # activeConsequence：新增 + 自然消退
        ac = data.get('active_consequences') or []
        if isinstance(ac, list):
            for item in ac:
                if not isinstance(item, dict):
                    continue
                eff = str(item.get('effect') or '').strip()
                if not eff:
                    continue
                try:
                    strength = min(max(float(item.get('strength') or 0.5), 0.0), 1.0)
                except Exception:
                    strength = 0.5
                minutes = self._parse_minutes(item.get('minutes')) or 60
                st['consequences'].append({'effect': eff, 'strength': strength,
                                           'ts': now, 'expires_ts': now + minutes * 60})
        for cq in list(st['consequences']):
            cq['strength'] = max(0.0, cq.get('strength', 0) - self.active_consequence_decay)
            if cq['strength'] <= 0 or cq.get('expires_ts', 0) <= now:
                st['consequences'].remove(cq)
        st['consequences'] = st['consequences'][-15:]

        # continuity 刷新
        if 'continuity' in data and isinstance(data['continuity'], dict):
            st['continuity'] = data['continuity']
            st['turns_since_refresh'] = 0
        else:
            st['turns_since_refresh'] = st.get('turns_since_refresh', 0) + 1

        pupdate = data.get('participant_update') or {}
        if isinstance(pupdate, dict):
            for name, upd in pupdate.items():
                if isinstance(upd, dict):
                    p = st['participants'].setdefault(name, {'profile': '', 'relation': '',
                                                             'relation_evolution': '', 'arc': '', 'last_seen_ts': 0.0})
                    if upd.get('relation_evolution'):
                        p['relation_evolution'] = str(upd['relation_evolution'])
                    if upd.get('arc'):
                        p['arc'] = str(upd['arc'])

        intents = data.get('intents') or []
        if isinstance(intents, list):
            for it in intents:
                if not isinstance(it, dict):
                    continue
                itype = it.get('type')
                if itype not in (INTENT_DELAYED_REPLY, INTENT_REMINDER, INTENT_FOLLOWUP, INTENT_PROACTIVE):
                    continue
                minutes = self._parse_minutes(it.get('when')) or delay_minutes or 30
                st['intents'].append({'type': itype, 'target_ts': now + minutes * 60,
                                      'target_user': (it.get('target_user') or user or '').strip(),
                                      'content': str(it.get('content') or '').strip(), 'done': False})

        # Alter：累计 + 权重生命周期 + 触发侧端分析
        if self.alter_enabled:
            self._accumulate_alter(data, now)

        # 主动联系：生活依据 + 限流 + 立即/稍后/放下
        self._handle_proactive(data, user, now, delivered, is_advance)

        # 裁剪剧本
        if len(st['script']) > self.script_budget + 20:
            st['script'] = st['script'][-self.script_budget:]

        self.save()
        return delivered

    def _accumulate_alter(self, data, now):
        alter = data.get('alter')
        if not isinstance(alter, (int, float)):
            return
        alter = int(max(-5, min(5, alter)))
        st = self.state
        m = st['mood']
        m['alter'] = alter
        m['recent'] = (m.get('recent', []) + [now])[-50:]
        m['cumulative'] = m.get('cumulative', 0) + alter
        sign = 1 if m['cumulative'] >= 0 else -1

        if m.get('description'):
            if m.get('direction') == sign:
                m['weight'] = min(1.0, m.get('weight', 0) + abs(alter) * ALTER_SAME_BOOST)
            else:
                m['weight'] = max(0.0, m.get('weight', 0) - abs(alter) * ALTER_OPPOSITE_DECAY)
            if m['weight'] < ALTER_MIN_WEIGHT:
                m['description'] = ''
                m['weight'] = 0.0
                m['direction'] = None

        # 达到动态阈值且尚未在分析中 → 后台侧端分析
        threshold = self._alter_threshold()
        if abs(m['cumulative']) >= threshold and (now - m.get('last_analysis_ts', 0)) > ALTER_RETRY_SECONDS:
            m['last_analysis_ts'] = now
            direction = '严肃/紧张/正式' if sign > 0 else '轻松/随意/活跃'
            thread = threading.Thread(target=self._run_alter_analysis,
                                      args=(direction, now), daemon=True)
            thread.start()

    def _run_alter_analysis(self, direction, now):
        """侧端模型生成 1-2 句情绪偏移描述（不阻塞本轮可见回复）。"""
        try:
            recent = self.state['script'][-10:]
            recent_text = '\n'.join(f"- {it.get('content','')}" for it in recent if it.get('content'))
            prompt = (f"你是这部生活剧本的氛围分析师。最近剧情的情绪评分轨迹显示整体氛围在向「{direction}」偏移。"
                      f"请用 1-2 句中文描述这种偏移的具体感受（不出现人名、不要引用原话），只输出 JSON："
                      f"{{\"description\":\"...\"}}。\n最近剧情：\n{recent_text[:800]}")
            text = self.llm_call(prompt) or ''
            data = self._extract_json(text)
            desc = (data or {}).get('description') or ''
            if not desc:
                return
            with self._alter_lock:
                m = self.state['mood']
                m['description'] = str(desc).strip()
                m['weight'] = 1.0
                m['direction'] = 1 if direction.startswith('严肃') else -1
                m['cumulative'] = 0
                self.save()
            logger.info(f'剧场《{self.theater_id}》Alter 情绪偏移已更新。')
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》Alter 分析失败: {e}')

    def _handle_proactive(self, data, user, now, delivered, is_advance):
        pro = data.get('proactive_candidate') or {}
        if not isinstance(pro, dict) or not pro.get('willingness'):
            return
        try:
            w = int(pro['willingness'])
        except Exception:
            w = 0
        content_p = str(pro.get('content') or '').strip()
        target_p = str(pro.get('target_user') or '').strip()
        reason = str(pro.get('reason') or '').strip()
        outcome = str(pro.get('outcome') or 'send-now').strip()
        if not content_p or not target_p:
            return
        # 生活依据：必须有 reason（origin 隐含在 reason 里）
        if not reason:
            logger.info(f'剧场《{self.theater_id}》主动联系 {target_p} 缺少生活理由(reason)，跳过。')
            return
        st = self.state
        last_ts = st['last_proactive'].get(target_p, 0)
        # 主动联系不受群聊 1% 闸门限制，只受意愿阈值与最小间隔约束
        if outcome == 'send-now':
            if w < self.proactive_threshold:
                return
            if now - last_ts < self.min_proactive_interval_seconds:
                # 限流 → 转为稍后重查
                minutes = self._parse_minutes(pro.get('notBefore')) or 30
                st['intents'].append({'type': INTENT_PROACTIVE, 'target_ts': now + minutes * 60,
                                      'target_user': target_p, 'content': content_p, 'done': False})
                return
            st['last_proactive'][target_p] = now
            delivered.append({'target_user': target_p, 'content': self._norm(content_p)})
        elif outcome == 'recheck-later':
            minutes = self._parse_minutes(pro.get('notBefore')) or 30
            st['intents'].append({'type': INTENT_PROACTIVE, 'target_ts': now + minutes * 60,
                                  'target_user': target_p, 'content': content_p, 'done': False})
        # let-go：不发

    # ---- 主入口 ----

    def handle_user_message(self, user, message, image_paths=None, drafts=None, reply_probability=1.0, force_reply=False):
        """处理一条用户消息，返回要发送的文本；返回 None 表示沉默/延迟。
        reply_probability(0~1)：控制"模型决定回复"后实际发送的概率（用于群聊降噪）。
        force_reply：被 @ 了，必须给出可见回复，禁止沉默/延迟。"""
        with self._op_lock:
            # 先重新读取磁盘上的剧场状态，尊重网页端「剧场记忆」编辑，避免被内存旧状态覆盖
            self.state = self._load()
            return self._do_user_message(user, message, image_paths, drafts, reply_probability, force_reply)

    def _do_user_message(self, user, message, image_paths=None, drafts=None, reply_probability=1.0, force_reply=False):
        now = self._now()
        p = self.ensure_participant(user)
        p['last_seen_ts'] = now

        # 把本次消息附带的图片/表情先识别成文字描述，让 AI 有据可依（避免瞎编）
        img_descs = []
        if image_paths and self.image_recognizer:
            for _p in image_paths:
                if not _p or not os.path.exists(str(_p)):
                    continue
                try:
                    d = (self.image_recognizer(_p) or '').strip()
                    if d:
                        img_descs.append(d)
                except Exception as e:
                    logger.warning(f'剧场识别图片失败 {_p}: {e}')
        if img_descs:
            message = f"{message}（图片内容：{'；'.join(img_descs)}）"
        img_note = ''
        if image_paths:
            img_note = f"（附带的图片内容：{'；'.join(img_descs) if img_descs else f'{len(image_paths)} 张图片，内容未识别'}）"
        current_event = f"[{time.strftime('%m-%d %H:%M', time.localtime(now))}] {user} 发来消息：{message}{img_note}"

        if not self.state['canon']['world']:
            self._init_canon()

        refresh = self.state.get('turns_since_refresh', 0) >= self.continuity_refresh_turns
        prompt = self._build_prompt(current_event=current_event, drafts=drafts,
                                    refresh_continuity=refresh, force_reply=force_reply)
        try:
            data = self._llm_json(prompt)
        except Exception as e:
            logger.error(f'剧场 {self.theater_id} 叙事写作失败: {e}')
            # 兜底：模型空内容/格式失败时，改用一个简短的"自然回复"请求，避免干沉默
            fallback = self._fallback_reply(user, message, now)
            return fallback

        delivered = self._apply_turn(data, user=user, current_message=message, now=now,
                                     reply_probability=reply_probability, force_reply=force_reply)
        for d in delivered:
            if d.get('target_user') == user:
                return d['content']
        # 被 @ 了但模型没有给出可见回复 → 兜底强制回复一次
        if force_reply:
            return self._force_reply(user)
        return None

    def _fallback_reply(self, user, message, now):
        """主叙事写作失败（模型空内容/格式失败）时的兜底：让模型自然回一句，避免干沉默。"""
        try:
            prompt = (f"{user} 发来消息：{message}\n"
                      "请以你的角色身份，用微信聊天口吻自然回复一句（50字以内，直接说话，不要 JSON、不要解释）。")
            text = self.llm_call(prompt) or ''
            text = re.sub(r'^```.*$', '', text, flags=re.M).strip()
            text = re.sub(r'^(回复|回复内容)[:：]\s*', '', text).strip()
            if text and not text.startswith('{'):
                self.state['script'].append({'time': now, 'type': 'user', 'who': user,
                                             'content': message, 'visible': True})
                self.state['script'].append({'time': now, 'type': 'char', 'who': user,
                                             'content': text, 'visible': True})
                self.save()
                return self._norm(text)
        except Exception as e:
            logger.warning(f'剧场 {self.theater_id} 兜底回复失败: {e}')
        # 无论如何都记录用户消息，保持剧本完整
        try:
            self.state['script'].append({'time': now, 'type': 'user', 'who': user,
                                         'content': message, 'visible': True})
            self.save()
        except Exception:
            pass
        return None

    def _force_reply(self, user):
        """被 @ 了却没回复时的兜底：再让模型必须回一句。"""
        now = self._now()
        try:
            prompt = (f"【强制回复】{user} 刚刚@了你，你必须马上给出一个符合你人设的回复，"
                      f"直接说话，不能沉默、不能延迟。只输出 JSON："
                      f"{{\"decision\":{{\"seen\":true,\"mode\":\"reply\",\"content\":\"你的回复\"}}}}")
            data = self._llm_json(prompt)
            content = (data.get('decision') or {}).get('content') or ''
            content = content.strip()
            if content:
                self.state['script'].append({'time': now, 'type': 'char', 'who': user,
                                             'content': content, 'visible': True})
                self.save()
                return self._norm(content)
        except Exception as e:
            logger.warning(f'剧场 {self.theater_id} 强制回复失败: {e}')
        return None

    def _init_canon(self):
        prompt = f"""你是一个剧场导演，请根据下面的角色设定，为这部剧构思并只输出一个 JSON 对象：
{{
  "world": "世界观/时代/地点/背景",
  "scene": "当前舞台场景",
  "script_brief": "剧情梗概（开场设定、人物处境）"
}}
角色设定：
{self.state['canon']['character']}
规则：只输出 JSON，不要多余文字。"""
        try:
            data = self._llm_json(prompt)
            self.state['canon']['world'] = str(data.get('world') or '')
            self.state['canon']['scene'] = str(data.get('scene') or '')
            self.state['canon']['script_brief'] = str(data.get('script_brief') or '')
            self.save()
        except Exception as e:
            logger.warning(f'初始化剧场 {self.theater_id} 世界观失败: {e}')

    # ---- 自动推进（幕间生活） ----

    def advance(self, quiet_fn=None):
        """幕间自动推进。返回待投递消息列表 [{target_user, content}]。"""
        with self._op_lock:
            # 同样先重读磁盘，尊重网页端编辑
            self.state = self._load()
            return self._do_advance(quiet_fn)

    def _do_advance(self, quiet_fn=None):
        now = self._now()
        delivered = []

        # 1) 先兑现到期意图（延迟回复/提醒/主动重查），不受推进间隔限制
        #    （延迟回复已在排期时过群聊闸门；主动联系不受 1% 限制）
        for it in self.state['intents']:
            if it.get('done'):
                continue
            if it.get('target_ts', 0) <= now and it.get('content'):
                if self.respect_quiet and quiet_fn and quiet_fn():
                    continue
                it['done'] = True
                delivered.append({'target_user': it.get('target_user') or '', 'content': it['content']})
        if delivered:
            self.save()

        interval = getattr(self, 'advance_interval_seconds', 20 * 60)
        if now - self.state.get('last_advance', 0) < interval:
            return delivered
        if self.respect_quiet and quiet_fn and quiet_fn():
            return delivered

        refresh = self.state.get('turns_since_refresh', 0) >= self.continuity_refresh_turns
        prompt = self._build_prompt(advance_only=True, refresh_continuity=refresh)
        try:
            data = self._llm_json(prompt)
        except Exception as e:
            logger.warning(f'剧场 {self.theater_id} 幕间推进失败: {e}')
            self.state['last_advance'] = now
            self.save()
            return delivered

        new_delivered = self._apply_turn(data, user=None, current_message=None, now=now, is_advance=True)
        self.state['last_advance'] = now
        self.save()
        for d in new_delivered:
            if d.get('content'):
                delivered.append(d)
        return delivered

    # ---- 工具 ----

    def status(self):
        c = self.state['canon']
        return {
            'theater': self.theater_id,
            'scene': c.get('scene'),
            'participants': list(self.state['participants'].keys()),
            'script_entries': len(self.state['script']),
            'threads': self.state['threads'],
            'long_term': self.state['long_term'][-5:],
            'consequences': self.state['consequences'][-5:],
            'overlays': self.state['overlays'][-5:],
            'intents': [i for i in self.state['intents'] if not i.get('done')][-5:],
            'mood': self.state['mood'],
            'last_advance': self.state.get('last_advance'),
        }
