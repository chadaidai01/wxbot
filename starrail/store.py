# -*- coding: utf-8 -*-
"""
用户绑定存储：cookie / uid / 抽卡链接，存 JSON 文件（替代原版 Redis）
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
BIND_FILE = DATA_DIR / "bindings.json"


def _load():
    if not BIND_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BIND_FILE.write_text("{}", encoding="utf-8")
        return {}
    try:
        return json.loads(BIND_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIND_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def bind_cookie(user_id, cookie):
    """绑定米游社 cookie"""
    data = _load()
    entry = data.setdefault(user_id, {})
    entry["cookie"] = cookie.strip()
    _save(data)


def bind_uid(user_id, uid):
    """绑定星铁 uid"""
    data = _load()
    entry = data.setdefault(user_id, {})
    entry["uid"] = str(uid).strip()
    _save(data)


def bind_authkey(user_id, authkey):
    """绑定抽卡链接 authkey"""
    data = _load()
    entry = data.setdefault(user_id, {})
    entry["authkey"] = authkey.strip()
    _save(data)


def get_user(user_id):
    return _load().get(user_id, {})


def get_cookie(user_id):
    return get_user(user_id).get("cookie", "")


def get_uid(user_id):
    return get_user(user_id).get("uid", "")


def get_authkey(user_id):
    return get_user(user_id).get("authkey", "")
