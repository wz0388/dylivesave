# -*- coding: utf-8 -*-
"""「上传成功后删本地视频」的离线自测（不联网、不真传 B 站）。

    python tests/test_delete_after_upload.py

覆盖：
  · 开关默认关闭 —— 什么都不删；
  · 开启后只删**真的传上去**的文件，被跳过（空文件 / 太小）的不动；
  · 上传失败时一个都不删（还要留着补传）；
  · 界面那个勾选框用的 set_config_value() 能原地改 bili.toml，注释和别的配置不丢。
"""
import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu  # noqa: E402

CALLS: list[str] = []


async def _fake_upload(paths, page_titles, **kw):
    CALLS.append("create")
    return {"bvid": "BV1DEL", "aid": 1}


def _patch(tmp: Path) -> None:
    bu.APP_DIR = tmp
    bu._do_upload = _fake_upload
    bu._pick_client = lambda: asyncio.sleep(0, result="FakeClient")
    bu.probe_duration = lambda *a, **k: 600.0
    bu._credential_object = lambda d: type("C", (), {"bili_jct": "fake"})()
    bu.read_credential = lambda f: {"sessdata": "x", "bili_jct": "y"}

    def _frame(video, out, at=0.0, ffmpeg="ffmpeg"):
        from PIL import Image
        Image.new("RGB", (8, 8), (20, 20, 20)).save(out, "JPEG")
        return True

    bu.grab_frame = _frame


def _mk(d: Path, name: str, size: int) -> Path:
    p = d / name
    p.write_bytes(b"x" * size)
    return p


def _upload(files, cfg, log=lambda *a: None):
    return bu.upload_recording(files, anchor="主播", cfg=cfg, room_id="1",
                               when=datetime(2026, 10, 17, 20, 0),
                               log=log)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_del_"))
    _patch(tmp)
    media = tmp / "rec"
    media.mkdir()

    off = bu.BiliConfig(min_size_mb=0, delete_after_upload=False)
    on = bu.BiliConfig(min_size_mb=1, delete_after_upload=True)

    # ---- 关闭时不删
    a = _mk(media, "a.ts", 3 * 1024 * 1024)
    _upload([a], off)
    assert a.is_file(), "开关关着还删文件了"

    # ---- 开启后：传上去的删掉，跳过的（小于 1MB）留着
    b = _mk(media, "b.ts", 3 * 1024 * 1024)
    tiny = _mk(media, "tiny.ts", 1024)
    _upload([b, tiny], on)
    assert not b.is_file(), "上传成功的文件应该被删掉"
    assert tiny.is_file(), "没上传的文件不该被删（还要留着补传）"

    # ---- 上传失败时一个都不删
    c = _mk(media, "c.ts", 3 * 1024 * 1024)

    async def _boom(*a, **k):
        raise bu.BiliError("HTTP 406 风控")

    bu._do_upload = _boom
    try:
        _upload([c], bu.BiliConfig(min_size_mb=0, delete_after_upload=True))
    except bu.BiliError:
        pass
    else:
        raise AssertionError("预期上传失败")
    assert c.is_file(), "上传失败不能删文件"
    bu._do_upload = _fake_upload

    # ---- set_config_value：原地改 bili.toml，注释/别的键都还在
    cfgfile = tmp / "bili.toml"
    shutil.copyfile(HERE / "bili.toml", cfgfile)
    before = cfgfile.read_text(encoding="utf-8")
    bu.set_config_value("delete_after_upload", "true", cfgfile)
    after = cfgfile.read_text(encoding="utf-8")
    assert "delete_after_upload = true" in after, after
    cfg, _ = bu.load_config(cfgfile)
    assert cfg.delete_after_upload is True
    assert cfg.multi_part is True                       # 别的键没被动
    assert "# 投稿成功后删掉本地录制文件" in after        # 注释还在
    assert len(after.splitlines()) == len(before.splitlines())   # 没多出/少行

    bu.set_config_value("delete_after_upload", "false", cfgfile)
    cfg, _ = bu.load_config(cfgfile)
    assert cfg.delete_after_upload is False

    # ---- 键不存在时会插到 [bili] 段里（老配置升级）
    cfgfile2 = tmp / "bili2.toml"
    cfgfile2.write_text("[bili]\nenabled = true\ntid = 21\n", encoding="utf-8")
    bu.set_config_value("delete_after_upload", "true", cfgfile2)
    cfg2, _ = bu.load_config(cfgfile2)
    assert cfg2.delete_after_upload is True, cfgfile2.read_text(encoding="utf-8")
    assert cfg2.tid == 21

    print("OK：删除开关、只删已上传的、失败不删、配置回写 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
