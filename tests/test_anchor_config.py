# -*- coding: utf-8 -*-
"""每个主播独立目录 / 独立 bili.toml 的离线自测（不联网、不真录）。

    python tests/test_anchor_config.py

验证：
  · 没配独立配置 → 走全局 bili.toml；
  · 配了独立 bili.toml → 用它的分区/标题模板（等于另一个账号/另一套参数）；
  · 独立配置里 enabled=false 或文件读不了 → 这一段不投（记待补传），不会拿全局顶上；
  · 界面：主播页填的录制目录/配置路径会存进 record_gui.json，重新打开还在。
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu   # noqa: E402
import record as rec         # noqa: E402

QUIET = lambda *a: None      # noqa: E731


def _write_cfg(path: Path, *, tid: int, title: str = "{anchor} 回放 {date}",
               enabled: bool = True) -> None:
    bu.write_config_template(path, force=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        if ln.startswith("tid ="):
            out.append(f"tid = {tid}                     # 分区")
        elif ln.startswith("title_template ="):
            out.append(f'title_template = "{title}"')
        elif ln.startswith("enabled ="):
            out.append(f"enabled = {'true' if enabled else 'false'}")
        else:
            out.append(ln)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_anchor_cfg_"))
    bu.APP_DIR = tmp
    bu.DEFAULT_CONFIG = tmp / "bili.toml"
    bu.write_config_template(bu.DEFAULT_CONFIG, force=True)

    # 离线：别真去查 B 站登录态
    bu.check_cookie_status = lambda cred: ("ok", "stub（测试）")
    bu.read_credential = lambda p: {"sessdata": "x", "bili_jct": "y", "dedeuserid": "1"}

    anchor_cfg = tmp / "主播A.toml"
    _write_cfg(anchor_cfg, tid=171, title="【A】{anchor} {date}")

    w = rec.UploadWorker(log=QUIET)
    w._ensure_cfg = lambda: True
    w._cfg = bu.load_config(bu.DEFAULT_CONFIG)[0]

    # ---- 1. 不配 → 全局
    cfg, problem = w._cfg_for({})
    assert problem == "" and cfg.tid == 21, (cfg.tid, problem)

    # ---- 2. 配了独立配置 → 用它（分区/模板都不一样）
    cfg2, problem = w._cfg_for({"config": str(anchor_cfg)})
    assert problem == "" and cfg2.tid == 171, (cfg2.tid, problem)
    assert cfg2.title_template == "【A】{anchor} {date}", cfg2.title_template

    # ---- 3. enabled=false 的独立配置 → 不投（也不退回全局）
    off = tmp / "off.toml"
    _write_cfg(off, tid=1, enabled=False)
    cfg3, problem3 = w._cfg_for({"config": str(off)})
    assert cfg3 is None and "enabled" in problem3, (cfg3, problem3)

    # ---- 4. 文件读不了 → 明确报错，不瞎投
    cfg4, problem4 = w._cfg_for({"config": str(tmp / "没有这个.toml")})
    assert cfg4 is None and "不存在" in problem4, (cfg4, problem4)

    # ---- 5. 界面：主播页的目录/配置存得住
    test_gui(tmp)

    print("OK：全局回退/独立配置/enabled=false/文件缺失/界面存盘 全部通过")
    print("   临时目录：", tmp)
    return 0


def test_gui(tmp: Path) -> None:
    from PySide6.QtWidgets import QApplication
    import record_gui as g

    app = QApplication.instance() or QApplication([])
    g.SETTINGS = Path(tempfile.mkdtemp()) / "record_gui.json"
    w = g.MainWindow()
    try:
        row = w.rows[0]
        row.room.setText("50141210842")
        page = w.anchor_pages[id(row)]

        d = tmp / "主播A录像"
        page.ed_dir.setText(str(d))
        page._dir_changed()
        cfg_file = tmp / "主播A.toml"
        page.ed_cfg.setText(str(cfg_file))
        page._cfg_changed()
        assert row.out_dir == str(d) and row.bili_config == str(cfg_file)

        w._save_settings()
        saved = json.loads(g.SETTINGS.read_text(encoding="utf-8"))
        assert saved["rooms"][0]["out_dir"] == str(d), saved["rooms"][0]
        assert saved["rooms"][0]["bili_config"] == str(cfg_file), saved["rooms"][0]
    finally:
        w.close()

    # ---- 重新打开：值还在
    w2 = g.MainWindow()
    try:
        row2 = w2.rows[0]
        page2 = w2.anchor_pages[id(row2)]
        assert row2.out_dir == str(tmp / "主播A录像"), row2.out_dir
        assert row2.bili_config == str(tmp / "主播A.toml"), row2.bili_config
        assert page2.ed_dir.text() == row2.out_dir
        assert page2.ed_cfg.text() == row2.bili_config
    finally:
        w2.close()


if __name__ == "__main__":
    raise SystemExit(main())
