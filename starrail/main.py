# -*- coding: utf-8 -*-
"""
崩坏：星穹铁道 - 微信版（适配自 StarRail-plugin）
指令：
  星铁帮助        查看帮助
  星铁绑定uid xxx  绑定 uid
  星铁绑定cookie xxx  绑定米游社 cookie（私聊使用）
  星铁体力        查询开拓力/委托
  星铁探索        探索卡片
  星铁收入        月星琼收入
  星铁抽卡/十连  模拟抽卡（无需 cookie）
"""
import re
import sys

from . import mys_api
from . import store
from . import render

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8")

TRIGGERS = ["星铁", "星轨", "崩铁", "星穹铁道", "铁道", "sr", "SR"]

HELP_TEXT = (
    "【崩坏：星穹铁道 助手】\n"
    "· 星铁绑定uid + 你的uid —— 绑定游戏uid\n"
    "· 星铁绑定cookie + 你的cookie —— 绑定米游社cookie（私聊）\n"
    "· 星铁体力 —— 查询开拓力/每日实训/委托\n"
    "· 星铁探索 —— 探索卡片\n"
    "· 星铁收入 —— 本月星琼收入\n"
    "· 星铁抽卡 / 星铁十连 —— 模拟抽卡（无需绑定）\n"
    "需要查询实时数据请先绑定 uid 和 cookie"
)


def _norm(content):
    """去掉前缀 # * 等，返回剩余指令文本"""
    c = content.strip()
    c = re.sub(r"^[#*＊]+", "", c)
    return c


def _extract_uid(content):
    m = re.search(r"[125-9]\d{8}", content)
    return m.group(0) if m else ""


def _extract_cookie(content):
    """cookie 通常在绑定指令后，包含 ltuid/lcookie 等"""
    idx = content.find("绑定cookie")
    if idx < 0:
        idx = content.find("绑定ck")
    if idx < 0:
        return ""
    return content[idx + len("绑定cookie"):].strip() if "绑定cookie" in content else content[idx + len("绑定ck"):].strip()


def handle(who, sender, content):
    """
    返回 (text, img_path) 或 None（未命中）
    """
    user_id = sender or who or "unknown"
    c = _norm(content)
    if not c:
        return None

    # 是否星铁相关指令
    is_sr = c.startswith("星铁") or c.startswith("星轨") or c.startswith("崩铁") \
        or c.startswith("星穹铁道") or c.startswith("铁道") or c.lower().startswith("sr")
    if not is_sr:
        return None

    body = c
    for prefix in ("星穹铁道", "星铁", "星轨", "崩铁", "铁道", "sr", "SR"):
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
            break
    if not body:
        return None

    # ---- 绑定 ----
    if body.startswith("绑定uid") or body.startswith("绑定UID"):
        uid = _extract_uid(body)
        if not uid:
            return "请发送正确格式：星铁绑定uid + 你的9位uid", None
        store.bind_uid(user_id, uid)
        return f"星铁 uid 绑定成功：{uid}", None

    if body.startswith("绑定cookie") or body.startswith("绑定ck"):
        cookie = _extract_cookie(content)
        if not cookie:
            return "请发送：星铁绑定cookie + 你的cookie内容", None
        store.bind_cookie(user_id, cookie)
        return "米游社 cookie 绑定成功", None

    if body.startswith("帮助") or body == "帮助":
        return HELP_TEXT, None

    # ---- 需要登录的功能 ----
    uid = store.get_uid(user_id)
    cookie = store.get_cookie(user_id)

    if body.startswith("体力"):
        if not uid:
            return "尚未绑定 uid，请发送：星铁绑定uid + 你的uid", None
        if not cookie:
            return "尚未绑定 cookie，请私聊发送：星铁绑定cookie + 你的cookie", None
        try:
            return _note(uid, cookie, user_id)
        except Exception as e:
            return f"体力查询失败：{e}", None

    if body.startswith("探索") or body.startswith("卡片"):
        if not uid:
            return "尚未绑定 uid，请发送：星铁绑定uid + 你的uid", None
        if not cookie:
            return "尚未绑定 cookie，请私聊发送：星铁绑定cookie + 你的cookie", None
        try:
            return _card(uid, cookie)
        except Exception as e:
            return f"探索卡片查询失败：{e}", None

    if body.startswith("收入") or body.startswith("星琼"):
        if not uid:
            return "尚未绑定 uid，请发送：星铁绑定uid + 你的uid", None
        if not cookie:
            return "尚未绑定 cookie，请私聊发送：星铁绑定cookie + 你的cookie", None
        try:
            return _month(uid, cookie)
        except Exception as e:
            return f"收入查询失败：{e}", None

    # ---- 练度查询（无需 cookie，走 mihomo 面板 API）----
    if body.startswith("练度") or body.startswith("面板") or body.startswith("角色"):
        uid = store.get_uid(user_id)
        if not uid:
            # 尝试从指令里直接提取 uid
            uid = _extract_uid(body)
        if not uid:
            return "尚未绑定 uid，请发送：星铁绑定uid + 你的uid（或直接 星铁练度 你的uid）", None
        try:
            return _panel(uid, body)
        except Exception as e:
            return f"练度查询失败：{e}", None

    # ---- 模拟抽卡（无需登录）----
    if body.startswith("抽卡") or body.startswith("十连") or body.startswith("单抽"):
        try:
            return _gacha_sim(body)
        except Exception as e:
            return f"模拟抽卡失败：{e}", None

    return None


def _note(uid, cookie, user_id):
    api = mys_api.MysSRApi(uid, cookie)
    fp = api.get_device_fp()
    note = api.get_note(device_fp=fp)
    if not note or note.get("retcode") != 0:
        msg = (note or {}).get("message", "") or f"retcode={note.get('retcode') if note else '?'}"
        return f"体力查询失败：{msg}", None
    data = note["data"]
    img = render.render_note_image(data, uid, nickname="开拓者", level=data.get("level", ""))
    return "体力查询结果：", img


def _card(uid, cookie):
    api = mys_api.MysSRApi(uid, cookie)
    fp = api.get_device_fp()
    card = api.get_card(device_fp=fp)
    if not card or card.get("retcode") != 0:
        msg = (card or {}).get("message", "") or f"retcode={card.get('retcode') if card else '?'}"
        return f"探索卡片查询失败：{msg}", None
    data = card["data"]
    lines = [f"【探索卡片】"]
    stats = data.get("stats", {})
    if stats:
        lines.append(f"活跃天数：{stats.get('active_days', '?')}")
        lines.append(f"已达成成就：{stats.get('achievement_count', '?')}")
        lines.append(f"开拓等级：{stats.get('level', '?')}级")
    return "\n".join(lines), None


_RELIC_POS = {1: "头部", 2: "手部", 3: "躯干", 4: "脚部", 5: "位面球", 6: "连结绳"}


def _clean_name(name):
    """清理 mihomo 返回的角色名里的 LV.xxx 后缀"""
    if not name:
        return name
    return re.sub(r"LV\.\d+$", "", name).strip()


def _panel(uid, body):
    """练度查询：无角色名返回列表卡片图，指定角色名返回单角色详细面板图"""
    from . import adapt
    data = mys_api.fetch_panel_parsed(uid)
    if "error" in data:
        return f"练度查询失败：{data['error']}", None
    player = data.get("player") or {}
    nickname = player.get("nickname", "开拓者")
    level = player.get("level", "?")
    chars = data.get("characters") or []

    target_name = ""
    for kw in ("练度", "面板", "角色"):
        if kw in body:
            rest = body.split(kw, 1)[1].strip()
            rest = re.sub(r"\d{8,}", "", rest).strip()
            if rest and rest not in ("查询", "列表", "看看"):
                target_name = rest
            break

    if not chars:
        return f"【{nickname}】开拓等级 {level} | UID {uid}\n（该账号角色展柜未公开，查不到练度数据）", None

    # 指定角色名 -> 单角色详细面板图
    if target_name:
        matched = [c for c in chars if target_name in _clean_name(c.get("name", ""))]
        if not matched:
            return f"未找到角色「{target_name}」，可能不在展柜中", None
        c = matched[0]
        try:
            ac = adapt.adapt_char(c)
            ac["uid"] = uid
            ac["api"] = "mihomo"
            img = render.to_image_full("panel/new_panel.html", ac, scale=1.6)
            tip = f"【{_clean_name(c.get('name',''))}】Lv.{c.get('level','?')} 星魂{c.get('rank',0) or 0}"
            return tip, img
        except Exception as e:
            return f"面板渲染失败：{e}", None

    # 无角色名 -> 列表卡片图
    card_chars = []
    for c in chars:
        card_chars.append({
            "avatarId": c.get("id"),
            "name": _clean_name(c.get("name", "")),
            "rank": c.get("rank", 0) or 0,
            "level": c.get("level", "?"),
            "is_new": False,
        })

    try:
        img = render.render_card_image(card_chars, uid, nickname=nickname)
        tip = f"【{nickname}】开拓等级 {level} 角色练度"
        return tip, img
    except Exception as e:
        lines = [f"【{nickname}】开拓等级 {level} | UID {uid}"]
        for c in chars:
            lines.append(f"· {_clean_name(c.get('name',''))} Lv.{c.get('level','?')} 星魂{c.get('rank',0) or 0}")
        return "\n".join(lines), None


def _month(uid, cookie):
    api = mys_api.MysSRApi(uid, cookie)
    fp = api.get_device_fp()
    month = api.get_month(device_fp=fp)
    if not month or month.get("retcode") != 0:
        msg = (month or {}).get("message", "") or f"retcode={month.get('retcode') if month else '?'}"
        return f"收入查询失败：{msg}", None
    data = month.get("data", {})
    lines = [f"【本月星琼收入】"]
    total = data.get("month_data", {})
    if total.get("current_month"):
        lines.append(f"本月：{total.get('current_month')} 星琼")
    group_by = total.get("group_by", [])
    for g in group_by[:8]:
        lines.append(f"· {g.get('action_name', '')}：{g.get('num', 0)}")
    return "\n".join(lines), None


# 模拟抽卡：简化概率
_UP5 = ["星", "穹", "希儿", "景元", "罗刹", "刃", "卡芙卡", "丹恒·饮月", "镜流", "托帕", "银枝", "阮·梅", "真理医生", "花火", "黄泉", "流萤"]
_UP4 = ["三月七", "丹恒", "停云", "青雀", "娜塔莎", "希露瓦", "艾丝妲", "驭空", "佩拉", "玲可", "虎克", "素裳"]


def _gacha_sim(body):
    import random
    times = 10 if ("十连" in body or "10" in body) else 1
    result = []
    for _ in range(times):
        r = random.random()
        if r < 0.006:
            result.append(("5", random.choice(_UP5)))
        elif r < 0.056:
            result.append(("4", random.choice(_UP4)))
        else:
            result.append(("3", None))
    has5 = any(star == "5" for star, _ in result)
    lines = []
    for star, name in result:
        if star == "5":
            lines.append(f"★5 {name}")
        elif star == "4":
            lines.append(f"★4 {name}")
        else:
            lines.append("★3")
    header = "【模拟抽卡】" + ("十连" if times == 10 else "单抽") + (" 出货了！" if has5 else "")
    return header + "\n" + "\n".join(lines), None
