# -*- coding: utf-8 -*-
"""
把录制好的直播视频投稿到 B 站（供 record.py 调用，也可单独当库用）
----------------------------------------------------------------
能力：
  · 登录：复用 bili_credential.json，没有/坏了就扫码（record.py --bili-login）
  · 封面：默认取视频**第一帧**（cover_position = 0），可改成第 N 秒
  · TS → MP4：ffmpeg `-c copy` 无损转封装（不重编码，秒级），避免 B 站不认 TS 封装
  · 多分P：**一场直播 = 一个稿件**。同一场直播按时间切出来的每一段（以及断流重连
    产生的 `_part2.ts`）都是这个稿件的 P1/P2/P3…；关播再开播算新的一场，投成新稿件。
    第一段用 archive/add 建稿，之后每段传完新文件再走 archive/edit 把分P列表补全。

（不管合集：投稿只管投上去。要归档到合集，用 09-15 工作区里的 bili_season.py。）

配置见同目录 bili.toml（缺失会自动生成一份带注释的模板）。

依赖：bilibili-api-python（另需 requests）+ ffmpeg 在 PATH 里。

本模块**不调用 sys.exit**，出错抛 BiliError；调用方决定怎么处理 ——
录制流程里投稿失败不该影响已经录好的文件。
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

def app_dir() -> Path:
    """可写配置放在哪儿。

    打包成单文件 exe 后 **必须** 用 exe 所在目录 —— onefile 模式下 __file__ 指向
    临时解包目录（sys._MEIPASS），把 bili.toml / 凭据写进去，程序一退出就没了。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DEFAULT_CONFIG = APP_DIR / "bili.toml"

# ── B 站接口（与已验证的实现一致）──────────────────────────────
MEMBER_API = "https://member.bilibili.com/x2/creative/web"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
QR_GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_SOURCE = "main-fe-header"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
REFERER = "https://member.bilibili.com/platform/upload/video/frame"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")

_POLL_DONE, _POLL_TIMEOUT, _POLL_CONF = 0, 86038, 86090

MAX_UPLOAD_BYTES = 4 * 1024 ** 3          # B 站单文件上限
MAX_DURATION_SEC = 5 * 3600               # B 站时长上限

#: 分P 标题默认模板：这一段的「开始时间-结束时间」（20:00-22:00）。
#: B 站播放器自己会在标题旁边标 P1/P2，所以模板里不用再写「第 N 段」。
DEFAULT_PART_TITLE = "{time}-{end}"


class BiliError(RuntimeError):
    """投稿流程里所有可预期的失败都抛这个，方便调用方统一兜住。"""


CONFIG_TEMPLATE = r'''# ============================================================
#  B 站投稿配置（record.py 录完直播后自动投稿用）
#  Windows 路径请用单引号，反斜杠不会被转义。
# ============================================================

[bili]
enabled = true                 # 录完是否自动投稿；命令行 --no-bili 可临时关掉
credential_file = 'bili_credential.json'   # 登录态；相对本配置文件所在目录
tid = 21                       # 分区 id：21 日常 / 138 搞笑 / 4 游戏 / 181 影视
tags = ["直播回放"]             # 标签 1~10 个
original = true                # true=原创
no_reprint = true              # true=禁止转载

# 标题/简介模板，可用占位符：
#   {anchor} 主播昵称   {title} 直播间标题   {date} 日期   {time} 时间
#   {datetime} 日期时间   {quality} 清晰度   {room_id} 房间号
#   {seg} 这是本次录制交出去的第几段   {segments} 总段数（「分段即传」时未知，填 0）
#   {part} 这一段从第几个分P开始   {parts} 这一段包含几个分P
#
# 注意 {date}/{time}/{datetime} 取的是**这一场直播的开始时间**（不是投稿时间）。
# 一场直播 = 一个稿件，关播再开播是另一场，靠时间区分同一天的多个稿件。
title_template = "{anchor} 直播回放 {date} {time}"
desc_template = """{anchor} 的直播回放（第 {seg} 段，分P 从第 {part} 段起）
直播间：https://live.douyin.com/{room_id}
本段开始：{datetime}
清晰度：{quality}"""

# 一场直播投成一个稿件，同一场直播的各分片当这个稿件的 P1/P2/P3…
# 关播再开播算新的一场，投成新稿件。设成 false 退回老行为：每段一个独立稿件。
multi_part = true
# 分P标题模板（播放器分P列表里显示的名字）。默认是「这一段的开始时间-结束时间」，
# 因为 B 站自己会在旁边标 P1/P2，不用再写「第 N 段」。
# 可用：{time} 开始时间  {end} 结束时间  {dur} 时长(00:00:00)  {start} 同 {time}
#       {part} 第几个分P  {date}/{datetime}/{anchor}/{quality}/{room_id}
# B 站分P标题上限 80 字。
part_title_template = "{time}-{end}"

# 投稿成功后删掉本地录制文件（默认关）。
# 只删**这一批真正传上去、并且确认投稿成功**的文件；被 min_size_mb 跳过、
# 超过 4GB 没传上去的都不动（它们还要留着补传）。
# ⚠️ 删了就找不回来 —— 想留着本地留档就别开。
delete_after_upload = false

# 上传成功后再等几小时才删（0 = 立刻删）。设成 6 就是「录完传完，6 小时后自动清理」，
# 留着这半天方便自己回看/补传；到期后由后台清理器删，程序没开也不会漏（写进队列文件）。
delete_delay_hours = 0

cover_position = 0             # 封面取第几秒的画面；0 = 视频第一帧

# 自定义封面图（jpg/png）。留空 = 按 cover_position 从视频里抽一帧。
# 路径用单引号写（Windows 反斜杠就不用转义了），例如 cover_file = 'D:/pics/封面.jpg'
cover_file = ''

container = 'ts'               # ts = 直接传（B 站云端转码，推荐）；mp4 = 先无损转封装再传
keep_mp4 = true                # container=mp4 时，转出来的 mp4 是否保留（原 ts 始终保留）
min_size_mb = 1                # 小于这个体积的片段不投稿，防止把空文件传上去
# 两次投稿之间的最小间隔（秒），0 = 不限。
# B 站有「上传过快」风控：短时间连投会被 406 拦下（要去网页端过一次人机验证才解除）。
# 多个主播同时录、或者用很短的分段测试时，建议设 300。
min_interval = 0

# 合集不在这里配 —— 投稿只管投上去。要归档到合集用 09-15 工作区里的 bili_season.py：
#   python bili_season.py add --season 合集名 --latest
'''


# ─────────────────── 配置 ───────────────────
def _resolve(base: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (base / p)


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


def _as_tags(v: Any) -> list[str]:
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    if isinstance(v, (list, tuple)):
        return [str(t).strip() for t in v if str(t).strip()]
    return []


@dataclass
class BiliConfig:
    enabled: bool = True
    cred_file: Path = APP_DIR / "bili_credential.json"
    tid: int = 21
    tags: list[str] = field(default_factory=lambda: ["直播回放"])
    title_template: str = "{anchor} 直播回放 {date} {time}"
    desc_template: str = "{anchor} 的直播回放\nhttps://live.douyin.com/{room_id}"
    original: bool = True
    no_reprint: bool = True
    cover_position: float = 0.0
    #: 自定义封面图（存在才用，否则从视频抽帧）
    cover_file: str = ""
    container: str = "ts"
    keep_mp4: bool = True
    min_size_mb: float = 1.0
    min_interval: int = 0
    multi_part: bool = True
    part_title_template: str = DEFAULT_PART_TITLE
    delete_after_upload: bool = False
    #: 上传成功后延后多少小时再删（0 = 立即删）
    delete_delay_hours: float = 0.0
    base_dir: Path = APP_DIR


def write_config_template(path: Path | None = None, force: bool = False) -> bool:
    """生成配置模板。已存在且非 force 时不动它，返回是否真的写了。"""
    path = Path(path or DEFAULT_CONFIG)
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


def set_config_value(key: str, literal: str,
                     path: Path | str | None = None) -> Path:
    """改 bili.toml 里 [bili] 段的某个键，**保留其它行和注释**。

    只替换 `key = 值` 里的值（行尾注释留着）；键不存在就插到 [bili] 段的第一行。
    界面上的「上传成功后删除本地视频」勾选框用它把开关写回配置 ——
    这样 GUI 和命令行读的是同一份配置，不会出现「界面勾了、命令行没勾」。
    """
    path = Path(path or DEFAULT_CONFIG)
    if not path.is_file():
        write_config_template(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"

    # 找 [bili] 段的范围
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\[bili\]\s*(#.*)?$", ln):
            start = i
            break
    if start is None:                       # 没有这一段就补一个
        lines.append("\n[bili]\n")
        start = len(lines) - 1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s*\[", lines[j]):
            end = j
            break

    pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)([^#\r\n]*?)(\s*#.*)?(\r?\n)?$")
    for j in range(start + 1, end):
        m = pat.match(lines[j])
        if m:
            lines[j] = f"{m.group(1)}{literal}{m.group(3) or ''}{m.group(4) or chr(10)}"
            break
    else:
        lines.insert(start + 1, f"{key} = {literal}\n")

    path.write_text("".join(lines), encoding="utf-8")
    return path


def load_config(path: Path | str | None = None) -> tuple[BiliConfig, bool]:
    """读配置；文件不存在就先写一份模板。返回 (配置, 是否新建了模板)。

    默认路径在**调用时**才取 DEFAULT_CONFIG —— 写成参数默认值的话，
    测试里改了 DEFAULT_CONFIG 也读不到（Python 默认参数只求值一次）。
    """
    path = Path(path or DEFAULT_CONFIG)
    created = write_config_template(path)

    raw: dict = {}
    if path.is_file():
        try:
            with path.open("rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise BiliError(
                f"配置文件不是合法的 TOML：{path}\n    {e}\n"
                "    最常见的坑：Windows 路径写在双引号里，反斜杠会被当转义符。\n"
                "    路径请用单引号，例如：credential_file = 'D:\\a\\b.json'"
            ) from e

    b = raw.get("bili") or {}
    base = path.parent

    cfg = BiliConfig(
        enabled=_as_bool(b.get("enabled"), True),
        cred_file=_resolve(base, str(b.get("credential_file") or "bili_credential.json")),
        tid=int(b.get("tid") or 21),
        tags=_as_tags(b.get("tags")) or ["直播回放"],
        title_template=str(b.get("title_template") or "{anchor} 直播回放 {date}"),
        desc_template=str(b.get("desc_template") or ""),
        original=_as_bool(b.get("original"), True),
        no_reprint=_as_bool(b.get("no_reprint"), True),
        cover_position=float(b.get("cover_position") or 0),
        cover_file=str(b.get("cover_file") or ""),
        container=str(b.get("container") or "ts").lower(),
        keep_mp4=_as_bool(b.get("keep_mp4"), True),
        min_size_mb=float(b.get("min_size_mb") or 1),
        min_interval=int(b.get("min_interval") or 0),
        multi_part=_as_bool(b.get("multi_part"), True),
        part_title_template=str(b.get("part_title_template") or DEFAULT_PART_TITLE),
        delete_after_upload=_as_bool(b.get("delete_after_upload"), False),
        delete_delay_hours=float(b.get("delete_delay_hours") or 0),
        base_dir=base,
    )
    if cfg.container not in ("mp4", "ts"):
        raise BiliError(f"[bili].container 只能是 'mp4' 或 'ts'，现在是 {cfg.container!r}")
    return cfg, created


# ─────────────────── 凭据 ───────────────────
_CRED_FIELDS = ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")


#: 凭据文件里的键是小写的（对应 Credential 的属性名），但 **B 站的 cookie 名大小写敏感**：
#: 必须发 `SESSDATA` / `DedeUserID`，发小写的 `sessdata` 会被当成没登录（code=-101）。
#: 这里踩过坑：误报「登录态失效」，实际账号好得很。
_COOKIE_NAMES = {
    "sessdata": "SESSDATA",
    "bili_jct": "bili_jct",
    "buvid3": "buvid3",
    "buvid4": "buvid4",
    "dedeuserid": "DedeUserID",
}


def check_cookie_status(cred: dict) -> tuple[str, str]:
    """上传前用 nav 接口实测一次 cookie 到底还认不认。返回 (状态, 说明)。

    只检查「字段在不在」是不够的：cookie 过期时字段一个不少、本地看着完全正常，
    于是几 GB 录播传完才在封面/提交那步炸（CredentialNoBiliJctException / -101）。
    先打一次只读接口，不可用就当场停下，别白传一场。

    状态 ∈ "ok" / "invalid"（明确未登录）/ "unknown"（检查本身没跑通）。
    网络原因导致的 unknown 只提示、不拦 —— B 站接口抖动不该挡着录制投稿。
    """
    if not cred or not str(cred.get("sessdata") or "").strip():
        return "invalid", "本地登录态里没有 SESSDATA"
    # ⚠️ 键必须换成 B 站的真实 cookie 名（大小写敏感），否则一律 -101
    cookies = {_COOKIE_NAMES[k]: str(cred.get(k)).strip()
               for k in _COOKIE_NAMES if cred.get(k)}
    try:
        import requests
        r = requests.get(NAV_URL, cookies=cookies, timeout=15,
                         headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        payload = r.json()
    except Exception as e:
        return "unknown", f"请求 nav 接口失败：{type(e).__name__}: {e}"

    code = payload.get("code")
    data = payload.get("data") or {}
    if code == -101 or (code == 0 and not data.get("isLogin")):
        return "invalid", f"B 站说未登录（code={code}）"
    if code != 0:
        msg = payload.get("message") or ""
        return "unknown", f"nav 返回 code={code} {msg}".strip()

    who = f"{data.get('uname') or '?'}（uid {data.get('mid') or '?'}）"
    if data.get("refresh"):
        return "ok", f"{who}；B 站建议刷新登录态（还能用，但快到期了）"
    return "ok", who


def read_credential(cred_file: Path) -> dict | None:
    """读登录态；缺 sessdata/bili_jct 时返回 None。"""
    if not cred_file.is_file():
        return None
    try:
        d = json.loads(cred_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return d if (d.get("sessdata") and d.get("bili_jct")) else None


def has_credential(cfg: BiliConfig) -> bool:
    return read_credential(cfg.cred_file) is not None


def _credential_object(d: dict):
    from bilibili_api import Credential
    return Credential(**{k: d.get(k) for k in _CRED_FIELDS})


def _save_credential(cred_file: Path, d: dict) -> None:
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: d.get(k) for k in _CRED_FIELDS}
    cred_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _collect_cookies(session, resp, data: dict) -> dict:
    """从 Set-Cookie / cookie jar / url 查询串三处收 cookie。

    不能用 bilibili_api.login_v2：它只从轮询响应的 url 查询串里取，B 站改版后
    那个字段已经废了，会**静默返回一个全空的凭据**，一路传到封面上传才炸。
    """
    jar: dict = {}
    try:
        for header in resp.raw.headers.getlist("Set-Cookie") or []:
            name, _, value = header.split(";", 1)[0].partition("=")
            if name.strip():
                jar[name.strip()] = value.strip()
    except Exception:
        pass
    try:
        for k, v in session.cookies.get_dict().items():
            jar.setdefault(k, v)
    except Exception:
        pass

    url = data.get("url") or ""
    if "?" in url:
        for part in url.split("?", 1)[1].split("&"):
            name, _, value = part.partition("=")
            if name.strip() and value and name.strip().lower() != "gourl":
                jar.setdefault(name.strip(), value.strip())
    return jar


class QrLogin:
    """B 站扫码登录，拆成分步调用，GUI 也能用。

    命令行直接看 login_qrcode()。GUI 的用法：

        qr = QrLogin()
        qr.begin()                       # 拿到二维码链接 qr.qr_link
        # 把 qr.qr_link 画成二维码图片显示出来
        while True:
            st = qr.poll()               # 'scan' / 'conf' / 'timeout' / 'done'
            if st in ("done", "timeout"):
                break
            time.sleep(2)
        cred = qr.credential()           # 拿不到关键字段会抛 BiliError
        qr.save(cred_file)
    """

    def __init__(self) -> None:
        self.session: requests.Session | None = None
        self.qr_link = ""
        self.qr_key = ""
        self.state = "new"               # new / scan / conf / timeout / done
        self.data: dict = {}
        self.uname = ""
        self.mid = ""
        self._resp = None

    def begin(self) -> str:
        """取二维码。返回二维码链接（内容就是 B 站 App 要扫的地址）。"""
        try:
            gen = requests.get(QR_GEN_URL, params={"source": QR_SOURCE},
                               headers={"User-Agent": UA}, timeout=30).json()
        except Exception as e:
            raise BiliError(f"请求登录二维码失败：{e}") from e
        if gen.get("code") != 0:
            raise BiliError(f"获取登录二维码失败：{gen}")
        self.qr_link = gen["data"]["url"]
        self.qr_key = gen["data"]["qrcode_key"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self.state = "scan"
        return self.qr_link

    def poll(self) -> str:
        """查一次扫码状态，返回 'scan' / 'conf' / 'timeout' / 'done'。"""
        if self.session is None:
            raise BiliError("还没调 begin()")
        try:
            self._resp = self.session.get(
                QR_POLL_URL, params={"qrcode_key": self.qr_key, "source": QR_SOURCE},
                timeout=30)
            self.data = (self._resp.json() or {}).get("data") or {}
        except Exception as e:
            raise BiliError(f"轮询登录状态失败：{e}") from e
        code = self.data.get("code")
        self.state = {_POLL_DONE: "done", _POLL_TIMEOUT: "timeout",
                      _POLL_CONF: "conf"}.get(code, "scan")
        return self.state

    def credential(self) -> dict:
        """取凭据。拿不到 SESSDATA / bili_jct 会抛 BiliError。"""
        jar = _collect_cookies(self.session, self._resp, self.data)
        cred = {
            "sessdata": jar.get("SESSDATA"),
            "bili_jct": jar.get("bili_jct"),
            "dedeuserid": jar.get("DedeUserID"),
            "buvid3": jar.get("buvid3"),
            "buvid4": jar.get("buvid4"),
            "ac_time_value": self.data.get("refresh_token") or jar.get("ac_time_value"),
        }
        if not (cred["sessdata"] and cred["bili_jct"]):
            got = ", ".join(sorted(jar)) or "（一个都没有）"
            raise BiliError(
                "扫码已确认，但拿不到 SESSDATA / bili_jct，无法投稿。\n"
                f"    收到的 cookie 字段：{got}\n"
                f"    响应 data 字段：{sorted(self.data.keys())}"
            )
        return cred

    def verify(self) -> str:
        """用 nav 接口确认真登录上了。返回昵称（没登录上返回空串）。"""
        try:
            nav = self.session.get(NAV_URL, timeout=30).json()
            info = nav.get("data") or {}
            if info.get("isLogin"):
                self.uname = info.get("uname") or ""
                self.mid = str(info.get("mid") or "")
        except Exception:
            pass
        return self.uname

    def save(self, cred_file: Path) -> dict:
        cred = self.credential()
        _save_credential(cred_file, cred)
        return cred


def login_qrcode(cred_file: Path, log: Callable = print) -> dict:
    """命令行用的扫码登录：终端打印二维码 → 轮询 → 保存凭据。"""
    try:
        import qrcode_terminal
    except ImportError as e:
        raise BiliError(f"缺少依赖 {e.name}：pip install qrcode-terminal") from e

    qr = QrLogin()
    qr.begin()
    log("请用 B 站 App 扫描下方二维码登录，并在手机上点击确认：\n")
    log(qrcode_terminal.qr_terminal_str(qr.qr_link))

    said_conf = False
    while True:
        st = qr.poll()
        if st == "done":
            break
        if st == "timeout":
            raise BiliError("二维码已过期，请重新运行")
        if st == "conf" and not said_conf:
            log("[i] 已扫码，请在手机上点击确认…")
            said_conf = True
        time.sleep(2)

    cred = qr.credential()
    if qr.verify():
        log(f"[√] 登录成功：{qr.uname}（mid {qr.mid}）")
    _save_credential(cred_file, cred)
    log(f"[√] 登录态已保存到 {cred_file}")
    return cred


# ─────────────────── 素材处理 ───────────────────
def grab_frame(video: Path, out: Path, at: float = 0.0, ffmpeg: str = "ffmpeg") -> bool:
    """抽一帧当封面。at=0 就是第一帧。ffmpeg 不行就用 cv2。"""
    exe = _which_ffmpeg(ffmpeg)
    if exe:
        cmd = [exe, "-y", "-loglevel", "error"]
        if at:
            cmd += ["-ss", str(at)]
        cmd += ["-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180)
            if out.is_file() and out.stat().st_size > 0:
                return True
        except Exception:
            pass

    try:
        import cv2
    except ImportError:
        return False
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            return False
        if at:
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * at))
        ok, frame = cap.read()
        return bool(ok and cv2.imwrite(str(out), frame))
    finally:
        cap.release()


def remux_to_mp4(src: Path, dst: Path, ffmpeg: str = "ffmpeg") -> bool:
    """TS → MP4，`-c copy` 不重编码。B 站对 mp4 封装最友好。"""
    exe = _which_ffmpeg(ffmpeg)
    if not exe:
        return False
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(src),
           "-c", "copy", "-movflags", "+faststart", str(dst)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except Exception:
        return False
    if p.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        return False
    return True


def _which_ffmpeg(ffmpeg: str = "ffmpeg") -> str:
    from shutil import which
    if ffmpeg and Path(ffmpeg).is_file():
        return ffmpeg
    return which(ffmpeg or "ffmpeg") or ""


def delete_uploaded(paths: list[Path], cfg: BiliConfig, log: Callable = print) -> int:
    """上传成功后删本地录制文件（bili.toml 里 delete_after_upload = true 才动手）。

    调用点都在**确认投稿成功之后**，而且只传这次真正上传的那几个文件：
    被 min_size_mb 跳过的、超过 4GB 没传上去的文件不在此列 —— 它们要留着补传。
    删不掉只记一行日志，不影响投稿结果。

    delete_delay_hours > 0 时**不立即删**，而是把文件排进延迟队列（见 schedule_delete），
    到期由 sweep_delete_queue() 清 —— 这样录完还能回看几小时。
    """
    if not getattr(cfg, "delete_after_upload", False):
        return 0
    delay = float(getattr(cfg, "delete_delay_hours", 0) or 0)
    if delay > 0:
        return schedule_delete(paths, delay, log)
    done = 0
    seen: set[str] = set()
    for p in paths:
        p = Path(p)
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if not p.is_file():
                continue
            mb = p.stat().st_size / 1024 / 1024
            p.unlink()
            done += 1
            log(f"[i] 已删除本地文件（{mb:.0f} MB，上传成功）：{p.name}")
        except Exception as e:
            log(f"[!] 删除失败，文件还在：{p}（{type(e).__name__}: {e}）")
    return done


# ─────────────────── 延迟删除（录完 x 小时后自动清） ───────────────────
DELETE_QUEUE_FILE = "bili_delete_queue.json"
_DELETE_LOCK = threading.Lock()      # 多个录制线程同时排/清，别写坏 json


def delete_queue_path() -> Path:
    return APP_DIR / DELETE_QUEUE_FILE


def load_delete_queue() -> list[dict]:
    try:
        d = json.loads(delete_queue_path().read_text(encoding="utf-8"))
        return [x for x in d if isinstance(x, dict) and x.get("path")]
    except Exception:
        return []


def _save_delete_queue(items: list[dict]) -> None:
    try:
        delete_queue_path().write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def schedule_delete(paths: list[Path], hours: float,
                    log: Callable = print) -> int:
    """把文件排进「到点就删」的队列，返回排进去的数量。

    队列落盘（bili_delete_queue.json），所以程序关了、重启了也不会漏删；
    重启后 sweep_delete_queue() 会把已经到期的清掉。
    """
    due = time.time() + max(0.0, float(hours)) * 3600
    n = 0
    with _DELETE_LOCK:
        items = load_delete_queue()
        have = {x["path"] for x in items}
        for p in paths:
            p = Path(p)
            if not p.is_file():
                continue
            key = str(p)
            if key in have:
                continue
            items.append({"path": key, "due": due,
                          "mb": p.stat().st_size / 1024 / 1024})
            have.add(key)
            n += 1
            log(f"[i] 已安排 {hours:g} 小时后删除：{p.name}")
        if n:
            _save_delete_queue(items)
    return n


def sweep_delete_queue(log: Callable = print,
                      protect: set[str] | None = None) -> int:
    """把到期的删掉（没到期/文件不在的留着或丢掉），返回删了几个。

    protect：这些路径先留着不删 —— 还在上传队列里、或者投稿失败要补传的文件
    删了就没法补传了。界面每次清理时把「还在用的文件」传进来。
    """
    items = load_delete_queue()
    if not items:
        return 0
    now = time.time()
    left: list[dict] = []
    done = 0
    guard = {str(x) for x in (protect or set())}
    for it in items:
        p = Path(it["path"])
        if not p.is_file():
            continue                       # 已经被删/移走了，从队列里消失
        if str(p) in guard:
            log(f"[i] 到点了但这个文件还要传，先留着：{p.name}")
            left.append(it)
            continue
        if float(it.get("due") or 0) > now:
            left.append(it)
            continue
        try:
            mb = p.stat().st_size / 1024 / 1024
            p.unlink()
            done += 1
            log(f"[i] 到期自动删除：{p.name}（{mb:.0f} MB）")
        except Exception as e:
            log(f"[!] 到期删除失败：{p}（{type(e).__name__}: {e}）")
            left.append(it)                # 下次再试
    if done or len(left) != len(items):
        with _DELETE_LOCK:
            _save_delete_queue(left)
    return done


def probe_duration(path: Path, ffmpeg: str = "ffmpeg") -> float:
    """拿时长（秒）；失败返回 0。

    优先 ffprobe（在 ffmpeg 旁边）。exe 里只内置了 ffmpeg 没打 ffprobe 时，
    退回解析 `ffmpeg -i` 打到 stderr 上的 `Duration: HH:MM:SS.ms`，
    不然日志里会一直显示「共 0.0 分钟」，看着像录了个空文件。
    """
    exe = _which_ffmpeg(ffmpeg)
    if not exe:
        return 0.0
    ffprobe = str(Path(exe).with_name("ffprobe.exe")) if exe.lower().endswith(".exe") \
        else "ffprobe"
    try:
        p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=120)
        return float((p.stdout or "0").strip() or 0)
    except Exception:
        pass

    try:
        p = subprocess.run([exe, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or "")
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def explain_network_error(e: Exception) -> list[str]:
    """把 bilibili_api 抛的网络错误翻译成人话（重点：406 是风控，不是网络问题）。"""
    text = f"{type(e).__name__}: {e}"
    if "406" in text:
        return [
            "这是 B 站的**风控**，不是网络问题，也不是程序坏了（HTTP 406 = 上传过快/投稿频繁）。",
            "解除办法（按顺序试）：",
            "  1. 浏览器打开 member.bilibili.com/platform/upload/video/frame 手动投稿一个，",
            "     会要求过一个人机验证 —— 过了验证风控即解除，这是最可靠的办法",
            "  2. 别短时间连投：bili.toml 里 [bili] min_interval 调大（建议 300 秒）",
            "  3. 等一段时间再试：风控有第二阶段，触发后大约 1 小时自动解除",
            "文件都还在本地 recordings/ 里，风控解除后重新投就行。",
        ]
    if "412" in text:
        return ["请求被 B 站风控直接拦掉（412，IP 被标记），换个网络或等一会再试。"]
    if "429" in text:
        return ["请求太频繁（429），等几分钟再试，别连续重试。"]
    if "登录" in text or "-101" in text:
        return ["B 站说没登录：登录态可能过期了，重新扫码登录一次。"]
    return []


class SubmitThrottle:
    """两次投稿之间的最小间隔。

    B 站有「上传过快」风控：短时间连投会被 406 拦下，还要去网页端过人机验证。
    多个主播共用同一个实例 —— 间隔是全局的，不是按主播算的。
    """

    def __init__(self, seconds: int):
        self.seconds = max(0, int(seconds or 0))
        self._last = 0.0

    def wait(self, log: Callable = print) -> None:
        if self.seconds <= 0:
            return
        left = self.seconds - (time.time() - self._last)
        if left > 0:
            log(f"[i] 距上次投稿不足 {self.seconds}s，等 {left:.0f}s 再投（防风控 406）")
            time.sleep(left)
        self._last = time.time()


# ─────────────────── 投稿失败的稿件（可补传） ───────────────────
# 投稿本身失败（比如 406 风控）时记下来，文件还在本地，点「补传」重投。
FAILED_FILE_NAME = "bili_failed_uploads.json"


def failed_path() -> Path:
    return APP_DIR / FAILED_FILE_NAME


def _fkey(item: dict) -> str:
    """一条失败记录的唯一键：按文件路径算（文件路径和段一一对应）"""
    return "|".join(sorted(str(f) for f in (item.get("files") or [])))


def load_failed() -> list[dict]:
    try:
        items = json.loads(failed_path().read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _save_failed(items: list[dict]) -> None:
    try:
        clean = [{k: (v.isoformat() if isinstance(v, datetime) else v)
                  for k, v in (it or {}).items() if k != "session"}
                 for it in items]
        failed_path().write_text(json.dumps(clean, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass


def remember_failed(item: dict, error: str = "") -> None:
    """投稿失败的稿件记到磁盘（跨重启也能补传）。"""
    safe = {}
    for k, v in (item or {}).items():
        # 场次对象里有 datetime，不能直接塞进 JSON；场次 key 已经在 session_key 里，
        # 对应的稿件号在 bili_sessions.json 里，补传时能找回来。
        if k == "session":
            continue
        try:
            safe[k] = v.isoformat() if isinstance(v, datetime) else v
        except Exception:
            safe[k] = str(v)
    safe["error"] = (error or "")[:300]
    safe["failed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    items = [x for x in load_failed() if _fkey(x) != _fkey(safe)]
    items.append(safe)
    _save_failed(items)


def remove_failed(item: dict) -> None:
    items = [x for x in load_failed() if _fkey(x) != _fkey(item)]
    _save_failed(items)


# ─────────────────── 直播场次 → 稿件（一场直播一个视频） ───────────────────
# 同一场直播要投成**同一个稿件**的多个分P，所以得记住「这一场对应哪个稿件」。
# bili_sessions.json: {"<房间号>-<开播时间>": {"bvid", "aid", "title", "updated"}}
# 存盘是为了「补传」—— 程序重启后，后到的分P还知道该往哪个稿件里加。
SESSION_FILE_NAME = "bili_sessions.json"
_SESSION_KEEP_DAYS = 90


def sessions_path() -> Path:
    return APP_DIR / SESSION_FILE_NAME


def load_sessions() -> dict:
    try:
        d = json.loads(sessions_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_sessions(d: dict) -> None:
    try:
        sessions_path().write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    except Exception:
        pass


def lookup_session(key: str) -> dict | None:
    """查这一场直播已经投到哪个稿件了（没有返回 None）。"""
    if not key:
        return None
    item = load_sessions().get(str(key))
    return item if isinstance(item, dict) and item.get("bvid") else None


def remember_session(key: str, *, bvid: str, aid: int = 0, title: str = "",
                     anchor: str = "") -> None:
    """记下这场直播对应的稿件 —— 后续分片靠它追加分P，重启后也认。"""
    if not key or not bvid:
        return
    d = load_sessions()
    d[str(key)] = {"bvid": bvid, "aid": int(aid or 0), "title": title or "",
                   "anchor": anchor or "",
                   "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    cutoff = time.time() - _SESSION_KEEP_DAYS * 86400
    for k in list(d):
        try:
            ts = datetime.strptime(str(d[k].get("updated") or ""),
                                   "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            continue
        if ts < cutoff:
            d.pop(k, None)
    _save_sessions(d)


def make_session(room_id: str, start: datetime) -> dict:
    """一场直播的内存状态。key 用「房间号-开播时间」，关播再开播就是另一场。"""
    return {"key": f"{room_id}-{start:%Y%m%d_%H%M%S}", "start": start,
            "rid": str(room_id), "bvid": None, "aid": 0, "parts": 0}


# ─────────────────── 合集（season） ───────────────────
# 投稿成功后把视频塞进用户指定的合集。接口都在创作中心域下，只认 cookie + csrf：
#   拉合集列表：GET  member.bilibili.com/x2/creative/web/seasons
#   加视频    ：POST member.bilibili.com/x2/creative/web/season/section/episodes/add
# ⚠️ 加视频加的是**小节**（section），不是合集本身 —— 合集默认那个小节叫「正片」，
#    所以选合集时要把它的第一个 section_id 一起带上。
SEASON_LIST_URL = "https://member.bilibili.com/x2/creative/web/seasons"
SEASON_ADD_URL = ("https://member.bilibili.com/x2/creative/web/season/"
                  "section/episodes/add")
ARCHIVE_VIEW_URL = "https://member.bilibili.com/x/vupre/web/archive/view"
PUBLIC_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
#: 这次加不进合集（多半是稿件还在审核）就先记下来，下次投稿/补传时再试
PENDING_SEASON_FILE = "bili_season_pending.json"


def _member_cookies(cred) -> dict:
    """Credential 对象或凭据 dict → 真名 cookie（SESSDATA / bili_jct …）。"""
    if isinstance(cred, dict):
        d = cred
    else:
        d = {k: getattr(cred, k, "") for k in _COOKIE_NAMES}
    return {_COOKIE_NAMES[k]: str(d.get(k)).strip()
            for k in _COOKIE_NAMES if str(d.get(k) or "").strip()}


def _member_headers() -> dict:
    return {"User-Agent": UA, "Referer":
            "https://member.bilibili.com/platform/upload/video/frame",
            "Origin": "https://member.bilibili.com"}


def list_seasons(cred) -> list[dict]:
    """账号下的所有合集。

    每项：{id, title, count, section_id, section_title}
    （section_id 是「正片」那个小节的 id，加视频时必须带）
    """
    seasons: list[dict] = []
    pn = 1
    while pn <= 20:                      # 最多 600 个，够用；防死循环
        r = requests.get(SEASON_LIST_URL,
                         params={"pn": pn, "ps": 30, "order": "ctime", "sort": "desc"},
                         cookies=_member_cookies(cred), headers=_member_headers(),
                         timeout=20)
        try:
            payload = r.json()
        except Exception as e:
            raise BiliError(f"合集列表返回的不是 JSON（HTTP {r.status_code}）：{e}") from e
        if payload.get("code") != 0:
            raise BiliError(f"拉合集列表失败：code={payload.get('code')} "
                            f"{payload.get('message') or ''}".strip())
        items = ((payload.get("data") or {}).get("seasons")) or []
        for it in items:
            meta = it.get("season") or it
            secs = ((it.get("sections") or {}).get("sections")) or []
            seasons.append({
                "id": int(meta.get("id") or 0),
                "title": meta.get("title") or "",
                "count": int(meta.get("epCount") or meta.get("ep_count") or 0),
                "section_id": int(secs[0].get("id") or 0) if secs else 0,
                "section_title": (secs[0].get("title") or "") if secs else "",
            })
        if len(items) < 30:
            break
        pn += 1
    return seasons


def _archive_ids(bvid: str, cred) -> tuple[int, int]:
    """拿稿件的 aid 和第一个分P 的 cid（加合集要这两个）。

    先用创作中心接口（自己的稿件，审核中也能查到），不行再退公开接口。
    """
    try:
        r = requests.get(ARCHIVE_VIEW_URL, params={"bvid": bvid},
                         cookies=_member_cookies(cred), headers=_member_headers(),
                         timeout=20)
        d = (r.json() or {}).get("data") or {}
        aid = int((d.get("archive") or {}).get("aid") or 0)
        vids = d.get("videos") or []
        cid = int((vids[0] or {}).get("cid") or 0) if vids else 0
        if aid and cid:
            return aid, cid
    except Exception:
        pass
    r = requests.get(PUBLIC_VIEW_URL, params={"bvid": bvid},
                     cookies=_member_cookies(cred), headers=_member_headers(), timeout=20)
    d = (r.json() or {}).get("data") or {}
    aid = int(d.get("aid") or 0)
    pages = d.get("pages") or []
    cid = int((pages[0] or {}).get("cid") or 0) if pages else 0
    if not (aid and cid):
        raise BiliError("拿不到稿件的 aid / cid（稿件还在审核中？）")
    return aid, cid


def add_video_to_season(bvid: str, season: dict | int, cred, title: str = "") -> str:
    """把稿件加进合集，返回合集名（给日志用）。

    season 可以是 picker 里存的 {"id","title","section_id"}，也可以只给合集 id
    （那就现查一遍列表找它的小节）。
    """
    sid, name = 0, ""
    if isinstance(season, dict):
        sid = int(season.get("section_id") or 0)
        name = season.get("title") or ""
        target = int(season.get("id") or 0)
    else:
        target = int(season or 0)
    if not sid:
        for s in list_seasons(cred):
            if s["id"] == target:
                sid, name = int(s["section_id"] or 0), s["title"] or name
                break
    if not sid:
        raise BiliError(f"合集 {target} 没有可用的小节（section），没法往里加视频")

    aid, cid = _archive_ids(bvid, cred)
    jct = _member_cookies(cred).get("bili_jct") or ""
    body = {"sectionId": int(sid),
            "episodes": [{"aid": aid, "cid": cid,
                          "title": (title or bvid)[:80], "charging_pay": 0}]}
    r = requests.post(f"{SEASON_ADD_URL}?csrf={jct}", json=body,
                      cookies=_member_cookies(cred), headers=_member_headers(),
                      timeout=25)
    try:
        payload = r.json()
    except Exception as e:
        raise BiliError(f"加合集返回的不是 JSON（HTTP {r.status_code}）：{e}") from e
    if payload.get("code") != 0:
        raise BiliError(f"加入合集失败：code={payload.get('code')} "
                        f"{payload.get('message') or ''}".strip())
    return name or f"合集 {target}"


def pending_season_path() -> Path:
    return APP_DIR / PENDING_SEASON_FILE


def load_pending_seasons() -> list[dict]:
    try:
        d = json.loads(pending_season_path().read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_pending_seasons(items: list[dict]) -> None:
    try:
        pending_season_path().write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def remember_pending_season(bvid: str, season, title: str = "", error: str = "") -> None:
    """这次没加进合集（多半是稿件还在审核）—— 记下来，下次再试。"""
    items = [it for it in load_pending_seasons()
             if not (it.get("bvid") == bvid and it.get("season") == season)]
    items.append({"bvid": bvid, "season": season, "title": title,
                  "error": error, "tries": 0,
                  "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    _save_pending_seasons(items)


def retry_pending_seasons(cred, log: Callable = print) -> int:
    """把之前没加进合集的稿件再试一遍，返回成功数。"""
    items = load_pending_seasons()
    if not items:
        return 0
    left: list[dict] = []
    done = 0
    for it in items:
        try:
            name = add_video_to_season(it.get("bvid") or "", it.get("season"),
                                       cred, it.get("title") or "")
            done += 1
            log(f"[√] 补加合集成功：{it.get('bvid')} → 《{name}》")
        except Exception as e:
            it["error"] = f"{type(e).__name__}: {e}"
            it["tries"] = int(it.get("tries") or 0) + 1
            it["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if it["tries"] < 8:
                left.append(it)
            else:
                log(f"[!] 加合集连续失败，先放弃：{it.get('bvid')}（{it['error']}）")
    _save_pending_seasons(left)
    return done


# ─────────────────── 投稿 ───────────────────
async def _pick_client():
    """确认 bilibili_api 真的能拿到 HTTP 客户端（它自己不带，靠第三方库）。

    bilibili_api 的客户端在库里是「装了哪个用哪个」，一个都没有时
    `get_client()` 会抛 ArgsException，而且**只在真正发请求时才炸** ——
    表现就是视频传完了、到封面上传那步才失败。所以上传前先探一下。
    """
    from bilibili_api.utils.network import get_client
    return type(get_client()).__name__


#: 上传多久没进度算「卡住」（秒）。B 站分块是并发的，正常情况下每几秒就会有一块。
STALL_SECONDS = 180
#: 卡住后最多重传几次（每次都是从头重传，已传的那部分作废）
STALL_RETRIES = 3
#: 两次重传之间等多久（秒）
STALL_RETRY_DELAY = 5


class UploadStalled(BiliError):
    """上传卡住了（长时间没有新分块），调用方应丢弃重传。"""


class _StallGuard:
    """盯着上传进度：STALL_SECONDS 秒没有新块就取消这次上传。

    为什么要独立线程：bilibili_api 的上传是一个 await 到底的协程，
    主线程卡在 asyncio.run() 里没法自己检查；另一个线程发现超时后
    loop.call_soon_threadsafe(task.cancel()) 把协程取消掉。
    """

    def __init__(self, timeout: float = STALL_SECONDS,
                 on_stall: Callable[[str], None] | None = None):
        self.timeout = timeout
        self.on_stall = on_stall
        self.last = time.time()
        self.done = -1
        self._stop = threading.Event()
        self._loop = None
        self._task = None
        self._fired = False
        self._thread: threading.Thread | None = None

    def touch(self, done: int) -> None:
        """有新分块了：只有 done 变化才算进度（回调可能重复触发）。"""
        if done != self.done:
            self.done = done
            self.last = time.time()

    def idle_seconds(self) -> float:
        return time.time() - self.last

    def bind(self, loop, task) -> None:
        self._loop, self._task = loop, task

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="upload-stall",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        step = max(0.2, min(5.0, float(self.timeout) / 5))
        while not self._stop.wait(step):
            if self.idle_seconds() >= self.timeout:
                self._fire()
                return

    def _fire(self) -> None:
        if self._fired:
            return
        self._fired = True
        if self.on_stall:
            try:
                self.on_stall(f"{int(self.timeout // 60)} 分钟没有进度")
            except Exception:
                pass
        try:
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        except Exception:
            pass


def custom_cover(cfg: BiliConfig, override: str = "") -> Path | None:
    """封面图：单场指定的 > 配置里的；都没有/文件不在返回 None（退回抽帧）。"""
    raw = str(override or getattr(cfg, "cover_file", "") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_file() else None


def _progress(log: Callable, progress_cb: Callable | None = None,
              guard: "_StallGuard | None" = None):
    """把库的分块事件换算成**整体进度百分比**。

    为什么不能直接用 chunk_number / total_chunk_count：
    库是**并发**上传分块的，chunk_number 是这一块的固定序号而不是「第几个完成」，
    直接拿它算百分比会乱跳；再把多分 P 的进度加起来才是整段的真实进度。

    ⚠️ B 站固定 10MB 一块（实测：223MB→23 块，2GB→205 块，4GB→410 块），
    所以大文件下「百分比」前几十兆一直在 0% —— 光看百分比会以为卡住了。
    因此除了百分比，也把**块数**（done/total）一起传出去，界面按
    「0%（2/205 块）」这样显示，从第一块就在动。

    progress_cb(pct, done_chunks, total_chunks)  —— 给界面用。
    日志里每 10% 打一行留轨迹（不管有没有 progress_cb）。
    """
    state = {"last": -1, "logged": -10}

    async def handler(data):
        total = data.get("total_chunk_count")
        cur = data.get("chunk_number")
        page = data.get("page")
        if not total or cur is None or page is None:
            return
        pages = state.setdefault("pages", {})
        key = id(page)
        pages[key] = max(pages.get(key, 0), cur + 1)      # 并发下只取最大值
        done_chunks = sum(pages.values())
        total_chunks = total * len(pages)
        pct = min(100, int(done_chunks / total_chunks * 100)) if total_chunks else 0
        state["last"] = pct
        if guard is not None:
            guard.touch(done_chunks)
        if progress_cb is not None:
            try:
                progress_cb(pct, done_chunks, total_chunks)
            except Exception:
                pass
        # 日志轨迹：每 10% 一行（第一块也打一次，好确认进度机制是活的）
        if pct >= state["logged"] + 10 or pct == 100:
            state["logged"] = pct
            log(f"    上传 {pct:3d}%（{done_chunks}/{total_chunks} 块）")

    return handler


async def _do_upload(paths: list[Path], page_titles: list[str], *, title: str, desc: str,
                     tid: int, tags: list[str], original: bool, no_reprint: bool,
                     cover, cred, log: Callable, progress: Callable | None = None,
                     guard: "_StallGuard | None" = None) -> dict:
    from bilibili_api import video_uploader

    pages = [video_uploader.VideoUploaderPage(path=str(p), title=t, description="")
             for p, t in zip(paths, page_titles)]
    kwargs: dict[str, Any] = {
        "tid": tid, "title": title, "desc": desc, "cover": cover,
        "tags": tags, "original": original, "no_reprint": no_reprint,
    }
    meta = video_uploader.VideoMeta(**kwargs)
    uploader = video_uploader.VideoUploader(pages=pages, meta=meta, credential=cred)
    uploader.add_event_listener(video_uploader.VideoUploaderEvents.AFTER_CHUNK.value,
                                _progress(log, progress, guard))
    result = await uploader.start()
    return result or {}


async def _do_append(bvid: str, paths: list[Path], *, cfg: BiliConfig, cred,
                     anchor: str = "", room_id: str = "", quality: str = "",
                     when: datetime | None = None, durs: list[float] | None = None,
                     multi: bool = True,
                     log: Callable = print,
                     progress: Callable | None = None,
                     guard: "_StallGuard | None" = None) -> dict:
    """往**已有稿件**追加分P。

    B 站没有「给已有稿件上传一个新分P」的接口，得自己拼两步：
      ① 用 uploader 的 preupload / 分块上传 / 提交分P 把新文件传上去 → filename + cid；
      ② 调 archive/edit，把**原分P列表 + 新分P**整体重交一遍（原有分P要带 cid，
         否则会被当成新分P重传 / 直接报错）。

    注意：edit 会把稿件重新送回审核，标题/简介/分区/标签/封面全部按现状原样带上，
    本函数不改这些字段 —— 只加分P。
    """
    from bilibili_api import video_uploader as vu
    from bilibili_api.utils.aid_bvid_transformer import bvid2aid
    from bilibili_api.utils.network import Api
    from bilibili_api.utils.picture import Picture
    from bilibili_api.video import Video

    # ---- ① 稿件现状：标题/简介/分区/标签/封面 + 已有分P的 filename
    try:
        info = (await Api(**vu._API["upload_args"], credential=cred)
                .update_params(bvid=bvid).result) or {}
    except Exception as e:
        raise BiliError(f"读不到稿件 {bvid} 的信息（{type(e).__name__}: {e}）") from e
    archive = info.get("archive") or {}
    old_pages = info.get("videos") or []
    if not old_pages:
        raise BiliError(f"稿件 {bvid} 里读不到已有分P列表，无法追加"
                        f"（接口返回：{str(info)[:200]}）")

    # edit 接口要求已有分P带上 cid。稿件信息里带就用，没带才去查播放器接口
    # （审核中的稿件有时查不到，那就等补传，不能拿错的 cid 硬提交）
    cids = [int(v.get("cid") or 0) for v in old_pages]
    if not all(cids):
        try:
            pages = await Video(bvid=bvid, credential=cred).get_pages()
        except Exception as e:
            raise BiliError(
                f"拿不到 {bvid} 的分P cid（{type(e).__name__}: {e}）。"
                "稿件可能还在审核中，过一会儿点「补传」重试即可"
            ) from e
        if len(pages) < len(old_pages):
            raise BiliError(f"稿件 {bvid} 的分P 信息不完整（{len(pages)}/{len(old_pages)}），"
                            "过一会儿点「补传」重试")
        cids = [int((pages[i] or {}).get("cid") or 0) for i in range(len(old_pages))]
    if not all(cids):
        raise BiliError(f"稿件 {bvid} 的分P cid 不完整，过一会儿点「补传」重试")

    videos = [{"title": v.get("title") or "", "desc": v.get("desc") or "",
               "filename": v.get("filename") or "", "cid": cids[i]}
              for i, v in enumerate(old_pages)]
    base = len(videos)

    # ---- ② 传新文件（只要 filename / cid，不建稿）
    # cover 传个空 Picture 就行：追加不动封面，不传 cover 时不该去读文件
    uploader = vu.VideoUploader(pages=[], meta={}, credential=cred,
                                cover=Picture(), line=None)
    uploader.line = await vu._choose_line(vu.Lines.BDA2)
    uploader.add_event_listener(vu.VideoUploaderEvents.AFTER_CHUNK.value,
                               _progress(log, progress, guard))
    titles = part_titles(cfg, len(paths), seg_start=when or datetime.now(),
                         durs=durs, first_part=base + 1, anchor=anchor,
                         room_id=room_id, quality=quality)
    for i, (p, name) in enumerate(zip(paths, titles), 1):
        log(f"[i] 上传第 {base + i} 个分P：{name}  ← {p.name}")
        data = await uploader._upload_page(
            vu.VideoUploaderPage(path=str(p), title=name, description=""))
        videos.append({"title": name, "desc": "",
                       "filename": data.get("filename") or "",
                       "cid": data.get("cid") or 0})

    # ---- ③ 整体重交分P列表（其它字段原样保留）
    tag = archive.get("tag") or ",".join(cfg.tags)
    meta: dict[str, Any] = {
        "aid": int(archive.get("aid") or bvid2aid(bvid)),
        "title": archive.get("title") or bvid,
        "copyright": int(archive.get("copyright") or 1),
        "tid": int(archive.get("tid") or cfg.tid),
        "tag": tag if isinstance(tag, str) else ",".join(map(str, tag)),
        "desc": archive.get("desc") or "",
        "desc_format_id": 0,
        "dynamic": "",
        "interactive": 0,
        "new_web_edit": 1,
        "act_reserve_create": 0,
        "handle_staff": False,
        "topic_grey": 1,
        "no_reprint": int(archive.get("no_reprint") or 0),
        "subtitles": {"lan": "", "open": 0},
        "web_os": 2,
        "cover": archive.get("cover") or "",
        "videos": videos,
        "csrf": cred.bili_jct,
    }
    params = {"csrf": cred.bili_jct, "t": int(time.time())}
    headers = {
        "content-type": "application/json;charset=UTF-8",
        "referer": "https://member.bilibili.com",
        "user-agent": UA,
    }
    try:
        await (Api(**vu._API["edit"], credential=cred, no_csrf=True, json_body=True)
               .update_params(**params).update_data(**meta)
               .update_headers(**headers).result)
    except Exception as e:
        raise BiliError(
            f"追加分P失败（{type(e).__name__}: {e}）。"
            "常见原因：稿件状态还在流转（审核中）/ 接口抖动 —— 过一会儿点「补传」"
            "重试即可，文件还在本地，不会白传"
        ) from e

    return {"bvid": bvid, "pages": len(videos), "added": len(paths)}


def _as_dt(v):
    """把「可能是字符串」的时间转回 datetime。

    补传时从 bili_failed_uploads.json 读回来的 start 是 isoformat 字符串，
    直接丢给 format_template 会 AttributeError: 'str' object has no attribute 'strftime'。
    """
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return datetime.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def format_template(tpl: str, anchor: str, title: str = "", room_id: str = "",
                    quality: str = "", when: datetime | None = None,
                    parts: int = 1, part: int = 1, seg: int = 1,
                    segments: int = 1, end: datetime | None = None,
                    dur: float = 0.0) -> str:
    """填模板。when 是**这一场直播（这一段）的开始时间**，不是投稿时间。

    end / dur 是给分P 标题用的：这一段的结束时间、时长（秒）。
    end 为 None（时长没探出来）时 {end} 填空串，{dur} 也空。
    """
    when = _as_dt(when) or datetime.now()
    end = _as_dt(end)
    try:
        return tpl.format(
            anchor=anchor or "主播", title=title or "", room_id=room_id or "",
            quality=quality or "", parts=parts, part=part, seg=seg, segments=segments,
            date=when.strftime("%Y-%m-%d"), time=when.strftime("%H:%M"),
            datetime=when.strftime("%Y-%m-%d %H:%M:%S"),
            start=when.strftime("%H:%M"),
            end=end.strftime("%H:%M") if end else "",
            dur=_hms(dur) if dur and dur > 0 else "",
        ).strip()
    except (KeyError, IndexError) as e:
        raise BiliError(f"模板里的占位符不认识：{e}（模板：{tpl!r}）") from e


def _hms(sec: float) -> str:
    """秒 → 时:分:秒（分P 标题里的时长用）。"""
    sec = max(0, int(sec or 0))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def part_title(cfg: BiliConfig, part: int, *, anchor: str = "", room_id: str = "",
               quality: str = "", when: datetime | None = None,
               dur: float = 0.0, multi: bool = True) -> str:
    """一个分P 的名字（播放器分P列表里显示的那行，最多 80 字）。

    默认模板是 `"{time}-{end}"` —— 这一段的**开始时间到结束时间**（20:00-22:00），
    比「第 1 段」好认：B 站自己会在标题旁边标 P1/P2，不用再写一遍序号。
    end 由 when + dur 算出来；dur 探不出来（文件坏了/抽不出时长）就只留开始时间。
    multi=False 是「每段一个独立稿件」的老模式，分P名固定成「第 N 段」。
    """
    if not multi:
        return f"第{part}段"
    tpl = getattr(cfg, "part_title_template", "") or DEFAULT_PART_TITLE
    end = (when + timedelta(seconds=float(dur))) if (when and dur and dur > 0) else None
    if end is None:
        # 时长未知：把范围那段去掉，别写成「20:00-」
        for sep in ("-", "~", "—", "至"):
            tpl = tpl.replace(f"{sep}{{end}}", "")
        tpl = tpl.replace("{end}", "")
    t = format_template(tpl, anchor=anchor, room_id=room_id, quality=quality,
                        when=when, part=part, seg=part, end=end, dur=dur)
    return (t or f"第{part}段")[:80]


def part_titles(cfg: BiliConfig, count: int, *, seg_start: datetime,
                durs: list[float] | None = None, first_part: int = 1,
                anchor: str = "", room_id: str = "", quality: str = "") -> list[str]:
    """一段录制（可能断流成好几个文件）对应的分P 标题列表。

    每个文件一个分P；时间和时长按**累计时长**往后推 —— 第 2 个文件的开头就是
    第 1 个文件的结尾（断流重连那几秒空档忽略），所以看到的是连续的时间区间。
    """
    durs = durs or []
    out: list[str] = []
    acc = 0.0
    for i in range(max(0, count)):
        d = float(durs[i]) if i < len(durs) and durs[i] and durs[i] > 0 else 0.0
        out.append(part_title(cfg, first_part + i, anchor=anchor, room_id=room_id,
                              quality=quality,
                              when=seg_start + timedelta(seconds=acc), dur=d))
        acc += d
    return out



def _run_upload_once(coro_factory, log: Callable, timeout: float | None = None):
    """跑一次上传协程，带「卡住就取消」的看门狗。"""
    guard = _StallGuard(timeout or STALL_SECONDS, on_stall=lambda why: log(
        f"[!] 上传卡住：{why}，取消这次上传，丢弃已传的部分重新来"))

    async def runner():
        guard.bind(asyncio.get_running_loop(), asyncio.current_task())
        guard.start()
        try:
            return await coro_factory(guard)
        finally:
            guard.stop()

    try:
        result = asyncio.run(runner())
        # bilibili_api 有时会把取消吞掉、返回空 dict —— 光看返回值会误判成
        # 「投稿没有返回 bvid」。看门狗真的响过，就按卡住处理，交给上层重传。
        if guard._fired:
            raise UploadStalled(f"{int(STALL_SECONDS // 60)} 分钟没有进度")
        return result
    except asyncio.CancelledError as e:      # 被看门狗 cancel 掉
        raise UploadStalled(f"{int(STALL_SECONDS // 60)} 分钟没有进度") from e
    except Exception as e:
        if guard._fired:                    # 取消过程中库又抛了别的，也算卡住
            raise UploadStalled(f"{int(STALL_SECONDS // 60)} 分钟没有进度") from e
        raise


def _upload_with_retry(coro_factory, *, log: Callable,
                       notify: Callable[[str], None] | None = None,
                       what: str = "上传"):
    """卡住就丢掉重传，最多 STALL_RETRIES 次；每次都从头传（旧进度作废）。"""
    for attempt in range(1, STALL_RETRIES + 1):
        try:
            return _run_upload_once(coro_factory, log)
        except UploadStalled as e:
            log(f"[!] {what}卡住（{e}），第 {attempt}/{STALL_RETRIES} 次，准备重传")
            if notify is not None:
                try:
                    notify(f"【上传卡住】{what} {e}，已丢弃重传"
                           f"（第 {attempt}/{STALL_RETRIES} 次）")
                except Exception:
                    pass
            if attempt >= STALL_RETRIES:
                raise BiliError(
                    f"{what}连续 {STALL_RETRIES} 次卡住，先不传了。"
                    "已记到待补传清单，稍后点「补传」重试") from e
            time.sleep(STALL_RETRY_DELAY)


def upload_recording(
    files: list[Path | str],
    *,
    anchor: str,
    cfg: BiliConfig,
    room_id: str = "",
    live_title: str = "",
    quality: str = "",
    ffmpeg: str = "ffmpeg",
    when: datetime | None = None,
    seg_index: int = 1,
    seg_total: int = 1,
    log: Callable = print,
    progress: Callable | None = None,
    session: dict | None = None,
    part: int = 0,
    season: dict | int | None = None,
    notify: Callable[[str], None] | None = None,
    cover: str = "",
) -> dict | None:
    """把**一段录制文件**交上去：这场直播还没投过就建新稿，投过了就追加成分P。

    files 是这一段产出的所有文件：断流重连产生的多个文件是这一段的多个连续分P。
    when 是这一段的**开始时间**；多分P模式下标题/简介统一用整场直播的开始时间。
    seg_index / seg_total 对应占位符 {seg} / {segments}。

    session: 一场直播的状态字典（见 make_session）。传了且 cfg.multi_part 为真时：
               · 这场直播已经建过稿 → 这些文件追加成该稿件的分P（_do_append）；
               · 还没建过稿 → 建一个新稿，并把 bvid 写回 session / bili_sessions.json。
             不传或 cfg.multi_part 为假 → 老行为，每次调用都是独立稿件。
    part: 这一段从第几个分P 起算（0 = 只用于日志，实际按稿件现有分P数算）。
    season: 建稿成功后要加入的合集（{"id","title","section_id"} 或合集 id）。
            只在**新建稿件**时加一次；追加分P 时视频已经在合集里了，不会重复加。

    progress(pct, done_chunks, total_chunks)：上传进度回调（给界面用）。

    返回 {"bvid", "appended", "pages"}；没有可投的文件时返回 None。
    任何可预期失败都抛 BiliError。
    """
    when = _as_dt(when)          # 补传时这里是 isoformat 字符串

    try:
        import bilibili_api  # noqa: F401
    except ImportError as e:
        raise BiliError("缺少 bilibili-api-python：pip install -U bilibili-api-python") from e

    # bilibili_api 自己不带 HTTP 客户端，必须有 curl_cffi / httpx / aiohttp 之一。
    # 缺了的话视频传完到封面那一步才炸，先在这里挡一道并说清怎么装。
    try:
        asyncio.run(_pick_client())
    except Exception as e:
        raise BiliError(
            "bilibili_api 找不到可用的 HTTP 客户端（需要一个第三方请求库）。\n"
            "    装一个就行：pip install curl_cffi   （或 httpx / aiohttp）\n"
            f"    原始错误：{e}"
        ) from e

    # ---- ① 挑文件
    src = [Path(f) for f in files]
    usable: list[Path] = []
    for p in src:
        if not p.is_file() or p.stat().st_size == 0:
            log(f"[!] 跳过（不存在或空文件）：{p}")
            continue
        if p.stat().st_size < cfg.min_size_mb * 1024 * 1024:
            log(f"[!] 跳过（小于 {cfg.min_size_mb} MB）：{p.name}")
            continue
        if p.stat().st_size > MAX_UPLOAD_BYTES:
            log(f"[!] 跳过（超过 B 站单文件 4GB）：{p.name} —— 建议降低清晰度或分段录制")
            continue
        usable.append(p)
    if not usable:
        log("[!] 没有可投稿的文件，跳过 B 站投稿")
        return None

    durs: list[float] = []
    too_long = []
    for p in usable:
        sec = probe_duration(p, ffmpeg)
        durs.append(sec)
        if sec > MAX_DURATION_SEC:
            too_long.append(p.name)
    total_sec = sum(durs)
    if too_long:
        raise BiliError(f"这些分段超过 B 站 5 小时时长上限：{', '.join(too_long)}")

    # ---- ② TS → MP4（无损转封装）
    to_upload = usable
    made_mp4: list[Path] = []
    if cfg.container == "mp4":
        to_upload = []
        for p in usable:
            if p.suffix.lower() == ".mp4":
                to_upload.append(p)
                continue
            dst = p.with_suffix(".mp4")
            log(f"[i] 无损转封装 {p.name} → {dst.name}（-c copy，不重编码）")
            if remux_to_mp4(p, dst, ffmpeg):
                to_upload.append(dst)
                made_mp4.append(dst)
            else:
                log(f"[!] 转封装失败，直接原样投稿：{p.name}")
                to_upload.append(p)

    # ---- ③ 这场直播投过稿了吗？投过就把这些文件追加成它的新分P
    seg_when = when or datetime.now()      # 这一段自己的开始时间（分P 标题用）
    video_when = seg_when                  # 稿件的标题/简介用（整场直播的开始时间）
    multi = bool(getattr(cfg, "multi_part", True) and session)
    target = ""
    if multi:
        target = str(session.get("bvid") or "")
        if not target:
            known = lookup_session(str(session.get("key") or ""))
            if known:
                target = str(known.get("bvid") or "")
                session["bvid"] = target
                session["aid"] = int(known.get("aid") or 0)
                log(f"[i] 这场直播之前投过稿：{target}（记在 {SESSION_FILE_NAME} 里）")
        # 同一场直播的所有分片共用一个稿件 —— 标题/简介里的时间用整场的开始时间，
        # 但**分P 标题**仍然用这一段自己的开始时间（否则每个P 都写同一个钟点）。
        if session.get("start"):
            video_when = session["start"]

    def _cleanup_mp4() -> None:
        if made_mp4 and not cfg.keep_mp4:
            for mp4 in made_mp4:
                try:
                    mp4.unlink()
                except Exception:
                    pass

    # 前面的分P 没传成功时，后面的分P 绝不能自己另起一个视频 —— 那样 P1 就跑到
    # 另一个稿件里去了，分P 顺序也乱了。记进待补传，补传时按 P1→P2→P3 顺序重投。
    if multi and not target and int(part or 0) > 1:
        raise BiliError(
            f"不能单独建稿：这是这场直播的第 {part} 个分P，前面的分P 还没传成功。"
            "已记入待补传清单，点「补传」会按顺序重投（文件都还在本地）")

    cred = _credential_object(read_credential(cfg.cred_file) or {})
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # ---- ④ 已有稿件：上传新文件 + 重交分P列表
    if target:
        log(f"[i] 第 {seg_index} 段：这场直播的稿件是 {target}，"
            f"这 {len(to_upload)} 个文件追加为分P"
            + (f"（从 P{part} 起）" if part else ""))
        try:
            added = _upload_with_retry(
                lambda g: _do_append(
                    target, to_upload, cfg=cfg, cred=cred, anchor=anchor,
                    room_id=str(room_id), quality=quality, when=seg_when,
                    durs=durs, log=log, progress=progress, guard=g),
                log=log, notify=notify, what=f"第 {seg_index} 段")
        finally:
            _cleanup_mp4()
        pages = int((added or {}).get("pages") or 0)
        log(f"[√] 已追加 {len(to_upload)} 个分P → "
            f"https://www.bilibili.com/video/{target}（共 {pages} P）")
        log("    编辑会把稿件重新送审，审核通过后新分P才可见。")
        delete_uploaded(usable + made_mp4, cfg, log)     # 上传成功才删
        return {"bvid": target, "appended": True, "pages": pages}

    # ---- ⑤ 封面：优先用配置里指定的图；没配才从视频抽帧
    #       （建新稿必须给封面；追加分P 不动封面）
    from bilibili_api.utils.picture import Picture
    cover = None
    mine = custom_cover(cfg, cover)
    if mine is not None:
        try:
            cover = Picture().from_file(str(mine))
            log(f"[i] 使用自定义封面：{mine}（{cover.width}x{cover.height}）")
        except Exception as e:
            log(f"[!] 自定义封面读不了，改用抽帧：{e}")
            cover = None
    if cover is None:
        cover_path = Path(tempfile.gettempdir()) / "bili_cover_douyin.jpg"
        log(f"[i] 抽取封面（第 {cfg.cover_position} 秒"
            f"{'，即第一帧' if not cfg.cover_position else ''}）← {to_upload[0].name}")
        if grab_frame(to_upload[0], cover_path, cfg.cover_position, ffmpeg):
            cover = Picture().from_file(str(cover_path))
            log(f"[i] 封面已生成：{cover.width}x{cover.height}")
        else:
            raise BiliError("抽第一帧失败，拿不到封面（B 站投稿必须给封面）。"
                            "确认 ffmpeg 可用，或装 opencv-python 作为备选。")

    # ---- ⑥ 组标题/简介（时间用**这一场直播的开始时间**，不是投稿时间）
    title = format_template(cfg.title_template, anchor=anchor, title=live_title,
                            room_id=str(room_id), quality=quality, when=video_when,
                            parts=len(to_upload), seg=seg_index, segments=seg_total)
    desc = format_template(cfg.desc_template, anchor=anchor, title=live_title,
                           room_id=str(room_id), quality=quality, when=video_when,
                           parts=len(to_upload), part=part or 1,
                           seg=seg_index, segments=seg_total)
    if len(title) > 80:
        title = title[:80]
        log(f"[!] 标题超过 80 字，已截断：{title}")
    if len(desc) > 2000:
        desc = desc[:2000]
    if not title:
        title = f"{anchor or '直播'}回放 {video_when:%Y-%m-%d %H:%M}"
    tags = cfg.tags[:10] if cfg.tags else ["直播回放"]

    if multi:
        # 分P 模式下一开始就把每页的标题写好 —— 后面追加的分P 会跟它对上号。
        # 标题是「这一段自己的开始时间-结束时间」，不是整场的时间。
        page_titles = part_titles(cfg, len(to_upload), seg_start=seg_when,
                                  durs=durs, first_part=1, anchor=anchor,
                                  room_id=str(room_id), quality=quality)
    elif len(to_upload) == 1:
        page_titles = [""]     # 单 P 留空，播放页只显示主标题
    else:
        page_titles = [f"第{i}段" for i in range(1, len(to_upload) + 1)]

    # ---- ⑦ 上传（建新稿）
    seg_show = (f"第 {seg_index}/{seg_total} 段" if seg_total and seg_total > 0
                else f"第 {seg_index} 段")
    log(f"[i] 开始投稿{seg_show}：{len(to_upload)} 个分P，"
        f"共 {total_sec / 60:.1f} 分钟")
    log(f"    标题：{title}")
    if multi:
        log(f"    这场直播后面的分片都会追加成这个稿件的分P")
    try:
        result = _upload_with_retry(
            lambda g: _do_upload(
                to_upload, page_titles, title=title, desc=desc, tid=cfg.tid,
                tags=tags, original=cfg.original, no_reprint=cfg.no_reprint,
                cover=cover, cred=cred, log=log, progress=progress, guard=g),
            log=log, notify=notify, what=f"第 {seg_index} 段")
    finally:
        _cleanup_mp4()
    bvid = (result or {}).get("bvid")
    if not bvid:
        raise BiliError(f"投稿没有返回 bvid，接口原样返回：{result}")
    if session is not None and multi:
        session["bvid"] = bvid
        session["aid"] = int((result or {}).get("aid") or 0)
        session["parts"] = len(to_upload)
        remember_session(str(session.get("key") or ""), bvid=bvid,
                         aid=session["aid"], title=title, anchor=anchor)
    log(f"[√] 投稿完成：https://www.bilibili.com/video/{bvid}")
    log("    稿件需审核通过后才会公开。")
    if season:
        # 视频已经投上去了，加合集是「锦上添花」：失败不影响投稿，记下来下次再试
        try:
            sname = add_video_to_season(bvid, season, cred, title)
            log(f"[√] 已加入合集《{sname}》")
        except Exception as e:
            log(f"[!] 加入合集失败（视频已经投上去了，不影响）：{e}")
            log("    已记到待加合集清单，下次投稿/补传时会自动再试")
            try:
                remember_pending_season(bvid, season, title, f"{type(e).__name__}: {e}")
            except Exception:
                pass
    delete_uploaded(usable + made_mp4, cfg, log)         # 上传成功才删
    return {"bvid": bvid, "appended": False, "pages": len(to_upload)}


def upload_segments(
    segments: list[dict],
    *,
    anchor: str,
    cfg: BiliConfig,
    room_id: str = "",
    live_title: str = "",
    quality: str = "",
    ffmpeg: str = "ffmpeg",
    log: Callable = print,
    session: dict | None = None,
) -> list[dict]:
    """把一次录制产出的**多个分段**交上去。

    cfg.multi_part = true（默认）：这些分段属于**同一场直播**，投成同一个稿件的
    P1/P2/P3…（第一段建稿，后面每段追加分P）。
    cfg.multi_part = false：每段一个独立稿件（老行为）。

    segments: [{"start": datetime, "files": [路径, ...]}, ...]
              每段的 {start} 填进标题的 {date}/{time}/{datetime}；
              同一段里断流重连产生的多个文件是该段的多个连续分P。

    某一段失败不影响其它段，返回成功的那些。
    """
    throttle = SubmitThrottle(getattr(cfg, "min_interval", 0))
    total = len(segments)
    multi = bool(getattr(cfg, "multi_part", True))
    if multi and session is None and segments:
        session = make_session(str(room_id),
                               segments[0].get("start") or datetime.now())
    out: list[dict] = []
    part = 1
    for i, seg in enumerate(segments, 1):
        files = list(seg.get("files") or [])
        when = seg.get("start") or datetime.now()
        log("")
        log(f"===== 第 {i}/{total} 段（{when:%Y-%m-%d %H:%M} 开始，{len(files)} 个文件）=====")
        try:
            throttle.wait(log)
            info = upload_recording(
                files, anchor=anchor, cfg=cfg, room_id=room_id, live_title=live_title,
                quality=quality, ffmpeg=ffmpeg,
                when=when, seg_index=i, seg_total=total, log=log,
                session=session, part=part)
        except Exception as e:
            log(f"[!] 第 {i} 段投稿失败，继续下一段：{type(e).__name__}: {e}")
            for line in explain_network_error(e):
                log("    " + line)
            part += len(files)
            continue
        part += len(files)
        if info:
            out.append(info)
    return out
