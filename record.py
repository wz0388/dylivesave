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
标题里带该段的开始时间，并放进该主播的合集。不想投稿加 --no-bili。
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from douyin_api import UA, fetch_room_streams, fetch_room_info, parse_rid   # noqa: E402
from cookie_store import load_cookie                              # noqa: E402

DEFAULT_DIR = os.path.join(HERE, "recordings")

#: 清晰度优先级（抖音返回哪些取决于主播推流，通常 FULL_HD1/SD1/SD2）
QUALITY_ORDER = ["FULL_HD1", "ORIGIN", "HD1", "SD1", "SD2", "LD"]
QUALITY_DESC = {"FULL_HD1": "原画/高清", "ORIGIN": "原画", "HD1": "高清",
                "SD1": "标清", "SD2": "较低", "LD": "流畅"}


def log(*a):
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)
    except Exception:
        pass


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


def find_ffmpeg(explicit=""):
    if explicit:
        return explicit if os.path.exists(explicit) else explicit
    return "ffmpeg"


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


def upload_to_bili(segments, *, rid, anchor, live_title, quality, ffmpeg,
                   config_path="", force=False, no_bili=False, season=""):
    """录完之后把每个分段投成**独立稿件**（配置见 bili.toml）。

    segments: [{"start": datetime, "files": [路径, ...]}, ...]
              标题里的时间用每段自己的 start。

    全程不抛异常：投稿是录制流程的附加步骤，失败了不该影响已经录好的文件。
    """
    if no_bili:
        log("B 站投稿：--no-bili，跳过")
        return

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
        return
    except ImportError as e:
        log(f"[!] 导入 bili_uploader 失败（{e}），跳过 B 站投稿")
        return

    cfg_path = config_path or bu.DEFAULT_CONFIG
    try:
        cfg, created = bu.load_config(cfg_path)
    except bu.BiliError as e:
        log(f"[!] B 站配置有问题，跳过投稿：{e}")
        return
    if created:
        log(f"[i] 已生成 B 站投稿配置模板：{cfg_path}（里面可以配每个主播的合集）")

    if not (force or cfg.enabled):
        log("B 站投稿：未开启（bili.toml 里 [bili].enabled = false），跳过")
        return
    if not bu.has_credential(cfg):
        log("[!] 没有可用的 B 站登录态，跳过投稿。先跑一次一次性设置：")
        log("    python record.py --bili-login")
        return

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
            quality=quality, ffmpeg=ffmpeg, season_override=season or None, log=log)
    except Exception as e:
        log(f"[!] 投稿出错：{type(e).__name__}: {e}")
        return

    log("")
    if results:
        log(f"投稿完成：{len(results)}/{len(segs)} 段")
        for r in results:
            log(f"   {r['bvid']}"
                + (f"  → 合集「{r['season']}」" if r.get("season") else ""))
    else:
        log("[!] 没有任何一段投稿成功")


def main():
    ap = argparse.ArgumentParser(description="抖音直播录制（ffmpeg 直接封装成 TS，不转码）")
    ap.add_argument("room", nargs="?", help="抖音号 / 房间号 / 直播间链接")
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
                    help="主播没开播时最多等多少秒（每 30 秒查一次），0 = 不等直接退出")
    ap.add_argument("--no-reconnect", action="store_true", help="断流后不自动重连")
    ap.add_argument("--list", action="store_true", help="只列出可用清晰度然后退出")
    # ---- 录完自动投稿到 B 站（配置见 bili.toml）----
    ap.add_argument("--bili", action="store_true",
                    help="本次强制投稿（配置里 enabled=false 时也能压过）")
    ap.add_argument("--no-bili", action="store_true", help="本次录制不投稿")
    ap.add_argument("--bili-config", default="", help="bili.toml 路径，默认用同目录那份")
    ap.add_argument("--bili-season", default="", help="本次投稿指定合集名（覆盖配置）")
    ap.add_argument("--bili-login", action="store_true",
                    help="只做 B 站扫码登录（一次性设置）然后退出")
    a = ap.parse_args()

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

    if not a.room:
        ap.error("需要给房间号；只看 B 站登录状态用 --bili-login")

    ffmpeg = find_ffmpeg(a.ffmpeg)
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, timeout=30)
    except Exception as e:
        sys.exit(f"找不到能用的 ffmpeg（{ffmpeg}）：{e}\n"
                 "请先安装 ffmpeg 并加到 PATH，或用 --ffmpeg 指定路径")

    rid = parse_rid(a.room)
    os.makedirs(a.dir, exist_ok=True)
    log(f"房间号 {rid}，输出目录 {a.dir}")

    # ---- 等开播 + 取流地址
    streams = fetch_room_streams(rid, cookie=load_cookie())
    waited = 0
    while not streams.live and a.wait and waited < a.wait:
        log(f"{streams.error or '未开播'}，30 秒后再查（已等 {waited}/{a.wait}s）")
        time.sleep(30)
        waited += 30
        streams = fetch_room_streams(rid, cookie=load_cookie())

    if not streams.ok:
        sys.exit("查询失败：" + (streams.error or "未知错误"))
    if not streams.live:
        sys.exit(f"主播没在播（{streams.error or '当前主播未直播'}）。"
                 f"加 --wait 600 可以守着等开播。")

    table = streams.flv if a.protocol == "flv" else (streams.hls or streams.flv)
    if not table:
        sys.exit("这个直播间没有返回可用的拉流地址（可能已被风控），试试 --protocol hls")

    log(f"主播：{streams.anchor} · {streams.title}")
    if a.list:
        print("可用清晰度：")
        for q in sorted(table):
            print(f"   {q:<9} {QUALITY_DESC.get(q, '')}  {table[q][:110]}...")
        return

    quality, url = pick_quality(table, a.quality)
    log(f"清晰度 {quality}（{QUALITY_DESC.get(quality, '')}）")
    log(f"流地址 {url[:110]}...")

    os.makedirs(a.dir, exist_ok=True)

    # ---- 录制：断流自动重连 + 按 --segment 分段（默认 2 小时一段）
    deadline = time.time() + a.duration if a.duration > 0 else None
    MAX_FILES = 200                      # 防止死循环的兜底
    segments = []                        # [{"start": datetime, "files": [...], "closed": bool}]
    files = []                           # 扁平列表，给汇总和 TS 自检用
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
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            seg_start = time.time()
            proc = subprocess.Popen(cmd, creationflags=flags)
            last_report = time.time()
            while proc.poll() is None:
                time.sleep(1)
                if deadline and time.time() >= deadline:
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

    print()
    if not files:
        print("没有录到任何文件")
        return
    print("录制完成：")
    total = 0
    for i, seg in enumerate(segments, 1):
        seg_size = sum(os.path.getsize(f) for f in seg["files"] if os.path.exists(f))
        total += seg_size
        print(f"   第 {i} 段（{seg['start']:%H:%M:%S} 开始）{len(seg['files'])} 个文件，"
              f"{human(seg_size)}")
        for f in seg["files"]:
            print(f"      {os.path.basename(f)}  ({human(os.path.getsize(f))})")
    print(f"合计 {human(total)}，共 {len(files)} 个文件 / {len(segments)} 段")
    info = probe(files[0], a.ffmpeg)
    if info:
        print("ffprobe：")
        for line in info.splitlines():
            print("   " + line)
    # TS 同步字节自检（每个 TS 包应是 188 字节，开头是 0x47）
    with open(files[0], "rb") as fh:
        head = fh.read(188 * 3)
    ok_head = len(head) >= 188 and head[0] == 0x47 and head[188] == 0x47
    print("TS 封装自检:", "通过（0x47 同步字节，188 字节包）" if ok_head else "异常，文件可能不完整")

    # ---- 录完了，每段投成一个 B 站稿件（bili.toml 里配 enabled / 每个主播的合集）
    upload_to_bili(segments, rid=rid, anchor=streams.anchor, live_title=streams.title,
                   quality=quality, ffmpeg=ffmpeg, config_path=a.bili_config,
                   force=a.bili, no_bili=a.no_bili, season=a.bili_season)


if __name__ == "__main__":
    main()
