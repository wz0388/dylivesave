# -*- coding: utf-8 -*-
"""分P 归属逻辑的离线自测（不联网、不真传 B 站）。

    python tests/test_multipart.py

覆盖的就是这次二次开发的核心约定：
  · 同一场直播的多个分段 → 同一个稿件的 P1/P2/P3…（第一段建稿，后面追加）；
  · 关播再开播（新的一场）→ 新稿件，不会被塞进上一场的视频；
  · 一场里断流重连产生的多个文件 → 连续的多个分P；
  · bili.toml 里 multi_part = false → 退回「每段一个独立稿件」。
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu  # noqa: E402

CALLS: list[tuple] = []


def _mkfile(dirpath: Path, name: str, mb: int = 2) -> Path:
    p = dirpath / name
    p.write_bytes(b"\x47" + b"\0" * (mb * 1024 * 1024 - 1))
    return p


async def _fake_upload(paths, page_titles, **kw):
    """假装建了一个新稿。"""
    CALLS.append(("create", [Path(p).name for p in paths], list(page_titles), kw["title"]))
    return {"bvid": "BV1CREATE", "aid": 111}


async def _fake_append(bvid, paths, **kw):
    """假装往已有稿件追加了分P（标题用真实逻辑算，好验证时间区间）。"""
    titles = bu.part_titles(kw["cfg"], len(paths), seg_start=kw["when"],
                            durs=kw.get("durs"), first_part=1)
    CALLS.append(("append", bvid, [Path(p).name for p in paths], titles))
    return {"bvid": bvid, "pages": 99, "added": len(paths)}


def _patch(tmp: Path) -> None:
    bu.APP_DIR = tmp                      # 场次登记表写到临时目录，别污染仓库
    bu._do_upload = _fake_upload
    bu._do_append = _fake_append
    bu._pick_client = lambda: asyncio.sleep(0, result="FakeClient")
    bu.probe_duration = lambda *a, **k: 600.0
    bu._credential_object = lambda d: type("C", (), {"bili_jct": "fake"})()
    bu.read_credential = lambda f: {"sessdata": "x", "bili_jct": "y"}

    def _fake_frame(video, out, at=0.0, ffmpeg="ffmpeg"):
        from PIL import Image
        Image.new("RGB", (8, 8), (20, 20, 20)).save(out, "JPEG")
        return True

    bu.grab_frame = _fake_frame


def _cfg(multi: bool) -> bu.BiliConfig:
    return bu.BiliConfig(min_size_mb=0, multi_part=multi)   # 用默认的分P 标题模板


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_test_"))
    _patch(tmp)
    media = tmp / "rec"
    media.mkdir()

    f1 = _mkfile(media, "a_2000.ts")
    f2 = _mkfile(media, "a_2000_part2.ts")     # 断流重连
    f3 = _mkfile(media, "a_2200.ts")
    f4 = _mkfile(media, "a_0000.ts")

    # ---- 第 1 场：第 1 段（2 个文件）+ 第 2 段（1 个文件）
    s1 = bu.make_session("1001", datetime(2026, 10, 17, 20, 0, 0))
    bu.upload_recording([f1, f2], anchor="主播A", cfg=_cfg(True), room_id="1001",
                        when=datetime(2026, 10, 17, 20, 0, 0), seg_index=1,
                        session=s1, part=1, log=lambda *a: None)
    assert s1["bvid"] == "BV1CREATE", s1
    bu.upload_recording([f3], anchor="主播A", cfg=_cfg(True), room_id="1001",
                        when=datetime(2026, 10, 17, 22, 0, 0), seg_index=2,
                        session=s1, part=3, log=lambda *a: None)

    assert CALLS[0][0] == "create" and CALLS[0][1] == ["a_2000.ts", "a_2000_part2.ts"], CALLS[0]
    # 分P 标题 = 这一段的「开始-结束」（probe_duration 被打桩成 600 秒）
    assert CALLS[0][2] == ["20:00-20:10", "20:10-20:20"], CALLS[0][2]
    # 追加的分P 用**这一段自己**的开始时间（22:00），不是整场开始的 20:00
    assert CALLS[1] == ("append", "BV1CREATE", ["a_2200.ts"], ["22:00-22:10"]), CALLS[1]

    # 场次登记表：同一场能查回稿件（补传/重启后要靠它）
    assert (bu.lookup_session(s1["key"]) or {}).get("bvid") == "BV1CREATE"

    # ---- 关播再开播：新的一场 → 必须新建稿件，不能追加到上一场
    s2 = bu.make_session("1001", datetime(2026, 10, 18, 9, 0, 0))
    assert s2["key"] != s1["key"]
    bu.upload_recording([f4], anchor="主播A", cfg=_cfg(True), room_id="1001",
                        when=datetime(2026, 10, 18, 9, 0, 0), seg_index=1,
                        session=s2, part=1, log=lambda *a: None)
    assert CALLS[2][0] == "create", CALLS[2]

    # ---- multi_part = false：退回老行为，每段一个独立稿件
    CALLS.clear()
    s3 = bu.make_session("1002", datetime(2026, 10, 17, 20, 0, 0))
    for i, f in enumerate([f1, f3], 1):
        bu.upload_recording([f], anchor="主播B", cfg=_cfg(False), room_id="1002",
                            when=datetime(2026, 10, 17, 20, 0, 0), seg_index=i,
                            session=s3, part=i, log=lambda *a: None)
    assert [c[0] for c in CALLS] == ["create", "create"], CALLS
    assert s3["bvid"] is None, s3          # 不该写回场次

    # ---- 前面的分P 没传成功时，后面的分P 不许自己另起一个视频
    CALLS.clear()
    s4 = bu.make_session("1003", datetime(2026, 10, 17, 20, 0, 0))
    try:
        bu.upload_recording([f2], anchor="主播C", cfg=_cfg(True), room_id="1003",
                            when=datetime(2026, 10, 17, 22, 0, 0), seg_index=2,
                            session=s4, part=2, log=lambda *a: None)
    except bu.BiliError as e:
        assert "第 2 个分P" in str(e), e
    else:
        raise AssertionError("P2 在没有稿件时不该能建稿")
    assert not CALLS and s4["bvid"] is None

    # ---- 分P 标题：默认是「开始-结束」；时长探不出来就只留开始时间
    t = datetime(2026, 10, 17, 22, 5)
    assert bu.part_title(_cfg(True), 3, when=t, dur=0) == "22:05", bu.part_title(_cfg(True), 3, when=t, dur=0)
    assert bu.part_title(_cfg(True), 3, when=t, dur=7200) == "22:05-00:05"
    # 断流成两个文件的段：第二个文件接着第一个的结尾，时间连续
    assert bu.part_titles(_cfg(True), 2, seg_start=t, durs=[1800, 1800]) == \
        ["22:05-22:35", "22:35-23:05"]

    # ---- 失败记录能跨重启补传：session 对象不落盘，但 bvid 在场次表里
    item = {"seq": 7, "files": [str(f3)], "start": datetime(2026, 10, 17, 22, 0),
            "session": s1, "session_key": s1["key"], "part": 3}
    bu.remember_failed(item, "boom")
    saved = bu.load_failed()
    assert saved and saved[-1]["session_key"] == s1["key"], saved
    assert "session" not in saved[-1], saved           # datetime 不能塞进 JSON
    bu.remove_failed(item)

    print("OK：分P 归属、场次切分、回退开关、失败补传记录 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
