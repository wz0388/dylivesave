# -*- coding: utf-8 -*-
"""合集（season）功能的自测（不联网：把 requests 换掉）。

    python tests/test_season.py

覆盖：
  · 拉合集列表（分页、小节 id、视频数）；
  · 加视频到合集：aid/cid 取对了、POST 的 body 是 sectionId + episodes；
  · 没加成功（比如稿件还在审核）→ 记进 bili_season_pending.json，
    下次 retry_pending_seasons() 补上并从清单里划掉；
  · upload_recording 建稿成功后确实会去加合集，加失败也不影响投稿结果。
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_uploader as bu  # noqa: E402

CALLS: dict = {"get": [], "post": [], "assigned": []}


class FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


class FakeRequests:
    """把 bili_uploader 里的 requests 换成它；按 URL 回不同的假数据。"""

    def __init__(self):
        self.fail_add = False

    def get(self, url, **kw):
        CALLS["get"].append((url, kw.get("params")))
        if "seasons" in url:
            return FakeResp({"code": 0, "data": {"seasons": [
                {"season": {"id": 111, "title": "直播回放", "epCount": 3},
                 "sections": {"sections": [{"id": 222, "title": "正片"}]}},
                {"season": {"id": 333, "title": "日常", "epCount": 0},
                 "sections": {"sections": [{"id": 444, "title": "正片"}]}},
            ]}})
        if "vupre/web/archive/view" in url:
            return FakeResp({"code": 0, "data": {"archive": {"aid": 9001},
                                                 "videos": [{"cid": 7001}]}})
        return FakeResp({"code": -400, "message": "unexpected"})

    def post(self, url, **kw):
        CALLS["post"].append((url, kw.get("json")))
        if self.fail_add:
            return FakeResp({"code": -400, "message": "稿件还在审核中"})
        return FakeResp({"code": 0, "message": "0"})


def test_list_and_add(fake: FakeRequests) -> None:
    seasons = bu.list_seasons({"sessdata": "s", "bili_jct": "j"})
    assert [s["id"] for s in seasons] == [111, 333], seasons
    assert seasons[0]["section_id"] == 222 and seasons[0]["count"] == 3

    name = bu.add_video_to_season("BV1TEST", seasons[0], {"sessdata": "s", "bili_jct": "j"},
                                  title="李大霄 直播回放")
    assert name == "直播回放", name
    url, body = CALLS["post"][-1]
    assert "section/episodes/add" in url and "csrf=j" in url, url
    assert body["sectionId"] == 222, body
    ep = body["episodes"][0]
    assert ep["aid"] == 9001 and ep["cid"] == 7001 and ep["title"].startswith("李大霄"), ep


def test_pending_roundtrip(fake: FakeRequests, tmp: Path) -> None:
    bu.APP_DIR = tmp
    fake.fail_add = True
    try:
        bu.add_video_to_season("BV1PEND", 111, {"sessdata": "s", "bili_jct": "j"},
                               title="待加合集")
    except bu.BiliError as e:
        bu.remember_pending_season("BV1PEND", {"id": 111, "title": "直播回放",
                                               "section_id": 222}, "待加合集", str(e))
    else:
        raise AssertionError("预期加合集失败")
    pending = bu.load_pending_seasons()
    assert len(pending) == 1 and pending[0]["bvid"] == "BV1PEND", pending

    fake.fail_add = False
    n = bu.retry_pending_seasons({"sessdata": "s", "bili_jct": "j"}, log=lambda *a: None)
    assert n == 1, n
    assert bu.load_pending_seasons() == [], "补加成功后应该从清单里划掉"


def test_upload_assigns_season(fake: FakeRequests, tmp: Path) -> None:
    """建稿成功后自动加合集；加失败也不影响投稿结果。"""
    from PIL import Image

    media = tmp / "rec"
    media.mkdir(exist_ok=True)
    f = media / "a.ts"
    f.write_bytes(b"G" * (2 * 1024 * 1024))

    async def _fake_upload(paths, page_titles, **kw):
        return {"bvid": "BV1NEW", "aid": 8001}

    bu.APP_DIR = tmp
    bu._do_upload = _fake_upload
    bu._pick_client = lambda: asyncio.sleep(0, result="Fake")
    bu.probe_duration = lambda *a, **k: 600.0
    bu.read_credential = lambda p: {"sessdata": "s", "bili_jct": "j"}
    bu._credential_object = lambda d: type("C", (), {"sessdata": "s", "bili_jct": "j",
                                                     "buvid3": "", "dedeuserid": ""})()

    def _frame(video, out, at=0.0, ffmpeg="ffmpeg"):
        Image.new("RGB", (8, 8), (30, 30, 30)).save(out, "JPEG")
        return True

    bu.grab_frame = _frame

    season = {"id": 111, "title": "直播回放", "section_id": 222}
    fake.fail_add = False
    info = bu.upload_recording([f], anchor="主播", cfg=bu.BiliConfig(min_size_mb=0),
                               room_id="1001", when=datetime(2026, 10, 30, 20, 0),
                               season=season, log=lambda *a: None)
    assert info and info["bvid"] == "BV1NEW", info
    assert CALLS["post"] and CALLS["post"][-1][1]["sectionId"] == 222, CALLS["post"][-1:]

    # 加合集失败：投稿仍然算成功，只是进了待加清单
    fake.fail_add = True
    f2 = media / "b.ts"
    f2.write_bytes(b"G" * (2 * 1024 * 1024))
    info2 = bu.upload_recording([f2], anchor="主播", cfg=bu.BiliConfig(min_size_mb=0),
                                room_id="1001", when=datetime(2026, 10, 30, 21, 0),
                                season=season, log=lambda *a: None)
    assert info2 and info2["bvid"] == "BV1NEW", info2
    assert len(bu.load_pending_seasons()) == 1, bu.load_pending_seasons()


def main() -> int:
    fake = FakeRequests()
    bu.requests = fake
    tmp = Path(tempfile.mkdtemp(prefix="dylivesave_season_"))

    test_list_and_add(fake)
    test_pending_roundtrip(fake, tmp)
    test_upload_assigns_season(fake, tmp)

    print("OK：合集列表 / 加视频 / 待加清单补加 / 建稿后自动加合集 全部通过")
    print("   临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
