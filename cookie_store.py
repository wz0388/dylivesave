# -*- coding: utf-8 -*-
"""
Cookie 存取
------------------------------------
抖音直播间的部分消息（尤其是礼物 WebcastGiftMessage）需要带登录态 cookie 才推得下来，
没带的时候能收到弹幕/进场，但礼物一直是空的。

用法：把浏览器里复制出来的 Cookie 整串粘到 exe 同目录的 cookie.txt，程序每次连接时读取。
找不到文件、文件为空或全是注释，就按「没配 cookie」处理（行为跟以前一样）。
"""
import json
import os
import sys

FILE_NAME = "cookie.txt"

TEMPLATE = (
    "# 抖音 Cookie：把浏览器里复制出来的那一整串粘到下面这行，保存后重新点“连接”即可\n"
    "# 复制方法：浏览器打开 live.douyin.com → F12 → Network → 任选一条请求 →\n"
    "#           Request Headers 里的 cookie: 后面那一长串，整段复制下来\n"
    "# 说明：没有 cookie 时弹幕/进场正常，但礼物可能一条都收不到\n"
    "# 以 # 开头的行会被忽略\n"
)


def app_dir():
    """exe 所在目录（frozen 时不能用 __file__，那是临时解包目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def cookie_path():
    return os.path.join(app_dir(), FILE_NAME)


def load_cookie(path=None):
    """读 cookie.txt，返回可直接放进请求头的字符串；没配返回空串。

    支持四种写法：整串一行 / 多行拼接 / JSON {"cookie": "..."} / 带 "cookie: " 前缀
    """
    p = path or cookie_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return ""

    lines = [x.strip() for x in txt.splitlines()]
    lines = [x for x in lines if x and not x.startswith("#")]
    if not lines:
        return ""

    if lines[0].startswith("{"):
        try:
            obj = json.loads("".join(lines))
            return (obj.get("cookie") or "").strip()
        except Exception:
            pass

    if lines[0].lower().startswith("cookie:"):
        lines = [lines[0].split(":", 1)[1].strip()] + lines[1:]

    parts = [x.rstrip(";").strip() for x in lines]
    return "; ".join(x for x in parts if x)


def cookie_items(cookie):
    return [x.strip() for x in (cookie or "").split(";") if "=" in x]


def cookie_value(cookie, name):
    """取单个 cookie 的值（ttwid / sessionid 之类）"""
    for item in cookie_items(cookie):
        k, _, v = item.partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


def summary(path=None):
    """给界面用：(有没有, 多少项, 文件路径)"""
    c = load_cookie(path)
    n = len(cookie_items(c))
    return bool(n), n, path or cookie_path()


def ensure_template(path=None):
    """首次运行时生成一个带说明的空模板，方便用户知道往哪儿粘"""
    p = path or cookie_path()
    if os.path.exists(p):
        return False
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(TEMPLATE)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    ok, n, p = summary()
    print("cookie 文件:", p)
    print("已加载:", ok, "项数:", n)
