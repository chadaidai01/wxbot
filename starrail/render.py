# -*- coding: utf-8 -*-
"""
art-template 精简渲染器 + playwright 截图
用于渲染星铁插件原版 HTML 模板
"""
import json
import os
import re
import tempfile
from pathlib import Path

# 资源根目录（原版插件 resources 目录）
RES_ROOT = Path(r"E:\StarRail-plugin\StarRail-plugin-main\resources")
LAYOUT_DIR = RES_ROOT / "common" / "layout"

BROWSER_PATH = r"E:\playwright-browsers\chromium-1234\chrome-win64\chrome.exe"

# 固化 chromium 路径，避免依赖启动时的手动环境变量
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", r"E:\playwright-browsers")

# 变量注入命名空间里不可信的部分剔除
_SAFE_BUILTINS = {
    "abs": abs, "round": round, "int": int, "str": str, "len": len,
    "max": max, "min": min, "float": float,
    "True": True, "False": False, "None": None,
}


class _Math:
    @staticmethod
    def floor(v):
        import math
        return math.floor(float(v))

    @staticmethod
    def ceil(v):
        import math
        return math.ceil(float(v))

    @staticmethod
    def round10(v, exp=0):
        return round(float(v), exp)


_NS = dict(_SAFE_BUILTINS)
_NS["Math"] = _Math
_NS["JSON"] = json


def _expr(expr, data):
    """求值 art-template 表达式，兼容 JS 的三元/比较/空值合并/点号访问"""
    e = expr.strip()
    # nullish: a ?? b  ->  (a if a is not None else b)
    e = re.sub(r"\?\?", " or ", e)
    # === / == 统一为 ==
    e = re.sub(r"===", "==", e)
    e = re.sub(r"!==", "!=", e)
    # 点号访问保持不变（Python 也支持 attr 访问），但 dict 用 get
    ns = dict(_NS)
    ns.update(data)
    try:
        return eval(e, {"__builtins__": {}}, ns)
    except Exception:
        return ""


def _resolve(path, data):
    """按点号路径取值，支持 a.b.c"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif hasattr(cur, part):
            cur = getattr(cur, part)
        else:
            return None
    return cur


def _render_each(block_body, item_var, list_val, data):
    out = []
    for item in list_val:
        local = dict(data)
        local[item_var] = item
        out.append(_render_template_text(block_body, local))
    return "".join(out)


def _render_if(block_body, cond, data, else_body=""):
    if _expr(cond, data):
        return _render_template_text(block_body, data)
    if else_body:
        return _render_template_text(else_body, data)
    return ""


def _render_template_text(text, data):
    """递归渲染模板片段（不含 extend）"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        # 找 {{ ... }}
        start = text.find("{{", i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find("}}", start)
        if end < 0:
            out.append(text[start:])
            break
        inner = text[start + 2:end].strip()
        i = end + 2

        if inner.startswith("@"):
            # raw 输出（不转义）
            out.append(str(_expr(inner[1:], data)))
            continue

        # 块标签
        if inner.startswith("each "):
            # {{each list item}} ... {{/each}}
            m = re.match(r"each\s+([\w.\[\]]+)\s+(\w+)", inner)
            if m:
                list_path, item_var = m.group(1), m.group(2)
                list_val = _resolve(list_path, data) or []
                # 找到匹配的 /each
                close = _find_close(text, i, "each")
                body = text[i:close]
                out.append(_render_each(body, item_var, list_val, data))
                i = close + len("{{/each}}")
                continue
        elif inner.startswith("if "):
            # {{if cond}} ... {{else}} ... {{/if}}
            cond = inner[3:].strip()
            close = _find_close(text, i, "if")
            body = text[i:close]
            # 处理 {{else}}
            else_body = ""
            else_marker = _find_else(body)
            if else_marker >= 0:
                else_body = body[else_marker + len("{{else}}"):]
                body = body[:else_marker]
            out.append(_render_if(body, cond, data, else_body))
            i = close + len("{{/if}}")
            continue
        elif inner == "/if" or inner == "/each" or inner == "else":
            # 已经由外层处理，跳过（理论不会走到这里）
            continue
        elif inner.startswith("include "):
            pass

        # 普通变量/表达式
        out.append(str(_expr(inner, data)))
    return "".join(out)


def _find_close(text, start_idx, tag):
    """从 start_idx 起，找匹配的 {{/tag}}，处理嵌套"""
    depth = 0
    i = start_idx
    while True:
        s = text.find("{{", i)
        if s < 0:
            return len(text)
        e = text.find("}}", s)
        if e < 0:
            return len(text)
        inner = text[s + 2:e].strip()
        if inner.startswith(tag) and inner not in (tag + " ",) and inner != tag:
            if inner.startswith(tag + " "):
                depth += 1
        elif inner == "/" + tag:
            if depth == 0:
                return s
            depth -= 1
        i = e + 2


def _find_else(body):
    """在 if 块 body 里找同层的 {{else}}"""
    depth = 0
    i = 0
    while True:
        s = body.find("{{", i)
        if s < 0:
            return -1
        e = body.find("}}", s)
        if e < 0:
            return -1
        inner = body[s + 2:e].strip()
        if inner.startswith("if "):
            depth += 1
        elif inner == "/if":
            depth -= 1
        elif inner == "else" and depth == 0:
            return s
        i = e + 2


def render(template_rel, data, res_prefix=None):
    """
    渲染模板文件为 HTML 字符串。
    template_rel: 相对 resources 的路径，如 'note/new_note.html'
    """
    template_path = RES_ROOT / template_rel
    raw = template_path.read_text("utf-8")
    return _render(raw, data, res_prefix)


def _render(raw, data, res_prefix):
    # 处理 extend：{{extend xxx}} 中 xxx 是变量名，指向布局文件路径或名称
    m = re.search(r"\{\{\s*extend\s+([\w.]+)\s*\}\}", raw)
    if m:
        layout_ref = m.group(1)
        layout_path = data.get(layout_ref, "")
        if layout_path and layout_path.endswith(".html"):
            layout = Path(layout_path).read_text("utf-8") if Path(layout_path).exists() else \
                (LAYOUT_DIR / Path(layout_path).name).read_text("utf-8")
        else:
            layout = (LAYOUT_DIR / "default.html").read_text("utf-8")
        # 提取子模板的 block
        blocks = {}
        for bm in re.finditer(r"\{\{\s*block\s+'(\w+)'\s*\}\}(.*?)\{\{\s*/block\s*\}\}", raw, re.S):
            blocks[bm.group(1)] = bm.group(2)
        # 用子模板 block 内容替换 layout 里的 block
        def repl(bm):
            name = bm.group(1)
            return blocks.get(name, "")
        layout = re.sub(r"\{\{\s*block\s+'(\w+)'\s*\}\}.*?\{\{\s*/block\s*\}\}", repl, layout, flags=re.S)
        raw = layout

    # 注入资源路径前缀
    if res_prefix:
        data = dict(data)
        data["pluResPath"] = res_prefix

    return _render_template_text(raw, data)


def to_image(html, output=None, width=None):
    """用 playwright 将 HTML 渲染为 PNG"""
    from playwright.sync_api import sync_playwright

    if output is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        output = tmp.name
        tmp.close()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=BROWSER_PATH)
        page = browser.new_page(viewport={"width": 1080, "height": 800}, device_scale_factor=2)
        # 写临时 HTML 文件，用 file:// 加载（保证 CSS/图片等本地资源能加载）
        tmp_html = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        tmp_html.write(html)
        tmp_html.close()
        page.goto("file:///" + tmp_html.name.replace("\\", "/"))
        page.wait_for_timeout(1500)
        # 测量内容实际尺寸，自适应视口高度
        el = page.query_selector(".container") or page.query_selector("body")
        if el:
            box = el.bounding_box()
            if box and box["height"] > 0:
                page.set_viewport_size({"width": 1080, "height": int(box["height"]) + 40})
                page.wait_for_timeout(500)
            el.screenshot(path=output)
        else:
            page.screenshot(path=output, full_page=True)
        browser.close()
        try:
            os.remove(tmp_html.name)
        except Exception:
            pass
    return output


def render_note_image(note_data, uid, nickname="开拓者", level="", avatar=None):
    """渲染体力图片，返回 PNG 路径"""
    data = dict(note_data)
    data["uid"] = uid
    data["game_uid"] = uid
    data["nickname"] = nickname
    data["level"] = level
    data["ktl_avatar"] = avatar or "https://q1.qlogo.cn/g?b=qq&nk=0&s=640"
    data["ktl_qq"] = ""
    data["ktl_name"] = nickname
    # 处理委托时间格式
    for ex in data.get("expeditions", []):
        ex["format_remaining_time"] = _fmt_duration(ex.get("remaining_time", 0))
        remain = ex.get("remaining_time", 0)
        ex["progress"] = f"{(72000 - remain) / 72000 * 100:.1f}%"
    data["ktl_full"] = "开拓力已完全恢复！" if data.get("current_stamina") == data.get("max_stamina") else \
        f"{_fmt_duration(data.get('stamina_recover_time', 0), 'HH小时mm分钟')} |"
    data["ktl_full_time_str"] = ""
    data["stamina_progress"] = f"{data.get('current_stamina', 0) / max(data.get('max_stamina', 1), 1) * 100:.1f}%"
    data["accepted_epedition_num"] = data.get("accepted_epedition_num", 0)
    data["is_sign"] = data.get("is_sign", False)

    res_prefix = "file:///" + str(RES_ROOT).replace("\\", "/") + "/"
    html = render("note/new_note.html", data, res_prefix)
    return to_image(html)


def _fmt_duration(seconds, fmt="HH时mm分"):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "已完成"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return fmt.replace("HH", str(h).zfill(2)).replace("mm", str(m).zfill(2))


AVATAR_DIR = RES_ROOT / "panel" / "resources" / "avatar"
_AVATAR_CDN = "https://gh-proxy.org/https://raw.githubusercontent.com/Mar-7th/StarRailRes/master/icon/character/{}.png"


def ensure_avatar(avatar_id):
    """确保本地有角色头像，缺失时从 CDN 下载缓存"""
    try:
        import urllib.request
        f = AVATAR_DIR / f"{avatar_id}.png"
        if f.exists() and f.stat().st_size > 0:
            return str(f)
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(_AVATAR_CDN.format(avatar_id), headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=20).read()
        if data:
            f.write_bytes(data)
            return str(f)
    except Exception:
        pass
    return None


def render_card_image(characters, uid, nickname="开拓者", api="mihomo"):
    """渲染角色列表卡片（练度概览），返回 PNG 路径"""
    import datetime
    # 补齐缺失头像
    for c in characters:
        ensure_avatar(c.get("avatarId"))
    data = {
        "uid": uid,
        "api": api,
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "userName": nickname,
        "data": characters,
        "type": "",
        "sys": {
            "scale": "",
            "createdby": "茶呆呆",
        },
    }
    return to_image_full("panel/new_card.html", data)


# ======================================================================
# 通用模板渲染：在浏览器端用 art-template 完整渲染（支持原版全部语法）
# 用于复杂模板（面板/挑战/模拟宇宙等含 set/MathPro/toLowerCase 等的模板）
# ======================================================================

_ART_TPL_JS = RES_ROOT / "common" / "art-template-web.js"


def render_html_full(template_rel, data, scale=1.0):
    """
    用浏览器端 art-template 渲染模板为完整 HTML 字符串。
    template_rel: 相对 resources 的路径，如 'panel/new_panel.html'
    data: 渲染数据（dict）
    返回完整 HTML（含布局继承、CSS 引用）
    """
    template_path = RES_ROOT / template_rel
    raw = template_path.read_text("utf-8")

    # 收集所有布局文件，供 art-template loader 使用
    layouts = {}
    if LAYOUT_DIR.exists():
        for f in LAYOUT_DIR.glob("*.html"):
            layouts[f.name] = f.read_text("utf-8")

    # 资源前缀（file:// 形式，让浏览器能加载本地 CSS/图片）
    res_prefix = "file:///" + str(RES_ROOT).replace("\\", "/") + "/"

    # 注入默认变量
    render_data = dict(data)
    render_data.setdefault("pluResPath", res_prefix)
    render_data.setdefault("defaultLayout", "default.html")
    render_data.setdefault("elemLayout", "elem.html")
    pct = "style='transform:scale(1)'"
    render_data["sys"] = {
        "scale": pct,
        "copyright": "Created By 茶呆呆",
        "createdby": "Created By 茶呆呆",
    }
    render_data["Math"] = {}
    render_data["JSON"] = {}

    html = _art_render(raw, render_data, layouts, res_prefix)
    # 行迹树图标 CDN（avocado.wiki 已挂）替换为 gh-proxy 的 StarRailRes
    # 模板里 skill.icon 是 'icon/skill/xxx.png' 相对路径，拼上 StarRailRes master 目录
    html = html.replace("https://avocado.wiki", "https://gh-proxy.org/https://raw.githubusercontent.com/Mar-7th/StarRailRes/master")
    return html


def _art_render(template_text, data, layouts, res_prefix):
    """调用 Node 里的 art-template 渲染（通过 subprocess，数据走临时文件）"""
    import subprocess
    import json as _json

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const artJsPath = process.argv[2];
const tplPath = process.argv[3];
const layoutsPath = process.argv[4];
const dataPath = process.argv[5];

const artSrc = fs.readFileSync(artJsPath, 'utf8');
const templateText = fs.readFileSync(tplPath, 'utf8');
const layouts = JSON.parse(fs.readFileSync(layoutsPath, 'utf8'));
const data = JSON.parse(fs.readFileSync(dataPath, 'utf8'));

const ctx = { window: {}, self: {}, Math: Math, JSON: JSON, console: console };
vm.createContext(ctx);
vm.runInContext(artSrc, ctx);
const template = ctx.self.template;

const decimalAdjust = (type, value, exp = 0) => {
  type = String(type);
  if (!['round','floor','ceil'].includes(type)) throw new TypeError('bad type');
  exp = Number(exp); value = Number(value);
  if (exp % 1 !== 0 || Number.isNaN(value)) return NaN;
  else if (exp === 0) return Math[type](value);
  const [m, e = 0] = value.toString().split('e');
  const adj = Math[type](`${m}e${e - exp}`);
  return Number(`${adj}e${+e + exp}`);
};
data.MathPro = {
  floor10: (v, e) => decimalAdjust('floor', v, e),
  ceil10: (v, e) => decimalAdjust('ceil', v, e),
  round10: (v, e) => decimalAdjust('round', v, e),
};
data.Math = Math;
data.JSON = JSON;

template.defaults.loader = function (filename) {
  const name = String(filename).replace(/\\/g, '/').split('/').pop();
  return layouts[name] || '';
};
template.defaults.root = '.';

const out = template.render(templateText, data);
process.stdout.write(out);
"""

    with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", encoding="utf-8") as f:
        f.write(node_script)
        script_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".tpl", delete=False, mode="w", encoding="utf-8") as f:
        f.write(template_text)
        tpl_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
        f.write(_json.dumps(layouts, ensure_ascii=False))
        layouts_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
        f.write(_json.dumps(data, ensure_ascii=False))
        data_path = f.name

    try:
        result = subprocess.run(
            ["node", script_path, str(_ART_TPL_JS), tpl_path, layouts_path, data_path],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"art-template 渲染失败: {result.stderr[:500]}")
        return result.stdout
    finally:
        for p in (script_path, tpl_path, layouts_path, data_path):
            try:
                os.remove(p)
            except Exception:
                pass


def to_image_full(template_rel, data, scale=1.0, output=None):
    """完整渲染模板为图片（浏览器端 art-template + playwright 截图）"""
    html = render_html_full(template_rel, data, scale=scale)
    return to_image(html, output=output)
