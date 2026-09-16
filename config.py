# -*- coding: utf-8 -*-

# ***********************************************************************
# Modified based on the KouriChat project
# Copyright of this modification: Copyright (C) 2025, iwyxdxl
# Licensed under GNU GPL-3.0 or higher, see the LICENSE file for details.
# 
# This file is part of WeChatBot, which includes modifications to the KouriChat project.
# The original KouriChat project's copyright and license information are preserved in the LICENSE file.
# For any further details regarding the license, please refer to the LICENSE file.
# ***********************************************************************

# 用户列表(请配置要和bot说话的账号的微信昵称/群名！)
# 格式：[微信昵称/群名, Prompt文件, 是否开启"剧场/全局阅读"模式(可省略，默认False)]
# 开启剧场模式后：该聊天（尤其是群聊）的消息会被全局读取并进入剧场叙事，AI 像"演员"一样
#   参与对话、在幕间生活、主动联系；回复时根据最近 THEATER_RECENT_CONTEXT 条聊天记录自然接话。
# 未开启的群聊仍按原来的 @机器人/关键词/概率 触发回复。
# 例如：LISTEN_LIST = [['微信名1', '角色1', True], ['群名', '角色1'], ['微信名2', '角色2', True]]
LISTEN_LIST = []  # 格式: [[微信昵称/群名, Prompt文件名, 是否开启], ...]
# 机器人自己的微信昵称（用于群聊@识别、自消息过滤）。
# 留空则尝试从 wxauto 登录窗口自动获取；wxauto 不可用且留空时，群聊@触发可能不生效。
# 建议填上登录微信的昵称，保证"被 @ 必回"稳定生效。
BOT_NICKNAME = ''

# DeepSeek API 配置
DEEPSEEK_API_KEY = ''
# 硅基流动API注册地址，免费15元额度 https://cloud.siliconflow.cn/
DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
# 硅基流动API的模型
MODEL = 'deepseek-v4-flash-vision-exp'
# 用户和AI对话轮数
MAX_GROUPS = 25

# 如果要使用官方的API
# DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
# 官方API的V3模型
# MODEL = 'deepseek-chat'

# 回复最大token
MAX_TOKEN = 2000
# DeepSeek温度
TEMPERATURE = 1.1

# Moonshot AI配置（用于图片和表情包识别）
# API申请https://platform.moonshot.cn/
MOONSHOT_API_KEY = ''
MOONSHOT_BASE_URL = 'https://vg.v1api.cc/v1'
MOONSHOT_MODEL = 'gpt-5.2'
MOONSHOT_TEMPERATURE = 0.8
# 是否让图片识别与Chat模型共用同一个API
# 开启后，图片识别将使用 DeepSeek/Chat 模型 的 base_url/api_key/model（即 DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY/MODEL）
ENABLE_IMAGE_USE_CHAT_API = True
# 是否直接让 Chat 模型原生看图（多模态直喂）
# 开启后，收到图片会把原图直接发给 Chat 模型，由模型看图并回答，免去"先识别成文字描述"的中转。
# 注意：需要 Chat 模型（MODEL）支持图片输入，且与 ENABLE_IMAGE_USE_CHAT_API 配合使用。
ENABLE_IMAGE_NATIVE_MULTIMODAL = True
ENABLE_IMAGE_RECOGNITION = True
ENABLE_EMOJI_RECOGNITION = True

# 消息队列等待时间
QUEUE_WAITING_TIME = 1

# 表情包存放目录
EMOJI_DIR = 'emojis'
ENABLE_EMOJI_SENDING = False
EMOJI_SENDING_PROBABILITY = 18

# 自动消息配置
AUTO_MESSAGE = '请你模拟系统设置的角色，在微信上找对方继续刚刚的话题或者询问对方在做什么'
ENABLE_AUTO_MESSAGE = True
# 等待时间
MIN_COUNTDOWN_HOURS = 3.0
MAX_COUNTDOWN_HOURS = 9.1
# 消息发送时间限制
QUIET_TIME_START = '22:00'
QUIET_TIME_END = '8:00'
# 不对群聊发送自动消息
IGNORE_GROUP_CHAT_FOR_AUTO_MESSAGE = False

# 消息回复时间间隔
# 间隔时间 = 字数 * (平均时间 + 随机时间)
AVERAGE_TYPING_SPEED = 0.01
RANDOM_TYPING_SPEED_MIN = 0.01
RANDOM_TYPING_SPEED_MAX = 0.03
SEPARATE_ROW_SYMBOLS = True
# 每段回复发送前的"打字/思考"等待时间范围（秒）
TYPING_DELAY_MIN = 0.2
TYPING_DELAY_MAX = 0.5

# 记忆功能
# 采用综合评分公式：0.6*重要度 - 0.4*(存在时间小时数)
# 示例：
# 重要度5的旧记忆（存在12小时）得分：0.65 - 0.412 = 3 - 4.8 = -1.8
# 重要度4的新记忆（存在1小时）得分：0.64 - 0.41 = 2.4 - 0.4 = 2.0 → 保留新记忆
ENABLE_MEMORY = True
MEMORY_TEMP_DIR = 'Memory_Temp'
MAX_MESSAGE_LOG_ENTRIES = 30
MAX_MEMORY_NUMBER = 50
UPLOAD_MEMORY_TO_AI = True
# 记忆存储方式：True = 保存到单独的JSON文件，False = 保存到prompt文件中
SAVE_MEMORY_TO_SEPARATE_FILE = True
CORE_MEMORY_DIR = 'CoreMemory'

# 是否接收全部群聊消息
ACCEPT_ALL_GROUP_CHAT_MESSAGES = False
ENABLE_GROUP_AT_REPLY = True
ENABLE_GROUP_KEYWORD_REPLY = True
GROUP_KEYWORD_LIST = []
# 群聊回复概率（%）：命中 @机器人 或关键词后按此概率回复。调低=群聊更安静。
GROUP_CHAT_RESPONSE_PROBABILITY = 1
# 关键词回复是否忽略概率（True=命中关键词必回，不受上面概率影响）
GROUP_KEYWORD_REPLY_IGNORE_PROBABILITY = True

# 登录配置编辑器设置
ENABLE_LOGIN_PASSWORD = False
LOGIN_PASSWORD = '123456'
PORT = 5000

# 文字指令识别开关
# 开启后，私聊/群聊（满足触发条件）中以“/”开头的指令将被解析并执行
ENABLE_TEXT_COMMANDS = True

# 定时器/提醒设置
# 启用提醒功能
ENABLE_REMINDERS = True
# 是否允许在安静时间内发送提醒 (True/False)
# 如果设置为 False，则在安静时间内安排的提醒将被跳过。
ALLOW_REMINDERS_IN_QUIET_TIME = True
# 是否使用语音通话进行提醒
# 群聊无法使用语音通话进行提醒
USE_VOICE_CALL_FOR_REMINDERS = True

# 联网API配置
ENABLE_ONLINE_API = True
ONLINE_BASE_URL = 'https://vg.v1api.cc/v1'
ONLINE_MODEL = 'deepseek-r1-searching'
ONLINE_API_KEY = ''
ONLINE_API_TEMPERATURE = 0.7
ONLINE_API_MAX_TOKEN = 2000
SEARCH_DETECTION_PROMPT = '搜索用户的词句中是否含梗。是否需要查询今天的天气、最新的新闻事件、梗、特定网站的内容、·股票价格、特定人物的最新动态等'
ONLINE_FIXED_PROMPT = ''

# 是否启用自动抓取消息中URL链接内容的功能
ENABLE_URL_FETCHING = True
# 网络请求超时时间 (秒)
REQUESTS_TIMEOUT = 10
# 抓取网页时使用的 User-Agent，模拟浏览器防止被屏蔽
# REQUESTS_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
# REQUESTS_USER_AGENT = 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
REQUESTS_USER_AGENT = 'Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Mobile Safari/537.36'
# 从网页提取内容的最大字符数，防止上下文过长，影响AI处理效率和成本
MAX_WEB_CONTENT_LENGTH = 2000

# 定时重启配置
ENABLE_SCHEDULED_RESTART = True
RESTART_INTERVAL_HOURS = 2.0
RESTART_INACTIVITY_MINUTES = 15

# 强制移除括号当中的内容
REMOVE_PARENTHESES = False

# 是否使用辅助模型
ENABLE_ASSISTANT_MODEL = False
ASSISTANT_BASE_URL = 'https://vg.v1api.cc/v1'
ASSISTANT_MODEL = 'gpt-4o-mini'
ASSISTANT_API_KEY = ''
ASSISTANT_TEMPERATURE = 0.3
ASSISTANT_MAX_TOKEN = 1000
USE_ASSISTANT_FOR_MEMORY_SUMMARY = True

# 敏感词处理配置
# 开启后遇到敏感词时自动清除Memory_Temp文件和聊天上下文
ENABLE_SENSITIVE_CONTENT_CLEARING = True

# ===== 复读机 =====
# 支持多群，指定在某个群里复读某人的文字/表情/图片/视频
# 格式: [{"group": "群名1", "target": "某人1"}, {"group": "群名2", "target": "某人2"}]
REPEATER_LIST = []

# ===== 今日小猪 =====
# 开启后，发送"今日小猪/抽小猪/我的小猪/rollpig"可抽取每日专属小猪（每人每天一次）
# "小猪列表"查看全部小猪；"小猪图鉴/我的猪圈"查看已解锁的小猪
ENABLE_ROLLPIG = True

# ===== 崩坏星穹铁道 =====
# 开启后支持"星铁体力/星铁收入/星铁抽卡"等指令（米游社查询 + 模拟抽卡）
ENABLE_STARRAIL = True

# ===== 群聊主动回复概率 =====
# 与"群聊回复概率"完全独立：未@、未命中关键词时，按此概率主动回复群友消息
# 复读机和消息轰炸不受此概率影响
GROUP_PROACTIVE_REPLY_PROBABILITY = 1

# 论坛自定义模型配置（可选）
ENABLE_FORUM_CUSTOM_MODEL = False
FORUM_BASE_URL = 'https://vg.v1api.cc/v1'
FORUM_MODEL = 'deepseek-ai/DeepSeek-V3'
FORUM_API_KEY = ''
FORUM_TEMPERATURE = 1.0
FORUM_MAX_TOKEN = 1200

# ===== 自动回拍 =====
# 开启后，别人拍一拍你时机器人自动拍回去
ENABLE_AUTO_PAT_BACK = True
# 我的拍一拍：填写你设置的个人拍一拍文字。收到system消息完整包含此文字时自动回拍。
MY_PAT_PHRASE = ''

# ===== 消息轰炸 =====
# 定时向指定用户发送消息/文件（间隔 0.1 ~ 6000 秒）
ENABLE_BOMBING = False
BOMBING_USER = ''
BOMBING_INTERVAL = 0.0
BOMBING_CONTENT = ''
BOMBING_FILE = ''

# ===== 本地数据监听（UserDataIsSafeFromUsers / WeFlow HTTP API） =====
# 底层监听改为轮询微信4.x本地数据库读取服务提供的 HTTP API（127.0.0.1:5031），
# 取代原 wxauto 的窗口监听。发送消息仍由 wxauto 完成。
# 前置条件：先运行 UserDataIsSafeFromUsers 应用（它会提供该 HTTP API）。
LOCALDB_API_BASE = 'http://127.0.0.1:5031'
# 该服务若设置了 access_token，则在此填写（没有就留空）
# 注意：WeFlow 的 HTTP API 必须设置访问令牌才可用（设置→HTTP API→访问令牌），
# 需要与 WeFlow-config.json 里的 httpApiToken 保持一致。
LOCALDB_API_TOKEN = ''
# 轮询间隔（秒）
LOCALDB_POLL_INTERVAL = 2.0
# 只处理最近多少秒内的新消息（防止启动时回灌历史记录）
LOCALDB_ONLY_RECENT_SECONDS = 15.0

# ===== 剧场系统（参考 HDS-Interlude 的叙事驱动思路）=====
# 让 AI 从"对话者"变成"演员"：每个角色维护一部持续上演的生活剧本（剧场），
# 每次对话补写幕间经历、决定回复/沉默/延迟，并自动推进剧情、主动联系。
ENABLE_THEATER = True
# 剧本数据存放目录
THEATER_DIR = 'Theater'
# 自动推进间隔（分钟）：没有消息时，角色也在"幕间生活"
THEATER_ADVANCE_INTERVAL_MINUTES = 50
# 主动联系意愿阈值（0-100），达到该值角色才会主动发消息
THEATER_PROACTIVE_THRESHOLD = 60
# 是否尊重安静时间（22:00-8:00 不自动推进、不主动联系、不投递延迟消息）
THEATER_RESPECT_QUIET_TIME = False
# 剧本上下文保留条数（每次写作喂给模型的近期剧本长度）
THEATER_SCRIPT_BUDGET = 100
# 回复时参考的"最近聊天记录"条数：剧场模式下，回复前把该聊天最近 N 条真实对话
# 单独高亮给模型，让它根据最近的上下文自然接话。
THEATER_RECENT_CONTEXT = 30
# Alter 心情惯性：每回合氛围净变化累计超过阈值后，生成"情绪偏移描述"注入后续回合
THEATER_ALTER_ENABLED = True
THEATER_ALTER_BASE_THRESHOLD = 10
# 主动联系最小间隔（分钟）：同一个人短时间内不被反复主动联系
THEATER_MIN_PROACTIVE_INTERVAL_MINUTES = 30
# 连续多少次对话/推进后刷新一次"当前状态摘要"（低频 continuity）
THEATER_CONTINUITY_REFRESH_TURNS = 5
# 剧情余波（active consequence）每回合自然衰减量（0~1）
THEATER_ACTIVE_CONSEQUENCE_DECAY = 0.1
# 剧场模式的群聊回复概率（%）：即使模型决定回复，也按此概率实际发送，用于群聊降噪
THEATER_GROUP_REPLY_PROBABILITY = 1
# 剧场叙事使用的模型。剧场写作是纯文本/JSON，用视觉模型(如 deepseek-v4-flash-vision-exp)会偶发
# 返回空内容/输出"ext"被过滤。填 'deepseek-chat' 这类稳定的文本模型可彻底避免；留空则用主模型 MODEL。
THEATER_MODEL = 'deepseek-chat'
# 剧场"主动冒泡"：去掉随时主动联系，改为每隔多少小时在群里随口冒个泡（0=关闭）
THEATER_BUBBLE_INTERVAL_HOURS = 9
# 剧场群聊被@时，回看最近多少条群聊记录（含图片识图）作为上下文
THEATER_RECENT_LOOKBACK = 15

# ===== 剧场系统：HDS-Interlude 0.1.4 对齐项 =====
# 时间导演（Time Director）：自动推进/对话后续/到期意图前，先由轻量模型生成 1-4 个相对时间
# 节拍（事件账本）并校验时间窗口，主叙事只渲染账本——防止自动推进越出当前时钟或自我复读。
THEATER_TIME_DIRECTOR_ENABLED = False
# 对话后续（conversation follow-up）：对话结束后约 N 分钟各补写一次"对话余波"
THEATER_FOLLOWUP_MINUTES_1 = 10
THEATER_FOLLOWUP_MINUTES_2 = 20
# Schedule Preplan（近期日程层）：后台每日一次轻量审查，保存周规律+日期例外；
# 主叙事只读取未来约 12 小时（最多八项，标记"计划非事实"）。
THEATER_PREPLAN_ENABLED = False
# 每天本地时间几点之后做日程审查（默认 8 点后第一个空闲机会）
THEATER_PREPLAN_REVIEW_HOUR = 8
# 日程颗粒度：stable=只接受稳定周规律 | contextual=允许阶段/日期例外 | granular=额外允许柔性候选
THEATER_PREPLAN_GRANULARITY = 'stable'
# 自动推进锚点：未来 12 小时内 fixed 日程块的开始/结束可作为推进锚点，
# 避免一次推进跨过到校/放学/补课结束等关键节点。
THEATER_PREPLAN_ANCHOR_ADVANCE = False
# Agency Window 主动联系：生活产生真实理由→检查日程/隐私/设备→立即/稍后重查/自然放下。
# 默认关闭（保持"只冒泡不主动"）；开启后仅私聊剧场生效，群聊仍走冒泡。
THEATER_PROACTIVE_ENABLED = False
# 场景压缩：较旧剧本后台压缩成"前情摘要+在场表"，保留因果/承诺/大事件与关系变化
# 开启后：剧本超阈值时不再直接丢弃旧条目，而是先由后台线程压缩进 story_summary（长期记忆滚动保留）。
THEATER_COMPACT_ENABLED = True
# 群聊意愿层（纯算法，不调模型不写库）：未@消息累积本地分数（边际递减+半衰期衰减），
# 超过阈值后按概率触发一次剧场写作；发言成功消耗意愿。@机器人始终直接处理。
THEATER_GROUP_WILLINGNESS_ENABLED = False
# 群聊意愿触发阈值（分数）
THEATER_GROUP_WILLINGNESS_THRESHOLD = 8.0
# 群聊意愿半衰期（分钟）
THEATER_GROUP_WILLINGNESS_HALF_LIFE_MINUTES = 30

# ===== 密钥/令牌的环境变量覆盖（强烈建议生产环境使用）=====
# 上面的 API Key / Token / 密码为开箱即用而内置成明文，存在泄露风险。
# 安全建议：
#   1) 立即在对应平台轮换（吊销并重新生成）所有已暴露的密钥；
#   2) 不要将本文件提交到公共仓库（加入 .gitignore）；
#   3) 用下面的环境变量在运行时覆盖明文默认值，避免密钥入库。
# 行为：设置了对应环境变量则优先用环境变量；未设置则回退到上面的内置默认值。
# 注意：网页配置编辑器读取/修改的仍是上面的字面量默认值，环境变量只在 bot 运行时覆盖。
import os as _os
_ENV_OVERRIDES = {
    'DEEPSEEK_API_KEY': 'WXBOT_DEEPSEEK_API_KEY',
    'DEEPSEEK_BASE_URL': 'WXBOT_DEEPSEEK_BASE_URL',
    'MOONSHOT_API_KEY': 'WXBOT_MOONSHOT_API_KEY',
    'ONLINE_API_KEY': 'WXBOT_ONLINE_API_KEY',
    'ASSISTANT_API_KEY': 'WXBOT_ASSISTANT_API_KEY',
    'LOCALDB_API_TOKEN': 'WXBOT_LOCALDB_API_TOKEN',
    'LOGIN_PASSWORD': 'WXBOT_LOGIN_PASSWORD',
}
for _key, _env in _ENV_OVERRIDES.items():
    _val = _os.environ.get(_env)
    if _val:
        globals()[_key] = _val


