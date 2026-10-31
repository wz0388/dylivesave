# -*- coding: utf-8 -*-
"""「每个主播自己有分段时间」的离线自测（Qt 版界面，不联网）。

    python tests/test_row_segment.py

验证：
  · 每行有独立的分段时间下拉框，互不影响；
  · 新加的行默认继承「公共设置」里那个默认值；
  · 存盘/读盘能带上每行的分段时间（老设置没有这一项时用默认值兜底）。
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from PySide6.QtWidgets import QApplication   # noqa: E402

import record_gui as g                       # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)


def _new_app(settings: Path):
    g.SETTINGS = settings
    return g.MainWindow()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_rowseg_"))
    settings = tmp / "record_gui.json"

    w = _new_app(settings)
    assert len(w.rows) == 1, w.rows

    # 第 1 行：30 分钟
    w.rows[0].room.setText("111")
    w.rows[0].segment.setCurrentText("30 分钟")
    # 第 2 行：不分段；第 3 行：不指定 → 跟「默认分段时间」走
    r1 = w.add_row("222", "", "不分段")
    r2 = w.add_row("333")

    assert w._segment_seconds(w.rows[0].segment.currentText()) == 1800
    assert w._segment_seconds(r1.segment.currentText()) == 0
    assert r2.segment.currentText() == w.cmb_segment.currentText() == g.SEGMENT_DEFAULT

    # 改全局默认不影响已经存在的行
    w.cmb_segment.setCurrentText("60 分钟")
    assert w.rows[0].segment.currentText() == "30 分钟"
    assert w.add_row("444").segment.currentText() == "60 分钟"

    w._save_settings()
    saved = json.loads(settings.read_text(encoding="utf-8"))
    segs = [r["segment"] for r in saved["rooms"]]
    assert segs == ["30 分钟", "不分段", g.SEGMENT_DEFAULT, "60 分钟"], segs
    assert saved["segment"] == "60 分钟"
    w.close()

    # ---- 重新打开：每行的分段时间都回来了
    w2 = _new_app(settings)
    assert [r.segment.currentText() for r in w2.rows] == segs, \
        [r.segment.currentText() for r in w2.rows]
    assert w2.cmb_segment.currentText() == "60 分钟"
    w2.close()

    # ---- 老设置（rooms 里没有 segment 字段）→ 用默认值兜底
    old = tmp / "old.json"
    old.write_text(json.dumps({"rooms": [{"room": "555", "quality": ""},
                                         {"room": "666", "quality": ""}],
                               "segment": "1 小时"}, ensure_ascii=False),
                   encoding="utf-8")
    w3 = _new_app(old)
    assert [r.segment.currentText() for r in w3.rows] == ["60 分钟", "60 分钟"], \
        [r.segment.currentText() for r in w3.rows]
    w3.close()

    print("OK：每行独立分段时间、新行继承默认、存盘读盘、老设置兜底 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
