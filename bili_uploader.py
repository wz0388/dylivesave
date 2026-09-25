# -*- coding: utf-8 -*-
"""
把录制好的直播视频投稿到 B 站（供 record.py 调用，也可单独当库用）
----------------------------------------------------------------
能力：
  · 登录：复用 bili_credential.json，没有/坏了就扫码（record.py --bili-login）
  · 封面：默认取视频**第一帧**（cover_position = 0），可改成第 N 秒
  · TS → MP4：ffmpeg `-c copy` 无损转封装（不重编码，秒级），避免 B 站不认 TS 封装
  · 多分P：断流重连产生的 `_part2.ts` 会作为 P2、P3… 一起投成一个稿件

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
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
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
#   {seg} 这是第几段   {segments} 总段数（「分片即传」时总段数未知，会填 0）
#
# 注意 {date}/{time}/{datetime} 取的是**这一段录制的开始时间**（不是投稿时间）——
# 录播按 2 小时分段后，靠它区分同一天的多个稿件。
# 一定要带上 {time}，否则同一天的多段标题会重复，B 站会拒（短时间内标题不能相同）。
title_template = "{anchor} 直播回放 {date} {time}"
desc_template = """{anchor} 的直播回放（第 {seg} 段）
直播间：https://live.douyin.com/{room_id}
本段开始：{datetime}
清晰度：{quality}"""

cover_position = 0             # 封面取第几秒的画面；0 = 视频第一帧
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
    container: str = "ts"
    keep_mp4: bool = True
    min_size_mb: float = 1.0
    min_interval: int = 0
    base_dir: Path = APP_DIR


def write_config_template(path: Path = DEFAULT_CONFIG, force: bool = False) -> bool:
    """生成配置模板。已存在且非 force 时不动它，返回是否真的写了。"""
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


def load_config(path: Path | str = DEFAULT_CONFIG) -> tuple[BiliConfig, bool]:
    """读配置；文件不存在就先写一份模板。返回 (配置, 是否新建了模板)。"""
    path = Path(path)
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
        container=str(b.get("container") or "ts").lower(),
        keep_mp4=_as_bool(b.get("keep_mp4"), True),
        min_size_mb=float(b.get("min_size_mb") or 1),
        min_interval=int(b.get("min_interval") or 0),
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
        failed_path().write_text(json.dumps(items, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass


def remember_failed(item: dict, error: str = "") -> None:
    """投稿失败的稿件记到磁盘（跨重启也能补传）。"""
    safe = {}
    for k, v in (item or {}).items():
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


# ─────────────────── 投稿 ───────────────────
async def _pick_client():
    """确认 bilibili_api 真的能拿到 HTTP 客户端（它自己不带，靠第三方库）。

    bilibili_api 的客户端在库里是「装了哪个用哪个」，一个都没有时
    `get_client()` 会抛 ArgsException，而且**只在真正发请求时才炸** ——
    表现就是视频传完了、到封面上传那步才失败。所以上传前先探一下。
    """
    from bilibili_api.utils.network import get_client
    return type(get_client()).__name__


def _progress(log: Callable, progress_cb: Callable | None = None):
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
                     cover, cred, log: Callable, progress: Callable | None = None) -> dict:
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
                                _progress(log, progress))
    result = await uploader.start()
    return result or {}


def format_template(tpl: str, anchor: str, title: str = "", room_id: str = "",
                    quality: str = "", when: datetime | None = None,
                    parts: int = 1, seg: int = 1, segments: int = 1) -> str:
    """填模板。when 是**这一段录制的开始时间**，不是投稿时间。"""
    when = when or datetime.now()
    try:
        return tpl.format(
            anchor=anchor or "主播", title=title or "", room_id=room_id or "",
            quality=quality or "", parts=parts, seg=seg, segments=segments,
            date=when.strftime("%Y-%m-%d"), time=when.strftime("%H:%M"),
            datetime=when.strftime("%Y-%m-%d %H:%M:%S"),
        ).strip()
    except (KeyError, IndexError) as e:
        raise BiliError(f"模板里的占位符不认识：{e}（模板：{tpl!r}）") from e


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
) -> dict | None:
    """把**一个分段**的文件投成一个稿件。

    files 是这一段产出的所有文件：只有一个就是单 P；断流重连产生的多个文件
    会作为这个稿件的多个分P（P1/P2/…）。
    when 是这一段录制的**开始时间**，用来填标题/简介里的 {date}{time}{datetime}；
    seg_index / seg_total 对应占位符 {seg} / {segments}。
    progress(pct, done_chunks, total_chunks)：上传进度回调（给界面用）。

    返回 {"bvid": ...}；没有可投的文件时返回 None。
    任何可预期失败都抛 BiliError。
    """
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

    total_sec = 0.0
    too_long = []
    for p in usable:
        sec = probe_duration(p, ffmpeg)
        total_sec += sec
        if sec > MAX_DURATION_SEC:
            too_long.append(p.name)
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

    # ---- ③ 封面：第一帧
    cover_path = Path(tempfile.gettempdir()) / "bili_cover_douyin.jpg"
    cover = None
    log(f"[i] 抽取封面（第 {cfg.cover_position} 秒{'，即第一帧' if not cfg.cover_position else ''}）"
        f"← {to_upload[0].name}")
    if grab_frame(to_upload[0], cover_path, cfg.cover_position, ffmpeg):
        from bilibili_api.utils.picture import Picture
        cover = Picture().from_file(str(cover_path))
        log(f"[i] 封面已生成：{cover.width}x{cover.height}")
    else:
        raise BiliError("抽第一帧失败，拿不到封面（B 站投稿必须给封面）。"
                        "确认 ffmpeg 可用，或装 opencv-python 作为备选。")

    # ---- ④ 组标题/简介（时间用**这一段的开始时间**，不是投稿时间）
    now = when or datetime.now()
    title = format_template(cfg.title_template, anchor=anchor, title=live_title,
                            room_id=str(room_id), quality=quality, when=now,
                            parts=len(to_upload), seg=seg_index, segments=seg_total)
    desc = format_template(cfg.desc_template, anchor=anchor, title=live_title,
                           room_id=str(room_id), quality=quality, when=now,
                           parts=len(to_upload), seg=seg_index, segments=seg_total)
    if len(title) > 80:
        title = title[:80]
        log(f"[!] 标题超过 80 字，已截断：{title}")
    if len(desc) > 2000:
        desc = desc[:2000]
    if not title:
        title = f"{anchor or '直播'}回放 {now:%Y-%m-%d %H:%M}"
    tags = cfg.tags[:10] if cfg.tags else ["直播回放"]

    if len(to_upload) == 1:
        page_titles = [""]     # 单 P 留空，播放页只显示主标题
    else:
        page_titles = [f"第{i}段" for i in range(1, len(to_upload) + 1)]

    # ---- ⑤ 上传
    log(f"[i] 开始投稿第 {seg_index}/{seg_total} 段：{len(to_upload)} 个分P，"
        f"共 {total_sec / 60:.1f} 分钟")
    log(f"    标题：{title}")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(_do_upload(
        to_upload, page_titles, title=title, desc=desc, tid=cfg.tid, tags=tags,
        original=cfg.original, no_reprint=cfg.no_reprint,
        cover=cover, cred=_credential_object(read_credential(cfg.cred_file) or {}),
        log=log, progress=progress))
    bvid = (result or {}).get("bvid")
    if not bvid:
        raise BiliError(f"投稿没有返回 bvid，接口原样返回：{result}")
    log(f"[√] 投稿完成：https://www.bilibili.com/video/{bvid}")
    log("    稿件需审核通过后才会公开。")

    # ---- ⑥ 清理转封装临时文件
    if made_mp4 and not cfg.keep_mp4:
        for p in made_mp4:
            try:
                p.unlink()
                log(f"[i] 已删除临时文件 {p.name}")
            except Exception:
                pass

    return {"bvid": bvid}


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
) -> list[dict]:
    """把一次录制产出的**多个分段**分别投成独立稿件。

    segments: [{"start": datetime, "files": [路径, ...]}, ...]
              每段的 {start} 会填进标题的 {date}/{time}/{datetime}；
              同一段里断流重连产生的多个文件是该稿件的多个分P。

    某一段失败不影响其它段，返回成功的那些（每项 {"bvid": ...}）。
    """
    throttle = SubmitThrottle(getattr(cfg, "min_interval", 0))
    total = len(segments)
    out: list[dict] = []
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
                when=when, seg_index=i, seg_total=total, log=log)
        except Exception as e:
            log(f"[!] 第 {i} 段投稿失败，继续下一段：{type(e).__name__}: {e}")
            for line in explain_network_error(e):
                log("    " + line)
            continue
        if info:
            out.append(info)
    return out
