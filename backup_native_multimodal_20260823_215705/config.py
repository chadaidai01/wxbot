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

# 用户列表(请配置要和bot说话的账号的微信昵称！)
# 例如：LISTEN_LIST = [['微信名1', '角色1'],['微信名2', '角色2']]
LISTEN_LIST = []  # 格式: [[微信昵称/群名, Prompt文件名, 是否开启], ...]
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
ENABLE_IMAGE_RECOGNITION = True
ENABLE_EMOJI_RECOGNITION = True

# 消息队列等待时间
QUEUE_WAITING_TIME = 2

# 表情包存放目录
EMOJI_DIR = 'emojis'
ENABLE_EMOJI_SENDING = True
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
GROUP_KEYWORD_LIST = ['@群搬史小助手']
GROUP_CHAT_RESPONSE_PROBABILITY = 100
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
BOMBING_INTERVAL = 0.1
BOMBING_CONTENT = '起床'
BOMBING_FILE = ''

# ===== AI 生图 =====
# 对方发「触发词 + 描述」时调用图片生成模型并发送图片
ENABLE_IMAGE_GEN = False
IMAGE_GEN_TRIGGER = '生成图片'
IMAGE_GEN_MODEL = 'gpt-image-2'
IMAGE_GEN_API_KEY = ''
IMAGE_GEN_BASE_URL = 'https://vg.v1api.cc/v1'


