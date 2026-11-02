# -*- coding: utf-8 -*-
"""自定义封面的离线自测（不联网、不真投稿）。

    python tests/test_cover.py

验证：
  · bili.toml 里没配 cover_file → custom_cover() 返回 None（退回抽第一帧）；
  · 配了且文件存在 → 返回那个路径；
  · 配了但文件不在 → 返回 None（不能因为封面配错就投不出去）；
  · 界面上「选择图片 / 用第一帧」能把路径写回 bili.toml，再读回来一致。
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu   # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_cover_"))
    bu.APP_DIR = tmp
    cfg_file = tmp / "bili.toml"
    # 界面里的「保存封面」走默认配置路径，测试时要指到临时目录，别改仓库里的真 bili.toml
    bu.DEFAULT_CONFIG = cfg_file
    bu.write_config_template(cfg_file, force=True)

    # ---- 1. 默认：没配封面 → 走抽帧
    cfg, _ = bu.load_config(cfg_file)
    assert cfg.cover_file == "", cfg.cover_file
    assert bu.custom_cover(cfg) is None

    # ---- 2. 配一张存在的图（带空格和中文的路径）
    img = tmp / "我的 封面.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")          # 内容无所谓，只判断文件在不在
    bu.set_config_value("cover_file", f"'{img}'", cfg_file)
    cfg2, _ = bu.load_config(cfg_file)
    assert cfg2.cover_file == str(img), cfg2.cover_file
    assert bu.custom_cover(cfg2) == img

    # ---- 3. 文件不在了 → 退回抽帧，不让投稿失败
    img.unlink()
    assert bu.custom_cover(cfg2) is None

    # ---- 4. 清空 → 回到自动
    bu.set_config_value("cover_file", "''", cfg_file)
    cfg3, _ = bu.load_config(cfg_file)
    assert cfg3.cover_file == "" and bu.custom_cover(cfg3) is None

    # ---- 5. 界面：选择图片 → 写回配置 → 重新读回来一致
    test_gui(tmp, cfg_file)

    print("OK：默认抽帧/自定义封面/文件缺失兜底/清空/界面读写 全部通过")
    print("   临时目录：", tmp)
    return 0


def test_gui(tmp: Path, cfg_file: Path) -> None:
    from PySide6.QtWidgets import QApplication
    import record_gui as g

    app = QApplication.instance() or QApplication([])
    g.SETTINGS = Path(tempfile.mkdtemp()) / "record_gui.json"
    w = g.MainWindow()
    try:
        img = tmp / "cover2.jpg"
        img.write_bytes(b"x")
        w._save_cover(str(img))
        assert w.ed_cover.text() == str(img), w.ed_cover.text()
        cfg, _ = bu.load_config()
        assert Path(cfg.cover_file) == img, cfg.cover_file
        assert bu.custom_cover(cfg) == img

        w.clear_cover()                            # 「用第一帧」
        assert w.ed_cover.text() == ""
        cfg2, _ = bu.load_config()
        assert cfg2.cover_file == "" and bu.custom_cover(cfg2) is None

        # ---- 每个主播自己的封面：写进 record_gui.json 的 rooms[].cover
        import json as _json
        row = w.rows[0]
        row.room.setText("50141210842")
        page = w.anchor_pages[id(row)]
        page.set_cover(str(img))
        assert row.cover == str(img), row.cover
        saved = _json.loads(g.SETTINGS.read_text(encoding="utf-8"))
        assert saved["rooms"][0]["cover"] == str(img), saved["rooms"][0]

        # 主播分页：日志按 [房间号] 分流；上传进度落到自己页里
        row.rid = row.tag = "50141210842"
        row.anchor = "李大霄"          # 真实运行时是 _status_from_log 从日志里学到的
        w.append_log("[50141210842] 主播：李大霄 · 测试", "")
        w._on_upload_event(1, "progress", 30, "李大霄", 30, 100, "#1 P1")
        assert "测试" in page.log_edit.toPlainText(), page.log_edit.toPlainText()
        assert len(page.up_rows) == 1, page.up_rows
    finally:
        w.close()


if __name__ == "__main__":
    raise SystemExit(main())
