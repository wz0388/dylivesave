# -*- coding: utf-8 -*-
"""QQ 机器人的离线自测（不连网、不需要凭证）。

    python tests/test_qqbot.py

按官方文档的坑逐条验证：
  · 指令解析/前缀/@ 触发；
  · 「允许机器人操作」开关 + 管理员白名单；
  · 回复走被动消息（带 msg_id、msg_seq 递增，@ 用户用 <@openid> 前缀）；
  · 平台重复推同一事件时只处理一次；
  · 通知（主动消息）发到配置的群/私聊。
"""
import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import qqbot as qb   # noqa: E402

CALLED: list[tuple] = []


def handler(cmd, arg, user):
    CALLED.append((cmd, arg, user))
    return f"处理了：{cmd} {arg}"


def make_bot(**kw) -> qb.QQBot:
    cfg = qb.BotConfig(app_id="appid", client_secret="secret",
                       groups=["G1"], users=[], admins=[], allow_manage=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    bot = qb.QQBot(cfg, log=lambda *a: None, handler=handler)
    bot.sent: list[tuple[str, dict]] = []

    async def fake_post(path, body):
        bot.sent.append((path, body))
        return {"code": 0}

    bot._post = fake_post
    return bot


def ev(content: str, *, eid="EV1", mid="MSG1", user="U1", etype="GROUP_AT_MESSAGE_CREATE"):
    author = ({"user_openid": user} if etype == "C2C_MESSAGE_CREATE"
              else {"member_openid": user})
    d = {"id": mid, "content": content, "group_openid": "G1", "author": author}
    return etype, {"id": eid, "t": etype, "d": d}


def run(bot, etype, payload):
    asyncio.run(bot._on_message(etype, payload))


def main() -> int:
    # ---- 1. 帮助
    b = make_bot()
    help_text = b._run_command("帮助", "U1")
    assert "添加" in help_text and "#帮助" in help_text, help_text

    # ---- 2. 面板关掉「允许操作」→ 管理指令被拒
    b = make_bot(allow_manage=False)
    r = b._run_command("添加 123", "U1")
    assert "允许机器人操作" in r, r
    assert CALLED == [], CALLED

    # ---- 3. 白名单：不在名单里的人不能操作，名单里的可以
    CALLED.clear()
    b = make_bot(admins=["U1"])
    assert "没有操作权限" in b._run_command("删除 123", "U2")
    assert b._run_command("添加 123", "U1") == "处理了：添加 123"
    assert CALLED == [("添加", "123", "U1")], CALLED

    # ---- 4. 只读指令不受 allow_manage 影响
    b = make_bot(allow_manage=False)
    assert b._run_command("列表", "U9") == "处理了：列表 "

    # ---- 5. @ 触发的回复：@ 用户 + 被动消息（msg_id + msg_seq）
    CALLED.clear()
    b = make_bot()
    run(b, *ev("#添加 50141210842 原画"))
    assert len(b.sent) == 1, b.sent
    path, body = b.sent[0]
    assert path == "/v2/groups/G1/messages", path
    assert body["content"].startswith("<@U1> "), body
    assert body["msg_id"] == "MSG1" and body["msg_seq"] == 1, body
    assert CALLED == [("添加", "50141210842 原画", "U1")], CALLED

    # ---- 6. 重复推送同一事件 → 只回一次
    run(b, *ev("#添加 50141210842 原画"))
    assert len(b.sent) == 1, f"重复事件被处理了：{b.sent}"

    # ---- 7. 群里没 @ 但用了指令前缀 → 也响应
    CALLED.clear()
    b = make_bot()
    run(b, *ev("#列表", eid="EV2", mid="MSG2"))
    assert len(b.sent) == 1 and CALLED == [("列表", "", "U1")], (b.sent, CALLED)

    # ---- 8. 群里既没 @ 也没前缀 → 不搭理（开了「接收所有消息」也不刷屏）
    b = make_bot()
    run(b, *ev("今天直播真好看", eid="EV3", mid="MSG3", etype="GROUP_MESSAGE_CREATE"))
    assert b.sent == [], b.sent

    # ---- 8b. 但「接收所有消息」里 @ 了机器人 → 要回
    b = make_bot()
    etype, payload = ev("#列表", eid="EV3b", mid="MSG3b", etype="GROUP_MESSAGE_CREATE")
    payload["d"]["mentions"] = [{"is_you": True, "openid": "U1"}]
    run(b, etype, payload)
    assert len(b.sent) == 1, b.sent

    # ---- 9. 单聊：不加 @，路径是 /v2/users/...
    CALLED.clear()
    b = make_bot()
    run(b, *ev("#状态", eid="EV4", mid="MSG4", etype="C2C_MESSAGE_CREATE"))
    path, body = b.sent[0]
    assert path == "/v2/users/U1/messages", path
    assert body["content"] == "处理了：状态 ", body

    # ---- 10. 通知：主动消息（不带 msg_id）发到群里
    b = make_bot()
    asyncio.run(b._broadcast("【开播】李大霄 正在直播"))
    assert b.sent[0][0] == "/v2/groups/G1/messages"
    assert "msg_id" not in b.sent[0][1], b.sent[0][1]

    # ---- 10b. 绑定码：openid 抄不到，靠 #绑定 <码> 让机器人自己记下来
    b = make_bot()
    bound = []

    def _bind(kind, oid, user=""):
        bound.append((kind, oid, user))
        return f"绑定成功（{kind}）"
    b.on_bind = _bind
    assert qb.ensure_bind_code(b.cfg) == b.cfg.bind_code != ""
    assert "#绑定" in b._run_command("帮助", "U1")

    # 码不对 → 拒绝，什么都不记
    assert "绑定码不对" in b._run_command("绑定 0000", "U1", ("group", "G1"))
    assert bound == []

    # 群里绑定 → 记成 group/G1
    good = b.cfg.bind_code
    r = b._run_command(f"绑定 {good}", "U1", ("group", "G1"))
    assert r.startswith("绑定成功") and bound == [("group", "G1", "U1")], (r, bound)

    # 私聊绑定 → 记成 user/U1
    bound.clear()
    b._run_command(f"绑定 {good}", "U1", ("user", "U1"))
    assert bound == [("user", "U1", "U1")], bound

    # 关掉「允许操作」也能绑定（绑定不算管理操作）
    b2 = make_bot(allow_manage=False)
    seen = []
    b2.on_bind = lambda kind, oid, user="": (seen.append((kind, oid, user)), "ok")[1]
    b2._run_command(f"绑定 {b2.cfg.bind_code}", "U1", ("group", "G2"))
    assert seen == [("group", "G2", "U1")], seen

    # 解绑
    un = []
    b2.on_unbind = lambda kind, oid: (un.append((kind, oid)), "已解绑")[1]
    assert "已解绑" in b2._run_command("解绑", "U1", ("group", "G2"))
    assert un == [("group", "G2")]

    # 没给码 → 只提示用法
    assert "用法" in b2._run_command("绑定", "U1", ("group", "G2"))

    # ---- 10c. master 与授权：绑定的人天然是管理员，能 @ 别人授权
    b3 = make_bot()
    b3.cfg.masters = ["BOSS"]
    b3.cfg.admins = []
    CALLED.clear()
    assert "没有操作权限" in b3._run_command("添加 123", "U2")
    assert b3._run_command("添加 123", "BOSS") == "处理了：添加 123"

    grants = []
    b3.on_grant = lambda oid, add: (grants.append((oid, add)), "ok")[1]
    # 非 master 授权被拒
    assert "只有绑定" in b3._run_command("授权", "U2", ("group", "G1"),
                                        [{"openid": "U3"}])
    # master @ 别人 → 授权成功
    assert b3._run_command("授权", "BOSS", ("group", "G1"),
                           [{"openid": "U3"}]) == "ok"
    assert grants == [("U3", True)], grants
    # 被授权的人也能管主播
    b3.cfg.admins.append("U3")
    CALLED.clear()
    assert b3._run_command("删除 123", "U3") == "处理了：删除 123"
    # 取消授权
    b3._run_command("取消授权", "BOSS", ("group", "G1"), [{"openid": "U3"}])
    assert grants[-1] == ("U3", False)
    # 既没 @ 也没 openid
    assert "要 @ 一个人" in b3._run_command("授权", "BOSS")

    # ---- 10d. 回复要「引用」对方那条消息
    b4 = make_bot()
    etype, payload = ev("#列表", eid="EV9", mid="MSG9")
    payload["d"]["message_scene"] = {"ext": ["msg_idx=REFIDX_ABC=="]}
    run(b4, etype, payload)
    body = b4.sent[0][1]
    assert body.get("message_reference", {}).get("message_id") == "REFIDX_ABC==", body
    assert body.get("msg_id") == "MSG9", body

    # ---- 10e. 收到消息要能记进日志（界面上看得到所有消息）
    logs = []
    b5 = qb.QQBot(qb.BotConfig(app_id="a", client_secret="s", groups=["G1"]),
                  log=logs.append, handler=handler)
    b5._post = make_bot()._post  # 复用假 post
    b5.sent = []

    async def _fp(path, body):
        b5.sent.append((path, body))
        return {"code": 0}
    b5._post = _fp
    etype, payload = ev("#列表", eid="EVL", mid="MSGL")
    payload["d"]["author"] = {"member_openid": "U9", "nickname": "张三"}
    run(b5, etype, payload)
    assert any("张三" in x and "#列表" in x and "群聊" in x for x in logs), logs

    # ---- 10f. #截图：先传富媒体（file_type=1），再用 msg_type=7 发图
    import base64 as _b64
    from pathlib import Path as _P
    img = _P(tempfile.mkdtemp()) / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    calls = []

    def _mk_bot(master="U1"):
        b = qb.QQBot(qb.BotConfig(app_id="a", client_secret="s", groups=["G1"]),
                     log=lambda *a: None, handler=handler,
                     on_screenshot=lambda i: (str(img), f"#{i} 李大霄"))
        b.cfg.allow_manage = True
        b.cfg.masters = [master]

        async def _post(path, body):
            calls.append((path, dict(body)))
            return {"file_info": "FI123", "ttl": 300} if path.endswith("/files") \
                else {"id": "MSG1"}
        b._post = _post

        async def _reply(target, content, d, seq=1):
            calls.append(("reply", content))
        b._reply = _reply
        return b

    b6 = _mk_bot()
    asyncio.run(b6._handle_screenshot("截图 1", ("group", "G1"), {"id": "M1"}, "", "U1"))
    posts = [c for c in calls if c[0] != "reply"]
    assert posts[0][0] == "/v2/groups/G1/files", posts
    assert posts[0][1]["file_type"] == 1 and posts[0][1]["srv_send_msg"] is False
    assert _b64.b64decode(posts[0][1]["file_data"]) == img.read_bytes()
    assert posts[1][0] == "/v2/groups/G1/messages", posts
    assert posts[1][1]["msg_type"] == 7 and posts[1][1]["media"]["file_info"] == "FI123"
    assert posts[1][1]["msg_id"] == "M1" and posts[1][1]["msg_seq"] == 3

    # 没权限 / 没序号 / 序号越界
    calls.clear()
    b7 = _mk_bot(master="BOSS")
    asyncio.run(b7._handle_screenshot("截图 1", ("group", "G1"), {"id": "M1"}, "", "U9"))
    assert any(c[0] == "reply" and "没有权限" in c[1] for c in calls), calls
    calls.clear()
    asyncio.run(b6._handle_screenshot("截图", ("group", "G1"), {"id": "M1"}, "", "U1"))
    assert any(c[0] == "reply" and "用法" in c[1] for c in calls), calls


    # ---- 11. 界面侧：机器人指令真的能加/删主播
    test_gui()

    print("OK：指令解析/权限/@与前缀触发/被动回复/去重/主动通知/绑定码/界面增删主播 全部通过")
    return 0


def test_gui() -> None:
    """把机器人指令接到真实窗口上跑一遍（不联网、不录制）。"""
    import tempfile
    from PySide6.QtWidgets import QApplication
    import record_gui as g

    app = QApplication.instance() or QApplication([])
    g.SETTINGS = Path(tempfile.mkdtemp()) / "record_gui.json"
    # 绑定会把 openid 写进 APP_DIR/qqbot.json —— 测试跑完清掉，别污染真实配置
    cfg_file = qb.config_path(g.APP_DIR)
    existed = cfg_file.exists()
    w = g.MainWindow()
    try:
        assert w.bot is None, "默认没启用机器人"

        r = w._bot_command_ui("添加", "50141210842 原画", "u1")
        assert "已添加" in r and "原画" in r, r
        assert len(w.rows) == 2 and w.rows[1].room.text() == "50141210842"

        r = w._bot_command_ui("添加", "50141210842", "u1")
        assert "已经在列表里" in r, r

        lst = w._bot_command_ui("列表", "", "u1")
        assert "50141210842" in lst and "#2" in lst, lst

        r = w._bot_command_ui("删除", "50141210842", "u1")
        assert "已删除" in r, r
        assert all(r_.room.text() != "50141210842" for r_ in w.rows)

        assert "没找到" in w._bot_command_ui("删除", "999", "u1")
        assert "个主播" in w._bot_command_ui("状态", "", "u1")

        # 绑定码：面板上有码，收到 #绑定 后 openid 自动填进配置
        code = w.ed_code.text()
        assert len(code) == 36 and code.count("-") == 4, code
        r = w._on_bind_ui("group", "GROUP_OPENID_1", "MASTER_1")
        assert "绑定成功" in r and "GROUP_OPENID_1" in w.ed_groups.text(), r
        assert w.bot_cfg.masters == ["MASTER_1"], w.bot_cfg.masters
        r = w._on_bind_ui("user", "USER_OPENID_1")
        assert "绑定成功" in r and "USER_OPENID_1" in w.ed_users.text(), r
        assert w._on_unbind_ui("group", "GROUP_OPENID_1") == "已解绑，不再往这里发通知"
        assert "GROUP_OPENID_1" not in w.ed_groups.text()

        # #状态 要能看到这一路的具体进度（已写/已录），不只一个「录制中」
        row0 = w.rows[0]
        row0.room.setText("50141210842")
        row0.rid = row0.tag = "50141210842"
        row0.anchor = "李大霄"
        row0.running = True
        row0.mode = "recording"
        row0.set_progress("已写 3.20 GB · 已录 01:23:45")
        txt = w._bot_command_ui("状态", "", "u1")
        assert "录制中" in txt and "已写 3.20 GB" in txt, txt
        assert "01:23:45" in txt and "李大霄" in txt, txt
        row0.running = False
        row0.set_progress("")

        # 界面截图：切到主播页并落盘一个 PNG
        shot_path, shot_title = w._screenshot_ui(1)
        if len(w.rows) >= 1:
            assert shot_path and Path(shot_path).is_file(), (shot_path, shot_title)
            assert shot_path.endswith(".png")
        else:
            assert shot_path == "" and "只有" in shot_title

        # 通知要带时间戳（年月日 时分秒）
        class _FakeBot:
            def __init__(self):
                self.msgs = []

            def notify(self, t):
                self.msgs.append(t)

        fake = _FakeBot()
        w.bot = fake
        w._bot_notify("【开播】李大霄")
        msg = fake.msgs[0]
        # 形如「[11-01 10:45:41] 【开播】李大霄」：前缀能被 strptime 解析才算对
        assert msg.endswith("【开播】李大霄"), msg
        stamp = msg.split("] ")[0].lstrip("[")
        datetime.strptime(stamp, "%m-%d %H:%M:%S")     # 格式不对会抛异常
        w.bot = None
    finally:
        w.close()
        if not existed and cfg_file.exists():
            cfg_file.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
