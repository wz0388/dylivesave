# -*- coding: utf-8 -*-
"""「已写」进度日志的离线自测（不联网、不真录）。

    python tests/test_live_log.py

验证这次改的两件事：
  · 进度行里有体积也有时长（时:分:秒）；
  · 同一个直播间的「已写」在控制台只留一条（旧的删掉，新的接在末尾），
    但文件日志照旧每条都写（含等待开播那种普通日志）。
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import record as rec        # noqa: E402
import record_gui as g      # noqa: E402

FILE_LINES: list[str] = []


def test_fmt_hms() -> None:
    assert rec.fmt_hms(0) == "00:00:00"
    assert rec.fmt_hms(61) == "00:01:01"
    assert rec.fmt_hms(3661) == "01:01:01"
    assert rec.fmt_hms(7200) == "02:00:00"
    assert rec.fmt_hms(90000) == "25:00:00"          # 超过一天也照常往上加
    assert rec.fmt_hms(None) == "00:00:00"


def test_emit_log() -> None:
    got: list[tuple[str, str]] = []

    def with_live(line, live=""):
        got.append((line, live))

    def without_live(line):
        got.append((line, ""))

    rec.emit_log(with_live, "有 live", "rec:1")
    rec.emit_log(without_live, "没有 live", "rec:1")   # 不认这个参数也不能丢日志
    assert got == [("有 live", "rec:1"), ("没有 live", "")], got


def test_console_single_line() -> None:
    import queue
    import tkinter as tk

    g.file_log = lambda text: FILE_LINES.append(text)   # 别往仓库里写 logs/gui.log
    root = tk.Tk()
    root.withdraw()
    txt = tk.Text(root)

    class Fake:
        pass

    app = Fake()
    app.txt = txt
    app.q = queue.Queue()
    log = g.RecorderApp.append_log

    # 录制线程那条真实路径：logf(...) → 队列 → append_log
    g.RecorderApp._log_from_thread(app, "[1001] 已写 1.00 GB，已录 00:00:15",
                                   live="rec:1001")
    kind, payload = app.q.get_nowait()
    assert kind == "log" and payload[0].startswith("[1001]") and payload[1] == "rec:1001", payload
    log(app, *payload)

    log(app, "开场白")
    for i in range(1, 3):
        log(app, f"[1001] 已写 {i + 1}.00 GB，已录 00:0{i}:15", "rec:1001")
    log(app, "[1002] 已写 9.00 GB，已录 01:00:00", "rec:1002")   # 另一个直播间
    log(app, "[1001] 已写 4.00 GB，已录 00:04:00", "rec:1001")   # 刷新第一个
    log(app, "[1001] 未开播，30 秒后再拉一次")                    # 普通日志不动

    lines = txt.get("1.0", "end-1c").splitlines()
    txt.destroy()
    root.destroy()

    n1 = [ln for ln in lines if "[1001] 已写" in ln]
    n2 = [ln for ln in lines if "[1002] 已写" in ln]
    assert len(n1) == 1, lines                       # 一个直播间只留一条
    assert "4.00 GB" in n1[0] and "00:04:00" in n1[0], n1
    assert len(n2) == 1 and "9.00 GB" in n2[0], n2    # 别的直播间互不影响
    assert lines[0].endswith("开场白"), lines          # 普通日志一条不少
    assert any("未开播" in ln for ln in lines), lines

    # 文件日志是全量（含被控制台删掉的那些进度和等待日志）
    assert sum("已写" in t for t in FILE_LINES) == 5, FILE_LINES
    assert any("未开播" in t for t in FILE_LINES), FILE_LINES


def main() -> int:
    test_fmt_hms()
    test_emit_log()
    test_console_single_line()
    print("OK：已写带时长、同一房间只留一条、文件日志全量 全部通过")
    print("   临时目录：", tempfile.gettempdir())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
