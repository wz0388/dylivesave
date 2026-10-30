# -*- coding: utf-8 -*-
"""「3 分钟内又开播 = 同一场，投进同一个稿件」的离线自测（不联网、不真拉流）。

    python tests/test_session_join.py

复现的场景（用户日志）：
    20:25:00 主播下播 → 进入监控
    20:25:01 又开播 → 第 4 场（新场次）
这种「闪断重推流」如果按新的一场处理，B 站上就会多一个几十秒的碎片视频。
这里验证：
  · 上一场刚结束（< SESSION_JOIN_GAP）就开播 → **沿用上一场的 key/开播时间/bvid**；
  · 超过阈值再开播 → 正常开新的一场（新稿件）。
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import record as rec   # noqa: E402

RID = "50141210842"


def _streams():
    return SimpleNamespace(
        live=True, ok=True, error="", anchor="李大霄", title="李大霄正在直播",
        flv={"FULL_HD1": "http://example.invalid/live.flv"}, hls={})


def _run(tmp: Path, logs: list[str], bvid: str | None = None):
    rec.fetch_room_streams = lambda rid, cookie=None: _streams()
    rec.load_cookie = lambda: None
    rec.probe = lambda *a, **k: ""
    # 假 ffmpeg：写 1MB 就正常退出（rc=0）→ 立刻走「流已结束」收尾
    rec.build_cmd = lambda ffmpeg, url, out, left: [
        sys.executable, "-c",
        f"open(r'{out}','wb').write(b'G'*1048576)"]
    if bvid:
        # 模拟「上一场的稿件已经建好了」
        rec._LAST_SESSIONS[RID]["bvid"] = bvid
    logs.clear()
    return rec.run_recording(
        RID, quality="FULL_HD1", protocol="flv", segment=1800, duration=0,
        out_dir=str(tmp), ffmpeg=sys.executable, wait=0, upload=False,
        logf=lambda *a, live="": logs.append(" ".join(str(x) for x in a)))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_join_"))
    rec._LAST_SESSIONS.clear()
    rec.SESSION_JOIN_GAP = 180

    logs: list[str] = []

    # ---- 第 1 场
    res1 = _run(tmp, logs)
    key1 = res1["session"]
    assert res1["bvid"] is None
    assert any("本场直播" in ln for ln in logs), logs
    print("第 1 场:", key1)

    # ---- 1 秒后又「开播」：应当接上一场（并带上上一场已经建好的稿件号）
    res2 = _run(tmp, logs, bvid="BV1JOINED")
    print("\n".join(logs[:6]))
    print("第 2 场:", res2["session"])
    assert res2["session"] == key1, (res2["session"], key1)
    assert res2["bvid"] == "BV1JOINED", res2
    assert any("算**同一场**" in ln for ln in logs), logs

    # ---- 隔了超过 3 分钟才开播：正常开新的一场（新稿件）
    rec._LAST_SESSIONS[RID]["ended"] -= (rec.SESSION_JOIN_GAP + 60)   # 手工拨旧
    res3 = _run(tmp, logs)
    print("第 3 场:", res3["session"])
    assert res3["session"] != key1, res3["session"]
    assert res3["bvid"] is None, res3
    assert any("关播 3m00s 内再开播也算同一场" in ln for ln in logs), logs

    # ---- 换一个直播间，不会误并到别人的场次
    assert rec.resume_hint("12345") is None

    # ---- 同一场 resume 出来的 key/开播时间要和上一场一致
    prev = rec._LAST_SESSIONS[RID]
    s = rec.LiveSession(RID, resume=prev)
    assert s.key == prev["key"] and s.start == prev["start"] and s.resumed

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nOK：3 分钟内再开播并进同一场（同 key / 同 bvid），超时才开新场")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
