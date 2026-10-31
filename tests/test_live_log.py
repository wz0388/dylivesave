# -*- coding: utf-8 -*-
"""进度型日志（「已写 …」）的离线自测（Qt 版界面，不联网）。

    python tests/test_live_log.py

验证：
  · 同一房间的「已写」在日志里只留最新一条（就地替换），别的房间不受影响；
  · 「等待开播」用的是另一个 key，不会把「已写」挤掉；
  · logs/gui.log 仍然是全量（每一行都写文件）。
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from PySide6.QtWidgets import QApplication   # noqa: E402

import record as rec                         # noqa: E402
import record_gui as g                       # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
FILE_LINES: list[str] = []


def test_emit_log_fallback() -> None:
    """回调不认 live 参数时也不能丢日志。"""
    got = []

    def with_live(text, live=""):
        got.append((text, live))

    def without_live(text):
        got.append((text, ""))

    rec.emit_log(with_live, "有 live", "rec:1")
    rec.emit_log(without_live, "没有 live", "rec:1")
    assert got == [("有 live", "rec:1"), ("没有 live", "")], got


def test_console_single_line() -> None:
    g.file_log = lambda text: FILE_LINES.append(text)   # 别往仓库里写 logs/gui.log
    g.SETTINGS = Path(tempfile.mkdtemp()) / "record_gui.json"
    w = g.MainWindow()
    edit = w.log_edit
    put = w._put

    put(edit, "[1001] 房间号 1001，开始录制", "", "")
    put(edit, "[1001]   已写 1.00 GB，已录 00:00:03", "", "rec:1001")
    put(edit, "[1001]   已写 2.00 GB，已录 00:00:06", "", "rec:1001")
    put(edit, "[1002]   已写 9.00 GB，已录 01:00:00", "", "rec:1002")   # 另一个直播间
    put(edit, "[1001]   已写 4.00 GB，已录 00:04:00", "", "rec:1001")   # 刷新第一个
    # 等待开播也是「进度型」，但 key 不同 —— 不能把「已写」那行挤掉
    for i in range(2):
        put(edit, f"[1001] 当前主播未直播，30 秒后再拉一次（已等 {i * 30}s）", "",
            "wait:1001")

    lines = edit.toPlainText().splitlines()
    n1 = [ln for ln in lines if "[1001]   已写" in ln]
    n2 = [ln for ln in lines if "[1002]   已写" in ln]
    nw = [ln for ln in lines if "秒后再拉一次" in ln]
    assert len(n1) == 1, lines
    assert "4.00 GB" in n1[0], n1
    assert len(n2) == 1 and "9.00 GB" in n2[0], n2
    assert len(nw) == 1 and "已等 30s" in nw[0], nw
    assert lines[0].endswith("房间号 1001，开始录制"), lines

    # 普通日志追加在后面不影响那两条
    put(edit, "[1001] [√] 投稿完成", "", "")
    lines2 = edit.toPlainText().splitlines()
    assert len([ln for ln in lines2 if "已写" in ln]) == 2, lines2
    assert lines2[-1].endswith("投稿完成"), lines2
    w.close()


def main() -> int:
    test_emit_log_fallback()
    test_console_single_line()
    print("OK：已写带时长、同一房间只留一条、别的房间不受影响、普通日志追加 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
