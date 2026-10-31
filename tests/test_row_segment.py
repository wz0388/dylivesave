# -*- coding: utf-8 -*-
"""「每个主播自己的分段时间」的离线自测（不联网、不真录）。

    python tests/test_row_segment.py

验证：
  · 每行有独立的分段时间下拉框，互不影响；
  · 新加的行默认继承主界面那个「默认分段时间」；
  · 存盘/读盘能带上每行的分段时间（老设置没有这一项时用默认值兜底）；
  · 点「开始」时用的是**这一行**的值，不是全局的。
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import customtkinter as ctk   # noqa: E402

import record_gui as g   # noqa: E402


def _new_app(settings: Path):
    g.SETTINGS = settings
    root = ctk.CTk()
    root.withdraw()
    return g.RecorderApp(root), root


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_rowseg_"))
    settings = tmp / "record_gui.json"

    app, root = _new_app(settings)
    assert len(app.rows) == 1, app.rows

    # 第 1 行：30 分钟
    r0 = app.rows[0]
    r0["room"].set("111")
    r0["segment"].set("30 分钟")
    # 第 2 行：不分段
    r1 = app.add_row("222", "", "不分段")
    # 第 3 行：不指定 → 跟「默认分段时间」走
    r2 = app.add_row("333")

    assert app._segment_seconds(r0["segment"].get()) == 1800
    assert app._segment_seconds(r1["segment"].get()) == 0
    assert r2["segment"].get() == app.var_segment.get() == g.SEGMENT_DEFAULT

    # 改全局默认不影响已经存在的行
    app.var_segment.set("60 分钟")
    assert r0["segment"].get() == "30 分钟"
    assert app.add_row("444")["segment"].get() == "60 分钟"

    app._save_settings()
    root.destroy()

    saved = json.loads(settings.read_text(encoding="utf-8"))
    segs = [r["segment"] for r in saved["rooms"]]
    assert segs == ["30 分钟", "不分段", g.SEGMENT_DEFAULT, "60 分钟"], segs
    assert saved["segment"] == "60 分钟"          # 全局默认也存着

    # ---- 重新打开：每行的分段时间都回来了
    app2, root2 = _new_app(settings)
    assert [r["segment"].get() for r in app2.rows] == segs, \
        [r["segment"].get() for r in app2.rows]
    assert app2.var_segment.get() == "60 分钟"
    root2.destroy()

    # ---- 老设置（rooms 里没有 segment 字段）→ 用默认值兜底
    old = tmp / "old.json"
    old.write_text(json.dumps({"rooms": [{"room": "555", "quality": ""},
                                         {"room": "666", "quality": ""}],
                               "segment": "1 小时"}, ensure_ascii=False),
                   encoding="utf-8")
    app3, root3 = _new_app(old)
    assert [r["segment"].get() for r in app3.rows] == ["60 分钟", "60 分钟"], \
        [r["segment"].get() for r in app3.rows]
    root3.destroy()

    print("OK：每行独立分段时间、新行继承默认、存盘读盘、老设置兜底 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
