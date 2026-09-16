# -*- coding: utf-8 -*-
"""
mihomo sr_info_parsed 数据 -> 原版模板字段 适配层
"""
import re
import urllib.request
from pathlib import Path

RES_ROOT = Path(r"E:\StarRail-plugin\StarRail-plugin-main\resources")
_CDN_BASE = "https://gh-proxy.org/https://raw.githubusercontent.com/Mar-7th/StarRailRes/master/"

# icon 相对路径 -> 本地资源子目录
_ICON_MAP = {
    "icon/skill/": "panel/resources/skill/",
    "icon/relic/": "panel/resources/relic/",
    "icon/light_cone/": "panel/resources/weapon/",
    "icon/character/": "panel/resources/avatar/",
    "image/character_portrait/": "image/character_portrait/",
}


def ensure_icon(icon_rel):
    """确保本地有资源图标，缺失时从 CDN 下载。icon_rel 形如 'icon/skill/xxx.png'"""
    if not icon_rel:
        return
    for prefix, sub in _ICON_MAP.items():
        if icon_rel.startswith(prefix):
            name = icon_rel[len(prefix):]
            dst = RES_ROOT / sub / name
            if dst.exists() and dst.stat().st_size > 0:
                return
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                req = urllib.request.Request(_CDN_BASE + icon_rel, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=30).read()
                if data:
                    dst.write_bytes(data)
            except Exception:
                pass
            return


def _clean_name(name):
    if not name:
        return name
    return re.sub(r"LV\.\d+$", "", name).strip()


def _find(items, field):
    """在 [{'field': ..., 'value': ...}] 里找指定 field 的 value"""
    if not items:
        return 0
    for it in items:
        if it.get("field") == field:
            return it.get("value", 0)
    return 0


def _prop_field(properties, field):
    """在 properties 列表里找 field"""
    return _find(properties, field)


def adapt_char(c):
    """把 mihomo 单个角色数据适配成 new_panel.html 模板期望的字段"""
    name = _clean_name(c.get("name", ""))
    level = c.get("level", 0)
    rank = c.get("rank", 0) or 0
    rarity = c.get("rarity", 5)

    path = c.get("path") or {}
    path_name = path.get("name", "")
    element = c.get("element") or {}
    element_id = element.get("id", "")
    element_name = element.get("name", "")

    # 属性
    attributes = c.get("attributes") or []
    additions = c.get("additions") or []
    statistics = c.get("statistics") or []

    hp_base = _find(attributes, "hp")
    atk_base = _find(attributes, "atk")
    def_base = _find(attributes, "def")
    spd_base = _find(attributes, "spd")
    crit_rate_base = _find(attributes, "crit_rate")
    crit_dmg_base = _find(attributes, "crit_dmg")

    hp_delta = _find(additions, "hp")
    atk_delta = _find(additions, "atk")
    def_delta = _find(additions, "def")
    spd_delta = _find(additions, "spd")

    # 各种百分比属性（properties 列表里按 field 找）
    properties_list = c.get("properties") or []
    crit_rate = _prop_field(properties_list, "crit_rate")
    crit_dmg = _prop_field(properties_list, "crit_dmg")
    status_res = _prop_field(properties_list, "effect_res")
    status_prob = _prop_field(properties_list, "effect_hit")
    break_dmg = _prop_field(properties_list, "break_dmg")
    heal_ratio = _prop_field(properties_list, "heal_ratio")
    sp_ratio = _prop_field(properties_list, "sp_ratio")
    spd_ratio = _prop_field(properties_list, "spd_ratio")

    # 元素伤害加成（element_id 转模板字段名）
    element_field_map = {
        "Fire": "fireAdded", "Ice": "iceAdded", "Imaginary": "imaginaryAdded",
        "Physical": "physicalAdded", "Quantum": "quantumAdded",
        "Thunder": "thunderAdded", "Wind": "windAdded",
    }
    elem_added = _prop_field(properties_list, element_id.lower() + "_dmg")

    properties = {
        "hpBase": hp_base, "hpDelta": hp_delta,
        "attackBase": atk_base, "attackDelta": atk_delta,
        "defenseBase": def_base, "defenseDelta": def_delta,
        "speedBase": spd_base, "speedAdd": spd_delta,
        "criticalChance": crit_rate, "criticalDamage": crit_dmg,
        "statusResistance": status_res, "statusProbability": status_prob,
        "breakDamageAdded": break_dmg, "spRatio": sp_ratio, "healRatio": heal_ratio,
        "speedAddedRatio": spd_ratio,
        "fireAdded": elem_added if element_id == "Fire" else 0,
        "iceAdded": elem_added if element_id == "Ice" else 0,
        "imaginaryAdded": elem_added if element_id == "Imaginary" else 0,
        "physicalAdded": elem_added if element_id == "Physical" else 0,
        "quantumAdded": elem_added if element_id == "Quantum" else 0,
        "thunderAdded": elem_added if element_id == "Thunder" else 0,
        "windAdded": elem_added if element_id == "Wind" else 0,
    }

    # 光锥
    lc = c.get("light_cone") or {}
    equipment = {}
    if lc:
        lc_attrs = lc.get("attributes") or []
        lc_rarity = lc.get("rarity", 5)
        equipment = {
            "id": lc.get("id", ""),
            "name": lc.get("name", ""),
            "level": lc.get("level", 0),
            "rank": lc.get("rank", 0),
            # 模板用 rarity.slice(-7) 取 "RarityN"，故存成字符串
            "rarity": f"CombatPowerLightconeRarity{lc_rarity}",
            "hp": _find(lc_attrs, "hp"),
            "atk": _find(lc_attrs, "atk"),
            "def": _find(lc_attrs, "def"),
        }

    # 遗器
    relics = []
    for r in (c.get("relics") or []):
        main = r.get("main_affix") or {}
        sub_affix = r.get("sub_affix") or []
        sub_list = []
        for sa in sub_affix:
            sub_list.append({
                "name": sa.get("name", ""),
                "value": sa.get("value", 0),
                "percent": sa.get("percent", False),
                "cnt": sa.get("count", 0),
                "step": sa.get("step", 0),
                "weight": sa.get("step", 0),
            })
        relics.append({
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "level": r.get("level", 0),
            "max_level": 15 if r.get("rarity", 5) >= 5 else 12,
            "rarity": r.get("rarity", 5),
            "type": r.get("type", 0),
            "path": r.get("icon", "").replace("icon/relic/", ""),
            "score": _relic_score(r),
            "main_affix_name": main.get("name", ""),
            "main_affix_value": main.get("value", 0),
            "main_affix_percent": main.get("percent", False),
            "sub_affix_id": sub_list,
        })

    # 行迹（behaviorList）：取前 4 个核心技能 + 秘技
    skills = c.get("skills") or []
    behavior_list = []
    type_order = {"Normal": 0, "BPSkill": 1, "Ultra": 2, "Talent": 3, "Maze": 4}
    core_skills = [s for s in skills if s.get("type") in type_order]
    core_skills.sort(key=lambda s: type_order.get(s.get("type"), 9))
    seen_types = set()
    for s in core_skills:
        typ = s.get("type")
        if typ in seen_types:
            continue
        seen_types.add(typ)
        icon = s.get("icon") or ""
        icon_name = icon.replace("icon/skill/", "")
        behavior_list.append({
            "id": s.get("id", ""),
            "level": s.get("level", 0),
            "max_level": s.get("max_level", 10),
            "type": s.get("type_text", ""),
            "path": icon_name,
            "anchor": s.get("type", ""),
        })

    # 行迹树
    skill_tree = []
    _skill_tree_pos = _load_skill_tree_pos()
    pos_map = _skill_tree_pos.get(path_name, {})
    for st in (c.get("skill_trees") or []):
        anchor = st.get("anchor", "")
        pos = pos_map.get(anchor)
        # 无坐标的节点跳过（新命途无坐标，canvas 无法定位）
        if pos is None:
            continue
        skill_tree.append({
            "id": st.get("id", ""),
            "level": st.get("level", 0),
            "anchor": anchor,
            "max_level": st.get("max_level", 0),
            "icon": st.get("icon", ""),
            "position": pos,
        })

    # 角色立绘路径
    portrait = c.get("portrait") or c.get("icon") or ""

    # 补齐资源图标（光锥/遗器/技能/头像）
    if equipment:
        ensure_icon(lc.get("icon", ""))
    for r in (c.get("relics") or []):
        ensure_icon(r.get("icon", ""))
    for s in (c.get("skills") or []):
        ensure_icon(s.get("icon", ""))
    ensure_icon(c.get("icon", ""))
    # 补齐角色立绘（大图）
    ensure_icon(c.get("portrait", ""))

    return {
        "id": c.get("id", ""),
        "name": name,
        "rarity": rarity,
        "rank": rank,
        "level": level,
        "charpath": path_name,
        "damage_type": element_id,
        "element_name": element_name,
        "charImage": portrait,
        "properties": properties,
        "equipment": equipment,
        "relics": relics,
        "behaviorList": behavior_list,
        "skillTree": skill_tree,
        "skillTreeBkg": f"panel/resources/skill_tree/{_path_bkg(path_name)}",
        "relic_sets": c.get("relic_sets") or [],
    }


def _path_bkg(path_name):
    m = {
        "存护": "Knight", "智识": "Mage", "丰饶": "Priest", "巡猎": "Rogue",
        "毁灭": "Warrior", "同谐": "Shaman", "虚无": "Warlock", "记忆": "Shaman",
        "欢愉": "Mage",
    }
    return m.get(path_name, "Warrior") + ".svg"


# 遗器评分：基于副词条强化次数（count）的简化评分
# 米游社星铁遗器评分核心：副词条有效强化次数越多分越高
def _relic_score(relic):
    sub = relic.get("sub_affix") or []
    if not sub:
        return 0
    # 每个副词条按强化次数计分：满强化（count 大）高分
    # 基础：每个词条 count 满值约 3（含初始1次 + 强化2次），按 0-5 档给分
    score = 0.0
    for sa in sub:
        cnt = sa.get("count", 0) or 0
        # 词条分数 = count * 每档分（约 6.48，取米游社标准值）
        score += cnt * 6.48
    return round(score, 1)


_SKILL_TREE_POS = None


def _load_skill_tree_pos():
    """加载行迹树坐标表（命途 -> anchor -> [x,y]）"""
    global _SKILL_TREE_POS
    if _SKILL_TREE_POS is not None:
        return _SKILL_TREE_POS
    import json
    p = RES_ROOT / "panel" / "data" / "skillTree.json"
    try:
        _SKILL_TREE_POS = json.load(open(p, encoding="utf-8"))
    except Exception:
        _SKILL_TREE_POS = {}
    return _SKILL_TREE_POS
