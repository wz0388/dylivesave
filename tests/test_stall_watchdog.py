# -*- coding: utf-8 -*-
"""断流卡死的看门狗自测（不联网、不真拉流）。

    python tests/test_stall_watchdog.py

复现的问题：主播下播 / 断流时，ffmpeg 有时**不退出**（TCP 半开连接会一直卡在
read 上），文件不再增长、进程也不结束 —— 表现就是「一直在录，最后一个文件
永远不结束，也永远不投稿」。

这个测试用「写 1MB 就挂住不动的假 ffmpeg」来验证：
  · 录制循环能在 STALL_SECONDS 之后主动收掉它，而不是一直等；
  · 收掉之后走正常的下播判断（重查发现没在播 → 退出并交最后一段）；
  · 整场能在十几秒内结束（不会挂死）。
"""
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import record as rec   # noqa: E402

LOGS: list[str] = []
CALLS = {"fetch": 0}


def _fake_streams(live: bool):
    return SimpleNamespace(
        live=live, ok=True, error="" if live else "当前主播未直播",
        anchor="测试主播", title="测试直播",
        flv={"FULL_HD1": "http://example.invalid/live.flv"}, hls={})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_stall_"))
    rec.STALL_SECONDS = 3                     # 真跑 60 秒太慢，压到 3 秒

    def fake_fetch(rid, cookie=None):
        CALLS["fetch"] += 1
        return _fake_streams(live=CALLS["fetch"] == 1)   # 第二次开始「已下播」

    def fake_cmd(ffmpeg, url, out, left):
        # 假 ffmpeg：先写 1MB（够过 min_size 的 too_short 判断），然后挂住不退
        code = (f"open(r'{out}','wb').write(b'G'*1048576);"
                "import time; time.sleep(600)")
        return [sys.executable, "-c", code]

    rec.fetch_room_streams = fake_fetch
    rec.load_cookie = lambda: None
    rec.build_cmd = fake_cmd
    rec.probe = lambda *a, **k: ""

    t0 = time.time()
    res = rec.run_recording(
        "9999", quality="FULL_HD1", protocol="flv", segment=7200, duration=0,
        out_dir=str(tmp), ffmpeg=sys.executable, wait=0, upload=False,
        logf=lambda *a: LOGS.append(" ".join(str(x) for x in a)))
    elapsed = time.time() - t0

    text = "\n".join(LOGS)
    print(text)
    print("-" * 60)

    assert "秒没有新数据" in text, "看门狗没触发"
    assert "重查发现主播已下播，停止" in text, "收掉之后没走正常的下播判断"
    assert elapsed < 30, f"整场耗时 {elapsed:.0f}s，还是偏久（应该十几秒内结束）"
    assert res and len(res["files"]) == 1, res
    assert Path(res["files"][0]).stat().st_size == 1048576

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"OK：断流卡死能被看门狗收掉（{elapsed:.1f}s 结束），最后一段也交出去了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
