# -*- coding: utf-8 -*-
"""后台上传队列 + 场次联动 的离线自测（不联网、不真传 B 站）。

    python tests/test_uploader_queue.py

验证 record.UploadWorker：
  · 同一场直播的两段：第 1 段 part=1 建稿，第 2 段 part=2 追加（session 一路带着走）；
  · 新的一场：part 从 1 重新开始，session 也换了；
  · 补传时同一场要按分P 顺序排，且「稿件还没建起来」的后面的分P 不能乱建稿。
"""
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu      # noqa: E402
import record as rec           # noqa: E402

CALLS: list[dict] = []


def _fake_upload(files, *, anchor, cfg, room_id="", live_title="", quality="",
                 ffmpeg="ffmpeg", when=None, seg_index=1, seg_total=1, log=print,
                 progress=None, session=None, part=0):
    CALLS.append({"files": [Path(f).name for f in files],
                  "session": (session or {}).get("key"),
                  "part": part})
    if session is not None:
        session["bvid"] = "BV1FROMQUEUE"
    return {"bvid": "BV1FROMQUEUE", "appended": False, "pages": 1}


def _worker(tmp: Path):
    w = rec.UploadWorker(log=lambda *a: None)
    w._ensure_cfg = lambda: True            # 跳过读配置 / 实测登录态
    w._cfg = bu.BiliConfig(min_size_mb=0, multi_part=True)
    w.min_interval = 0
    return w


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_queue_"))
    bu.APP_DIR = tmp
    bu.upload_recording = _fake_upload

    f1 = tmp / "seg1.ts"
    f2 = tmp / "seg2.ts"
    f1.write_bytes(b"x" * 2048)
    f2.write_bytes(b"x" * 2048)

    w = _worker(tmp)

    # ---- 同一场直播：两段
    s1 = rec.LiveSession("1001", datetime(2026, 10, 17, 20, 0, 0))
    for f in (f1, f2):
        w.submit({"start": datetime(2026, 10, 17, 20, 0), "files": [str(f)]},
                 rid="1001", anchor="主播A", live_title="直播标题", quality="FULL_HD1",
                 ffmpeg="ffmpeg", session=s1.state, part=s1.take_parts(1))
    assert w.wait(20), "队列没跑完"
    assert [c["part"] for c in CALLS] == [1, 2], CALLS
    assert {c["session"] for c in CALLS} == {s1.key}, CALLS
    assert s1.bvid == "BV1FROMQUEUE", s1

    # ---- 关播再开播：新的一场，分P 从 1 重新开始
    CALLS.clear()
    s2 = rec.LiveSession("1001", datetime(2026, 10, 18, 21, 0, 0))
    w.submit({"start": datetime(2026, 10, 18, 21, 0), "files": [str(f1)]},
             rid="1001", anchor="主播A", live_title="直播标题", quality="FULL_HD1",
             ffmpeg="ffmpeg", session=s2.state, part=s2.take_parts(1))
    assert w.wait(20)
    assert [c["part"] for c in CALLS] == [1] and CALLS[0]["session"] == s2.key, CALLS

    # ---- 补传：后面的分P 在稿件还没建起来时不许自己建稿
    bu._save_failed([
        {"seq": 9, "files": [str(f2)], "start": "2026-10-17T20:00:00",
         "anchor": "主播A", "rid": "1001", "quality": "FULL_HD1",
         "ffmpeg": "ffmpeg", "live_title": "直播标题",
         "session_key": "1001-20261017_200000", "part": 2},
    ])
    CALLS.clear()
    w2 = _worker(tmp)
    assert w2.retry_failed(log=lambda *a: None) == 0, "不该把孤儿分P 重新排队"
    left = bu.load_failed()
    assert left and left[0]["part"] == 2, left      # 还留在清单里，等前面的分P 补完

    # ---- 稿件已经有 bvid 了 → 后面的分P 正常补传
    bu.remember_session("1001-20261017_200000", bvid="BV1FROMQUEUE", aid=1)
    assert w2.retry_failed(log=lambda *a: None) == 1
    assert w2.wait(20)
    assert CALLS and CALLS[0]["part"] == 2, CALLS
    assert not bu.load_failed(), "成功的应该从失败清单里划掉"

    # ---- 上传出错：item 里带着 session（含 datetime）也要能正常落盘
    def _boom(*a, **k):
        raise RuntimeError("HTTP 406 风控")

    bu.upload_recording = _boom
    w3 = _worker(tmp)
    s5 = rec.LiveSession("1005", datetime(2026, 10, 19, 20, 0, 0))
    w3.submit({"start": datetime(2026, 10, 19, 20, 0), "files": [str(f1)]},
              rid="1005", anchor="主播D", live_title="t", quality="FULL_HD1",
              ffmpeg="ffmpeg", session=s5.state, part=s5.take_parts(1))
    assert w3.wait(20)
    saved = bu.load_failed()
    assert saved and saved[-1]["session_key"] == s5.key, saved
    assert saved[-1]["part"] == 1, saved

    print("OK：队列的场次传递、分P 序号、补传顺序、失败落盘 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
