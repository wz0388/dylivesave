# -*- coding: utf-8 -*-
"""
把录制好的直播视频投稿到 B 站（供 record.py 调用，也可单独当库用）
----------------------------------------------------------------
能力：
  · 登录：复用 bili_credential.json，没有/坏了就扫码（record.py --bili-login）
  · 封面：默认取视频**第一帧**（cover_position = 0），可改成第 N 秒
  · TS → MP4：ffmpeg `-c copy` 无损转封装（不重编码，秒级），避免 B 站不认 TS 封装
  · 多分P：断流重连产生的 `_part2.ts` 会作为 P2、P3… 一起投成一个稿件
  · 合集：每个主播一个合集（`{anchor}` 模板 + 单独覆盖），不存在自动新建

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

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "bili.toml"

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
#   {seg} 这是第几段   {segments} 总段数
#
# 注意 {date}/{time}/{datetime} 取的是**这一段录制的开始时间**（不是投稿时间）——
# 录播按 2 小时分段后，靠它区分同一天的多个稿件。
# 一定要带上 {time}，否则同一天的多段标题会重复，B 站会拒（短时间内标题不能相同）。
title_template = "{anchor} 直播回放 {date} {time}"
desc_template = """{anchor} 的直播回放（第 {seg}/{segments} 段）
直播间：https://live.douyin.com/{room_id}
本段开始：{datetime}
清晰度：{quality}"""

cover_position = 0             # 封面取第几秒的画面；0 = 视频第一帧
container = 'ts'               # ts = 直接传（B 站云端转码，推荐）；mp4 = 先无损转封装再传
keep_mp4 = true                # container=mp4 时，转出来的 mp4 是否保留（原 ts 始终保留）
min_size_mb = 1                # 小于这个体积的片段不投稿，防止把空文件传上去

[season]
# ── 每个主播一个合集 ──
enabled = true
# 合集名模板：{anchor} 替换成主播昵称。
#   想要「主播各自一个合集」就保留 "{anchor}"；
#   想让所有主播都进同一个合集，就写成固定名字，例如 '直播回放'
name_template = "{anchor}"
create_if_missing = true       # 合集不存在就自动新建（新建要传封面，B站有人工审核）
desc_template = "{anchor} 的直播回放合集"

# 单独给某些主播指定合集名（优先级高于上面的模板），按需打开注释改
[season.anchors]
# "黄同学书屋" = "黄同学书屋的直播回放"
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
    cred_file: Path = HERE / "bili_credential.json"
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
    season_enabled: bool = True
    season_template: str = "{anchor}"
    season_create: bool = True
    season_desc_template: str = ""
    season_anchors: dict[str, str] = field(default_factory=dict)
    base_dir: Path = HERE


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
    s = raw.get("season") or {}
    anchors = {str(k): str(v) for k, v in (s.get("anchors") or {}).items()}
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
        season_enabled=_as_bool(s.get("enabled"), True),
        season_template=str(s.get("name_template") or "{anchor}"),
        season_create=_as_bool(s.get("create_if_missing"), True),
        season_desc_template=str(s.get("desc_template") or ""),
        season_anchors=anchors,
        base_dir=base,
    )
    if cfg.container not in ("mp4", "ts"):
        raise BiliError(f"[bili].container 只能是 'mp4' 或 'ts'，现在是 {cfg.container!r}")
    return cfg, created


# ─────────────────── 凭据 ───────────────────
_CRED_FIELDS = ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")


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


def login_qrcode(cred_file: Path, log: Callable = print) -> dict:
    """扫码登录并把凭据写到 cred_file。返回凭据 dict。"""
    try:
        import qrcode_terminal
    except ImportError as e:
        raise BiliError(f"缺少依赖 {e.name}：pip install qrcode-terminal") from e

    gen = requests.get(QR_GEN_URL, params={"source": QR_SOURCE},
                       headers={"User-Agent": UA}, timeout=30).json()
    if gen.get("code") != 0:
        raise BiliError(f"获取登录二维码失败：{gen}")
    qr_link = gen["data"]["url"]
    qr_key = gen["data"]["qrcode_key"]

    log("请用 B 站 App 扫描下方二维码登录，并在手机上点击确认：\n")
    log(qrcode_terminal.qr_terminal_str(qr_link))

    session = requests.Session()
    session.headers["User-Agent"] = UA
    said_conf = False
    data: dict = {}
    resp = None
    while True:
        resp = session.get(QR_POLL_URL,
                           params={"qrcode_key": qr_key, "source": QR_SOURCE},
                           timeout=30)
        data = (resp.json() or {}).get("data") or {}
        code = data.get("code")
        if code == _POLL_DONE:
            break
        if code == _POLL_TIMEOUT:
            raise BiliError("二维码已过期，请重新运行")
        if code == _POLL_CONF and not said_conf:
            log("[i] 已扫码，请在手机上点击确认…")
            said_conf = True
        time.sleep(2)

    jar = _collect_cookies(session, resp, data)
    cred = {
        "sessdata": jar.get("SESSDATA"),
        "bili_jct": jar.get("bili_jct"),
        "dedeuserid": jar.get("DedeUserID"),
        "buvid3": jar.get("buvid3"),
        "buvid4": jar.get("buvid4"),
        "ac_time_value": data.get("refresh_token") or jar.get("ac_time_value"),
    }
    if not (cred["sessdata"] and cred["bili_jct"]):
        got = ", ".join(sorted(jar)) or "（一个都没有）"
        raise BiliError(
            "扫码已确认，但拿不到 SESSDATA / bili_jct，无法投稿。\n"
            f"    收到的 cookie 字段：{got}\n"
            f"    响应 data 字段：{sorted(data.keys())}"
        )

    try:
        nav = session.get(NAV_URL, timeout=30).json()
        info = nav.get("data") or {}
        if info.get("isLogin"):
            log(f"[√] 登录成功：{info.get('uname')}（mid {info.get('mid')}）")
    except Exception:
        pass

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
    """拿时长（秒）；失败返回 0。"""
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
        return 0.0


# ─────────────────── 合集 ───────────────────
class SeasonClient:
    """新版合集（season）操作。旧的「列表」是另一套，别混。"""

    def __init__(self, cred: dict):
        self.cred = cred
        self.mid = cred.get("dedeuserid")
        self.jct = cred["bili_jct"]
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA, "Referer": REFERER,
            "Origin": "https://member.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        })
        jar = {"SESSDATA": cred["sessdata"], "bili_jct": self.jct}
        if self.mid:
            jar["DedeUserID"] = str(self.mid)
        self.s.cookies.update(jar)
        self._cache: list[dict] | None = None

    def seasons(self, refresh: bool = False) -> list[dict]:
        if self._cache is not None and not refresh:
            return self._cache
        try:
            j = self.s.get(f"{MEMBER_API}/seasons", params={"pn": 1, "ps": 50}, timeout=30).json()
        except Exception as e:
            raise BiliError(f"拉取合集列表失败：{e}") from e
        if j.get("code") != 0:
            raise BiliError(f"拉取合集列表失败：{j}")
        out: list[dict] = []
        for item in (j.get("data") or {}).get("seasons") or []:
            s = item.get("season") or {}
            secs = (item.get("sections") or {}).get("sections") or []
            out.append({
                "season_id": s.get("id"),
                "title": (s.get("title") or "").strip(),
                "sections": [{"id": x.get("id"),
                              "title": (x.get("title") or "").strip(),
                              "ep_count": x.get("epCount") or 0} for x in secs],
                "episodes": [{"aid": e.get("aid"), "bvid": e.get("bvid"),
                              "title": e.get("title")}
                             for e in (item.get("part_episodes") or [])],
            })
        self._cache = out
        return out

    def fetch_video(self, bvid: str) -> dict:
        try:
            j = requests.get(VIEW_API, params={"bvid": bvid},
                             headers={"User-Agent": UA}, timeout=30).json()
        except Exception as e:
            raise BiliError(f"查询视频信息失败：{e}") from e
        if j.get("code") != 0:
            raise BiliError(f"查询视频信息失败：{j}")
        d = j["data"]
        return {"aid": d["aid"], "bvid": d["bvid"], "cid": d["cid"], "title": d["title"]}

    def create(self, title: str, desc: str, cover) -> int:
        """新建合集。cover 支持 Picture 对象或图片路径。"""
        from bilibili_api.utils.picture import Picture
        from bilibili_api.video_uploader import upload_cover

        if isinstance(cover, Picture):
            pic = Picture.from_content(cover.content, cover.imageType or "jpg")
        else:
            p = Path(cover)
            if not p.is_file():
                raise BiliError(f"合集封面文件不存在：{p}")
            pic = Picture().from_file(str(p))

        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        cover_url = asyncio.run(upload_cover(pic, _credential_object(self.cred)))

        url = f"{MEMBER_API}/season/add"
        form = {"title": title, "desc": desc, "cover": cover_url,
                "season_price": 0, "csrf": self.jct}
        j = self.s.post(url, params={"csrf": self.jct}, data=form, timeout=30).json()
        if j.get("code") != 0:
            j = self.s.post(url, params={"csrf": self.jct}, json=form, timeout=30).json()
        if j.get("code") != 0:
            raise BiliError(f"新建合集「{title}」失败：{j}")
        return j.get("data")

    def add_episode(self, section_id: int, video: dict) -> dict:
        url = f"{MEMBER_API}/season/section/episodes/add"
        ep = {"aid": video["aid"], "cid": video["cid"],
              "title": video["title"], "charging_pay": 0}
        j = self.s.post(url, params={"csrf": self.jct},
                        json={"sectionId": section_id, "episodes": [ep]}, timeout=30).json()
        if j.get("code") == 0:
            return j
        # 文档参数表写 section_id/episode，实测生效的是 sectionId/episodes，两种都试
        j2 = self.s.post(url, params={"csrf": self.jct},
                         json={"section_id": section_id, "episode": [ep]}, timeout=30).json()
        if j2.get("code") == 0:
            return j2
        raise BiliError(f"加入合集失败：{j} / {j2}")

    def ensure_and_add(self, name: str, video: dict, *, create_if_missing: bool,
                       desc: str = "", cover=None, log: Callable = print,
                       wait_rounds: int = 3) -> bool:
        """确保合集 name 存在（没有就建），把 video 加进去。返回是否成功。"""
        def find():
            return next((r for r in self.seasons(refresh=True) if r["title"] == name), None)

        rec = find()
        if rec is None:
            if not create_if_missing:
                log(f"[!] 没有叫「{name}」的合集，且未开启自动新建，跳过")
                return False
            if cover is None:
                log(f"[!] 要新建合集「{name}」但没有可用封面，跳过")
                return False
            log(f"[i] 没有叫「{name}」的合集，新建一个…")
            season_id = self.create(name, desc, cover)
            log(f"[i] 已创建合集 season_id={season_id}（B 站有人工审核）")
            for _ in range(wait_rounds):
                rec = find()
                if rec is not None:
                    break
                time.sleep(2)
            if rec is None:
                log(f"[!] 新合集「{name}」还没出现在接口里，稍后手动补：\n"
                    f"    bili_season.py add --season \"{name}\" --bvid {video['bvid']}")
                return False

        if not rec["sections"]:
            log(f"[!] 合集「{name}」下没有小节，无法自动加入")
            return False

        sec = rec["sections"][0]
        if any(e.get("aid") == video["aid"] for e in rec["episodes"]):
            log(f"[i] 稿件已在合集「{name}」里，无需重复添加")
            return True

        self.add_episode(sec["id"], video)
        log(f"[√] 已加入合集「{name}」/「{sec['title']}」")
        log(f"    https://space.bilibili.com/{self.mid}"
            f"/channel/collectiondetail?sid={rec['season_id']}")
        return True


# ─────────────────── 投稿 ───────────────────
def _progress(log: Callable):
    state = {"last": -1}

    async def handler(data):
        total = data.get("total_chunk_count")
        cur = data.get("chunk_number")
        if not total or cur is None:
            return
        pct = int((cur + 1) / total * 100)
        if pct != state["last"] and pct % 2 == 0:
            state["last"] = pct
            log(f"    上传 {pct:3d}%")

    return handler


async def _do_upload(paths: list[Path], page_titles: list[str], *, title: str, desc: str,
                     tid: int, tags: list[str], original: bool, no_reprint: bool,
                     cover, cred, log: Callable) -> dict:
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
                                _progress(log))
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


def season_name_for(cfg: BiliConfig, anchor: str, override: str | None = None) -> str:
    """这个主播该进哪个合集：优先 override，其次 [season.anchors] 里的单独指定，
    最后套 name_template（默认 {anchor}，即每个主播一个同名合集）。"""
    if override:
        return override.strip()
    if anchor and anchor in cfg.season_anchors:
        return cfg.season_anchors[anchor].strip()
    return format_template(cfg.season_template, anchor=anchor)


def upload_recording(
    files: list[Path | str],
    *,
    anchor: str,
    cfg: BiliConfig,
    room_id: str = "",
    live_title: str = "",
    quality: str = "",
    ffmpeg: str = "ffmpeg",
    season_override: str | None = None,
    when: datetime | None = None,
    seg_index: int = 1,
    seg_total: int = 1,
    log: Callable = print,
) -> dict | None:
    """把**一个分段**的文件投成一个稿件，然后进该主播的合集。

    files 是这一段产出的所有文件：只有一个就是单 P；断流重连产生的多个文件
    会作为这个稿件的多个分P（P1/P2/…）。
    when 是这一段录制的**开始时间**，用来填标题/简介里的 {date}{time}{datetime}；
    seg_index / seg_total 对应占位符 {seg} / {segments}。

    返回 {"bvid":..., "season": 合集名或None}；没有可投的文件时返回 None。
    任何可预期失败都抛 BiliError。
    """
    try:
        import bilibili_api  # noqa: F401
    except ImportError as e:
        raise BiliError(f"缺少 bilibili-api-python：pip install -U bilibili-api-python") from e

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
        cover=cover, cred=_credential_object(read_credential(cfg.cred_file) or {}), log=log))
    bvid = (result or {}).get("bvid")
    if not bvid:
        raise BiliError(f"投稿没有返回 bvid，接口原样返回：{result}")
    log(f"[√] 投稿完成：https://www.bilibili.com/video/{bvid}")
    log("    稿件需审核通过后才会公开。")

    # ---- ⑥ 合集
    season_used = None
    if cfg.season_enabled:
        name = season_name_for(cfg, anchor, season_override)
        if name:
            season_used = name
            log(f"[i] 合集：把稿件放进「{name}」")
            desc_season = format_template(cfg.season_desc_template, anchor=anchor, when=now) \
                if cfg.season_desc_template else ""
            try:
                sc = SeasonClient(read_credential(cfg.cred_file) or {})
                video = sc.fetch_video(bvid)
                sc.ensure_and_add(name, video, create_if_missing=cfg.season_create,
                                  desc=desc_season, cover=cover, log=log)
            except BiliError as e:
                log(f"[!] 合集处理失败（稿件已提交成功）：{e}")
            except Exception as e:
                log(f"[!] 合集处理出错（稿件已提交成功）：{type(e).__name__}: {e}")

    # ---- ⑦ 清理转封装临时文件
    if made_mp4 and not cfg.keep_mp4:
        for p in made_mp4:
            try:
                p.unlink()
                log(f"[i] 已删除临时文件 {p.name}")
            except Exception:
                pass

    return {"bvid": bvid, "season": season_used}


def upload_segments(
    segments: list[dict],
    *,
    anchor: str,
    cfg: BiliConfig,
    room_id: str = "",
    live_title: str = "",
    quality: str = "",
    ffmpeg: str = "ffmpeg",
    season_override: str | None = None,
    log: Callable = print,
) -> list[dict]:
    """把一次录制产出的**多个分段**分别投成独立稿件，全部进该主播的合集。

    segments: [{"start": datetime, "files": [路径, ...]}, ...]
              每段的 {start} 会填进标题的 {date}/{time}/{datetime}；
              同一段里断流重连产生的多个文件是该稿件的多个分P。

    某一段失败不影响其它段，返回成功的那些（每项 {"bvid":.., "season":..}）。
    """
    total = len(segments)
    out: list[dict] = []
    for i, seg in enumerate(segments, 1):
        files = list(seg.get("files") or [])
        when = seg.get("start") or datetime.now()
        log("")
        log(f"===== 第 {i}/{total} 段（{when:%Y-%m-%d %H:%M} 开始，{len(files)} 个文件）=====")
        try:
            info = upload_recording(
                files, anchor=anchor, cfg=cfg, room_id=room_id, live_title=live_title,
                quality=quality, ffmpeg=ffmpeg, season_override=season_override,
                when=when, seg_index=i, seg_total=total, log=log)
        except BiliError as e:
            log(f"[!] 第 {i} 段投稿失败，继续下一段：{e}")
            continue
        except Exception as e:
            log(f"[!] 第 {i} 段投稿出错，继续下一段：{type(e).__name__}: {e}")
            continue
        if info:
            out.append(info)
    return out
