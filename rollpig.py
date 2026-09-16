# -*- coding: utf-8 -*-
# 今日小猪 - 微信版（适配自 astrbot_plugin_rollpig）
import datetime
import json
import random
import tempfile
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

BASE_DIR = Path(__file__).parent
RES_DIR = BASE_DIR / "rollpig" / "astrbot_plugin_rollpig-main" / "resource"
PIGINFO_PATH = RES_DIR / "pig.json"
IMAGE_DIR = RES_DIR / "image"
FONT_DIR = RES_DIR / "font"
DATA_DIR = BASE_DIR / "rollpig_data"
TODAY_PATH = DATA_DIR / "rollpig_today.json"
COLLECTION_PATH = DATA_DIR / "rollpig_collection.json"

DRAW_TRIGGERS = ["今日小猪", "抽小猪", "我的小猪", "rollpig"]
LIST_TRIGGERS = ["小猪列表", "全部小猪", "猪猪列表"]
COLLECTION_TRIGGERS = ["小猪图鉴", "我的猪圈", "我的收藏", "解锁小猪", "猪圈"]

CANVAS_WIDTH = 800
CANVAS_HEIGHT = 800
AVATAR_SIZE = 280
SPACING_AVATAR_NAME = 20
SPACING_NAME_DESC = 25
SPACING_DESC_ANALYSIS = 30
DESC_FONT_SIZE = 32
ANALYSIS_FONT_SIZE = 28
ANALYSIS_LINE_HEIGHT_FACTOR = 1.6
ANALYSIS_WIDTH_RATIO = 0.85
NAME_FONT_SIZE = 66


def _load_font(candidates, size):
    for fp in candidates:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(str(fp), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _get_text_size(text, font):
    draw = ImageDraw.Draw(PILImage.new("RGB", (1, 1)))
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
    except Exception:
        return draw.textsize(text, font=font)


def _draw_bold_text(draw, pos, text, font, fill):
    x, y = pos
    for ox, oy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        draw.text((x + ox, y + oy), text, fill=fill, font=font)
    draw.text((x, y), text, fill=fill, font=font)


def find_image_file(pig_id):
    for ext in ["png", "jpg", "jpeg", "webp", "gif"]:
        f = IMAGE_DIR / f"{pig_id}.{ext}"
        if f.exists():
            return f
    return None


def load_json(path, default):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def render_pig_image(pig_data):
    pig_id = pig_data.get("id", "")
    pig_name = pig_data.get("name", "未知小猪")
    pig_desc = pig_data.get("description", "无描述")
    pig_analysis = pig_data.get("analysis", "无解析")

    canvas = PILImage.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    font_bold = _load_font(
        [FONT_DIR / "荆南麦圆体.otf", FONT_DIR / "SourceHanSansCN-Bold.otf",
         "C:/Windows/Fonts/msyhbd.ttc"], NAME_FONT_SIZE)
    font_regular = _load_font(
        [FONT_DIR / "可爱字体.ttf", FONT_DIR / "SourceHanSansCN-Regular.otf",
         "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"], DESC_FONT_SIZE)

    avatar_w = avatar_h = AVATAR_SIZE
    avatar = None
    avatar_path = find_image_file(pig_id)
    if avatar_path:
        try:
            avatar = PILImage.open(avatar_path)
            avatar.thumbnail((avatar_w, avatar_h))
            if avatar.size != (avatar_w, avatar_h):
                cx, cy = avatar.width // 2, avatar.height // 2
                half = AVATAR_SIZE // 2
                avatar = avatar.crop((cx - half, cy - half, cx + half, cy + half))
        except Exception:
            avatar = None

    name_w, name_h = _get_text_size(pig_name, font_bold)
    desc_font = font_regular.font_variant(size=DESC_FONT_SIZE)
    desc_w, desc_h = _get_text_size(pig_desc, desc_font)

    analysis_font = font_regular.font_variant(size=ANALYSIS_FONT_SIZE)
    line_height = int(ANALYSIS_FONT_SIZE * ANALYSIS_LINE_HEIGHT_FACTOR)
    max_analysis_width = int(CANVAS_WIDTH * ANALYSIS_WIDTH_RATIO)
    analysis_lines = []
    current_line = ""
    for char in pig_analysis:
        current_line += char
        line_w, _ = _get_text_size(current_line, analysis_font)
        if line_w > max_analysis_width:
            analysis_lines.append(current_line[:-1])
            current_line = char
    if current_line:
        analysis_lines.append(current_line)
    analysis_total_h = len(analysis_lines) * line_height

    total_content_h = (
        avatar_h + SPACING_AVATAR_NAME + name_h + SPACING_NAME_DESC
        + desc_h + SPACING_DESC_ANALYSIS + analysis_total_h
    )
    start_y = (CANVAS_HEIGHT - total_content_h) // 2

    avatar_x = (CANVAS_WIDTH - avatar_w) // 2
    avatar_y = start_y
    if avatar:
        canvas.paste(avatar, (avatar_x, avatar_y), mask=avatar if avatar.mode == "RGBA" else None)

    name_y = avatar_y + avatar_h + SPACING_AVATAR_NAME
    _draw_bold_text(draw, ((CANVAS_WIDTH - name_w) // 2, name_y), pig_name, font_bold, (0, 0, 0))

    desc_y = name_y + name_h + SPACING_NAME_DESC
    draw.text(((CANVAS_WIDTH - desc_w) // 2, desc_y), pig_desc, fill=(85, 85, 85), font=desc_font)

    analysis_y = desc_y + desc_h + SPACING_DESC_ANALYSIS
    for line in analysis_lines:
        line_w, line_h = _get_text_size(line, analysis_font)
        draw.text(((CANVAS_WIDTH - line_w) // 2, analysis_y), line, fill=(51, 51, 51), font=analysis_font)
        analysis_y += line_height

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            canvas.save(tmp_path, format="PNG")
        return tmp_path
    except Exception:
        return None


def get_today_pig(user_id):
    today_str = datetime.date.today().isoformat()
    cache = load_json(TODAY_PATH, {"date": "", "records": {}})
    if cache.get("date") != today_str:
        cache = {"date": today_str, "records": {}}
    records = cache["records"]
    if user_id in records:
        return records[user_id]
    pig_list = load_json(PIGINFO_PATH, [])
    if not pig_list:
        return None
    pig = random.choice(pig_list)
    records[user_id] = pig
    save_json(TODAY_PATH, cache)
    add_to_collection(user_id, pig)
    return pig


def add_to_collection(user_id, pig):
    collection = load_json(COLLECTION_PATH, {})
    ids = collection.get(user_id, [])
    if pig.get("id") not in ids:
        ids.append(pig["id"])
        collection[user_id] = ids
        save_json(COLLECTION_PATH, collection)


def get_collection(user_id):
    collection = load_json(COLLECTION_PATH, {})
    return collection.get(user_id, [])


def pig_list_text():
    pig_list = load_json(PIGINFO_PATH, [])
    if not pig_list:
        return "小猪信息加载失败，请检查后台报错"
    lines = [f"🐷 小猪图鉴 · 共 {len(pig_list)} 只"]
    for idx, pig in enumerate(pig_list, 1):
        lines.append(f"{idx}. {pig.get('name', '未知小猪')}")
    return "\n".join(lines)


def collection_text(user_id):
    pig_list = load_json(PIGINFO_PATH, [])
    total = len(pig_list)
    owned_ids = get_collection(user_id)
    owned_set = set(owned_ids)
    owned_names = [pig.get("name", "未知小猪") for pig in pig_list if pig.get("id") in owned_set]
    if not owned_names:
        return f"你的猪圈还是空的（0/{total}）\n发送\"今日小猪\"抽一只，解锁你的第一只小猪吧！"
    lines = [f"🐷 我的猪圈 · 已解锁 {len(owned_names)}/{total}"]
    lines.extend(f"· {name}" for name in owned_names)
    missing = total - len(owned_names)
    if missing > 0:
        lines.append(f"还有 {missing} 只小猪待解锁，每天抽一只，慢慢收集吧~")
    return "\n".join(lines)


def handle(who, sender, content=""):
    user_id = sender or who or "unknown"
    if any(t in content for t in LIST_TRIGGERS):
        return pig_list_text(), None
    if any(t in content for t in COLLECTION_TRIGGERS):
        return collection_text(user_id), None
    pig = get_today_pig(user_id)
    if not pig:
        return "小猪信息加载失败，请检查后台报错", None
    img = render_pig_image(pig)
    if img:
        return f"这是你的今日小猪：", img
    text = (
        f"【今日小猪】\n"
        f"名称：{pig.get('name', '未知小猪')}\n"
        f"描述：{pig.get('description', '无描述')}\n"
        f"解析：{pig.get('analysis', '无解析')}"
    )
    return text, None
