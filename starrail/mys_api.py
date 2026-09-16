# -*- coding: utf-8 -*-
"""
崩坏：星穹铁道 米游社 API 客户端（Python 版）
移植自 StarRail-plugin (hewang1an / TsukinaKasumi)，Apache-2.0
"""
import hashlib
import json
import random
import string
import time
import uuid

import requests

# DS 签名 salt
SALT_CN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"      # 国服 / B服
SALT_OS = "okr4obncj8bw5a65hbnn5oo6ixjc3l9w"      # 国际服
SALT_WEB = "WGtruoQrwczmsjLOPXzJLnaAYycsLavx"     # web 签名 (getDS2)

HEADER_CN = {
    "app_version": "2.73.1",
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; XQ-BC52 Build/61.2.A.0.472A; wv) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/111.0.5563.116 "
                  "Mobile Safari/537.36 miHoYoBBS/2.73.1",
    "client_type": "5",
    "Origin": "https://webstatic.mihoyo.com",
    "X-Requested-With": "com.mihoyo.hyperion",
    "Referer": "https://webstatic.mihoyo.com/",
}

HEADER_OS = {
    "app_version": "2.57.1",
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; XQ-BC52 Build/61.2.A.0.472A; wv) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/111.0.5563.116 "
                  "Mobile Safari/537.36 miHoYoBBSOversea/2.57.1",
    "client_type": "2",
    "Origin": "https://act.hoyolab.com",
    "X-Requested-With": "com.mihoyo.hoyolab",
    "Referer": "https://act.hoyolab.com/",
}


def _random_str(length):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _random_hex(length):
    return "".join(random.choice("0123456789abcdef") for _ in range(length))


class MysSRApi:
    """米游社星铁 API 客户端"""

    def __init__(self, uid="", cookie=""):
        self.uid = str(uid) if uid else ""
        self.cookie = cookie or ""
        self.server = self._get_server()
        self._device = str(uuid.uuid4())
        self.session = requests.Session()

    def _get_server(self):
        head = self.uid[:1] if self.uid else ""
        if head == "5":
            return "prod_qd_cn"          # B服
        if head == "6":
            return "prod_official_usa"    # 美服
        if head == "7":
            return "prod_official_euro"   # 欧服
        if head in ("8", "18"):
            return "prod_official_asia"   # 亚服
        if head == "9":
            return "prod_official_cht"    # 港澳台服
        return "prod_gf_cn"               # 官服

    # ---------- 签名 ----------
    def get_ds(self, q="", b=""):
        if self.server in ("prod_gf_cn", "prod_qd_cn"):
            salt = SALT_CN
        else:
            salt = SALT_OS
        t = int(time.time())
        r = random.randint(100000, 999999)
        ds = hashlib.md5(f"salt={salt}&t={t}&r={r}&b={b}&q={q}".encode()).hexdigest()
        return f"{t},{r},{ds}"

    def get_ds2(self):
        t = int(time.time())
        r = _random_str(6)
        sign = hashlib.md5(f"salt={SALT_WEB}&t={t}&r={r}".encode()).hexdigest()
        return f"{t},{r},{sign}"

    # ---------- URL 构造 ----------
    def _hosts(self):
        if self.server in ("prod_gf_cn", "prod_qd_cn"):
            return {
                "host": "https://api-takumi.mihoyo.com/",
                "record": "https://api-takumi-record.mihoyo.com/",
                "public": "https://public-data-api.mihoyo.com/",
            }
        return {
            "host": "https://sg-public-api.hoyolab.com/",
            "record": "https://sg-act-public-api.hoyolab.com/",
            "public": "https://sg-public-data-api.hoyoverse.com/",
        }

    def _url_map(self):
        h = self._hosts()
        cn = self.server in ("prod_gf_cn", "prod_qd_cn")
        return {
            "getFp": {
                "url": h["public"] + "device-fp/api/getFp",
                "body": {
                    "app_name": "bbs_cn" if cn else "bbs_oversea",
                    "device_fp": "38d7f4c72b736",
                    "device_id": "cc57c40f763ae4cc",
                    "ext_fields": (
                        '{"proxyStatus":1,"isRoot":0,"romCapacity":"768","deviceName":"XQ-BC52",'
                        '"productName":"XQ-BC52_EEA","romRemain":"727","hostname":"BuildHost",'
                        '"screenSize":"1096x2434","isTablet":0,"model":"XQ-BC52","brand":"Sony",'
                        '"hardware":"qcom","deviceType":"XQ-BC52","devId":"REL","serialNumber":"unknown",'
                        '"sdCapacity":224845,"buildTime":"1692775759000","buildUser":"BuildUser",'
                        '"simState":1,"ramRemain":"218344","appUpdateTimeDiff":1740498108042,'
                        '"deviceInfo":"Sony/XQ-BC52_EEA/XQ-BC52:13/61.2.A.0.472A/061002A0000472A0046651803:user/release-keys",'
                        '"vaid":"","buildType":"user","sdkVersion":"33","ui_mode":"UI_MODE_TYPE_NORMAL",'
                        '"isMockLocation":0,"cpuType":"arm64-v8a","isAirMode":0,"ringMode":2,'
                        '"chargeStatus":1,"manufacturer":"Sony","emulatorStatus":0,"appMemory":"768",'
                        '"osVersion":"13","vendor":"unknown","accelerometer":"-1.588236x6.8404818x6.999604",'
                        '"sdRemain":218214,"buildTags":"release-keys","packageName":"com.mihoyo.hyperion",'
                        '"networkType":"WiFi","oaid":"","debugStatus":1,"ramCapacity":"224845",'
                        '"magnetometer":"-47.04375x51.3375x137.96251","display":"61.2.A.0.472A",'
                        '"appInstallTimeDiff":1740498108042,"packageVersion":"2.35.0",'
                        '"gyroscope":"-0.22601996x-0.09453133x0.09040799","batteryStatus":88,'
                        '"hasKeyboard":0,"board":"lahaina"}'
                    ),
                    "platform": "2",
                    "seed_id": self._device,
                    "seed_time": str(int(time.time() * 1000)),
                },
                "no_ds": True,
            },
            "deviceLogin": {
                "url": "https://bbs-api.miyoushe.com/apihub/api/deviceLogin",
                "body": {
                    "app_version": "2.73.1",
                    "device_id": "",
                    "device_name": "SonyXQ-BC52",
                    "os_version": "33",
                    "platform": "Android",
                    "registration_id": _random_hex(19),
                },
                "ds2": True,
            },
            "saveDevice": {
                "url": "https://bbs-api.miyoushe.com/apihub/api/saveDevice",
                "body": {
                    "app_version": "2.73.1",
                    "device_id": "",
                    "device_name": "SonyXQ-BC52",
                    "os_version": "33",
                    "platform": "Android",
                    "registration_id": _random_hex(19),
                },
                "ds2": True,
            },
            "srUser": {
                "url": h["host"] + "binding/api/getUserGameRolesByCookie",
                "query": (f"game_biz=hkrpg_cn&region={self.server}&game_uid={self.uid}"
                          if cn else f"game_biz=hkrpg_global&region={self.server}&game_uid={self.uid}"),
            },
            "srNote": {
                "url": h["record"] + "game_record/app/hkrpg/api/note",
                "query": f"role_id={self.uid}&server={self.server}",
            },
            "srCard": {
                "url": h["record"] + "game_record/app/hkrpg/api/index",
                "query": f"role_id={self.uid}&server={self.server}",
            },
            "srCharacter": {
                "url": h["record"] + "game_record/app/hkrpg/api/avatar/basic",
                "query": f"rolePageAccessNotAllowed=&role_id={self.uid}&server={self.server}",
            },
            "srCharacterDetail": {
                "url": h["record"] + "game_record/app/hkrpg/api/avatar/info",
                "query": f"need_wiki=true&role_id={self.uid}&server={self.server}",
            },
            "srMonth": {
                "url": h["host"] + "event/srledger/month_info",
                "query": f"lang=zh-cn&uid={self.uid}&region={self.server}&month=",
            },
        }

    def _headers(self, query="", body=""):
        client = HEADER_CN if self.server in ("prod_gf_cn", "prod_qd_cn") else HEADER_OS
        return {
            "x-rpc-app_version": client["app_version"],
            "x-rpc-client_type": client["client_type"],
            "User-Agent": client["User-Agent"],
            "Referer": client["Referer"],
            "Origin": client["Origin"],
            "DS": self.get_ds(query, body),
        }

    def request(self, typ, device_fp=None, ds2=None, post=None, body=None, no_ds=None):
        m = self._url_map().get(typ)
        if not m:
            return None
        url = m["url"]
        query = m.get("query", "")
        if query:
            url += "?" + query
        b = ""
        if body is not None:
            b = body if isinstance(body, str) else json.dumps(body)
        if post is None:
            post = m.get("post", False)
        if no_ds is None:
            no_ds = m.get("no_ds", False)
        if ds2 is None:
            ds2 = m.get("ds2", False)
        headers = self._headers(query, b)
        if ds2:
            headers["DS"] = self.get_ds2()
        if device_fp:
            headers["x-rpc-device_fp"] = device_fp
        headers["x-rpc-device_id"] = self._device
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            if post or body is not None:
                resp = self.session.post(url, headers=headers, data=b or None, timeout=15)
            else:
                resp = self.session.get(url, headers=headers, timeout=15)
            return resp.json()
        except Exception as e:
            return {"retcode": -1, "message": f"请求异常: {e}"}

    # ---------- 便捷方法 ----------
    def get_user_roles(self):
        """根据 cookie 获取账号下所有星铁角色列表"""
        return self.request("srUser")

    def get_device_fp(self):
        """获取设备指纹，并做设备绑定（降低风控），失败时返回兜底值"""
        m = self._url_map().get("getFp")
        fp = None
        if m:
            body = dict(m["body"])
            body["seed_id"] = _random_hex(16)
            body["seed_time"] = str(int(time.time() * 1000))
            try:
                r = self.request("getFp", post=True, body=body, no_ds=True)
                if r and r.get("retcode") == 0:
                    fp = (r.get("data") or {}).get("device_fp")
            except Exception:
                pass
        # 国服/B服：绑定设备，降低风控
        if fp and self.server in ("prod_gf_cn", "prod_qd_cn"):
            try:
                for typ in ("deviceLogin", "saveDevice"):
                    b = dict(self._url_map()[typ]["body"])
                    b["device_id"] = self._device
                    b["registration_id"] = _random_hex(19)
                    self.request(typ, post=True, body=b, ds2=True)
            except Exception:
                pass
        if fp:
            return fp
        # 兜底指纹
        if self.uid and self.uid[:1] in ("6", "7", "8", "9"):
            return "38d805c20d53d"
        return "38d7f4c72b736"

    def get_note(self, device_fp=None):
        """体力"""
        return self.request("srNote", device_fp=device_fp)

    def get_card(self, device_fp=None):
        """探索卡片"""
        return self.request("srCard", device_fp=device_fp)

    def get_month(self, device_fp=None):
        """月收入"""
        return self.request("srMonth", device_fp=device_fp)

    def get_characters(self, device_fp=None):
        """角色列表"""
        return self.request("srCharacter", device_fp=device_fp)


# 角色 id -> 中文名 映射（从插件资源加载）
_CHAR_NAME = {}
_CHAR_DATA = {}
_RELIC_NAME = {}
_CONE_NAME = {}
import os as _os

def _load_char_data():
    global _CHAR_NAME, _CHAR_DATA
    if _CHAR_NAME:
        return
    # 优先用新版映射（GitHub StarRailRes），旧版兜底
    for p in (r"E:\StarRail-plugin\StarRail-plugin-main\resources\baseData\new_characters.json",
              r"E:\StarRail-plugin\StarRail-plugin-main\resources\panel\data\character.json"):
        try:
            d = json.load(open(p, encoding="utf-8"))
            for cid, c in d.items():
                _CHAR_NAME[str(cid)] = c.get("name", cid)
                _CHAR_DATA[str(cid)] = c
        except Exception:
            continue


def _load_relic_data():
    global _RELIC_NAME
    if _RELIC_NAME:
        return
    p = r"E:\StarRail-plugin\StarRail-plugin-main\resources\panel\data\relics.json"
    try:
        d = json.load(open(p, encoding="utf-8"))
        for rid, r in d.items():
            _RELIC_NAME[str(rid)] = r.get("name", rid)
    except Exception:
        pass


def _load_cone_data():
    global _CONE_NAME
    if _CONE_NAME:
        return
    # 优先新版映射
    for p in (r"E:\StarRail-plugin\StarRail-plugin-main\resources\baseData\new_light_cones.json",
              r"E:\StarRail-plugin\StarRail-plugin-main\resources\baseData\light_cones.json"):
        try:
            d = json.load(open(p, encoding="utf-8"))
            for cid, c in d.items():
                _CONE_NAME[str(cid)] = c.get("name", cid)
        except Exception:
            continue


def char_name(avatar_id):
    _load_char_data()
    return _CHAR_NAME.get(str(avatar_id), str(avatar_id))


def char_info(avatar_id):
    _load_char_data()
    return _CHAR_DATA.get(str(avatar_id), {})


def relic_name(tid):
    _load_relic_data()
    return _RELIC_NAME.get(str(tid), str(tid))


def cone_name(tid):
    _load_cone_data()
    return _CONE_NAME.get(str(tid), str(tid))


def fetch_panel(uid):
    """从 mihomo 面板 API 获取账号练度数据（无需 cookie）"""
    url = f"https://api.mihomo.me/sr_info/{uid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def fetch_panel_parsed(uid):
    """从 mihomo 面板解析接口获取完整练度数据（含属性/光锥/遗器解析名）"""
    url = f"https://api.mihomo.me/sr_info_parsed/{uid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


import urllib.request
