# -*- coding: utf-8 -*-
"""上传卡住 → 取消重传 的离线自测（不联网、不真传）。

    python tests/test_upload_stall.py

验证：
  · 3 分钟（这里用很短的超时）没有新分块 → 看门狗触发：回调一次、cancel 一次；
  · 一直在传（touch 持续更新）→ 不触发；
  · 卡住后旧的重传逻辑：每次都从头再来，最多 STALL_RETRIES 次，
    每次都给 QQ 发一条通知；全卡住才抛 BiliError（进待补传清单）。
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu   # noqa: E402

QUIET = lambda *a: None      # noqa: E731


class FakeLoop:
    def __init__(self):
        self.calls = 0

    def call_soon_threadsafe(self, fn):
        self.calls += 1
        fn()


class FakeTask:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def test_guard_fires() -> None:
    fired = []
    g = bu._StallGuard(timeout=0.4, on_stall=fired.append)
    loop, task = FakeLoop(), FakeTask()
    g.bind(loop, task)
    g.start()
    time.sleep(1.2)
    g.stop()
    assert fired and "没有进度" in fired[0], fired
    assert loop.calls == 1 and task.cancelled == 1, (loop.calls, task.cancelled)


def test_guard_touch_keeps_alive() -> None:
    g = bu._StallGuard(timeout=1.0)
    loop, task = FakeLoop(), FakeTask()
    g.bind(loop, task)
    g.start()
    for i in range(6):           # 一共 1.2 秒，但每 0.2 秒都有新块
        g.touch(i)
        time.sleep(0.2)
    g.stop()
    assert loop.calls == 0 and task.cancelled == 0, (loop.calls, task.cancelled)


def test_guard_same_done_not_counted() -> None:
    """同一条进度重复回调（库会重复触发）不算「有新进度」。"""
    g = bu._StallGuard(timeout=0.5)
    g.touch(5)
    t0 = g.last
    time.sleep(0.05)
    g.touch(5)
    assert g.last == t0, (g.last, t0)
    g.touch(6)
    assert g.last > t0


def test_retry_then_give_up() -> None:
    bu.STALL_RETRY_DELAY = 0
    notes, logs = [], []
    calls = []

    def always_stall(coro_factory, log):
        calls.append(1)
        raise bu.UploadStalled("3 分钟没有进度")

    orig = bu._run_upload_once
    bu._run_upload_once = always_stall
    try:
        try:
            bu._upload_with_retry(lambda g: None, log=logs.append,
                                  notify=notes.append, what="第 2 段")
            raise AssertionError("应该抛 BiliError")
        except bu.BiliError as e:
            assert "连续" in str(e), e
    finally:
        bu._run_upload_once = orig

    assert len(calls) == bu.STALL_RETRIES, len(calls)
    assert len(notes) == bu.STALL_RETRIES, notes        # 每次卡住都通知
    assert "【上传卡住】第 2 段" in notes[0] and "丢弃重传" in notes[0], notes
    assert any("卡住" in x for x in logs), logs


def test_retry_then_success() -> None:
    bu.STALL_RETRY_DELAY = 0
    notes, calls = [], []

    def flaky(coro_factory, log):
        calls.append(1)
        if len(calls) == 1:
            raise bu.UploadStalled("3 分钟没有进度")
        return {"bvid": "BV1ok"}

    orig = bu._run_upload_once
    bu._run_upload_once = flaky
    try:
        out = bu._upload_with_retry(lambda g: None, log=QUIET,
                                    notify=notes.append, what="第 3 段")
    finally:
        bu._run_upload_once = orig
    assert out == {"bvid": "BV1ok"}, out
    assert len(calls) == 2 and len(notes) == 1, (calls, notes)


def main() -> int:
    test_guard_fires()
    test_guard_touch_keeps_alive()
    test_guard_same_done_not_counted()
    test_retry_then_give_up()
    test_retry_then_success()
    print("OK：卡住检测（触发/心跳/去重）+ 丢弃重传（重试/放弃/成功）全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
