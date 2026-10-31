# -*- coding: utf-8 -*-
"""
QQ 官方机器人（API v2）· 通知 + 管理
------------------------------------------------------------------
给录制工具加一个 QQ 机器人：

  · **通知**：开播 / 下播 / 投稿完成 / 投稿失败，推到指定的群或私聊；
  · **管理**：在 QQ 里发指令就能添加/删除主播、看列表和状态 ——
    前提是面板里打开了「允许机器人操作」，且发指令的人在白名单里。

协议按官方 v2 文档（https://bot.q.qq.com/wiki/develop/api-v2/）手写，不依赖 SDK：
  取凭证  POST https://api.bot.qq.com/app/getAppAccessToken
  OpenAPI  https://api.bot.qq.com        鉴权头 Authorization: QQBot {token}
  网关    GET /gateway → wss://api.bot.qq.com/websocket/
  发群消息 POST /v2/groups/{group_openid}/messages
  发单聊   POST /v2/users/{user_openid}/messages
  intents  GROUP_AND_C2C_EVENT = 1<<25

⚠️ 失败时 HTTP 也可能是 200，必须看响应体里的 code（0 才是成功）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import requests

API_BASE = "https://api.bot.qq.com"
TOKEN_URL = f"{API_BASE}/app/getAppAccessToken"
GATEWAY_URL = f"{API_BASE}/gateway"
INTENTS_GROUP_AND_C2C = 1 << 25          # 群聊 + 单聊事件
CONFIG_NAME = "qqbot.json"

#: 关闭码：这些可以 Resume，那些必须重新 Identify，剩下的是致命错误
_RESUME_CODES = {4009, 4008}
_REIDENTIFY_CODES = {4006, 4007}
_FATAL_CODES = {4013, 4014, 4914, 4915}


@dataclass
class BotConfig:
    enabled: bool = False
    app_id: str = ""
    client_secret: str = ""
    sandbox: bool = False
    #: 通知发到哪里（openid，不是 QQ 号；先让机器人在群里收一条消息，从日志里抄）
    groups: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    #: 面板上的「允许机器人操作」开关 + 谁能操作（留空 = 不限制）
    allow_manage: bool = True
    admins: list[str] = field(default_factory=list)
    notify_live_on: bool = True
    notify_live_off: bool = True
    notify_upload_ok: bool = True
    notify_upload_fail: bool = True
    prefix: str = "#"


def config_path(app_dir: Path) -> Path:
    return Path(app_dir) / CONFIG_NAME


def load_config(app_dir: Path) -> BotConfig:
    p = config_path(app_dir)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return BotConfig()
    cfg = BotConfig()
    for k, v in (d or {}).items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    cfg.groups = [str(x).strip() for x in (cfg.groups or []) if str(x).strip()]
    cfg.users = [str(x).strip() for x in (cfg.users or []) if str(x).strip()]
    cfg.admins = [str(x).strip() for x in (cfg.admins or []) if str(x).strip()]
    return cfg


def save_config(app_dir: Path, cfg: BotConfig) -> None:
    p = config_path(app_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


class QQBot:
    """一条 WebSocket 长连接 + 一组命令。

    handler(cmd, arg, user_openid) → 回复文本：由界面那边实现（添加/删除主播…），
    本类只负责「收事件 → 解析指令 → 要一句话 → 回给对方」。
    """

    def __init__(self, cfg: BotConfig, *, log: Callable[[str], None] = print,
                 handler: Callable[[str, str, str], str] | None = None,
                 on_status: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.log = log
        self.handler = handler
        self.on_status = on_status or (lambda s: None)
        self.status = "stopped"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._token = ""
        self._token_exp = 0.0
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._seq = 0
        self._ws = None
        self._session = None
        self._ack = True
        self._session_id = ""
        self._last_s = None
        self.stats = {"events": 0, "commands": 0, "sent": 0, "errors": 0}

    # ─────────────────── 生命周期 ───────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="qqbot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            except Exception:
                pass
        self._set_status("stopped")

    async def _shutdown(self) -> None:
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass

    def _set_status(self, s: str) -> None:
        self.status = s
        try:
            self.on_status(s)
        except Exception:
            pass

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            self.log(f"[!] 机器人主循环退出：{type(e).__name__}: {e}")
            self._set_status(f"已停止（{type(e).__name__}）")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    # ─────────────────── 主流程 ───────────────────
    async def _main(self) -> None:
        import aiohttp

        if not self.cfg.app_id or not self.cfg.client_secret:
            self.log("[!] 没填 AppID / AppSecret，机器人不会连")
            self._set_status("缺少凭证")
            return

        backoff = 3
        async with aiohttp.ClientSession() as session:
            self._session = session
            while not self._stop.is_set():
                try:
                    await self._ensure_token()
                    await self._connect_once()
                    backoff = 3
                except _Fatal as e:
                    self.log(f"[x] 致命错误，停止重连：{e}")
                    self._set_status(f"不可用：{e}")
                    return
                except Exception as e:
                    self.stats["errors"] += 1
                    self.log(f"[!] 连接出错：{type(e).__name__}: {e}")
                    self._set_status(f"断线，{backoff}s 后重连")
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
        self._set_status("stopped")

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token

        def _get():
            r = requests.post(TOKEN_URL, json={"appId": self.cfg.app_id,
                                               "clientSecret": self.cfg.client_secret},
                              timeout=20)
            return r.json()

        data = await asyncio.get_running_loop().run_in_executor(None, _get)
        # ⚠️ HTTP 200 也可能是失败，必须看 code
        if not data.get("access_token"):
            raise _Fatal(f"取 token 失败：{data}")
        self._token = data["access_token"]
        expires = int(data.get("expires_in") or 7200)
        self._token_exp = time.time() + max(60, expires - 120)
        self.log(f"[i] 已获取 access_token（{expires}s）")
        return self._token

    async def _connect_once(self) -> None:
        import aiohttp

        headers = {"Authorization": f"QQBot {self._token}"}
        async with self._session.get(GATEWAY_URL, headers=headers, timeout=20) as r:
            data = await r.json()
        url = (data or {}).get("url")
        if not url:
            raise RuntimeError(f"取网关地址失败：{data}")

        self._set_status("连接中")
        async with self._session.ws_connect(url, heartbeat=None, timeout=30) as ws:
            self._ws = ws
            identify_timer = None
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    op = payload.get("op")
                    if op == 10:                       # Hello
                        interval = float(payload["d"].get("heartbeat_interval") or 30000) / 1000
                        await self._identify(ws)
                        identify_timer = asyncio.create_task(self._heartbeat(ws, interval))
                    elif op == 0:
                        await self._dispatch(payload)
                    elif op == 11:
                        self._ack = True
                    elif op == 7:                      # 要求重连
                        self.log("[i] 网关要求重连")
                        return
                    elif op == 9:                      # Invalid Session
                        self.log("[!] session 失效，重新 Identify")
                        self._session_id = ""
                        self._last_s = None
                        return
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            if identify_timer:
                identify_timer.cancel()

    async def _identify(self, ws) -> None:
        if self._session_id and self._last_s is not None:
            await ws.send_json({"op": 6, "d": {"token": f"QQBot {self._token}",
                                               "session_id": self._session_id,
                                               "seq": self._last_s}})
            return
        await ws.send_json({"op": 2, "d": {
            "token": f"QQBot {self._token}",
            "intents": INTENTS_GROUP_AND_C2C,
            "shard": [0, 1],
            "properties": {"$os": "Windows", "$browser": "recorder", "$device": "pc"}}})

    async def _heartbeat(self, ws, interval: float) -> None:
        self._ack = True
        while True:
            await asyncio.sleep(interval)
            if not self._ack:
                self.log("[!] 心跳没收到 ACK，断开重连")
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            self._ack = False
            try:
                await ws.send_json({"op": 1, "d": self._last_s})
            except Exception:
                return

    # ─────────────────── 事件 ───────────────────
    async def _dispatch(self, payload: dict) -> None:
        t = payload.get("t")
        self._last_s = payload.get("s", self._last_s)
        d = payload.get("d") or {}
        if t == "READY":
            self._session_id = d.get("session_id") or ""
            bot = (d.get("user") or {}).get("username") or "机器人"
            self.log(f"[√] 机器人已上线：{bot}（session={self._session_id[:8]}…）")
            self._set_status("已连接")
            return
        if t == "RESUMED":
            self.log("[√] 会话已恢复")
            self._set_status("已连接")
            return
        if t in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            await self._on_message(t, payload)

    async def _on_message(self, etype: str, payload: dict) -> None:
        d = payload.get("d") or {}
        eid = payload.get("id") or d.get("id") or ""
        if eid and self._dedup(eid):
            return
        self.stats["events"] += 1

        content = (d.get("content") or "").strip()
        author = d.get("author") or {}
        if etype == "C2C_MESSAGE_CREATE":
            target, user = ("user", author.get("user_openid") or ""), author.get("user_openid") or ""
            at = ""
        else:
            target = ("group", d.get("group_openid") or "")
            user = author.get("member_openid") or ""
            at = f"<@{user}> " if user else ""

        # 群里开了「接收所有消息」时每句都会推过来 —— 只认 @我 或 指令前缀，
        # 否则机器人会把群聊每句话都接一遍，变成刷屏机。
        pref = self.cfg.prefix or "#"
        if etype == "GROUP_MESSAGE_CREATE":
            at_me = any(m.get("is_you") for m in (d.get("mentions") or []))
            if not (at_me or content.startswith(pref)):
                return

        text = content.strip()
        for t in (pref, "/", "／"):
            if text.startswith(t):
                text = text[len(t):].strip()
                break

        if not text:
            await self._reply(target, at + "发「" + pref + "帮助」看看能做什么", d)
            return

        reply = self._run_command(text, user)
        self.stats["commands"] += 1
        await self._reply(target, at + reply, d)

    def _dedup(self, eid: str) -> bool:
        """平台可能重复推同一事件 —— 按最外层 id 去重（带上限，别无限涨）。"""
        now = time.time()
        if eid in self._seen:
            return True
        self._seen[eid] = now
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return False

    def _run_command(self, text: str, user: str) -> str:
        """解析指令 → 权限检查 → handler。返回给用户的一句话。"""
        parts = text.split()
        cmd = parts[0]
        arg = " ".join(parts[1:]).strip()
        aliases = {"add": "添加", "del": "删除", "remove": "删除", "ls": "列表",
                   "list": "列表", "status": "状态", "help": "帮助", "?": "帮助"}
        cmd = aliases.get(cmd.lower(), cmd)

        if cmd in ("帮助", "help"):
            p = self.cfg.prefix or "#"
            return ("可用指令：\n"
                    f"{p}列表 —— 看现在在录哪些主播\n"
                    f"{p}状态 —— 录制/上传概况\n"
                    f"{p}添加 房间号 [清晰度] —— 添加主播\n"
                    f"{p}删除 房间号 —— 删除主播\n"
                    f"{p}帮助 —— 就是这条")

        if cmd in ("列表", "状态"):
            if self.handler is None:
                return "界面没接上，暂时查不了"
            return self.handler(cmd, arg, user)

        if cmd in ("添加", "删除", "开始", "停止"):
            if not self.cfg.allow_manage:
                return "管理员没在面板里打开「允许机器人操作」，现在只能看不能改"
            if self.cfg.admins and user not in self.cfg.admins:
                return "你没有操作权限（要加白名单，请在面板的机器人页里加你的 openid）"
            if self.handler is None:
                return "界面没接上，暂时改不了"
            return self.handler(cmd, arg, user)

        return f"不认识的指令：{cmd}（发「{self.cfg.prefix or '#'}帮助」看看）"

    # ─────────────────── 发消息 ───────────────────
    @staticmethod
    def _target_path(target) -> str:
        kind, oid = target
        return f"/v2/{'groups' if kind == 'group' else 'users'}/{oid}/messages"

    async def _reply(self, target, content: str, d: dict, seq: int = 1) -> None:
        """被动回复：带 msg_id，可以在 5 分钟里用不同 msg_seq 连回几次。"""
        body = {"content": content, "msg_type": 0}
        msg_id = d.get("id")
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        await self._post(self._target_path(target), body)

    async def _post(self, path: str, body: dict) -> dict:
        if self._session is None:
            return {}
        try:
            await self._ensure_token()
            headers = {"Authorization": f"QQBot {self._token}",
                       "X-Union-Appid": self.cfg.app_id}
            async with self._session.post(API_BASE + path, json=body,
                                          headers=headers, timeout=20) as r:
                data = await r.json()
            if data.get("code") not in (0, None):
                self.log(f"[!] 发消息失败：{data.get('code')} {data.get('message')}")
                self.stats["errors"] += 1
            else:
                self.stats["sent"] += 1
            return data
        except Exception as e:
            self.stats["errors"] += 1
            self.log(f"[!] 发消息出错：{type(e).__name__}: {e}")
            return {}

    async def _broadcast(self, content: str) -> None:
        for gid in self.cfg.groups:
            await self._post(self._target_path(("group", gid)),
                             {"content": content, "msg_type": 0})
        for uid in self.cfg.users:
            await self._post(self._target_path(("user", uid)),
                             {"content": content, "msg_type": 0})

    def notify(self, text: str) -> None:
        """给界面/录制线程用的：把一条通知推出去（线程安全，不阻塞）。"""
        loop = self._loop
        if loop is None or not (self.cfg.groups or self.cfg.users):
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(text), loop)
        except Exception as e:
            self.log(f"[!] 通知投递失败：{e}")


class _Fatal(Exception):
    """重连也没用的错误（凭证错、被封、intents 没权限）。"""
