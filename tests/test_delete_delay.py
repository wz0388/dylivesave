# -*- coding: utf-8 -*-
"""「录完 x 小时后自动删」的离线自测（不联网、不真删你的片子）。

    python tests/test_delete_delay.py

验证：
  · 开关关着 → 一个文件都不动；
  · delete_delay_hours = 0 → 上传成功就删（老行为）；
  · delete_delay_hours = 6 → 文件留着，排进 bili_delete_queue.json，到点才删；
  · 队列落盘，重启后 sweep 能把「已经到期」的补删掉；
  · 文件已经被手动删掉 → 队列自动清理，不会报错。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu   # noqa: E402

QUIET = lambda *a: None   # noqa: E731


def _cfg(delay: float, on: bool = True) -> bu.BiliConfig:
    return bu.BiliConfig(delete_after_upload=on, delete_delay_hours=delay)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_del_"))
    bu.APP_DIR = tmp                      # 队列文件写到临时目录，别碰真实配置

    # ---- 1. 开关关着：什么都不删
    f = tmp / "a.ts"
    f.write_bytes(b"x" * 2048)
    assert bu.delete_uploaded([f], _cfg(6, on=False), QUIET) == 0
    assert f.exists()

    # ---- 2. 延迟 6 小时：文件还在，队列里有它，到期时间在未来
    n = bu.delete_uploaded([f], _cfg(6), QUIET)
    assert n == 1 and f.exists(), (n, f.exists())
    q = bu.load_delete_queue()
    assert len(q) == 1 and q[0]["path"] == str(f), q
    assert q[0]["due"] > time.time() + 5 * 3600, q

    # 重复排同一个文件不会加两条
    bu.delete_uploaded([f], _cfg(6), QUIET)
    assert len(bu.load_delete_queue()) == 1

    # ---- 3. 没到期：sweep 不删
    assert bu.sweep_delete_queue(QUIET) == 0
    assert f.exists()

    # ---- 4. 把到期时间改到过去（模拟「6 小时后」）→ sweep 删掉并清空队列
    q = bu.load_delete_queue()
    q[0]["due"] = time.time() - 1
    bu.delete_queue_path().write_text(json.dumps(q), encoding="utf-8")
    assert bu.sweep_delete_queue(QUIET) == 1
    assert not f.exists()
    assert bu.load_delete_queue() == []

    # ---- 5. 延迟 0 = 立即删（老行为）
    f2 = tmp / "b.ts"
    f2.write_bytes(b"x" * 1024)
    assert bu.delete_uploaded([f2], _cfg(0), QUIET) == 1
    assert not f2.exists()

    # ---- 6. 排进队列后文件被用户自己删了 → sweep 静默清理
    f3 = tmp / "c.ts"
    f3.write_bytes(b"x" * 1024)
    bu.delete_uploaded([f3], _cfg(2), QUIET)
    f3.unlink()
    assert bu.sweep_delete_queue(QUIET) == 0
    assert bu.load_delete_queue() == []

    # ---- 7. 配置里读得出来
    cfg_file = tmp / "bili.toml"
    bu.write_config_template(cfg_file, force=True)
    bu.set_config_value("delete_delay_hours", "6", cfg_file)
    bu.set_config_value("delete_after_upload", "true", cfg_file)
    cfg, _ = bu.load_config(cfg_file)
    assert cfg.delete_after_upload is True and cfg.delete_delay_hours == 6, cfg

    print("OK：开关/立即删/延迟排队/到期清理/队列落盘/配置读写 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
