# -*- coding: utf-8 -*-
"""
剧场系统（Theater / 幕间），对齐 HDS-Interlude 0.1.4 的叙事驱动对话设计。

核心概念：
- 每个用户/群聊单独维护一部持续上演的"生活剧本"（一个剧场）。
- 固定四阶段：用户消息、对话后续（约10/20分钟）、到期意图、独立生活推进。
  每一轮都从故事游标补写已经过的时间，并以当前 nowLocal 结束。
- 时间导演（Time Director）：自动推进/对话后续/到期意图先由轻量模型生成 1-4 个
  相对时间节拍（事件账本），宿主校验时间窗口；主叙事只渲染账本，不再独自决定世界时间。
- Schedule Preplan：后台每日一次轻量日程审查（unchanged/extend/patch/replace），
  保存周规律 regimes + 日期例外 exceptions；主叙事只读取未来约 12 小时、最多八项，
  标记"计划非事实"；fixed 块边界可作为自动推进锚点。
- Agency Window：沉淀日程负荷/隐私/设备三类行动条件；主动联系必须先有真实生活理由
  （生活事件/承诺/实际安排/关系后续），经容量矩阵裁决 立即联系/稍后重查(proactive-check)/自然放下；
  重查不保存预写消息，到期重新读取当前生活再裁决。
- 承诺回访（Promise）：用户回合说出口的"晚点答复"等承诺，每人最多两条待处理，
  只在创建回合、用户后续消息和到期时进入提示词；到期没有可见结果则保留延后，不静默完成。
- 记忆分层：Canon / 前情摘要 / 近期剧本（条目下限+60分钟时间窗口）/ 引子 / 长期事实
  （已完成事件与未兑现承诺双通道）/ Overlay / Perspective / 余波 / 意图 / Preplan。
- 场景压缩：较旧剧本后台压缩成"前情摘要 + 在场表（最多8项）"，保留因果、承诺、大事件与关系变化。
- 自动投递摘要：仅自动回合读取的已完成沟通摘要（最多6项），让下一次推进只表达新增进展。
- Alter 心情惯性：氛围净变化累计，超过动态阈值后由侧端模型生成情绪偏移描述并注入；
  先完成本轮持久化与投递，再在串行队列中执行侧端分析。
- 群聊意愿层：纯算法本地分数（消息/关键词累积、边际递减、半衰期衰减、阈值概率触发、
  发言消耗；@ 机器人始终立即通过），只减少不必要的群聊主模型调用。

所有状态持久化为 JSON（Theater/<聊天名>.json），下次对话无缝续演。
"""

import os
import re
import json
import time
import random
import logging
import threading

logger = logging.getLogger('theater')

MODE_REPLY = 'reply'
MODE_SILENT = 'silent'
MODE_DELAYED = 'delayed'

INTENT_DELAYED_REPLY = 'delayed_reply'
INTENT_REMINDER = 'reminder'
INTENT_FOLLOWUP = 'followup'
INTENT_PROMISE = 'promise'              # 承诺回访：说出口的"晚点答复"，到期必须可见兑现/说明
INTENT_PROACTIVE = 'proactive'          # 旧版预写主动消息（兼容旧存档，直接投递）
INTENT_PROACTIVE_CHECK = 'proactive_check'  # Agency 重查：只存动机/来源/约束，不存预写消息

# 直接投递类意图（到期即发预写内容）
DIRECT_INTENT_TYPES = (INTENT_DELAYED_REPLY, INTENT_REMINDER, INTENT_FOLLOWUP, INTENT_PROACTIVE)

# Alter System 参数（参考 HDS-Interlude）
ALTER_BASE_THRESHOLD = 10.0
ALTER_DENSITY_FACTOR = 0.3
ALTER_SAME_BOOST = 0.05
ALTER_OPPOSITE_DECAY = 0.15
ALTER_MIN_WEIGHT = 0.2
ALTER_MAX_INTENSITY = 2.0
ALTER_RETRY_SECONDS = 300

# Schedule Preplan 参数
PREPLAN_WINDOW_HOURS = 12          # 主叙事只读取未来约 12 小时的计划
PREPLAN_MAX_ITEMS = 8              # 投影最多八项
PREPLAN_DEFAULT_DAYS = 14          # 程序确定性展开的天数（仅内部维护）
PREPLAN_MAX_REGIMES = 5
PREPLAN_MAX_BLOCKS_PER_REGIME = 6
PREPLAN_MAX_EXCEPTIONS = 10
PREPLAN_BLOCK_PRIORITY = {'fixed': 0, 'routine': 1, 'flexible': 2, 'open': 3}
PREPLAN_ALLOWED_TYPES = {
    'stable': ('fixed', 'routine'),
    'contextual': ('fixed', 'routine'),
    'granular': ('fixed', 'routine', 'flexible', 'open'),
}

# 承诺回访
PROMISE_MAX_PER_USER = 2           # 每位参与者最多两条待处理承诺
PROMISE_MAX_ATTEMPTS = 3           # 到期最多裁决次数，超过则记录"未能兑现"
PROMISE_RETRY_SECONDS = 15 * 60

# 自动投递摘要上限
DELIVERED_SUMMARY_MAX = 6

# 剧本压缩
SCRIPT_COMPACT_TRIGGER = 60        # 超出预算多少条后触发后台压缩
PRESENCE_MAX = 8
STORY_SUMMARY_MAX = 10

FOLLOWUP_STALE_HOURS = 6           # 对话后续过期阈值：太久就不补写


def _sanitize(name):
    return re.sub(r'[\\/:*?"<>|\s]', '_', str(name))


def _as_text(v):
    """把模型返回的字段稳健地转成文本：None->''，str 原样，list/tuple 换行拼接，其余 str()。
    避免模型偶发把字符串字段返回成 list/dict 时 .strip() 抛异常而中断整回合。"""
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return '\n'.join(str(x) for x in v)
    return str(v)


# 判定一段文本是不是"给自己的计划/口谕"而非"要对对方说出口的原话"。
# 只高置信命中（宁可漏判不误伤正常聊天），命中后不外发原文，而是渲染成一句真实的话或抑制。
_SELF_INSTRUCTION_PATTERNS = [
    r'如果.{0,10}(没|还没|尚未|不曾).{0,6}(回|吭|理|回复|冒泡|说话)',
    r'(对方|主人|他|她|你).{0,4}没(回|吭|理|回复|冒泡|说话).{0,12}(就|那|再|便|于是|干脆|还是).{0,6}(问|戳|发|提醒|说|招呼|联系|试探|问一句)',
    r'(换|另|改)个?说法',
    r'再.{0,4}(问|戳|发|试|说|喊|招呼).{0,3}(一|两)?(次|遍|下)',
    r'主动(问|发|戳|联系|提醒|打招呼)',
    r'(要不要|需不需要|该不该|得不得)',
    r'提醒(自己|我)',
    r'记得(去|要|问|说|提醒)',
    r'(试探|观察|看看)(一下|下)?.{0,6}(反应|态度|有没有)',
    r'^[我咱俺].{0,4}(去|来|该|得|要).{0,4}(问|说|提醒|打招呼|戳)',
    r'找(个|一)?(话头|借口|理由)',
    r'顺势.{0,4}(问|说|提|发)',
    r'要不要(不)?(轻|悄悄)?(戳|问|提)',
]
_SELF_INSTRUCTION_RE = re.compile('|'.join(_SELF_INSTRUCTION_PATTERNS))


def _looks_like_self_instruction(text):
    t = str(text or '').strip()
    if not t:
        return False
    return bool(_SELF_INSTRUCTION_RE.search(t))


class GroupWillingness:
    """群聊纯算法意愿层（对齐 HDS-Interlude）：
    - 内存分数，不写库、不调用模型、不影响私聊/Alter/Agency。
    - 分数随消息、关键词累积（边际递减），按半衰期衰减。
    - 超过阈值后以概率决定是否值得调用主模型；发言成功后消耗意愿。
    - @ 机器人由调用方直接处理（始终立即通过），不走本层。"""

    def __init__(self, threshold=8.0, half_life=1800.0):
        self.threshold = max(1.0, float(threshold or 8.0))
        self.half_life = max(60.0, float(half_life or 1800.0))
        self.score = 0.0
        self.last_ts = 0.0

    def _decay(self, now):
        if self.last_ts:
            dt = max(0.0, now - self.last_ts)
            if dt > 0:
                self.score *= 0.5 ** (dt / self.half_life)
        self.last_ts = now

    def observe(self, content, now=None):
        """喂一条未 @ 的群消息，累积意愿分数。"""
        now = now if now is not None else time.time()
        self._decay(now)
        text = str(content or '')
        raw = 1.0
        if '？' in text or '?' in text or '吗' in text or '在吗' in text:
            raw += 1.5
        if any(k in text for k in ('哈哈', '笑死', '急', '帮忙', '谢谢', '感谢', '为什么', '怎么')):
            raw += 1.0
        if len(text) > 50:
            raw += 0.5
        # 边际递减：分数越高，单条消息的增量越小
        self.score += raw / (1.0 + 0.15 * self.score)

    def ready(self, now=None):
        """是否值得为当前话题调用一次主模型（超阈值后按概率触发）。"""
        now = now if now is not None else time.time()
        self._decay(now)
        if self.score < self.threshold:
            return False
        p = min(0.6, 0.2 + 0.4 * (self.score - self.threshold) / max(self.threshold, 1e-6))
        return random.random() <= p

    def consume(self):
        """主角成功发言后消耗意愿。"""
        self.score = max(0.0, self.score - self.threshold * 0.8)


class Theater:
    """一个聊天/群独立的生活剧本（剧场）。"""

    def __init__(self, theater_id, data_dir='Theater', character_canon='',
                 llm_call=None, script_budget=40, proactive_threshold=60,
                 respect_quiet=True, recent_context=12,
                 alter_enabled=True, alter_base_threshold=ALTER_BASE_THRESHOLD,
                 min_proactive_interval_seconds=1800,
                 continuity_refresh_turns=5, active_consequence_decay=0.1,
                 image_recognizer=None, now=None,
                 time_director_enabled=True,
                 followup_minutes=(10, 20),
                 preplan_enabled=True, preplan_review_hour=8,
                 preplan_granularity='stable', preplan_anchor_advance=True,
                 proactive_enabled=False, compact_enabled=True,
                 group_will_threshold=8.0, group_will_half_life=1800.0):
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
        # ---- HDS-Interlude 0.1.4 新机制 ----
        self.time_director_enabled = time_director_enabled
        self.followup_minutes = tuple(followup_minutes) or (10, 20)
        self.preplan_enabled = preplan_enabled
        self.preplan_review_hour = int(preplan_review_hour or 8)
        self.preplan_granularity = str(preplan_granularity or 'stable')
        if self.preplan_granularity not in PREPLAN_ALLOWED_TYPES:
            self.preplan_granularity = 'stable'
        self.preplan_anchor_advance = preplan_anchor_advance
        self.proactive_enabled = proactive_enabled
        self.compact_enabled = compact_enabled
        self.is_group = False                      # 是否为群聊（由 bot 注入）
        self.group_reply_probability = 1.0         # 群聊发送闸门（bot 注入，默认全放行）
        self.group_will = GroupWillingness(group_will_threshold, group_will_half_life)
        self._now = now or (lambda: time.time())
        self._op_lock = threading.Lock()   # 串行化本剧场的读-写-存，并避免与网页编辑竞态
        self._compact_running = False
        self._review_running = False
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
            'intents': [],           # {type, target_ts, target_user, content, done, attempts}
            'mood': {'alter': 0, 'cumulative': 0, 'description': '', 'weight': 0,
                     'direction': None, 'recent': [], 'last_analysis_ts': 0},
            'last_proactive': {},    # target -> 最近一次主动发送时间戳（限流用）
            'last_bubble_ts': 0.0,   # 最近一次"冒泡"时间戳
            'turns_since_refresh': 0,
            'last_advance': 0.0,
            'updated_at': 0.0,
            # ---- HDS-Interlude 0.1.4 新状态 ----
            'story_summary': [],     # 前情摘要：较旧剧本压缩成的分段概要 [{ts, text, count}]
            'presence': [],          # 在场表：压缩时按真实条目确认的配角状态（最多8项）
            'delivered_summary': [],  # 自动投递摘要：仅自动回合读取的已完成沟通（最多6项）
            'agency': {},            # Agency Window：{activityLoad, privacy, deviceAccess, valid_until, basis, updated_at}
            'preplan': {             # Schedule Preplan：近期日程层
                'version': 0, 'reviewed_date': '', 'reviewed_ts': 0,
                'next_review_ts': 0, 'note': '', 'regimes': [], 'exceptions': [],
            },
            'last_user': '',         # 最近一位说话的参与者
            'last_user_ts': 0.0,     # 最近一次用户消息时间（对话后续的起点）
            'last_followup_ts': 0.0, # 最近一次对话后续完成时间
            'followup_done': {},     # 对话后续完成标记 {0: bool, 1: bool}（用户新消息时重置）
        }

    def _load(self):
        base = self._new_state()
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        base[k] = v
                else:
                    logger.error(f'剧场 {self.theater_id} 文件内容非字典，将重建。')
        except Exception as e:
            logger.error(f'加载剧场 {self.theater_id} 失败: {e}，将备份损坏文件后重建。')
            # 重建前备份损坏文件，避免 save() 直接覆盖造成全量记忆永久丢失
            try:
                if os.path.exists(self.path):
                    bak = f'{self.path}.corrupt-{int(time.time())}'
                    os.replace(self.path, bak)
                    logger.warning(f'已备份损坏的剧场文件到: {bak}')
            except Exception as be:
                logger.error(f'备份损坏剧场文件失败: {be}')
        # 归一化，兼容旧版本文件（防御磁盘数据类型不符）
        m = base.get('mood')
        if not isinstance(m, dict):
            m = {}
            base['mood'] = m
        m.setdefault('cumulative', 0)
        m.setdefault('weight', 0)
        m.setdefault('direction', None)
        m.setdefault('recent', [])
        m.setdefault('last_analysis_ts', 0)
        base.setdefault('consequences', [])
        base.setdefault('continuity', {})
        base.setdefault('last_proactive', {})
        base.setdefault('turns_since_refresh', 0)
        base.setdefault('story_summary', [])
        base.setdefault('presence', [])
        base.setdefault('delivered_summary', [])
        base.setdefault('agency', {})
        if not isinstance(base.get('agency'), dict):
            base['agency'] = {}
        if not isinstance(base.get('continuity'), dict):
            base['continuity'] = {}
        if not isinstance(base.get('last_proactive'), dict):
            base['last_proactive'] = {}
        pp = base.get('preplan')
        if not isinstance(pp, dict):
            pp = {}
            base['preplan'] = pp
        pp.setdefault('version', 0)
        pp.setdefault('reviewed_date', '')
        pp.setdefault('reviewed_ts', 0)
        pp.setdefault('next_review_ts', 0)
        pp.setdefault('note', '')
        pp.setdefault('regimes', [])
        pp.setdefault('exceptions', [])
        base.setdefault('last_user', '')
        base.setdefault('last_user_ts', 0.0)
        base.setdefault('last_followup_ts', 0.0)
        base.setdefault('followup_done', {})
        # 关键字段类型守卫：磁盘数据若被写坏成非 list/dict，避免后续迭代崩溃
        for _lk in ('intents', 'script', 'consequences', 'story_summary', 'presence', 'delivered_summary'):
            if not isinstance(base.get(_lk), list):
                base[_lk] = []
        for _dk in ('continuity', 'last_proactive', 'agency', 'followup_done', 'preplan'):
            if not isinstance(base.get(_dk), dict):
                base[_dk] = {}
        if not isinstance(base.get('participants'), dict):
            base['participants'] = {}
        return base

    def save(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            self.state['updated_at'] = self._now()
            self._prune_intents()
            # tmp 名带 pid，避免 bot 进程与 config_editor 进程写同一 <name>.json.tmp 互相截断
            tmp = f'{self.path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f'保存剧场 {self.theater_id} 失败: {e}')

    def _prune_intents(self):
        """清理已完成且过期的意图，并对总数设硬上限，防止 intents 无限增长导致
        JSON 膨胀 + 每轮多处全量扫描变慢。优先保留未完成(pending)意图。"""
        try:
            intents = self.state.get('intents')
            if not isinstance(intents, list) or not intents:
                return
            now = self._now()
            STALE = 7 * 24 * 3600  # 已完成且超过 7 天的意图视为可清理
            kept = []
            for it in intents:
                if not isinstance(it, dict):
                    continue
                try:
                    tts = float(it.get('target_ts') or 0)
                except (TypeError, ValueError):
                    tts = 0.0
                if it.get('done') and (now - tts) > STALE:
                    continue
                kept.append(it)
            MAX_INTENTS = 200
            if len(kept) > MAX_INTENTS:
                not_done = [x for x in kept if not x.get('done')]
                done = [x for x in kept if x.get('done')]
                done.sort(key=lambda x: float(x.get('target_ts') or 0), reverse=True)
                keep_done = max(0, MAX_INTENTS - len(not_done))
                kept = not_done + done[:keep_done]
            self.state['intents'] = kept
        except Exception as e:
            logger.debug(f'_prune_intents 失败: {e}')

    # ---- 参与者 ----

    def ensure_participant(self, user):
        return self.state['participants'].setdefault(user, {
            'profile': '', 'relation': '', 'relation_evolution': '', 'arc': '', 'last_seen_ts': 0.0,
        })

    # ---- Schedule Preplan：日程层 ----

    @staticmethod
    def _parse_hhmm(s):
        m = re.match(r'^(\d{1,2}):(\d{2})$', str(s or '').strip())
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        return h * 60 + mi

    @staticmethod
    def _day_start_ts(ts):
        lt = time.localtime(ts)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    def _validate_preplan(self, regimes, exceptions):
        """校验模型返回的日程结构；非法返回 (None, None)，整体拒绝、旧版本继续有效。"""
        allowed = PREPLAN_ALLOWED_TYPES[self.preplan_granularity]

        def _clean_blocks(blocks):
            out = []
            if not isinstance(blocks, list):
                return None
            for b in blocks[:PREPLAN_MAX_BLOCKS_PER_REGIME]:
                if not isinstance(b, dict):
                    continue
                typ = str(b.get('type') or '').strip()
                if typ not in allowed:
                    continue
                s = self._parse_hhmm(b.get('start'))
                e = self._parse_hhmm(b.get('end'))
                if s is None or e is None or e <= s:
                    continue
                label = str(b.get('label') or '').strip()[:40]
                if not label:
                    continue
                out.append({'type': typ,
                            'start': f'{s // 60:02d}:{s % 60:02d}',
                            'end': f'{e // 60:02d}:{e % 60:02d}',
                            'label': label})
            return out

        clean_regimes = []
        if isinstance(regimes, list):
            for r in regimes[:PREPLAN_MAX_REGIMES]:
                if not isinstance(r, dict):
                    continue
                days = []
                for d in (r.get('days') or []):
                    try:
                        di = int(d)
                    except Exception:
                        continue
                    if 1 <= di <= 7 and di not in days:
                        days.append(di)
                blocks = _clean_blocks(r.get('blocks'))
                if days and blocks:
                    clean_regimes.append({'days': days, 'blocks': blocks})
        clean_exceptions = []
        if self.preplan_granularity in ('contextual', 'granular') and isinstance(exceptions, list):
            for e in exceptions[:PREPLAN_MAX_EXCEPTIONS]:
                if not isinstance(e, dict):
                    continue
                d = str(e.get('date') or '').strip()
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
                    continue
                blocks = _clean_blocks(e.get('blocks'))
                if blocks:
                    clean_exceptions.append({'date': d, 'blocks': blocks})
        if not clean_regimes and not clean_exceptions:
            # 空结果也可能是合法的"无计划"，但要求至少结构合法才接受空
            if isinstance(regimes, list):
                return [], clean_exceptions
            return None, None
        return clean_regimes, clean_exceptions

    def _expand_day_blocks(self, day_ts):
        """展开某天（当天0点时间戳）的日程块：周规律 + 日期例外（例外优先）。
        冲突时按 fixed > routine > flexible > open 保留。"""
        pp = self.state.get('preplan') or {}
        lt = time.localtime(day_ts)
        # tm_wday: 0=周一..6=周日；本项目约定 1=周一..7=周日，故直接 +1。
        # （旧写法 (tm_wday+1)%7+1 会得到 周一=2..周日=1，导致日程整体错位一天）
        weekday = lt.tm_wday + 1
        raw = []
        for r in (pp.get('regimes') or []):
            try:
                days = [int(x) for x in (r.get('days') or [])]
            except Exception:
                continue
            if weekday in days:
                raw.extend(r.get('blocks') or [])
        if self.preplan_granularity in ('contextual', 'granular'):
            ds = time.strftime('%Y-%m-%d', time.localtime(day_ts))
            for e in (pp.get('exceptions') or []):
                if e.get('date') == ds and e.get('blocks'):
                    raw = list(e['blocks'])
                    break
        blocks = []
        for b in raw:
            if not isinstance(b, dict):
                continue
            s = self._parse_hhmm(b.get('start'))
            e = self._parse_hhmm(b.get('end'))
            typ = str(b.get('type') or '')
            if s is None or e is None or e <= s or typ not in PREPLAN_BLOCK_PRIORITY:
                continue
            blocks.append({'type': typ, 'start': b.get('start'), 'end': b.get('end'),
                           'label': str(b.get('label') or ''), '_s': s, '_e': e})
        blocks.sort(key=lambda x: (x['_s'], PREPLAN_BLOCK_PRIORITY[x['type']]))
        result = []
        for b in blocks:
            if result and b['_s'] < result[-1]['_e']:
                # 与上一块重叠：保留优先级高的
                if PREPLAN_BLOCK_PRIORITY[b['type']] < PREPLAN_BLOCK_PRIORITY[result[-1]['type']]:
                    result[-1] = b
                continue
            result.append(b)
        return result

    def _preplan_projection(self, now):
        """主叙事投影：未来约 12 小时、最多八项；计划只约束合理性，不是已发生事实。"""
        if not self.preplan_enabled:
            return []
        out = []
        today_ts = self._day_start_ts(now)
        for off in range(2):
            day_ts = today_ts + off * 86400
            label = '今天' if off == 0 else '明天'
            for b in self._expand_day_blocks(day_ts):
                s_ts = day_ts + b['_s'] * 60
                if s_ts <= now or s_ts > now + PREPLAN_WINDOW_HOURS * 3600:
                    continue
                tag = b['type']
                if b['type'] in ('flexible', 'open') and self.preplan_granularity == 'granular':
                    tag = '可能的个人安排'
                out.append((s_ts, f"- {label} {b['start']}-{b['end']} {b['label']}（{tag}）"))
        out.sort(key=lambda x: x[0])
        return [x[1] for x in out[:PREPLAN_MAX_ITEMS]]

    def _next_preplan_anchor(self, now):
        """自动推进锚点：未来 12 小时内最近的 fixed 块开始/结束边界。"""
        if not (self.preplan_enabled and self.preplan_anchor_advance):
            return None
        best = None
        today_ts = self._day_start_ts(now)
        for off in range(2):
            day_ts = today_ts + off * 86400
            for b in self._expand_day_blocks(day_ts):
                if b['type'] != 'fixed':
                    continue
                for boundary in (day_ts + b['_s'] * 60, day_ts + b['_e'] * 60):
                    if now < boundary < now + PREPLAN_WINDOW_HOURS * 3600:
                        if best is None or boundary < best:
                            best = boundary
        return best

    def _maybe_preplan_review(self, now):
        """每日最多一次轻量日程审查（后台空闲整理时触发，不阻塞前台）。"""
        if not (self.preplan_enabled and self.llm_call):
            return
        pp = self.state.get('preplan') or {}
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        if pp.get('reviewed_date') == today:
            return
        if pp.get('next_review_ts', 0) > now:
            return
        if time.localtime(now).tm_hour < self.preplan_review_hour:
            return
        if self._review_running:
            return
        self._review_running = True
        threading.Thread(target=self._preplan_review_safe, args=(now,), daemon=True).start()

    def _preplan_review_safe(self, now):
        try:
            with self._op_lock:
                self.state = self._load()
                self._run_preplan_review(now)
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》日程审查异常: {e}')
        finally:
            self._review_running = False

    def _run_preplan_review(self, now):
        st = self.state
        pp = st.setdefault('preplan', {})
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        try:
            # 证据：幕间生活条目 + 引子 + 长期事实（隐私边界：不发送用户私聊原文）
            evid = []
            for it in st['script']:
                if it.get('type') in ('advance', 'scene'):
                    evid.append(str(it.get('content') or '')[:80])
            evid_text = '\n'.join('- ' + x for x in evid[-25:] if x)[:1000]
            threads_text = '；'.join(st.get('threads') or [])[:300]
            cur = json.dumps({'regimes': pp.get('regimes') or [], 'exceptions': pp.get('exceptions') or []},
                             ensure_ascii=False)
            prompt = f"""你是日程审查员，为主角维护"近期日程计划"（Schedule Preplan）。
当前计划：{cur or '（暂无）'}
近期生活证据（幕间剧本）：
{evid_text or '（无）'}
当前剧情线索：{threads_text or '（无）'}
颗粒度要求：{self.preplan_granularity}（stable=只接受稳定的周规律如上课/作息；contextual=允许学期/假期阶段与日期例外；granular=额外允许少量 flexible 柔性候选安排）。

只输出一个 JSON 对象：
{{
  "decision": "unchanged | extend | patch | replace",
  "regimes": [{{"days": [1,2,3,4,5], "blocks": [{{"type": "fixed|routine|flexible|open", "start": "08:00", "end": "12:00", "label": "上课"}}]}}],
  "exceptions": [{{"date": "YYYY-MM-DD", "blocks": [同上]}}],
  "reason": "一句话依据"
}}
规则：
- 没有可靠的重复日程证据时输出 unchanged；不要虚构固定课程、考试或约定。
- fixed=上学/补课/考试/明确约定（需要真实来源）；routine=通勤/午休/常规作息；flexible=证据不足的爱好；open=空闲。
- days 用 1=周一..7=周日；每天最多 6 块；regimes 最多 5 条。
- 优先参考当前计划，只在新证据需要建立、续写或修正时才补写。
- 只输出 JSON，不要多余文字。"""
            data = self._llm_json(prompt)
            dec = str((data or {}).get('decision') or 'unchanged').strip()
            if dec in ('extend', 'patch', 'replace'):
                regimes, exceptions = self._validate_preplan(data.get('regimes'), data.get('exceptions'))
                if regimes is None:
                    logger.info(f'剧场《{self.theater_id}》日程提案非法，保留旧计划。')
                else:
                    pp['regimes'] = regimes
                    pp['exceptions'] = exceptions
                    pp['version'] = int(pp.get('version', 0)) + 1
                    pp['note'] = ''
            else:
                if not (pp.get('regimes') or pp.get('exceptions')):
                    # 明确的空审查记录：避免空结果被误判为失败而反复重试
                    pp['note'] = '已审查，暂无固定日程证据'
            pp['reviewed_date'] = today
            pp['reviewed_ts'] = now
            pp['next_review_ts'] = 0
            self.save()
            logger.info(f'剧场《{self.theater_id}》日程审查完成（{dec}，v{pp.get("version", 0)}）。')
        except Exception as e:
            # 失败不清空旧计划；一小时内不重试
            pp['next_review_ts'] = now + 3600
            try:
                self.save()
            except Exception:
                pass
            logger.warning(f'剧场《{self.theater_id}》日程审查失败: {e}')

    # ---- 上下文构建 ----

    def _fmt_script(self):
        items = self.state['script']
        if not items:
            return '（还没有发生过什么）'
        start = max(0, len(items) - self.script_budget)
        # 条目下限 + 时间窗口并集：保护最近 60 分钟内的真实收发消息不被截断
        if start > 0:
            now = self._now()
            extra = 0
            i = start - 1
            while i >= 0 and extra < self.script_budget:
                it = items[i]
                if it.get('type') in ('user', 'char') and now - it.get('time', 0) <= 3600:
                    start = i
                    extra += 1
                    i -= 1
                else:
                    break
        lines = []
        for it in items[start:]:
            t = it.get('type')
            who = it.get('who', '')
            content = str(it.get('content') or '')
            if t == 'advance' and len(content) > 240:
                content = content[:240] + '…'
            elif len(content) > 160:
                content = content[:160] + '…'
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
            content = str(it.get('content') or '')
            if len(content) > 120:
                content = content[:120] + '…'
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
        """长期事实双通道：未兑现承诺单独成栏，防止开放事项挤出刚完成的重要事件。"""
        st = self.state
        pending_promises = [it for it in st.get('intents') or []
                            if not it.get('done') and it.get('type') in (INTENT_PROMISE, INTENT_DELAYED_REPLY)]
        lines = []
        if pending_promises:
            seen = set()
            for it in pending_promises:
                c = str(it.get('content') or '').strip()
                if c and c[:16] not in seen:
                    seen.add(c[:16])
                    lines.append(f"- [未兑现承诺→{it.get('target_user') or '自己'}] {c[:80]}")
        for t in st['long_term']:
            lines.append('- ' + str(t))
        return '\n'.join(lines) if lines else '（无）'

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

    def _fmt_intents(self, include_promise=True):
        """待办意图。承诺回访只在用户回合（创建/后续消息）显示，普通自动推进不读取。"""
        out = []
        now = self._now()
        for it in self.state['intents']:
            if it.get('done'):
                continue
            if it.get('type') == INTENT_PROMISE and not include_promise:
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

    def _fmt_story_summary(self):
        """前情摘要：较旧剧本压缩成的分段概要（保留因果、承诺、大事件与关系变化）。"""
        ss = self.state.get('story_summary') or []
        if not ss:
            return ''
        lines = []
        for s in ss[-4:]:
            ts = s.get('ts', 0)
            local = time.strftime('%m-%d', time.localtime(ts)) if ts else '?'
            lines.append(f"- [{local}] {str(s.get('text') or '')[:200]}")
        return '\n'.join(lines)

    def _fmt_presence(self):
        """在场表：按真实剧本条目确认的少量配角在场/离场/待会合状态。"""
        pr = self.state.get('presence') or []
        if not pr:
            return ''
        zh = {'present': '在场', 'left': '离场', 'joining': '待会合'}
        lines = []
        for p in pr[:PRESENCE_MAX]:
            status = zh.get(p.get('status'), str(p.get('status') or ''))
            note = p.get('note') or ''
            lines.append(f"- {p.get('name', '?')}：{status}{('，' + note) if note else ''}")
        return '\n'.join(lines)

    def _fmt_delivered_summary(self):
        """自动投递摘要：仅自动回合读取，让下一次推进只表达新增进展。"""
        ds = self.state.get('delivered_summary') or []
        if not ds:
            return ''
        lines = []
        for d in ds:
            ts = d.get('ts', 0)
            local = time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else '?'
            lines.append(f"- [{local}] 已向 {d.get('to') or '?'} 发送过：{d.get('brief', '')}")
        return '\n'.join(lines)

    def _fmt_agency(self, now=None):
        """Agency Window：日程负荷/隐私/设备三类行动条件（过期不进入主模型）。"""
        a = self.state.get('agency') or {}
        if not a or a.get('valid_until', 0) < (now or self._now()):
            return ''
        zh_load = {'free': '空闲', 'occupied': '忙碌', 'overloaded': '过载'}
        zh_priv = {'private': '私密空间', 'shared': '共享场合', 'public': '公共场合'}
        zh_dev = {'available': '设备可用', 'limited': '设备受限', 'unavailable': '设备不可用'}
        return (f"日程={zh_load.get(a.get('activityLoad'), a.get('activityLoad'))}，"
                f"隐私={zh_priv.get(a.get('privacy'), a.get('privacy'))}，"
                f"设备={zh_dev.get(a.get('deviceAccess'), a.get('deviceAccess'))}"
                f"（依据：{a.get('basis') or '未说明'}）")

    def _fmt_preplan(self, now=None):
        lines = self._preplan_projection(now or self._now())
        return '\n'.join(lines) if lines else ''

    def _alter_threshold(self):
        # 动态阈值：density=最近一小时有效回合数/10，阈值由 base 平滑下降（最大降到一半）
        now = self._now()
        recent = [s for s in self.state['mood'].get('recent', []) if now - s < 3600]
        density = min(len(recent) / 10.0, 1.0)
        base = self.alter_base_threshold
        return max(base * 0.5, base * (1.0 - density * ALTER_DENSITY_FACTOR))

    def _time_director(self, window_start, now):
        """时间导演：为自动回合生成 1-4 个相对时间节拍（事件账本）。
        宿主校验节拍落在窗口内；主叙事只渲染账本，不再独自决定世界时间。"""
        if not self.llm_call:
            return None
        span = int((now - window_start) / 60)
        if span < 1:
            return None
        try:
            evid = []
            for it in self.state['script']:
                if it.get('type') == 'advance':
                    evid.append(str(it.get('content') or '')[:80])
            evid_text = '\n'.join('- ' + x for x in evid[-12:] if x)[:800]
            threads_text = '；'.join(self.state.get('threads') or [])[:200]
            preplan_text = self._fmt_preplan(now)
            prompt = f"""你是"时间导演"，为一段无人打扰的幕间时间规划真实的生活事件节拍。
时间窗口：从 {(span)} 分钟前到现在（共 {span} 分钟）。
近期幕间生活：
{evid_text or '（无）'}
当前剧情线索：{threads_text or '（无）'}
近期日程（计划非事实）：{preplan_text or '（无）'}

只输出一个 JSON 对象：
{{"beats": [{{"offset_minutes": -30, "event": "一句话生活事件"}}]}}
规则：
- 1-4 个节拍；offset_minutes 为负数（相对现在多少分钟前），必须落在 -{span} 到 0 之间。
- 事件要平凡、真实、符合角色作息与日程；按时间顺序自然衔接。
- 禁止编造收到的消息/电话/通知/外部互动；只写角色自己的生活。
- 最后一个节拍应接近现在。只输出 JSON。"""
            data = self._llm_json(prompt)
            beats = (data or {}).get('beats') or []
            valid = []
            for b in beats[:4]:
                if not isinstance(b, dict):
                    continue
                try:
                    off = int(b.get('offset_minutes'))
                except Exception:
                    continue
                ev = str(b.get('event') or '').strip()
                if not ev or off > 0 or off < -(span + 5):
                    continue
                valid.append((off, ev))
            if not valid:
                return None
            valid.sort(key=lambda x: x[0])
            lines = []
            for off, ev in valid:
                when = '刚刚' if off == 0 else f'约{abs(off)}分钟前'
                lines.append(f"- {when}：{ev}")
            lines.append("- 现在：（当前时刻，收尾）")
            return '\n'.join(lines)
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》时间导演失败: {e}')
            return None

    def _build_prompt(self, stage='user', current_event=None, drafts=None,
                      force_reply=False, group_recent=None, timeline=None,
                      refresh_continuity=False, allow_bubble=False, want_proactive=False):
        """按 HDS-Interlude 的"稳定前缀优先"顺序组装主叙事提示词。
        stage: user=用户消息 / followup=对话后续 / advance=独立生活推进 /
               promise_due=承诺到期 / proactive_recheck=主动联系重查"""
        c = self.state['canon']
        now = self._now()
        now_local = time.strftime('%Y-%m-%d %H:%M:%S %A', time.localtime(now))
        hour = time.localtime(now).tm_hour
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
        is_auto = stage != 'user'

        p = f"""【权威现实时间】现在是 {now_local}（{period}）。这是绝对基准。
【时间铁律】剧本必须严格以现在的时间为准推进：
- 现在几点、星期几、什么时段（早晨/上午/中午/下午/傍晚/晚上/深夜），就以它为准。
- 最近剧本/记忆/旧场景里写的旧时间（如"深夜/半夜/天黑/凌晨"）已经过去，必须推进到当前时段。
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

【前情摘要（较旧剧本的压缩概要，仅作背景）】
{self._fmt_story_summary() or '（无）'}

【在场人物（按真实剧本确认的配角状态）】
{self._fmt_presence() or '（无）'}

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

【长期事实（含未兑现承诺）】
{self._fmt_long_term()}

【剧情余波（active consequence，短期影响、会自然消退）】
{self._fmt_consequences()}

【角色个体视角（看待世界/他人/自己的方式，独立于设定）】
{self.state['perspective'] or '（尚未形成明确个体视角）'}

【Overlay 渐进变化（长期剧情中形成的性格/世界/关系微调，非破坏性）】
{self._fmt_overlays()}

【情绪氛围】{self._fmt_mood_offset()}

【行动条件（Agency：只约束联系行动，不控制文风）】{self._fmt_agency(now)}

【近期日程（计划，不是已发生的事实；仅用于保持时间/地点/可用性合理，不要逐项复述，也不要因时间已过就声称完成）】
{self._fmt_preplan(now) or '（无）'}
"""
        if is_auto:
            ds = self._fmt_delivered_summary()
            if ds:
                p += f"""
【自动投递摘要（此前自动回合已经发出的话，不要重复表达，只写新增进展）】
{ds}
"""
        if stage in ('followup', 'advance', 'promise_due', 'proactive_recheck'):
            if timeline:
                p += f"""
【幕间事件账本（时间导演已排定：只能逐项渲染这些事件，不得增删节拍、改变时间或越出账本）】
{timeline}
"""
            p += """
【本轮无实时用户消息】按"幕间写作"处理：补写已经过的时间，处理到期事项，不虚构任何外部输入。
"""
        if stage == 'followup':
            p += """
【本轮任务】对话后续补写：刚才的对话告一段落。结合对话余波与未完成事项，补写对话结束后角色的生活与心情；若确有自然的一句话想补充（例如想起什么、兑现一个小承诺），可给出可见回复，否则沉默也是合理剧情。
"""
        if stage == 'promise_due':
            p += """
【本轮任务】承诺到期：兑现你在剧本中说出口的承诺。优先给出可见回复（把兑现的话发给对方）；若确实无法兑现，必须在 intents 里重新排期并在剧本里写出原因，不能默默消失。
"""
        if stage == 'proactive_recheck':
            p += """
【本轮任务】主动联系重查：此前的生活给了你一个联系动机。结合当前生活与行动条件重新裁决一次：现在是否值得联系对方（输出 proactive_candidate）。重查时重新读取当前生活，不要照搬旧结论。
"""
        p += f"""
【时间】{now_local_context}；{period}是权威判断依据——旧剧本写的时段已过，就把生活推进到现在的时段。
"""
        if drafts:
            p += f"""
【被打断的草稿（角色上次还在打字、没发出去的"未说完的念头"，用户新消息打断了它）】
{self._fmt_drafts(drafts)}
——这些不是已发送的对话，只是被打断的念头。不要把它当作对方收到的话，也不要自动发送；让它自然地影响这次的新剧本，然后重新做一个全新的回复决定。
"""
        # ---- 各阶段输出契约 ----
        if stage == 'user':
            p += f"""
【当前事件】{current_event}
"""
            if group_recent:
                p += f"""
【最近群聊记录（回看，最新的在后，用于理解当下语境）】
{group_recent}
"""
            if force_reply:
                p += "【强制回复】对方 @ 了你：本回合必须给出可见回复（decision.mode=reply 且 content 非空），禁止 silent / delayed / seen=false。\n"
            p += """
请以演员身份完成一次"幕间写作"，只输出一个 JSON 对象（不要任何多余文字、不要 markdown 代码块）：
{
  "elapsed_script": "从上次游标到现在，角色在幕间经历的事（一段叙述，时间很短可为空字符串）",
  "decision": {
    "seen": true,
    "mode": "reply | silent | delayed",
    "content": "要回复的话（mode 不是 reply 时可为空）",
    "delay_minutes": 0
  },
  "scene_update": "当前舞台场景一句话更新（可为空字符串）",
  "threads": ["保留或新增的剧情引子，最多5条"],
  "long_term": ["新增的长期事实，无则空数组"],
  "perspective_update": "角色个体视角/价值观的一句话更新（无则空字符串）",
  "overlay_candidates": [{"layer": "character|world|relationship", "target": "目标（人/物/关系）", "description": "长期剧情中渐进形成的非破坏性变化描述"}],
  "active_consequences": [{"effect": "持续影响角色的一件事", "strength": 0.0, "minutes": 0}],
  "participant_update": {"<参与者名>": {"relation_evolution": "关系变化", "arc": "剧情线/待办"}},
  "intents": [{"type": "reminder|delayed_reply|followup|promise", "target_user": "<参与者名或空>", "when": "10分钟后", "content": "..."}],
  "alter": 0,
  "agency_update": {"activityLoad": "free|occupied|overloaded", "privacy": "private|shared|public", "deviceAccess": "available|limited|unavailable", "validMinutes": 120, "basis": "来自本轮剧本的一句话依据"}
}
规则：
- 完全代入角色，像真实的人一样生活、思考、说话，禁止出现"作为AI/我是机器人"等字眼。
- 【现实约束】禁止虚构角色"收到的消息/电话/通知/对方回复/引用"——剧本只能写观察到的现实；禁止用 [表情]、[图片]、引用：原句 这类占位文本假装平台功能。
- 【人格稳定】单次的情绪、一次回复、一个异常事件不改变 Canon/Overlay/Perspective；只有跨回合反复出现的行为才写成 overlay_candidates/perspective_update。
- 【时间】剧本必须严格以"权威现实时间"（当前几点/时段）为准，把生活推进到当前时段；当前是白天就绝不能写深夜/半夜/天黑。
- 【三态】seen=false 表示没看到；seen=true 且 mode=silent 表示看到了但不回（沉默本身也是剧情）；mode=reply 立即回；mode=delayed 晚点回并写进 intents。
- 【alter】为-5..+5，只衡量本轮新增的氛围净变化：正=更严肃/紧张/正式，负=更轻松/随意；不要把已有氛围当作打分依据。
- 【承诺】若回复里说"想想再回/晚点回/明天给你"，必须同时写一条 type=promise 的 intent（每人最多两条待处理），之后必须可见兑现，不能默默消失。
- 【agency_update】只描述本轮剧本实际体现的日程负荷/隐私/设备状态，basis 必须引用本轮真实剧情。
- 【发送文案】decision.content 与 intents[].content 一律填写【要原样发给对方的那句话本身】，禁止写成给自己的计划或条件（如"如果对方没回就…""再问一次""要不要提醒""换个说法戳一下"）。这类念头只能用画面化的内心独白写进 elapsed_script，绝不能进 content。
- 每次写作都要让"故事"更丰满，让下一次对话能自然延续。
"""
        else:
            schema = """
{
  "elapsed_script": "按【幕间事件账本】逐项渲染的剧本（若无账本则自行短推进，结尾落在现在）",
  "scene_update": "当前舞台场景一句话更新（可为空）",
  "threads": ["保留或新增的剧情引子，最多5条"],
  "long_term": ["新增的长期事实，无则空数组"],
  "perspective_update": "角色个体视角/价值观的一句话更新（无则空字符串）",
  "overlay_candidates": [{"layer": "character|world|relationship", "target": "目标", "description": "..."}],
  "active_consequences": [{"effect": "持续影响角色的一件事", "strength": 0.0, "minutes": 0}],
  "intents": [{"type": "reminder|delayed_reply|followup|promise", "target_user": "<参与者名或空>", "when": "20分钟后", "content": "..."}],
  "alter": 0,
  "agency_update": {"activityLoad": "free|occupied|overloaded", "privacy": "private|shared|public", "deviceAccess": "available|limited|unavailable", "validMinutes": 120, "basis": "一句话依据"}
}"""
            if stage in ('followup', 'promise_due'):
                schema = """
{
  "elapsed_script": "按【幕间事件账本】逐项渲染的剧本（若无账本则自行短推进，结尾落在现在）",
  "decision": {
    "seen": true,
    "mode": "reply | silent",
    "content": "要补充发送的话（mode=silent 时为空）",
    "delay_minutes": 0
  },
  "scene_update": "当前舞台场景一句话更新（可为空）",
  "threads": ["保留或新增的剧情引子，最多5条"],
  "long_term": ["新增的长期事实，无则空数组"],
  "perspective_update": "角色个体视角/价值观的一句话更新（无则空字符串）",
  "overlay_candidates": [{"layer": "character|world|relationship", "target": "目标", "description": "..."}],
  "active_consequences": [{"effect": "持续影响角色的一件事", "strength": 0.0, "minutes": 0}],
  "intents": [{"type": "reminder|delayed_reply|followup|promise", "target_user": "<参与者名或空>", "when": "20分钟后", "content": "..."}],
  "alter": 0,
  "agency_update": {"activityLoad": "free|occupied|overloaded", "privacy": "private|shared|public", "deviceAccess": "available|limited|unavailable", "validMinutes": 120, "basis": "一句话依据"}
}"""
            if want_proactive:
                schema = """
{
  "elapsed_script": "按【幕间事件账本】逐项渲染的剧本（若无账本则自行短推进，结尾落在现在）",
  "scene_update": "当前舞台场景一句话更新（可为空）",
  "threads": ["保留或新增的剧情引子，最多5条"],
  "long_term": ["新增的长期事实，无则空数组"],
  "perspective_update": "角色个体视角/价值观的一句话更新（无则空字符串）",
  "overlay_candidates": [{"layer": "character|world|relationship", "target": "目标", "description": "..."}],
  "active_consequences": [{"effect": "持续影响角色的一件事", "strength": 0.0, "minutes": 0}],
  "intents": [{"type": "reminder|delayed_reply|followup|promise", "target_user": "<参与者名或空>", "when": "20分钟后", "content": "..."}],
  "alter": 0,
  "agency_update": {"activityLoad": "free|occupied|overloaded", "privacy": "private|shared|public", "deviceAccess": "available|limited|unavailable", "validMinutes": 120, "basis": "一句话依据"},
  "proactive_candidate": {"origin": "life-event|promise|practical-update|relationship-follow-up", "motive": "具体、真实的生活动机", "target_user": "<参与者名>", "sensitive": false, "willingness": 0, "outcome": "send-now|recheck-later|let-go", "notBefore": "30分钟后"}
}"""
            p += f"""
请以演员身份完成这次"幕间写作"，只输出一个 JSON 对象（不要任何多余文字、不要 markdown 代码块）：
{schema}
规则：
- 完全代入角色；【现实约束】禁止虚构收到的消息/电话/通知/引用；禁止 [表情]/[图片] 占位文本。
- 【人格稳定】overlay_candidates 只记录长期稳定变化，一次 0-2 条；perspective_update 一句话即可。
- 【时间】严格以权威现实时间收尾；若给了【幕间事件账本】，只能渲染账本内事件。
- 【alter】为-5..+5，只衡量本轮新增的氛围净变化。
- 【承诺】剧本里说出口的未来承诺必须写进 intents（type=promise），不能默默消失。
- 【发送文案】intents[].content 与（若有）bubble 只能填【要原样发给对方的那句话】，绝不能是给自己的计划或条件（"如果…没回""再问一次""主动提醒""换个说法"等）。留给自己的待办念头用画面化独白写进 elapsed_script。
"""
        if allow_bubble:
            p += ('\n【冒泡】本回合你可以随口在群里冒个泡（一句轻松的闲聊，40字内，像真人随口说一句）。'
                  'bubble 只能填【你真正要发到群里的那句话】，不要写打算/条件；没有可说的话就不输出 bubble。\n')
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
        """把相对时间解析为分钟数，识别 秒/分钟/小时/天/周 单位并 clamp 到 1..43200（30天）。

        旧实现只取第一个数字、忽略单位，会把"1小时后"当成 1 分钟、"30天后"当成 30 分钟，
        导致延迟回复/承诺/提醒的时间全部错乱。
        """
        if when is None:
            return None
        s = str(when).strip()
        if not s:
            return None
        MAX_MIN = 30 * 24 * 60
        m = re.search(r'(\d+(?:\.\d+)?)\s*(秒|分钟|分|小时|钟头|天|日|周|星期)?', s)
        minutes = None
        if m:
            num = float(m.group(1))
            unit = m.group(2) or ''
            if unit == '秒':
                minutes = num / 60.0
            elif unit in ('小时', '钟头'):
                minutes = num * 60.0
            elif unit in ('天', '日'):
                minutes = num * 24 * 60.0
            elif unit in ('周', '星期'):
                minutes = num * 7 * 24 * 60.0
            else:  # 分钟/分/无单位 → 按分钟
                minutes = num
        # 语义词兜底（"明天"/"半小时"等无显式数字单位的情况）
        if not minutes or minutes <= 0:
            if '明天' in s:
                minutes = 24 * 60.0
            elif '半小时' in s or '半个小时' in s:
                minutes = 30.0
            elif '小时' in s or '钟头' in s:
                minutes = 60.0
            elif '天' in s or '日' in s:
                minutes = 24 * 60.0
        if not minutes or minutes <= 0:
            return None
        return int(max(1, min(minutes, MAX_MIN)))

    @staticmethod
    def _norm(content):
        return str(content or '').replace('<sep/>', '$')

    def _group_gate_ok(self, force=False):
        """剧场群聊的发送闸门：返回是否放行（非群聊或 force 时直接放行）。
        用于延迟回复/主动联系/推进投递，让群聊所有发送统一受限流。"""
        if force or not self.is_group:
            return True
        return random.random() <= max(0.0, min(1.0, self.group_reply_probability))

    # ---- 自动投递摘要 / 承诺事实关闭 ----

    def _render_note_to_utterance(self, note, target, now):
        """把一条"给自己的计划/口谕"渲染成一句现在要真正对 target 说出口的原话。
        返回可发送文本；失败或仍是内部口吻则返回 ''（调用方据此抑制，绝不外发原文）。"""
        if not self.llm_call:
            return ''
        tgt = target or self.theater_id
        prompt = (
            "把下面这条【给自己的念头/打算】改写成【现在要对{t}说出口的一句自然的话】，"
            "要符合角色口吻、可直接作为微信消息发送，只输出这句话本身，"
            "不要引号、不要解释、不要任何前后缀、也不要再出现\u300c如果\u2026没回\u300d\u300c要不要\u300d\u300c再问一次\u300d这类内部口吻。\n"
            "对方：{t}\n"
            "你的念头：{n}"
        ).format(t=tgt, n=str(note)[:200])
        try:
            out = _as_text(self.llm_call(prompt)).strip()
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》念头渲染成话失败: {e}')
            return ''
        out = out.strip().strip('"“”\'').strip()
        # 只取一句（去掉换行/多余解释），并限制长度
        out = out.splitlines()[0].strip() if out else ''
        out = out[:120]
        if not out or _looks_like_self_instruction(out):
            return ''
        return out

    def _note_delivered(self, target, content, now):
        """自动回合投递成功后记录摘要（仅自动回合读取，最多6项）。"""
        st = self.state
        ds = st.setdefault('delivered_summary', [])
        ds.append({'ts': now, 'to': target or '', 'brief': str(content or '')[:40]})
        st['delivered_summary'] = ds[-DELIVERED_SUMMARY_MAX:]

    def _close_promise_fact(self, content):
        """承诺兑现后关闭旧 fact，并促使 continuity 提前刷新。"""
        if not content:
            return
        key = str(content)[:12]
        if not key:
            return
        lt = self.state.get('long_term') or []
        new_lt = [x for x in lt if key not in str(x)]
        if len(new_lt) != len(lt):
            self.state['long_term'] = new_lt
            self.state['turns_since_refresh'] = self.continuity_refresh_turns

    # ---- 应用一次写作结果 ----

    def _apply_turn(self, data, user=None, current_message=None, now=None,
                    delivered=None, is_advance=False, reply_probability=1.0, force_reply=False,
                    allow_bubble=False, stage='user', deliver_to=None):
        now = now if now is not None else self._now()
        delivered = delivered if delivered is not None else []
        st = self.state
        c = st['canon']

        elapsed = _as_text(data.get('elapsed_script')).strip()
        if elapsed:
            st['script'].append({'time': now, 'type': 'advance', 'who': user or '', 'content': elapsed, 'visible': False})

        if current_message is not None:
            st['script'].append({'time': now, 'type': 'user', 'who': user or '?', 'content': current_message, 'visible': True})

        # decision：用户回合 / 对话后续 / 承诺到期
        if stage in ('user', 'followup', 'promise_due'):
            decision = data.get('decision') or {}
            mode = decision.get('mode', 'silent' if stage != 'user' else 'reply')
            content = _as_text(decision.get('content')).strip()
            # 稳健解析延迟分钟：模型偶尔把数字字段写成 "10分钟"/"稍后" 等，直接 int() 会抛
            # ValueError 而中断整个回合（导致刚写入的用户消息还没 save 就丢失）。改用单位感知的解析。
            delay_minutes = self._parse_minutes(decision.get('delay_minutes')) or 0
            target = user or deliver_to or st.get('last_user') or ''

            if mode == MODE_REPLY and content:
                if reply_probability < 1.0 and random.random() > reply_probability:
                    # 概率没中：角色最终没把话发出去，保持沉默（故事里体现"想说没说"）
                    st['script'].append({'time': now, 'type': 'advance', 'who': target,
                                         'content': f'你本想回复「{content[:40]}…」，但最后没有发出去，保持了沉默。', 'visible': False})
                else:
                    st['script'].append({'time': now, 'type': 'char', 'who': target, 'content': content, 'visible': True})
                    delivered.append({'target_user': target, 'content': self._norm(content)})
                    if stage != 'user':
                        self._note_delivered(target, content, now)
                        self._close_promise_fact(content)
            elif mode == MODE_DELAYED and stage == 'user':
                if force_reply:
                    # 被 @ 了：必须现在回，不能延迟 → 转成即时回复
                    st['script'].append({'time': now, 'type': 'char', 'who': target, 'content': content, 'visible': True})
                    delivered.append({'target_user': target, 'content': self._norm(content)})
                elif not self._group_gate_ok(force=(reply_probability >= 1.0)):
                    # 群聊闸门没放行：晚点回也不发，记一句"想说没说"
                    st['script'].append({'time': now, 'type': 'advance', 'who': target,
                                         'content': '你本想晚点再回，但最终没说出口。', 'visible': False})
                else:
                    ts = now + max(delay_minutes, 1) * 60
                    st['intents'].append({'type': INTENT_DELAYED_REPLY, 'target_ts': ts, 'target_user': target,
                                          'content': self._norm(content), 'done': False})

        scene = _as_text(data.get('scene_update')).strip()
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
                if itype not in (INTENT_DELAYED_REPLY, INTENT_REMINDER, INTENT_FOLLOWUP, INTENT_PROMISE):
                    continue
                # 群聊闸门：意图排期也受限，避免"提醒/后续"过几分钟就发一条
                if not self._group_gate_ok(force=(reply_probability >= 1.0)):
                    continue
                target_u = (it.get('target_user') or user or st.get('last_user') or '').strip()
                if itype == INTENT_PROMISE:
                    # 承诺回访：每位参与者最多两条待处理
                    pending = [x for x in st['intents']
                               if not x.get('done') and x.get('type') == INTENT_PROMISE and x.get('target_user') == target_u]
                    if len(pending) >= PROMISE_MAX_PER_USER:
                        continue
                minutes = self._parse_minutes(it.get('when')) or 30
                st['intents'].append({'type': itype, 'target_ts': now + minutes * 60,
                                      'target_user': target_u,
                                      'content': str(it.get('content') or '').strip(), 'done': False, 'attempts': 0})

        # Agency Window 状态更新（必须引用真实剧本，受 validMinutes 限制）
        ag = data.get('agency_update')
        if isinstance(ag, dict) and ag.get('activityLoad'):
            load = str(ag.get('activityLoad'))
            priv = str(ag.get('privacy'))
            dev = str(ag.get('deviceAccess'))
            if load in ('free', 'occupied', 'overloaded') and priv in ('private', 'shared', 'public') \
                    and dev in ('available', 'limited', 'unavailable'):
                try:
                    valid = min(max(int(ag.get('validMinutes') or 120), 15), 1440)
                except Exception:
                    valid = 120
                st['agency'] = {'activityLoad': load, 'privacy': priv, 'deviceAccess': dev,
                                'valid_until': now + valid * 60,
                                'basis': str(ag.get('basis') or '')[:80], 'updated_at': now}

        # Alter：累计 + 权重生命周期 + 触发侧端分析
        if self.alter_enabled:
            self._accumulate_alter(data, now)

        # 冒泡：幕间推进时每隔几小时随口说一句（群里同样受闸门限制，私聊不受限）
        if is_advance and allow_bubble:
            bubble = data.get('bubble') if isinstance(data, dict) else None
            if isinstance(bubble, str) and bubble.strip():
                b = bubble.strip()
                # 冒泡同样拦截"给自己的计划/口谕"：能渲染成自然的话就发渲染后的，否则不发
                if _looks_like_self_instruction(b):
                    b = self._render_note_to_utterance(b, self.theater_id, now)
                if b and self._group_gate_ok(False):
                    st['script'].append({'time': now, 'type': 'char', 'who': self.theater_id,
                                         'content': b, 'visible': True})
                    st['last_bubble_ts'] = now
                    delivered.append({'target_user': self.theater_id, 'content': self._norm(b)})
                    self._note_delivered(self.theater_id, b, now)
                else:
                    # 闸门没放行 / 渲染不出：这次不冒，等下一个周期
                    st['last_bubble_ts'] = now

        # 主动联系（Agency Window）：仅独立生活推进回合，且仅私聊剧场
        if stage == 'advance' and self.proactive_enabled and not self.is_group:
            self._handle_proactive(data, user, now, delivered)

        # 裁剪剧本（后台压缩线程会进一步整理成前情摘要）
        if len(st['script']) > self.script_budget + SCRIPT_COMPACT_TRIGGER + 20:
            st['script'] = st['script'][-(self.script_budget + SCRIPT_COMPACT_TRIGGER):]

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

        # 达到动态阈值且尚未在分析中 → 先完成本轮持久化与投递，侧端分析进串行队列
        threshold = self._alter_threshold()
        if abs(m['cumulative']) >= threshold and (now - m.get('last_analysis_ts', 0)) > ALTER_RETRY_SECONDS:
            m['last_analysis_ts'] = now
            direction = '严肃/紧张/正式' if sign > 0 else '轻松/随意/活跃'
            threading.Thread(target=self._run_alter_analysis,
                             args=(direction, now, sign, threshold), daemon=True).start()

    def _run_alter_analysis(self, direction, now, sign, threshold):
        """侧端模型生成 1-2 句情绪偏移描述（串行队列执行，不阻塞本轮可见回复）。"""
        try:
            recent = self.state['script'][-10:]
            recent_text = '\n'.join(f"- {str(it.get('content',''))[:100]}" for it in recent if it.get('content'))
            old_offset = (self.state['mood'].get('description') or '')[:120]
            prompt = (f"你是这部生活剧本的氛围分析师。最近剧情的情绪评分轨迹显示整体氛围在向「{direction}」偏移。"
                      f"请用 1-2 句中文描述这种偏移的具体感受（不出现人名、不要引用原话），只输出 JSON："
                      f"{{\"description\":\"...\"}}。\n最近剧情：\n{recent_text[:800]}"
                      + (f"\n旧氛围参考（可能已过时）：{old_offset}" if old_offset else ''))
            text = self.llm_call(prompt) or ''
            data = self._extract_json(text)
            desc = (data or {}).get('description') or ''
            if not desc:
                return
            with self._op_lock:
                self.state = self._load()
                m = self.state['mood']
                m['description'] = str(desc).strip()
                m['weight'] = 1.0
                m['direction'] = sign
                m['cumulative'] = 0
                self.save()
            logger.info(f'剧场《{self.theater_id}》Alter 情绪偏移已更新。')
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》Alter 分析失败: {e}')

    # ---- Agency Window：主动联系 ----

    def _agency_verdict(self, cand):
        """容量矩阵：日程负荷/隐私/设备。返回 send / recheck。"""
        a = self.state.get('agency') or {}
        load = a.get('activityLoad', 'free')
        priv = a.get('privacy', 'private')
        dev = a.get('deviceAccess', 'available')
        origin = str(cand.get('origin') or '')
        sensitive = bool(cand.get('sensitive'))
        if dev in ('unavailable', 'limited'):
            return 'recheck'
        if load == 'overloaded':
            return 'recheck'
        if sensitive and priv != 'private':
            return 'recheck'
        if load == 'occupied' and origin not in ('promise', 'practical-update'):
            return 'recheck'
        return 'send'

    def _has_pending_check(self, target, origin):
        for it in self.state.get('intents') or []:
            if it.get('done') or it.get('type') != INTENT_PROACTIVE_CHECK:
                continue
            if it.get('target_user') == target and it.get('origin') == origin:
                return True
        return False

    def _handle_proactive(self, data, user, now, delivered):
        """处理主动联系候选：生活产生理由 → 容量矩阵 → 立即/重查/放下。"""
        pro = data.get('proactive_candidate') or {}
        if not isinstance(pro, dict):
            return
        try:
            w = int(pro.get('willingness') or 0)
        except Exception:
            w = 0
        motive = str(pro.get('motive') or '').strip()
        target_p = str(pro.get('target_user') or '').strip()
        origin = str(pro.get('origin') or 'life-event').strip()
        if origin not in ('life-event', 'promise', 'practical-update', 'relationship-follow-up'):
            origin = 'life-event'
        sensitive = bool(pro.get('sensitive'))
        outcome = str(pro.get('outcome') or 'send-now').strip()
        # 生活依据：必须有具体动机；"对方很久没说话"本身不能成为理由
        if not motive or not target_p:
            return
        # 目标合法性：必须是已知参与者（主动联系是私聊行为，群聊走冒泡）
        if target_p not in self.state.get('participants', {}):
            logger.info(f'剧场《{self.theater_id}》主动联系目标 {target_p} 不是已知参与者，跳过。')
            return
        if self._has_pending_check(target_p, origin):
            return  # 去重：同参与者+来源的 pending 候选不重复建
        st = self.state
        last_ts = st.get('last_proactive', {}).get(target_p, 0)
        verdict = self._agency_verdict(pro)
        if outcome == 'send-now' and verdict == 'send':
            if w < self.proactive_threshold:
                return
            # 最小间隔限流（承诺/实际安排可绕过）
            if origin not in ('promise', 'practical-update') and now - last_ts < self.min_proactive_interval_seconds:
                outcome = 'recheck-later'
            else:
                st['last_proactive'][target_p] = now
                st['script'].append({'time': now, 'type': 'advance', 'who': '',
                                     'content': f'（你主动联系了 {target_p}：{motive[:60]}）', 'visible': False})
                delivered.append({'target_user': target_p, 'content': self._norm(pro.get('content') or motive)})
                self._note_delivered(target_p, str(pro.get('content') or motive), now)
                return
        if outcome == 'recheck-later' or (outcome == 'send-now' and verdict == 'recheck'):
            minutes = self._parse_minutes(pro.get('notBefore')) or 30
            st['intents'].append({'type': INTENT_PROACTIVE_CHECK, 'target_ts': now + minutes * 60,
                                  'target_user': target_p, 'origin': origin, 'motive': motive[:120],
                                  'sensitive': sensitive, 'content': '', 'done': False, 'attempts': 0})
        # let-go：自然放下，不发送

    # ---- 主入口 ----

    def handle_user_message(self, user, message, image_paths=None, drafts=None, reply_probability=1.0, force_reply=False, group_recent=None):
        """处理一条用户消息，返回要发送的文本；返回 None 表示沉默/延迟。
        reply_probability(0~1)：控制"模型决定回复"后实际发送的概率（用于群聊降噪）。
        force_reply：被 @ 了，必须给出可见回复，禁止沉默/延迟。
        group_recent：被 @ 时回看的最近群聊记录文本（含图片识别结果）。"""
        with self._op_lock:
            # 先重新读取磁盘上的剧场状态，尊重网页端「剧场记忆」编辑，避免被内存旧状态覆盖
            self.state = self._load()
            return self._do_user_message(user, message, image_paths, drafts, reply_probability, force_reply, group_recent)

    def _do_user_message(self, user, message, image_paths=None, drafts=None, reply_probability=1.0, force_reply=False, group_recent=None):
        now = self._now()
        p = self.ensure_participant(user)
        p['last_seen_ts'] = now
        st = self.state
        st['last_user'] = user
        st['last_user_ts'] = now
        st['followup_done'] = {}       # 新消息重置对话后续：从现在重新计时

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
        prompt = self._build_prompt(stage='user', current_event=current_event, drafts=drafts,
                                    refresh_continuity=refresh, force_reply=force_reply,
                                    group_recent=group_recent)
        try:
            data = self._llm_json(prompt)
        except Exception as e:
            logger.error(f'剧场 {self.theater_id} 叙事写作失败: {e}')
            # 兜底：模型空内容/格式失败时，改用一个简短的"自然回复"请求，避免干沉默
            fallback = self._fallback_reply(user, message, now)
            return fallback

        delivered = self._apply_turn(data, user=user, current_message=message, now=now,
                                     reply_probability=reply_probability, force_reply=force_reply, stage='user')
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

    # ---- 群聊意愿层（纯算法，内存不落盘） ----

    def observe_group_message(self, sender, content, now=None):
        """喂一条未 @ 的群消息给意愿层；返回是否值得触发一次剧场写作。"""
        if not self.is_group:
            return False
        gw = self.group_will
        if gw is None:
            return False
        try:
            gw.observe(content, now)
        except Exception:
            return False
        hit = gw.ready(now)
        if hit:
            logger.info(f'剧场《{self.theater_id}》群聊意愿触发（分数 {gw.score:.1f} ≥ 阈值 {gw.threshold:.0f}）。')
        return hit

    def consume_group_will(self):
        """主角成功发言后消耗群聊意愿。"""
        gw = getattr(self, 'group_will', None)
        if gw is not None:
            try:
                gw.consume()
            except Exception:
                pass

    # ---- 自动推进（幕间生活） ----

    def _run_auto_narrator(self, stage, current_event, now, allow_bubble=False,
                           want_proactive=False, window_start=None, deliver_to=None):
        """自动回合统一入口：时间导演出账本 → 主叙事渲染 → 应用结果。"""
        timeline = None
        if self.time_director_enabled:
            ws = window_start
            if ws is None:
                ws = max(self.state.get('last_advance', 0), self.state.get('last_user_ts', 0))
            timeline = self._time_director(ws, now)
        refresh = self.state.get('turns_since_refresh', 0) >= self.continuity_refresh_turns
        prompt = self._build_prompt(stage=stage, current_event=current_event, timeline=timeline,
                                    refresh_continuity=refresh, allow_bubble=allow_bubble,
                                    want_proactive=want_proactive)
        data = self._llm_json(prompt)   # 失败抛出，由调用方决定重试时机
        rp = self.group_reply_probability if self.is_group else 1.0
        return self._apply_turn(data, user=None, current_message=None, now=now,
                                is_advance=True, allow_bubble=allow_bubble,
                                reply_probability=rp, stage=stage, deliver_to=deliver_to)

    def advance(self, quiet_fn=None):
        """幕间自动推进（四阶段：到期意图 → 对话后续 → 独立生活推进 → 后台维护）。
        返回待投递消息列表 [{target_user, content}]。"""
        with self._op_lock:
            # 先重读磁盘，尊重网页端编辑
            self.state = self._load()
            return self._do_advance(quiet_fn)

    def _do_advance(self, quiet_fn=None):
        now = self._now()
        delivered = []
        quiet = bool(self.respect_quiet and quiet_fn and quiet_fn())

        # 1) 到期意图：投递。reminder/followup 常存的是"给自己的计划"，需先渲染成真正可发的话，
        #    绝不把内部口吻（如"如果主人没回就再问一次…"）原样发给对方。
        if not quiet:
            for it in self.state['intents']:
                if it.get('done') or it.get('type') not in DIRECT_INTENT_TYPES:
                    continue
                if it.get('target_ts', 0) <= now and it.get('content'):
                    tgt = it.get('target_user') or ''
                    note = str(it['content'])
                    if _looks_like_self_instruction(note):
                        msg = self._render_note_to_utterance(note, tgt, now)
                        if not msg:
                            # 渲染不出自然的话 → 抑制，只记一笔内部旁白，不外发原始指令
                            it['done'] = True
                            self.state['script'].append({
                                'time': now, 'type': 'advance', 'who': tgt or self.theater_id,
                                'content': f'（本来想搭句话过去，但没找到合适的说法，就先没开口。）', 'visible': False})
                            logger.info(f'剧场《{self.theater_id}》到期意图为内部口吻，已抑制不外发')
                            continue
                        it['content'] = msg  # 用渲染后的真实话术替换，避免下次再命中
                    it['done'] = True
                    delivered.append({'target_user': tgt, 'content': self._norm(it['content'])})
                    self._note_delivered(tgt, it['content'], now)
                    self._close_promise_fact(note)
            if delivered:
                self.save()

            # 2) 承诺到期裁决（最多两个/轮；没有可见结果则保留延后，不静默完成）
            delivered += self._due_promise_round(now)

            # 3) 主动联系重查（最多一个/轮）
            if self.proactive_enabled:
                delivered += self._proactive_recheck_round(now)

            # 4) 对话后续：约 10 / 20 分钟各补写一次对话余波
            delivered += self._followup_check(now)

        # 5) 独立生活推进（含 Preplan 锚点）
        if not quiet:
            delivered += self._life_advance(now)

        # 6) 后台维护：Preplan 每日审查 / 场景压缩（不发送消息，安静时段也可做）
        self._maybe_preplan_review(now)
        self._maybe_compact(now)
        return delivered

    def _due_promise_round(self, now):
        """承诺到期：触发一次主叙事裁决，可见兑现或显式延后。"""
        st = self.state
        due = [it for it in st['intents']
               if not it.get('done') and it.get('type') == INTENT_PROMISE and it.get('target_ts', 0) <= now]
        out = []
        for it in due[:2]:
            target = it.get('target_user') or st.get('last_user') or ''
            content = it.get('content') or ''
            event = (f"[{time.strftime('%m-%d %H:%M', time.localtime(now))}] 承诺到期："
                     f"你曾对 {target} 说出口的承诺「{content[:80]}」现在到期了。")
            try:
                dv = self._run_auto_narrator('promise_due', event, now, deliver_to=target)
            except Exception as e:
                logger.warning(f'剧场《{self.theater_id}》承诺到期写作失败: {e}')
                break   # 模型失败：保留 intent，下轮再试
            fulfilled = any(d.get('target_user') == target and d.get('content') for d in dv)
            if fulfilled:
                it['done'] = True
            else:
                attempts = int(it.get('attempts', 0) or 0) + 1
                it['attempts'] = attempts
                if attempts >= PROMISE_MAX_ATTEMPTS:
                    it['done'] = True
                    st['script'].append({'time': now, 'type': 'advance', 'who': target,
                                         'content': f'（你没能兑现对 {target} 说过的「{content[:40]}」，这件事在心里留下了疙瘩。）',
                                         'visible': False})
                else:
                    it['target_ts'] = now + PROMISE_RETRY_SECONDS   # 保留并延后，不静默完成
            out.extend(dv)
            self.save()
        return out

    def _proactive_recheck_round(self, now):
        """proactive-check 到期：重新读取当前生活与 Agency，重新裁决（不使用预写消息）。"""
        st = self.state
        due = [it for it in st['intents']
               if not it.get('done') and it.get('type') == INTENT_PROACTIVE_CHECK and it.get('target_ts', 0) <= now]
        if not due:
            return []
        out = []
        for it in due[:1]:
            it['done'] = True   # 本次重查结束；结果可能是新的 check
            target = it.get('target_user') or ''
            motive = it.get('motive') or ''
            origin = it.get('origin') or 'life-event'
            event = (f"[{time.strftime('%m-%d %H:%M', time.localtime(now))}] 主动联系重查："
                     f"此前生活给了你联系 {target} 的动机「{motive[:80]}」（来源：{origin}）。"
                     f"结合当前生活与行动条件重新裁决。")
            try:
                dv = self._run_auto_narrator('proactive_recheck', event, now, want_proactive=True)
                out.extend(dv)
            except Exception as e:
                logger.warning(f'剧场《{self.theater_id}》主动重查写作失败: {e}')
            self.save()
        return out

    def _followup_check(self, now):
        """对话后续：对话结束后约 10/20 分钟各一次短期补写，之后按常规间隔生活推进。"""
        st = self.state
        last_user_ts = st.get('last_user_ts', 0)
        if not last_user_ts:
            return []
        fd = st.setdefault('followup_done', {})
        out = []
        for i, minutes in enumerate(self.followup_minutes):
            key = str(i)
            if fd.get(key):
                continue
            elapsed = now - last_user_ts
            if elapsed < minutes * 60:
                continue
            if elapsed > minutes * 60 + FOLLOWUP_STALE_HOURS * 3600:
                fd[key] = True   # 太久：过期不补
                continue
            fd[key] = True
            ws = max(last_user_ts, st.get('last_followup_ts', 0))
            target = st.get('last_user') or ''
            event = (f"[{time.strftime('%m-%d %H:%M', time.localtime(now))}] 对话后续：与 {target} 的对话"
                     f"结束于 {time.strftime('%H:%M', time.localtime(last_user_ts))}。补写对话余波。")
            try:
                dv = self._run_auto_narrator('followup', event, now, window_start=ws, deliver_to=target)
                out.extend(dv)
            except Exception as e:
                logger.warning(f'剧场《{self.theater_id}》对话后续写作失败: {e}')
            st['last_followup_ts'] = now
            self.save()
            break   # 每轮最多补写一次，下一轮继续
        return out

    def _life_advance(self, now):
        """独立生活推进：常规间隔 + Preplan fixed 锚点（避免推进跨过到校/放学等节点）。"""
        st = self.state
        interval = max(60, int(getattr(self, 'advance_interval_seconds', 20 * 60) or 20 * 60))
        last = st.get('last_advance', 0)
        trigger = (now - last) >= interval
        # 锚点：常规推进会跨过未来 fixed 边界时，提前到边界前写一段。
        # 用 last_anchor_used 去重：同一锚点只触发一次，否则在锚点前 5 分钟窗口内
        # 每个 advance tick（约 60s 一轮）都会重复触发，5 分钟内写约 5 段重复剧本并多烧 LLM。
        anchor = self._next_preplan_anchor(now)
        if anchor and last + interval > anchor and now >= anchor - 300 \
                and st.get('last_anchor_used') != anchor:
            trigger = True
            st['last_anchor_used'] = anchor
        if not trigger:
            return []
        bubble_interval = max(0, int(getattr(self, 'bubble_interval_hours', 3) or 0) * 3600)
        allow_bubble = bubble_interval > 0 and (now - st.get('last_bubble_ts', 0)) >= bubble_interval
        try:
            dv = self._run_auto_narrator('advance', '独立生活推进（无用户消息）', now,
                                         allow_bubble=allow_bubble, want_proactive=self.proactive_enabled)
        except Exception as e:
            logger.warning(f'剧场 {self.theater_id} 幕间推进失败: {e}')
            st['last_advance'] = now
            self.save()
            return []
        st['last_advance'] = now
        self.save()
        return [d for d in dv if d.get('content')]

    # ---- 场景压缩（后台空闲整理） ----

    def _maybe_compact(self, now):
        if not (self.compact_enabled and self.llm_call):
            return
        if len(self.state.get('script') or []) <= self.script_budget + SCRIPT_COMPACT_TRIGGER:
            return
        if self._compact_running:
            return
        self._compact_running = True
        threading.Thread(target=self._compact_safe, daemon=True).start()

    def _compact_safe(self):
        try:
            with self._op_lock:
                self.state = self._load()
                self._compact_script()
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》场景压缩异常: {e}')
        finally:
            self._compact_running = False

    def _compact_script(self):
        """把较旧剧本压缩成前情摘要 + 在场表，同时分档合并 Overlay。
        保留因果、承诺、大事件与关系变化，减少重复性叙述。"""
        st = self.state
        items = st.get('script') or []
        keep = max(10, self.script_budget)
        if len(items) <= keep + 20:
            return
        old = items[:-keep]
        recent = items[-keep:]
        try:
            old_lines = []
            for it in old:
                t = it.get('type')
                ts = it.get('time', 0)
                local = time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else '?'
                c = str(it.get('content') or '')[:100]
                if t == 'user':
                    old_lines.append(f"[{local}] {it.get('who','')}：{c}")
                elif t == 'char':
                    old_lines.append(f"[{local}] 你：{c}")
                elif t == 'advance':
                    old_lines.append(f"[{local}] (幕间) {c}")
                elif t == 'scene':
                    old_lines.append(f"[{local}] (场景) {c}")
            old_text = '\n'.join(old_lines)[-4000:]
            ov = st.get('overlays') or []
            ov_text = json.dumps(ov[-15:], ensure_ascii=False) if ov else '（无）'
            prompt = f"""你是剧本整理员。请把下面这段较早的剧本压缩成"前情摘要"，并顺带整理配角在场状态与 Overlay。
要求：保留因果、承诺、大事件与关系变化；删除重复叙述；摘要用中文一段话（200字内）。

较早剧本：
{old_text}

当前 Overlay（最多保留合并后的精华）：
{ov_text}

只输出一个 JSON 对象：
{{
  "summary": "前情摘要一段话",
  "presence": [{{"name": "配角名", "status": "present|left|joining", "note": "一句话"}}],
  "overlay_merged": [{{"layer": "character|world|relationship", "target": "目标", "description": "合并后的稳定变化"}}]
}}
规则：presence 最多8项且必须来自剧本真实条目；overlay_merged 是对旧 Overlay 的分档合并（保留有证据的，合并重复的）。只输出 JSON。"""
            data = self._llm_json(prompt)
            summary = str((data or {}).get('summary') or '').strip()
            if summary:
                st.setdefault('story_summary', []).append({'ts': self._now(), 'text': summary, 'count': len(old)})
                st['story_summary'] = st['story_summary'][-STORY_SUMMARY_MAX:]
            pres = (data or {}).get('presence')
            if isinstance(pres, list) and pres:
                clean = []
                for x in pres[:PRESENCE_MAX]:
                    if isinstance(x, dict) and x.get('name') and x.get('status') in ('present', 'left', 'joining'):
                        clean.append({'name': str(x['name'])[:20], 'status': x['status'],
                                      'note': str(x.get('note') or '')[:40]})
                st['presence'] = clean
                ovmerged = (data or {}).get('overlay_merged')
                if isinstance(ovmerged, list) and ovmerged:
                    fresh = ov[-10:]
                    merged = []
                    for x in ovmerged[:15]:
                        if isinstance(x, dict) and x.get('layer') in ('character', 'world', 'relationship') and x.get('description'):
                            merged.append({'layer': x['layer'], 'target': str(x.get('target') or '')[:20],
                                           'description': str(x['description'])[:80], 'ts': self._now()})
                    # 去重：LLM 的 merged 已覆盖这些 layer/target，避免把同一条 raw 再塞回去造成重复
                    merged_keys = {(o['layer'], o['target']) for o in merged}
                    kept_fresh = [o for o in fresh
                                  if (o.get('layer'), str(o.get('target') or '')) not in merged_keys]
                    st['overlays'] = (merged + kept_fresh)[-30:]
            st['script'] = recent
            self.save()
            logger.info(f'剧场《{self.theater_id}》已压缩 {len(old)} 条旧剧本为前情摘要。')
        except Exception as e:
            logger.warning(f'剧场《{self.theater_id}》场景压缩失败: {e}')

    # ---- 工具 ----

    def status(self):
        c = self.state['canon']
        pp = self.state.get('preplan') or {}
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
            'story_summary': len(self.state.get('story_summary') or []),
            'presence': (self.state.get('presence') or [])[:PRESENCE_MAX],
            'delivered_summary': self.state.get('delivered_summary') or [],
            'agency': self.state.get('agency') or {},
            'preplan': {'version': pp.get('version', 0), 'reviewed_date': pp.get('reviewed_date', ''),
                        'regimes': len(pp.get('regimes') or []), 'exceptions': len(pp.get('exceptions') or [])},
            'group_will_score': round(self.group_will.score, 2) if self.group_will else 0.0,
        }
