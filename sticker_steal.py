# -*- coding: utf-8 -*-
"""偷表情包：心情分类解析、去重索引、文件落库。

只做纯逻辑与文件操作，不依赖 bot.py / hdsi，方便单测。
bot.py 负责"什么时候偷 + 调用视觉模型判心情"，本模块负责"怎么判/怎么存"。
"""

import hashlib
import json
import os
import re
import threading

IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')

# 关键词兜底：模型输出不可用（或没有视觉描述）时，用描述里的词判断心情
_MOOD_KEYWORDS = (
    ('happy', ('笑', '开心', '高兴', '乐', '哈哈', '滑稽', '得意', '欢呼', '点赞', '耶')),
    ('loved', ('爱', '喜欢', '亲', '比心', '心动', '恋爱', '抱', '贴贴', '害羞')),
    ('sad', ('哭', '难过', '伤心', '委屈', '泪', '悲', '沮丧', '失落')),
    ('angry', ('生气', '怒', '气', '恼', '火', '拍桌', '掀桌')),
    ('surprised', ('惊', '震惊', '意想', '吃瓜', '瞪', '呆住', '啊这')),
    ('confused', ('疑惑', '困惑', '问号', '不懂', '迷惑', '挠头', '？？')),
    ('tired', ('困', '累', '睡', '疲惫', '摆烂', '躺平', '没精神')),
    ('evasive', ('无语', '敷衍', '沉默', '白眼', '尴尬', '汗', '路过')),
    ('reminded', ('提醒', '加油', '鼓励', '敲打', '打气', '督促')),
)


def pick_mood(raw_text, categories, default='misc'):
    """把模型返回的文本解析成 categories 里的一个心情分类。

    - 优先在返回文本里找分类词（大小写不敏感）；
    - 找不到就用关键词表兜底（适合传进来的是图片描述而不是一个词）；
    - 再找不到返回 default。
    """
    cats = list(categories or [])
    text = str(raw_text or '').strip().lower()
    if text:
        for cat in cats:
            if str(cat).lower() in text:
                return cat
    for cat, keywords in _MOOD_KEYWORDS:
        if cat in cats and any(keyword in text for keyword in keywords):
            return cat
    return default


def discover_categories(root, default='happy'):
    """列出贴纸库下的一级子目录（即心情分类）；没有则返回 [default]。"""
    try:
        cats = sorted(name for name in os.listdir(root)
                      if os.path.isdir(os.path.join(root, name)))
    except OSError:
        cats = []
    return cats or [default]


def hash_index(root):
    """遍历贴纸库，返回 {sha256: 文件路径}，用于防止重复收藏。"""
    index = {}
    if not root:
        return index
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                with open(path, 'rb') as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            index.setdefault(digest, path)
    return index


def sanitize_mood(mood):
    """目录名安全化：只保留中英文、数字、下划线、短横线。"""
    cleaned = re.sub(r'[^0-9A-Za-z_\u4e00-\u9fff-]', '', str(mood or ''))
    return cleaned or 'misc'


def save_stolen(data, extension, mood, root, digest, now_token='x'):
    """把表情包字节写入 root/<mood>/stolen_<token>_<hash8><ext>，返回落盘路径。"""
    safe_mood = sanitize_mood(mood)
    ext = str(extension or '').lower()
    if ext not in IMAGE_EXTENSIONS:
        ext = '.png'
    target_dir = os.path.join(root, safe_mood)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, 'stolen_%s_%s%s' % (now_token, str(digest)[:8], ext))
    with open(target, 'wb') as handle:
        handle.write(data)
    return target


def dhash(data, size=8):
    """计算 8x8 差值哈希（64bit hex）；动图取第二帧，近纯色返回 ''（无区分度）。"""
    try:
        import io as _io
        from PIL import Image
        image = Image.open(_io.BytesIO(data))
        frames = int(getattr(image, 'n_frames', 1) or 1)
        if frames > 1:
            try:
                image.seek(min(1, frames - 1))
            except Exception:
                pass
        gray = image.convert('L').resize((size + 1, size), Image.LANCZOS)
        pixels = list(gray.getdata())
        if not pixels:
            return ''
        if max(pixels) - min(pixels) < 8:
            return ''
        bits = 0
        index = 0
        for row_index in range(size):
            offset = row_index * (size + 1)
            for column in range(size):
                if pixels[offset + column] > pixels[offset + column + 1]:
                    bits |= 1 << index
                index += 1
        return '%016x' % bits
    except Exception:
        return ''


def source_id_from_path(path):
    """WeFlow 表情缓存文件名本身就是内容哈希，可直接当来源特征（跨会话去重）。"""
    name = os.path.splitext(os.path.basename(str(path or '')))[0]
    return name[:64]


def iter_image_files(root, max_depth=3):
    """递归收集贴纸库里的图片文件（深度受限），用于首次重建偷取记忆。"""
    results = []
    root = str(root or '')
    if not root or not os.path.isdir(root):
        return results

    def visit(directory, depth):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir():
                    visit(entry.path, depth + 1)
                elif entry.is_file() and entry.name.lower().endswith(IMAGE_EXTENSIONS):
                    results.append(entry.path)
            except OSError:
                continue

    visit(root, 0)
    return results


class StickerStealMemory:
    """独立的“表情包偷取记忆”：记录 sha256 / 来源哈希 / 差值哈希 + 落盘位置。

    - 命中任意特征即认为偷过，不再重复收藏，也不再调用分类 API；
    - 记忆持久化到独立 JSON 文件，重启后依然有效；
    - 记忆为空时可从现有表情库重建（兼容启用本功能前已攒下的表情）。
    """

    VERSION = 1

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.records = []
        self._by_sha = {}
        self._by_source = {}
        self._by_dhash = {}
        self.load()

    def _reindex(self):
        self._by_sha = {}
        self._by_source = {}
        self._by_dhash = {}
        for record in self.records:
            if not isinstance(record, dict):
                continue
            sha = str(record.get('sha256') or '')
            source = str(record.get('source') or '')
            dhash_value = str(record.get('dhash') or '')
            if sha:
                self._by_sha.setdefault(sha, record)
            if source:
                self._by_source.setdefault(source, record)
            if dhash_value:
                self._by_dhash.setdefault(dhash_value, record)

    def load(self):
        with self._lock:
            self.records = []
            try:
                if os.path.isfile(self.path):
                    with open(self.path, 'r', encoding='utf-8') as handle:
                        data = json.load(handle)
                    if isinstance(data, dict):
                        self.records = [item for item in (data.get('records') or []) if isinstance(item, dict)]
                    elif isinstance(data, list):
                        self.records = [item for item in data if isinstance(item, dict)]
            except Exception:
                self.records = []
            self._reindex()

    def save(self):
        with self._lock:
            try:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                payload = {'version': self.VERSION, 'records': self.records}
                temp_path = self.path + '.tmp'
                with open(temp_path, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                os.replace(temp_path, self.path)
            except Exception:
                pass

    def empty(self):
        return not self.records

    def __len__(self):
        return len(self.records)

    def match(self, sha256='', source='', dhash_value=''):
        """命中返回 (record, 特征名)，未命中返回 (None, '')。"""
        with self._lock:
            if sha256 and sha256 in self._by_sha:
                return self._by_sha[sha256], 'sha256'
            if source and source in self._by_source:
                return self._by_source[source], 'source'
            if dhash_value and dhash_value in self._by_dhash:
                return self._by_dhash[dhash_value], 'dhash'
            return None, ''

    def add(self, record):
        """记录一条（按 sha256 去重），并立即落盘。"""
        if not isinstance(record, dict):
            return False
        sha = str(record.get('sha256') or '')
        with self._lock:
            if sha and sha in self._by_sha:
                return False
            self.records.append(record)
            self._reindex()
        self.save()
        return True

    def rebuild_from_files(self, root):
        """从现有表情库重建记忆（只补缺失的 sha256），返回新增条数。"""
        added = 0
        for file_path in iter_image_files(root):
            try:
                with open(file_path, 'rb') as handle:
                    data = handle.read()
            except OSError:
                continue
            if not data:
                continue
            sha = hashlib.sha256(data).hexdigest()
            with self._lock:
                if sha in self._by_sha:
                    continue
            relative = os.path.relpath(file_path, root).replace(os.sep, '/')
            record = {
                'sha256': sha,
                'source': source_id_from_path(file_path),
                'dhash': dhash(data),
                'size': len(data),
                'path': relative,
                'mood': relative.split('/')[0] if '/' in relative else 'default',
                'from': 'library-rebuild',
                'at': '',
            }
            with self._lock:
                self.records.append(record)
                added += 1
        if added:
            self._reindex()
        return added
