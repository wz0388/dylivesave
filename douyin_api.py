# -*- coding: utf-8 -*-
"""
抖音直播间接入层：查开播状态 / 拿房间信息。
只用 REST 接口 + 纯 Python 的 a_bogus 签名，不需要 Node。
"""

import json
import re
import urllib.parse

import requests

from vendor.ab_sign import ab_sign

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36")

_sessions = {}


def parse_rid(text: str) -> str:
    """把用户输入解析成直播间号（web_rid）。

    支持：纯数字房间号 / 字母数字抖音号 / 完整直播间链接 / v.douyin.com 短链。
    放在这里而不是 danmu.py —— 录制只需要它，不该依赖弹幕模块。
    """
    text = text.strip()
    m = re.search(r"live\.douyin\.com/(?:u/)?([A-Za-z0-9_.\-]{4,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_.\-]{4,}", text):
        return text
    if "v.douyin.com" in text:
        r = requests.get(text, headers={"User-Agent": UA}, allow_redirects=True, timeout=15)
        m = re.search(r"live\.douyin\.com/(?:u/)?([A-Za-z0-9_.\-]{4,})", r.url)
        if m:
            return m.group(1)
    raise SystemExit(
        f"无法从 {text!r} 里解析出直播间，请填抖音号（如 yall1102）、数字房间号"
        "（如 224951713326），或直接粘贴 https://live.douyin.com/xxx 链接"
    )


def _session(proxy=None):
    key = proxy or ""
    if key not in _sessions:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://live.douyin.com/",
        })
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        _sessions[key] = s
    return _sessions[key]


class RoomInfo:
    """一次查询的结果"""

    def __init__(self, ok=False, live=False, room_id=None, anchor="", title="",
                 online="", total="", like=0, cover="", error=""):
        self.ok = ok                  # 接口是否成功返回
        self.live = live              # 是否正在直播
        self.room_id = room_id
        self.anchor = anchor          # 主播昵称
        self.title = title            # 直播间标题
        self.online = online          # 当前在线观众（字符串，带"万"这类单位）
        self.total = total            # 累计观看
        self.like = like
        self.cover = cover
        self.error = error

    def __repr__(self):
        return (f"<RoomInfo ok={self.ok} live={self.live} anchor={self.anchor!r} "
                f"room_id={self.room_id} online={self.online} error={self.error!r}>")


def _cookie():
    """从 cookie.txt 读登录态；读不到就是空串（匿名）"""
    try:
        from cookie_store import load_cookie
        return load_cookie()
    except Exception:
        return ""


def _enter_api(web_rid, proxy=None, timeout=15, cookie=None):
    """调 webcast/room/web/enter/，返回 (payload, error)。出错时 payload 为 None"""
    s = _session(proxy)
    if cookie is None:
        cookie = _cookie()
    hdrs = {"User-Agent": UA}
    if cookie:
        hdrs["cookie"] = cookie
    try:
        if not cookie and not s.cookies.get("ttwid"):
            try:
                r = s.get("https://live.douyin.com/", headers=hdrs, timeout=timeout)
                if not s.cookies.get("ttwid") and r.cookies.get("ttwid"):
                    s.cookies.set("ttwid", r.cookies.get("ttwid"), domain=".douyin.com")
            except Exception:
                pass

        params = {
            "aid": "6383", "app_name": "douyin_web", "live_id": "1",
            "device_platform": "web", "language": "zh-CN",
            "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": "116.0.0.0",
            "web_rid": str(web_rid), "msToken": "",
        }
        query = urllib.parse.urlencode(params)
        url = ("https://live.douyin.com/webcast/room/web/enter/?" + query
               + "&a_bogus=" + urllib.parse.quote(ab_sign(query, UA), safe=""))

        resp = s.get(url, headers=hdrs, timeout=timeout)
        if not resp.text:
            return None, "接口返回空内容（可能触发了风控，稍后重试）"
        return resp.json(), ""
    except requests.RequestException as e:
        return None, f"网络错误：{type(e).__name__}"
    except (ValueError, KeyError, TypeError) as e:
        return None, f"解析失败：{type(e).__name__} {e}"


def fetch_room_info(web_rid, proxy=None, timeout=15, cookie=None):
    """查直播间状态。任何异常都吞掉并放进 RoomInfo.error，不抛给界面"""
    payload, err = _enter_api(web_rid, proxy, timeout, cookie)
    if payload is None:
        return RoomInfo(error=err)
    try:
        data = payload.get("data") or {}
        rooms = data.get("data") or []
        user = data.get("user") or {}
        if not rooms:
            # 房间存在但当前没有直播场次
            return RoomInfo(ok=True, live=False,
                            anchor=user.get("nickname", ""),
                            error="当前主播未直播")

        room = rooms[0]
        status = room.get("status")
        view = room.get("room_view_stats") or {}
        stats = room.get("stats") or {}
        total_user = stats.get("total_user_str") or ""
        info = RoomInfo(
            ok=True,
            live=(status == 2),
            room_id=str(room.get("id_str") or ""),
            anchor=user.get("nickname") or ((room.get("owner") or {}).get("nickname") or ""),
            title=room.get("title") or "",
            online=str(view.get("display_short") or stats.get("user_count_str") or ""),
            total=str(total_user),
            like=room.get("like_count") or (stats.get("like_count") or 0),
            cover=((room.get("cover") or {}).get("url_list") or [""])[0],
        )
        if not info.live:
            info.error = "当前主播未直播"
        return info
    except requests.RequestException as e:
        return RoomInfo(error=f"网络错误：{type(e).__name__}")
    except (ValueError, KeyError, TypeError) as e:
        return RoomInfo(error=f"解析失败：{type(e).__name__} {e}")


class RoomStreams:
    """一个房间的拉流地址（只给录制用）"""

    def __init__(self, ok=False, live=False, room_id="", anchor="", title="",
                 flv=None, hls=None, default_resolution="", error=""):
        self.ok = ok
        self.live = live
        self.room_id = room_id
        self.anchor = anchor
        self.title = title
        self.flv = flv or {}          # {"FULL_HD1": "http://...flv", ...}
        self.hls = hls or {}          # {"FULL_HD1": "http://.../index.m3u8", ...}
        self.default_resolution = default_resolution
        self.error = error

    def __repr__(self):
        return (f"<RoomStreams ok={self.ok} live={self.live} anchor={self.anchor!r} "
                f"qualities={sorted(self.flv)} error={self.error!r}>")


def _flat_urls(raw):
    """清晰度映射的值可能是字符串，也可能是 {"MAIN": url} 这样的字典"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if isinstance(v, str) and v.startswith("http"):
            out[k] = v
        elif isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str) and vv.startswith("http"):
                    out[k] = vv
                    break
    return out


def fetch_room_streams(web_rid, proxy=None, timeout=15, cookie=None):
    """拿直播间的拉流地址（flv / hls 各一张清晰度表）。没在播就是空表"""
    payload, err = _enter_api(web_rid, proxy, timeout, cookie)
    if payload is None:
        return RoomStreams(error=err)
    try:
        data = payload.get("data") or {}
        rooms = data.get("data") or []
        user = data.get("user") or {}
        if not rooms:
            return RoomStreams(ok=True, live=False, anchor=user.get("nickname", ""),
                               error="当前主播未直播")
        room = rooms[0]
        su = room.get("stream_url") or {}
        streams = RoomStreams(
            ok=True,
            live=(room.get("status") == 2),
            room_id=str(room.get("id_str") or ""),
            anchor=user.get("nickname") or ((room.get("owner") or {}).get("nickname") or ""),
            title=room.get("title") or "",
            flv=_flat_urls(su.get("flv_pull_url")),
            hls=_flat_urls(su.get("hls_pull_url_map")) or _flat_urls(su.get("hls_pull_url")),
            default_resolution=su.get("default_resolution") or "",
        )
        if not streams.live:
            streams.error = "当前主播未直播"
        elif not streams.flv and not streams.hls:
            streams.error = "接口没返回拉流地址（可能已被风控）"
        return streams
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return RoomStreams(error=f"解析失败：{type(e).__name__} {e}")


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    for rid in sys.argv[1:] or ["224951713326"]:
        print(rid, "->", fetch_room_info(rid))
        print(rid, "流 ->", fetch_room_streams(rid))
