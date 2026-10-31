# -*- coding: utf-8 -*-
"""拉不到流时的诊断 / 停止响应自测（不联网）。

    python tests/test_stream_errors.py

对应的问题（用户日志）：
    开始录制 → …_102410_FULL_HD1.ts
    只录到 0.0 MB/6s，判断为没真正拉到流      ← 但不知道为什么
    （点了停止之后仍然又开了一个 _part3.ts）

这里验证：
  · ffmpeg 的 stderr（403 Forbidden 之类）会被写进日志；
  · 没用的残片会被删掉；
  · 停止请求在两次重试之间就生效 —— 不会再开一个新文件。
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import record as rec   # noqa: E402

LOGS: list[str] = []


def _streams():
    return SimpleNamespace(live=True, ok=True, error="", anchor="主播", title="直播",
                           flv={"FULL_HD1": "http://x.invalid/a.flv"}, hls={})


def _patch(script: str) -> None:
    rec.load_cookie = lambda: None
    rec.probe = lambda *a, **k: ""
    rec.build_cmd = lambda ffmpeg, url, out, left: [
        sys.executable, "-c", script.replace("{OUT}", str(out).replace("\\", "\\\\"))]


def _run(tmp: Path, **kw):
    LOGS.clear()
    return rec.run_recording(
        "1001", quality="FULL_HD1", protocol="flv", segment=1800, duration=0,
        out_dir=str(tmp), ffmpeg=sys.executable, wait=0, upload=False,
        logf=lambda *a, live="": LOGS.append(" ".join(str(x) for x in a)), **kw)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_err_"))

    # ---- ① ffmpeg 直接报 403 退出：日志里要能看到原因，残片要删掉
    calls = {"n": 0}

    def fetch(rid, cookie=None):
        calls["n"] += 1
        return SimpleNamespace(live=calls["n"] == 1, ok=True, error="当前主播未直播",
                               anchor="主播", title="直播",
                               flv={"FULL_HD1": "http://x.invalid/a.flv"}, hls={})

    rec.fetch_room_streams = fetch
    # 写出 100 字节残片 + 报 403 退出（真实的「拉到了 URL 但 CDN 拒绝」就是这样）
    _patch("import sys\n"
           "open(r'{OUT}','wb').write(b'x'*100)\n"
           "sys.stderr.write('403 Forbidden\\n')\n"
           "sys.exit(1)\n")
    res = _run(tmp)
    text = "\n".join(LOGS)
    assert "只录到" in text, text
    assert "403 Forbidden" in text, f"没把 ffmpeg 的报错打出来：\n{text}"
    assert "删掉这个没用的残片" in text, text
    assert res is None, res
    assert not list(tmp.glob("*.ts")), list(tmp.glob("*.ts"))

    # ---- ② 已经点了停止：一个 ffmpeg 都不该再起
    _patch("import time; time.sleep(30)")
    ev = rec.threading.Event()
    ev.set()
    t0 = time.time()
    res2 = _run(tmp, stop_event=ev)
    assert "开始录制" not in "\n".join(LOGS), LOGS
    assert res2 is None
    assert time.time() - t0 < 5, "停止之后还磨蹭"

    # ---- ③ 「已写」要按 PROGRESS_INTERVAL（3 秒）刷新，不是以前的 15 秒
    rec.fetch_room_streams = lambda rid, cookie=None: _streams()
    _patch("import sys, time\n"
           "f = open(r'{OUT}','wb')\n"
           "for _ in range(14):\n"
           "    f.write(b'x' * 1024); f.flush(); time.sleep(0.5)\n"
           "sys.exit(0)\n")
    _run(tmp)
    ticks = [ln for ln in LOGS if "已写" in ln]
    assert len(ticks) >= 2, f"7 秒里只刷了 {len(ticks)} 次「已写」：\n" + "\n".join(LOGS)
    assert "已录 00:00:0" in ticks[0], ticks[0]

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK：ffmpeg 报错能看见、残片会删、停止后不再开新文件、已写每 3 秒刷新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
