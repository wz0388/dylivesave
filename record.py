# -*- coding: utf-8 -*-
"""
抖音直播录制：只拉视频流，交给 ffmpeg 直接封装成 .ts（不转码）
------------------------------------------------------------------
流程：房间号 → webcast/room/web/enter/ 取拉流地址（flv / hls）→ ffmpeg -c copy -f mpegts 落盘。
全程不解码不重编码，CPU 占用极低，画质与原流一致。

用法:
    python record.py 805104115974                      # 一直录到 Ctrl+C，每 2 小时切一段
    python record.py 805104115974 --duration 300       # 录 5 分钟自动停
    python record.py https://live.douyin.com/805104115974 --quality SD1
    python record.py 805104115974 --list               # 只看有哪些清晰度
    python record.py 805104115974 --dir D:\录播 --wait 600
    python record.py 805104115974 --segment 3600       # 改成每 1 小时切一段
    python record.py 805104115974 --segment 0          # 不分段，整场一个文件
    python record.py --bili-login                      # 一次性：扫码登录 B 站

输出默认在项目目录的 recordings/ 下：主播名_时间_清晰度.ts。断流重连会接着写
_part2.ts；切成新的一段时会用**新一段的开始时间**重新起名，所以文件名能看出分段。
需要 ffmpeg 在 PATH 里（或用 --ffmpeg 指定路径）。

录完（直播结束 / 到时长 / Ctrl+C）会按 bili.toml 的配置把**每段单独投成一个 B 站稿件**，
标题里带该段的开始时间（不管合集）。不想投稿加 --no-bili。
"""
import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from douyin_api import UA, fetch_room_streams, fetch_room_info, parse_rid   # noqa: E402
from cookie_store import load_cookie                              # noqa: E402

def _app_dir():
    """录制产物放哪儿。打包成单文件 exe 时要用 exe 所在目录 ——
    onefile 模式下 __file__ 指向临时解包目录，录到那儿一退出就没了。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return HERE


DEFAULT_DIR = os.path.join(_app_dir(), "recordings")

#: 清晰度优先级（抖音返回哪些取决于主播推流，通常 FULL_HD1/SD1/SD2）
QUALITY_ORDER = ["FULL_HD1", "ORIGIN", "HD1", "SD1", "SD2", "LD"]
QUALITY_DESC = {"FULL_HD1": "原画/高清", "ORIGIN": "原画", "HD1": "高清",
                "SD1": "标清", "SD2": "较低", "LD": "流畅"}

#: 未开播时的重试间隔（秒）
WAIT_INTERVAL = 30
#: 给没传 stop_event 的调用方用的哑对象（永远不置位）
_NEVER_SET = threading.Event()


class RecordError(RuntimeError):
    """录制的可预期失败（找不到 ffmpeg / 查询失败 / 主播没开播 …）。

    抛异常而不是 sys.exit —— GUI 是在后台线程里跑录制的，sys.exit 在子线程里
    只会把那个线程干掉，界面上什么都看不到。命令行里由 main() 转成 sys.exit。
    """


def fmt_dur(sec):
    """把秒数写成人看的样子：1h23m45s"""
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def log(*a):
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)
    except Exception:
        pass


#: 模块级 log 的别名。下面有些函数会定义同名的局部 log 来遮蔽它，
#: 遮蔽之后函数体里就再也拿不到模块级那个了，所以先留个别名备用。
_module_log = log


def safe_name(text, fallback="live"):
    """文件名去掉非法字符"""
    t = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (text or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:40] or fallback


def pick_quality(available, wanted):
    """挑清晰度：指定就用指定的，auto/没命中就按优先级挑第一个有的"""
    if not available:
        return None, None
    if wanted and wanted != "auto":
        if wanted in available:
            return wanted, available[wanted]
        log(f"没有清晰度 {wanted}，可用的是 {sorted(available)}，改用默认策略")
    for q in QUALITY_ORDER:
        if q in available:
            return q, available[q]
    q = sorted(available)[0]
    return q, available[q]


#: Windows 下「创建进程时不分配控制台窗口」
CREATE_NO_WINDOW = 0x08000000


def find_ffmpeg(explicit=""):
    """定位 ffmpeg。

    优先级：命令行/界面指定 > **打进 exe 里的那份** > PATH 里的。
    （打包时用 --add-binary 把 ffmpeg.exe 放到 _MEIPASS 根目录，
      这样 exe 拷到哪都能录，不依赖机器上装没装 ffmpeg。）
    """
    if explicit:
        return explicit if os.path.exists(explicit) else explicit
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
    return "ffmpeg"


def ffmpeg_creationflags():
    """启动 ffmpeg 用的 Windows 进程标志。

    ffmpeg 是**控制台程序**：打包成 GUI exe（--windowed，本身没有控制台）时，
    系统会给它新建一个控制台窗口 —— 表现为录制时莫名弹出黑框。
    加上 CREATE_NO_WINDOW 就不会弹。已实测：加了它之后 CTRL_BREAK 依然能
    让 ffmpeg 优雅收尾（rc=0），所以隐藏窗口不影响收尾质量。

    源码运行时不加（保留 ffmpeg 的输出，方便排错）。
    """
    if os.name != "nt":
        return 0
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if getattr(sys, "frozen", False):
        flags |= CREATE_NO_WINDOW
    return flags


def build_cmd(ffmpeg, url, out, duration, extra=""):
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
        # 网络：超时 + 自动重连（http 协议选项）
        "-rw_timeout", "15000000",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-fflags", "+genpts",
        "-headers", f"Referer: https://live.douyin.com/\r\nUser-Agent: {UA}\r\n",
        "-i", url,
        "-c", "copy",            # 不转码，直接拷贝
        "-f", "mpegts",          # 封装成 TS
    ]
    if duration and duration > 0:
        cmd += ["-t", str(int(duration))]
    if extra:
        cmd += extra.split()
    cmd.append(out)
    return cmd


def human(size):
    mb = size / 1024 / 1024
    return f"{mb:.1f} MB" if mb < 1024 else f"{mb / 1024:.2f} GB"


def probe(path, ffmpeg_dir=""):
    """用 ffprobe 报一下结果；没装就算了"""
    exe = "ffprobe"
    if ffmpeg_dir:
        cand = os.path.join(os.path.dirname(ffmpeg_dir), "ffprobe.exe")
        if os.path.exists(cand):
            exe = cand
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries",
             "format=duration,size:stream=codec_name,width,height",
             "-of", "default=nw=1", path],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return ""
    return (out.stdout or "").strip()


class UploadWorker:
    """后台上传队列：分段一录完就扔进来，录制线程立刻接着录下一段。

    为什么必须后台化：
      · 投稿失败要重试/退避时不阻塞录制（406 风控 + 网络抖动都在这条队列里处理）；
      · 多个主播同时录时，一个人的上传不该拖住其它人的录制。

    串行上传（一次只投一个）：既是带宽考虑，也避免 bilibili_api 被多线程同时调用。
    """

    def __init__(self, *, config_path="", force=False, no_bili=False,
                 log=None, progress=None):
        """progress(seq, phase, pct)：上传事件回调，给界面刷「上传」分页用。

        phase：start（开始传）/ progress（进行中，pct=百分比）/ ok / fail。
        """
        self.config_path = config_path
        self.force = force
        self.no_bili = no_bili
        self._log = log or _module_log
        self._progress = progress          # 上传事件回调（界面用来刷上传分页）
        self.q: queue.Queue = queue.Queue()
        self.results: list[dict] = []
        self.failed: list[dict] = []
        self._pending = 0
        self._seq = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cfg_checked = False
        self._cfg = None
        self._enabled = False
        self._cred_problem = ""            # 登录态失效的原因（非空=这些段要等登录后补传）
        self._throttle = None               # bu.SubmitThrottle，第一次用到才建

    def log(self, *a):
        try:
            self._log(*a)
        except Exception:
            pass

    # ---- 配置：第一次真要传时才读，读一次就够
    def _ensure_cfg(self) -> bool:
        if self._cfg_checked:
            return self._enabled
        self._cfg_checked = True
        self._cfg, self._enabled = resolve_upload_config(
            self.config_path, self.force, self.no_bili, self.log)
        if self._enabled:
            # 再实测一次 cookie 到底还认不认：字段齐全 ≠ 还有效。
            # 过期时不查的话要等几 GB 传完才在封面那步炸（-101 / 缺 bili_jct）。
            try:
                import bili_uploader as bu
                cred = bu.read_credential(self._cfg.cred_file) or {}
                status, detail = bu.check_cookie_status(cred)
            except Exception as e:               # 检查本身出错不该挡着投稿
                status, detail = "unknown", f"{type(e).__name__}: {e}"
            if status == "invalid":
                self._enabled = False
                self._cred_problem = f"B 站登录态不可用：{detail}"
                self.log(f"[!] {self._cred_problem}")
                self.log("    先重新登录（界面点「B 站扫码登录」，或 python record.py --bili-login），")
                self.log("    登录好之后再点「补传」，这些段会按原参数补投。")
            elif status == "unknown":
                self.log(f"[!] 没检查出 B 站登录态（{detail}）—— 网络原因不拦，继续上传")
            else:
                self.log(f"[√] B 站登录态正常：{detail}")
        return self._enabled

    # ---- 队列
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="upload-worker",
                                        daemon=True)
        self._thread.start()

    def submit(self, seg: dict, *, rid, anchor, live_title, quality, ffmpeg) -> None:
        """把一个录完的分段交给后台上传（立刻返回，不阻塞录制）。"""
        files = list(seg.get("files") or [])
        if not files:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._pending += 1
        self.q.put(dict(seq=seq, start=seg.get("start"), files=files,
                        rid=str(rid), anchor=anchor, live_title=live_title,
                        quality=quality, ffmpeg=ffmpeg))
        self.start()
        self.log(f"[i] 第 {seq} 段已交给后台上传（队列里还有 {self.pending()} 段）")

    def pending(self) -> int:
        with self._lock:
            return self._pending

    def _loop(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                self._run_one(item)
            finally:
                with self._lock:
                    self._pending -= 1
                self.q.task_done()

    def _notify(self, seq, phase, pct, anchor="", done=0, total=0) -> None:
        """给界面发上传事件：phase ∈ start / progress / ok / fail。

        done/total 是**分块数**（B 站固定 10MB 一块）——
        大文件只报百分比会长时间停在 0%，界面上按「0%（2/205 块）」显示才看得出在动。
        """
        if self._progress is None:
            return
        try:
            self._progress(seq, phase, pct, anchor, done, total)
        except Exception:
            pass

    def _run_one(self, item) -> None:
        if not self._ensure_cfg():
            import bili_uploader as bu
            if self._cred_problem:
                # 登录态失效：不是「不必传」，而是「传不了」—— 记成待补传，别丢了
                self.failed.append(item)
                bu.remember_failed(item, self._cred_problem)
                self._notify(item["seq"], "fail", 0, item.get("anchor") or "")
                self.log(f"[!] 第 {item['seq']} 段没传：{self._cred_problem}")
                self.log(f"    已记到 {bu.failed_path().name}，重新登录后点「补传」即可")
            else:
                self.log(f"[i] 第 {item['seq']} 段不投稿（原因见上面）")
            return
        import bili_uploader as bu
        # 多个主播共用一个节流器：B 站风控按账号算，短时间连投会被 406 拦下
        if self._throttle is None:
            self._throttle = bu.SubmitThrottle(getattr(self._cfg, "min_interval", 0))
        self._throttle.wait(self.log)
        self._notify(item["seq"], "start", 0, item.get("anchor") or "")
        self.log(f"[i] 后台开始上传第 {item['seq']} 段：{len(item['files'])} 个文件")
        try:
            info = bu.upload_recording(
                item["files"], anchor=item["anchor"], cfg=self._cfg,
                room_id=item["rid"], live_title=item["live_title"],
                quality=item["quality"], ffmpeg=item["ffmpeg"],
                when=item["start"], seg_index=item["seq"], seg_total=0,
                log=self.log,
                progress=lambda pct, d, t: self._notify(
                    item["seq"], "progress", pct, "", d, t))
        except Exception as e:
            self.log(f"[!] 第 {item['seq']} 段投稿失败：{type(e).__name__}: {e}")
            for line in bu.explain_network_error(e):
                self.log("    " + line)
            self.failed.append(item)
            bu.remember_failed(item, f"{type(e).__name__}: {e}")
            self.log(f"    已记到 {bu.failed_path().name}，点「补传」或下次启动时会再投")
            self._notify(item["seq"], "fail", 0, item.get("anchor") or "")
            return
        bu.remove_failed(item)
        if info:
            self.results.append(info)
            self.log(f"[√] 第 {item['seq']} 段投稿完成：{info['bvid']}")
            self._notify(item["seq"], "ok", 100, item.get("anchor") or "")
        else:
            self.log(f"[i] 第 {item['seq']} 段没有可投稿的文件（可能已被删除），跳过")
            self._notify(item["seq"], "fail", 0)

    def retry_failed(self, log=None) -> int:
        """把投稿失败的稿件重新排队上传（界面「补传」按钮 / CLI --bili-retry）。

        失败记录在 exe 同目录的 bili_failed_uploads.json 里（跨重启也在）。
        重新入队后走同一条串行队列：节流（防 406）和进度回调都照样生效；
        再失败的会自动记回去，成功的自动从清单里划掉。
        """
        _log = log or self.log
        try:
            import bili_uploader as bu
        except Exception as e:
            _log(f"[x] 没有 bili_uploader，补传用不了：{e}")
            return 0
        # 用户很可能就是「刚登录完就来点补传」——重新实测一次登录态再决定，
        # 别拿上次缓存下来的「失效」结论把人挡在外面。
        self._cfg_checked = False
        self._cred_problem = ""
        if not self._ensure_cfg():
            _log("[x] 投稿没开启或没登录，先处理好再补传（原因见上面）")
            return 0

        items = {bu._fkey(it): it for it in list(self.failed) if bu._fkey(it)}
        for it in bu.load_failed():
            key = bu._fkey(it)
            if key and key not in items:
                items[key] = it
        if not items:
            return 0

        # 清空旧记录：重新入队后，仍失败的会自己记回来，成功的就不用留了
        self.failed = []
        bu._save_failed([])

        n = 0
        for it in items.values():
            when = it.get("start")
            if isinstance(when, str):
                try:
                    when = datetime.fromisoformat(when)
                except Exception:
                    when = None
            missing = [f for f in (it.get("files") or []) if not os.path.isfile(f)]
            if missing:
                _log(f"[!] 第 {it.get('seq')} 段的文件已不在本地，跳过：{missing[0]}")
                continue
            self.q.put({**it, "start": when})
            with self._lock:
                self._pending += 1
            n += 1
        self.start()
        _log(f"[i] 补传：{n} 段已重新排队，串行上传（节流和进度照常生效）")
        return n

    def wait(self, timeout=None) -> bool:
        """等队列里的分段传完。返回是否等到了（超时返回 False，后台会继续传）。"""
        if self.pending() == 0:
            return True
        self.log(f"[i] 等待后台上传：还剩 {self.pending()} 段…")
        end = time.time() + timeout if timeout else None
        while self.pending() > 0:
            if end and time.time() > end:
                self.log(f"[!] 上传没等完，还剩 {self.pending()} 段（后台会继续传）")
                return False
            time.sleep(0.5)
        self.log("[i] 后台上传全部完成")
        return True


def plan_segment(deadline, segment_seconds, now):
    """算出这一段 ffmpeg 该录多少秒（0 = 不限），以及是否被「分段上限」截断。

    Returns:
        (limit, capped)
        limit  : 传给 ffmpeg -t 的秒数，0 表示不限
        capped : True 表示录满这一段后应该**开新的一段**（而不是收工）

    纯函数，方便单独验证。
    """
    seg_left = int(segment_seconds) if segment_seconds and segment_seconds > 0 else 0
    overall_left = max(0, int(deadline - now)) if deadline else 0
    if seg_left and overall_left:
        return min(seg_left, overall_left), seg_left <= overall_left
    if seg_left:
        return seg_left, True
    return overall_left, False


def resolve_upload_config(config_path="", force=False, no_bili=False, log=None):
    """读 bili.toml 并判断这次到底要不要投稿。返回 (cfg, 要不要投)。

    不投稿时会把原因说清楚（未开启 / 没登录 / 配置有问题），调用方只需看返回值。
    """
    log = log or _module_log
    if no_bili:
        log("B 站投稿：--no-bili，跳过")
        return None, False

    try:
        import bili_uploader as bu
    except ModuleNotFoundError as e:
        top = (e.name or "").split(".")[0]
        if top == "bilibili_api":
            log("[!] 当前 Python 里没有 bilibili-api-python，跳过 B 站投稿。装一下：")
            log("    pip install -U bilibili-api-python")
        elif top in ("requests", "qrcode_terminal"):
            log(f"[!] 缺少依赖 {top}，跳过 B 站投稿：pip install -U {top}")
        else:
            log(f"[!] 没找到 bili_uploader.py（{e}），跳过 B 站投稿")
        return None, False
    except ImportError as e:
        log(f"[!] 导入 bili_uploader 失败（{e}），跳过 B 站投稿")
        return None, False

    cfg_path = config_path or bu.DEFAULT_CONFIG
    try:
        cfg, created = bu.load_config(cfg_path)
    except bu.BiliError as e:
        log(f"[!] B 站配置有问题，跳过投稿：{e}")
        return None, False
    if created:
        log(f"[i] 已生成 B 站投稿配置模板：{cfg_path}")

    if not (force or cfg.enabled):
        log("B 站投稿：未开启（bili.toml 里 [bili].enabled = false），跳过")
        return None, False
    if not bu.has_credential(cfg):
        log("[!] 没有可用的 B 站登录态，跳过投稿。先跑一次一次性设置：")
        log("    python record.py --bili-login")
        return None, False
    return cfg, True


def upload_to_bili(segments, *, rid, anchor, live_title, quality, ffmpeg,
                   config_path="", force=False, no_bili=False, logf=None):
    """录完之后把每个分段投成**独立稿件**（配置见 bili.toml）。

    segments: [{"start": datetime, "files": [路径, ...]}, ...]
              标题里的时间用每段自己的 start。
    logf: 自定义日志回调（GUI 用它把输出接到界面），默认走模块级 log。

    全程不抛异常：投稿是录制流程的附加步骤，失败了不该影响已经录好的文件。
    """
    # 下面定义了局部 log()，这里要用模块级那个的别名
    _log = logf or _module_log

    def log(*a):                      # 局部遮蔽模块级 log，输出交给调用方
        try:
            _log(*a)
        except Exception:
            pass

    cfg, do_it = resolve_upload_config(config_path, force, no_bili, log)
    if not do_it:
        return None
    import bili_uploader as bu            # 上面确认过了，这里一定能 import

    # 转绝对路径，别依赖当前工作目录；顺手丢掉空段
    segs = [{"start": s.get("start"),
             "files": [os.path.abspath(f) for f in (s.get("files") or [])]}
            for s in segments]
    segs = [s for s in segs if s["files"]]
    if not segs:
        log("B 站投稿：没有可投稿的文件，跳过")
        return

    n_files = sum(len(s["files"]) for s in segs)
    log("=" * 58)
    log(f"开始投稿到 B 站：{len(segs)} 个分段（共 {n_files} 个文件），"
        f"封面取第一帧，格式 {cfg.container}")
    try:
        results = bu.upload_segments(
            segs, anchor=anchor, cfg=cfg, room_id=str(rid), live_title=live_title,
            quality=quality, ffmpeg=ffmpeg, log=log)
    except Exception as e:
        log(f"[!] 投稿出错：{type(e).__name__}: {e}")
        for line in bu.explain_network_error(e):
            log("    " + line)
        return

    log("")
    if results:
        log(f"投稿完成：{len(results)}/{len(segs)} 段")
        for r in results:
            log(f"   {r['bvid']}")
    else:
        log("[!] 没有任何一段投稿成功")


def run_recording(rid, *, quality="FULL_HD1", protocol="flv", duration=0, segment=7200,
                  out_dir=None, name="", ffmpeg="", wait=0, no_reconnect=False,
                  list_only=False, upload=True, bili_config="", force_bili=False,
                  no_bili=False, logf=None, stop_event=None,
                  uploader=None, tag=""):
    """录制一个直播间，录完（可选）自动投稿。

    命令行和 GUI 共用这一份逻辑：
      rid          直播间号（用 parse_rid() 从用户输入解析得到）
      wait         未开播时最多等多少秒。**0 = 一直等**（每 30 秒重试一次拉流）；
                   <0 = 不等，没开播就直接抛错
      stop_event   threading.Event，置位后尽快收尾并返回
      logf         日志回调，GUI 用它把输出接到界面
      tag          日志前缀（多个主播同时录时，用来区分是谁的日志）
      upload       录完是否投稿（还要看 bili.toml 里的 enabled）
      uploader     给了就在**每段录完的当下**交给后台上传（录制不等它）；
                   不传则退回「整场录完再一起传」

    返回：{"segments": [...], "files": [...], "anchor": 主播昵称}；
          被取消 / 没录到东西时返回 None。失败抛 RecordError。
    """
    # 注意：下面定义了局部 log()，函数体里就不能再引用模块级 log 了，用别名
    _log = logf or _module_log

    def log(*args):                   # 局部遮蔽模块级 log，把输出交给调用方
        msg = " ".join(str(x) for x in args)
        try:
            _log(f"[{tag}] {msg}" if tag else msg)
        except Exception:
            pass

    out_dir = out_dir or DEFAULT_DIR
    # 凑成原来的 a.xxx 形式，下面的录制逻辑就不用改
    a = SimpleNamespace(
        quality=quality, protocol=protocol, duration=duration, segment=segment,
        dir=out_dir, name=name, ffmpeg=ffmpeg, wait=wait, no_reconnect=no_reconnect,
        list=list_only, bili_config=bili_config, bili=force_bili, no_bili=no_bili)
    if stop_event is None:
        stop_event = _NEVER_SET

    ffmpeg = find_ffmpeg(a.ffmpeg)
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, timeout=30)
    except Exception as e:
        raise RecordError(f"找不到能用的 ffmpeg（{ffmpeg}）：{e}\n"
                          "请先安装 ffmpeg 并加到 PATH，或用 --ffmpeg 指定路径")

    os.makedirs(a.dir, exist_ok=True)
    log(f"房间号 {rid}，输出目录 {a.dir}")

    # ---- 等开播：没开播就每 30 秒重试一次拉流
    #      a.wait > 0 最多等这么久；a.wait == 0 一直等；a.wait < 0 不等
    streams = fetch_room_streams(rid, cookie=load_cookie())
    waited = 0
    while not streams.live and a.wait >= 0:
        if a.wait and waited >= a.wait:
            break
        if stop_event.is_set():
            log("已取消等待开播")
            return None
        log(f"{streams.error or '未开播'}，{WAIT_INTERVAL} 秒后再拉一次"
            + (f"（已等 {fmt_dur(waited)}，上限 {fmt_dur(a.wait)}）" if a.wait
               else f"（已等 {fmt_dur(waited)}，一直等）"))
        if stop_event.wait(WAIT_INTERVAL):        # 可被立刻打断的 sleep
            log("已取消等待开播")
            return None
        waited += WAIT_INTERVAL
        streams = fetch_room_streams(rid, cookie=load_cookie())

    if not streams.live:
        reason = streams.error or "当前主播未直播"
        if not streams.ok:
            raise RecordError(f"查询失败：{reason}")
        raise RecordError(f"主播没在播（{reason}）。"
                          "去掉 --no-wait 就会一直守着，每 30 秒拉一次流。")

    table = streams.flv if a.protocol == "flv" else (streams.hls or streams.flv)
    if not table:
        raise RecordError("这个直播间没有返回可用的拉流地址（可能已被风控），"
                          "试试 --protocol hls")

    log(f"主播：{streams.anchor} · {streams.title}")
    if a.list:
        log("可用清晰度：")
        for q in sorted(table):
            log(f"   {q:<9} {QUALITY_DESC.get(q, '')}  {table[q][:110]}...")
        return None

    quality, url = pick_quality(table, a.quality)
    log(f"清晰度 {quality}（{QUALITY_DESC.get(quality, '')}）")
    log(f"流地址 {url[:110]}...")

    os.makedirs(a.dir, exist_ok=True)

    # ---- 录制：断流自动重连 + 按 --segment 分段（默认 2 小时一段）
    deadline = time.time() + a.duration if a.duration > 0 else None
    MAX_FILES = 200                      # 防止死循环的兜底
    segments = []                        # [{"start": datetime, "files": [...], "closed": bool}]
    files = []                           # 扁平列表，给汇总和 TS 自检用
    end_upload = []                      # 没有后台队列时，攒到最后一起传
    part = 0
    stopped = False
    protocol = a.protocol
    prefix = safe_name(a.name or streams.anchor)
    log(f"分段长度：{a.segment} 秒" if a.segment else "分段长度：不分段（整场录成一个文件）")

    def open_segment():
        """开新的一段：记下开始时间，并按这个时间生成文件名前缀。"""
        start = datetime.now()
        segments.append({"start": start, "files": [], "closed": False})
        log(f"── 第 {len(segments)} 段开始：{start:%H:%M:%S} ──")
        return f"{prefix}_{start:%Y%m%d_%H%M%S}_{quality}"

    def hand_over(seg):
        """这一段录完了：交给后台上传（**不等它**，录制立刻继续）。

        没有后台队列就攒起来，等整场录完一起传（命令行单独用的老行为）。
        """
        if not upload:
            return
        if uploader is not None:
            uploader.submit(seg, rid=rid, anchor=streams.anchor,
                            live_title=streams.title, quality=quality,
                            ffmpeg=ffmpeg)
        else:
            end_upload.append(seg)

    base = ""
    file_no = 0
    try:
        while part < MAX_FILES:
            if deadline and time.time() >= deadline:
                break
            if not segments or segments[-1]["closed"]:
                base = open_segment()
                file_no = 0
            cur = segments[-1]

            part += 1
            file_no += 1
            out = os.path.join(a.dir, base + ("" if file_no == 1 else f"_part{file_no}") + ".ts")
            left, capped = plan_segment(deadline, a.segment, time.time())
            cmd = build_cmd(ffmpeg, url, out, left)
            log(f"开始录制 → {os.path.basename(out)}"
                + (f"（{left}s{'，本段上限' if capped else ''}）" if left else "（Ctrl+C 停止）"))
            flags = ffmpeg_creationflags()   # frozen 时加 CREATE_NO_WINDOW，不弹黑框
            seg_start = time.time()
            proc = subprocess.Popen(cmd, creationflags=flags)
            last_report = time.time()
            while proc.poll() is None:
                time.sleep(1)
                if (deadline and time.time() >= deadline) or stop_event.is_set():
                    stopped = True
                    break
                if time.time() - last_report >= 15 and os.path.exists(out):
                    log(f"  已写 {human(os.path.getsize(out))}")
                    last_report = time.time()
            if proc.poll() is None:          # 到点了，让它收尾
                try:
                    if os.name == "nt":
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            rc = proc.poll()
            size = os.path.getsize(out) if os.path.exists(out) else 0
            secs = time.time() - seg_start
            # 只录到几秒 / 几百 KB 就退出，基本都是流没真正拉到（例如 hls 切片 404）
            too_short = (not stopped) and size < 300 * 1024 and secs < 8 \
                and (left == 0 or left > 15)
            if size and not too_short:
                cur["files"].append(out)
                files.append(out)
                log(f"  {os.path.basename(out)} 写完，{human(size)}，{secs:.0f}s")
            if stopped:
                break

            # 录满了给它的时间（ffmpeg 的 -t 生效）？
            ran_full = left > 0 and secs >= left - 2
            if ran_full and capped:
                cur["closed"] = True
                log(f"── 第 {len(segments)} 段结束（{human(size)}）──")
                hand_over(cur)            # ← 录完就交出去上传，不等直播结束
                # 拉流地址带 sign，可能已经过期，换一个新的再开下一段
                again = fetch_room_streams(rid, cookie=load_cookie())
                if not again.ok:
                    log(f"重取流地址失败（{again.error}），沿用当前地址继续")
                elif not again.live:
                    log("重查发现主播已下播，停止")
                    break
                else:
                    table2 = again.flv if protocol == "flv" else (again.hls or again.flv)
                    if table2:
                        _q, url = pick_quality(table2, quality)
                continue
            if ran_full:
                log("已录满指定时长，停止")
                break

            if rc == 0 and not too_short:
                log("录制结束（流已结束）")
                break
            if a.no_reconnect:
                log(f"ffmpeg 退出码 {rc}，未开启重连，停止")
                break
            if too_short:
                log(f"只录到 {human(size)}/{secs:.0f}s，判断为没真正拉到流")
                if protocol == "hls":     # hls 拉不到就回退 flv，抖音的 flv 直连更稳
                    protocol = "flv"
                    log("切换到 flv 协议重试")
            else:
                log(f"ffmpeg 退出码 {rc}，可能是断流；5 秒后重连 …")
            time.sleep(5)
            again = fetch_room_streams(rid, cookie=load_cookie())
            if not again.live:
                log("重查发现主播已下播，停止")
                break
            table2 = again.flv if protocol == "flv" else (again.hls or again.flv)
            if not table2:
                log("重查没拿到流地址，停止")
                break
            _q, url = pick_quality(table2, quality)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在收尾 …")

    log("")
    if not files:
        log("没有录到任何文件")
        return None
    log("录制完成：")
    total = 0
    for i, seg in enumerate(segments, 1):
        seg_size = sum(os.path.getsize(f) for f in seg["files"] if os.path.exists(f))
        total += seg_size
        log(f"   第 {i} 段（{seg['start']:%H:%M:%S} 开始）{len(seg['files'])} 个文件，"
            f"{human(seg_size)}")
        for f in seg["files"]:
            log(f"      {os.path.basename(f)}  ({human(os.path.getsize(f))})")
    log(f"合计 {human(total)}，共 {len(files)} 个文件 / {len(segments)} 段")
    info = probe(files[0], a.ffmpeg)
    if info:
        log("ffprobe：")
        for line in info.splitlines():
            log("   " + line)
    # TS 同步字节自检（每个 TS 包应是 188 字节，开头是 0x47）
    try:
        with open(files[0], "rb") as fh:
            head = fh.read(188 * 3)
        ok_head = len(head) >= 188 and head[0] == 0x47 and head[188] == 0x47
        log("TS 封装自检: " + ("通过（0x47 同步字节，188 字节包）" if ok_head
                              else "异常，文件可能不完整"))
    except Exception as e:
        log(f"TS 自检跳过：{e}")

    # ---- 最后一段（没被分段上限收掉的那段）也交出去
    if segments and not segments[-1].get("closed"):
        hand_over(segments[-1])

    # ---- 投稿
    # 有后台队列时，每段录完的当下就已经交出去了，这里不用再做（调用方最后 wait 一下）；
    # 没有后台队列（命令行单跑）就按老样子：整场录完一起传。
    if upload and uploader is None:
        upload_to_bili(end_upload, rid=rid, anchor=streams.anchor,
                       live_title=streams.title, quality=quality, ffmpeg=ffmpeg,
                       config_path=a.bili_config, force=a.bili, no_bili=a.no_bili,
                       logf=logf)
    return {"segments": segments, "files": files, "anchor": streams.anchor}


def run_one_room(rid, *, tag="", logf=None, monitor=False, **kw):
    """一个主播一个线程。单个主播出错不影响其它主播。

    monitor=True 是**值守模式**：一场直播结束（下播）后不退出，
    回到「等开播」的循环（每 30 秒重试一次拉流），开播了自动接着录下一场。
    指定了录制时长（duration > 0）的算一次性任务，录满就停，监控不生效。
    """
    def log(*a):
        msg = " ".join(str(x) for x in a)
        try:
            (logf or _module_log)(f"[{tag or rid}] {msg}")
        except Exception:
            pass

    session = 0
    while True:
        session += 1
        if session > 1:
            log(f"[i] 第 {session} 场：等主播开播（每 {WAIT_INTERVAL} 秒查一次）…")
        try:
            res = run_recording(rid, logf=log, **kw)
        except RecordError as e:
            log(f"[x] {e}")
            break
        except Exception as e:
            log(f"[x] 未预期的错误：{type(e).__name__}: {e}")
            break

        stop_ev = kw.get("stop_event")
        if not monitor or (stop_ev is not None and stop_ev.is_set()):
            break
        if kw.get("duration", 0):
            log("[i] 已录满指定时长。要一直值守请把录制时长设成「一直录到手动停止」")
            break
        if res is None:
            log("[i] 这一场没有录到内容，监控继续")
            time.sleep(WAIT_INTERVAL)
            if stop_ev is not None and stop_ev.is_set():
                break
            continue
        log("[i] 主播下播，进入监控状态（每 30 秒查一次，开播了自动接着录）")


def record_rooms(rids, *, uploader=None, logf=None, tags=None, stop_event=None, **kw):
    """同时开录多个主播（每人一个线程）。返回 (线程列表, 共用的 stop_event)。

    多个主播共用同一个 UploadWorker：谁的段先录完谁先传，互相不阻塞录制。
    """
    stop_event = stop_event or threading.Event()
    tags = tags or {}
    threads = []
    for rid in rids:
        t = threading.Thread(target=run_one_room, name=f"rec-{rid}", daemon=True,
                             kwargs=dict(rid=rid, tag=tags.get(rid, rid),
                                         logf=logf, uploader=uploader,
                                         stop_event=stop_event, **kw))
        t.start()
        threads.append(t)
    return threads, stop_event


def main():
    ap = argparse.ArgumentParser(description="抖音直播录制（ffmpeg 直接封装成 TS，不转码）")
    ap.add_argument("rooms", nargs="*",
                    help="抖音号 / 房间号 / 直播间链接；可以填多个，同时开录")
    ap.add_argument("--quality", default="FULL_HD1",
                    help="清晰度：FULL_HD1/ORIGIN/HD1/SD1/SD2/LD/auto，默认 FULL_HD1")
    ap.add_argument("--protocol", default="flv", choices=["flv", "hls"],
                    help="拉流协议，默认 flv（hls 是 m3u8 切片）")
    ap.add_argument("--duration", type=int, default=0, help="录制秒数，0 = 一直录到 Ctrl+C")
    ap.add_argument("--segment", type=int, default=7200,
                    help="每多少秒切一段，默认 7200（2 小时），0 = 不分段。"
                         "每段会单独投成一个 B 站稿件，标题带上该段开始时间")
    ap.add_argument("--dir", default=DEFAULT_DIR, help=f"输出目录，默认 {DEFAULT_DIR}")
    ap.add_argument("--name", default="", help="文件名前缀，默认用主播昵称")
    ap.add_argument("--ffmpeg", default="", help="ffmpeg 路径，默认用 PATH 里的")
    ap.add_argument("--wait", type=int, default=0,
                    help=f"没开播时最多等多少秒（每 {WAIT_INTERVAL} 秒重试一次拉流）。"
                         f"默认 0 = 一直等，直到开播或 Ctrl+C")
    ap.add_argument("--no-wait", action="store_true",
                    help="没开播就立刻退出，不守着等")
    ap.add_argument("--no-reconnect", action="store_true", help="断流后不自动重连")
    ap.add_argument("--list", action="store_true", help="只列出可用清晰度然后退出")
    # ---- 录完自动投稿到 B 站（配置见 bili.toml）----
    ap.add_argument("--bili", action="store_true",
                    help="本次强制投稿（配置里 enabled=false 时也能压过）")
    ap.add_argument("--no-bili", action="store_true", help="本次录制不投稿")
    ap.add_argument("--bili-config", default="", help="bili.toml 路径，默认用同目录那份")
    ap.add_argument("--bili-login", action="store_true",
                    help="只做 B 站扫码登录（一次性设置）然后退出")
    ap.add_argument("--bili-retry", action="store_true",
                    help="只补传「投稿失败」的稿件（文件还在本地的那批），然后退出（不录制）")
    ap.add_argument("--no-monitor", action="store_true",
                    help="下播后就退出，不进入监控状态（默认：没指定录制时长时，"
                         "下播后会每 30 秒查一次，开播自动接着录）")
    ap.add_argument("--gui", action="store_true", help="打开图形界面")
    a = ap.parse_args()

    # ---- 图形界面
    if a.gui:
        import record_gui
        record_gui.main()
        return

    # ---- 补传投稿失败的稿件（406 风控解除后用它，文件都还在本地）
    if a.bili_retry:
        try:
            import bili_uploader as bu
        except ImportError as e:
            sys.exit(f"没找到 bili_uploader.py：{e}")
        try:
            items = bu.load_failed()
            if not items:
                print("[i] 没有需要补传的稿件。")
                return
            print(f"[i] 有 {len(items)} 段投稿失败，开始补传…")
            w = UploadWorker(config_path=a.bili_config, force=a.bili,
                             no_bili=a.no_bili, log=log)
            n = w.retry_failed(log=log)
            if not n:
                print("[i] 没有可补传的（文件可能已不在本地）。")
                return
            w.wait(timeout=7200)
            print(f"[√] 补传完成：成功 {len(w.results)} 段，仍失败 {len(w.failed)} 段"
                  + ("（可再跑一次 --bili-retry）" if w.failed else ""))
        except bu.BiliError as e:
            sys.exit(f"[x] {e}")
        return

    # ---- 一次性设置：扫码登录 B 站
    if a.bili_login:
        try:
            import bili_uploader as bu
        except ImportError as e:
            sys.exit(f"没找到 bili_uploader.py：{e}")
        try:
            cfg, created = bu.load_config(a.bili_config or bu.DEFAULT_CONFIG)
            if created:
                print(f"[i] 已生成 B 站投稿配置模板：{a.bili_config or bu.DEFAULT_CONFIG}")
            bu.login_qrcode(cfg.cred_file, log=print)
        except bu.BiliError as e:
            sys.exit(f"[x] {e}")
        return

    if not a.rooms:
        ap.error("需要给房间号（或用 --gui 打开图形界面）")

    try:
        rids = [parse_rid(r) for r in a.rooms]
    except SystemExit as e:
        sys.exit(e)
    if len(set(rids)) != len(rids):
        sys.exit("[x] 有重复的直播间，去掉重复的再来")

    common = dict(
        quality=a.quality, protocol=a.protocol, duration=a.duration,
        segment=a.segment, out_dir=a.dir, name=a.name, ffmpeg=a.ffmpeg,
        wait=-1 if a.no_wait else a.wait, no_reconnect=a.no_reconnect,
        list_only=a.list, upload=True, bili_config=a.bili_config,
        force_bili=a.bili, no_bili=a.no_bili,
        monitor=(a.duration == 0) and not a.no_monitor)

    # 多个主播共用这一个后台上传队列：谁的段先录完谁先传，不阻塞录制
    uploader = UploadWorker(config_path=a.bili_config, force=a.bili,
                            no_bili=a.no_bili, log=log)
    threads, stop_event = record_rooms(rids, uploader=uploader, logf=log, **common)
    log(f"已启动 {len(threads)} 个录制线程")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在停止所有录制 …")
        stop_event.set()
        for t in threads:
            t.join(timeout=30)

    # 等后台把剩下的传完（最多等 30 分钟，超时就让它在后台继续）
    uploader.wait(timeout=1800)
    if uploader.results:
        log(f"本次共投稿 {len(uploader.results)} 段")
    if uploader.failed:
        log(f"[!] 有 {len(uploader.failed)} 段没传成功（文件都还在本地）")


if __name__ == "__main__":
    main()
