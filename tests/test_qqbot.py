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

    def _bind(kind, oid):
        bound.append((kind, oid))
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
    assert r.startswith("绑定成功") and bound == [("group", "G1")], (r, bound)

    # 私聊绑定 → 记成 user/U1
    bound.clear()
    b._run_command(f"绑定 {good}", "U1", ("user", "U1"))
    assert bound == [("user", "U1")], bound

    # 关掉「允许操作」也能绑定（绑定不算管理操作）
    b2 = make_bot(allow_manage=False)
    seen = []
    b2.on_bind = lambda kind, oid: (seen.append((kind, oid)), "ok")[1]
    b2._run_command(f"绑定 {b2.cfg.bind_code}", "U1", ("group", "G2"))
    assert seen == [("group", "G2")], seen

    # 解绑
    un = []
    b2.on_unbind = lambda kind, oid: (un.append((kind, oid)), "已解绑")[1]
    assert "已解绑" in b2._run_command("解绑", "U1", ("group", "G2"))
    assert un == [("group", "G2")]

    # 没给码 → 只提示用法
    assert "用法" in b2._run_command("绑定", "U1", ("group", "G2"))

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
        r = w._on_bind_ui("group", "GROUP_OPENID_1")
        assert "绑定成功" in r and "GROUP_OPENID_1" in w.ed_groups.text(), r
        r = w._on_bind_ui("user", "USER_OPENID_1")
        assert "绑定成功" in r and "USER_OPENID_1" in w.ed_users.text(), r
        assert w._on_unbind_ui("group", "GROUP_OPENID_1") == "已解绑，不再往这里发通知"
        assert "GROUP_OPENID_1" not in w.ed_groups.text()
    finally:
        w.close()
        if not existed and cfg_file.exists():
            cfg_file.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
