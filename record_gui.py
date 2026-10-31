# -*- coding: utf-8 -*-
"""
抖音直播录制 → B 站自动投稿 · 图形界面（CustomTkinter）
------------------------------------------------------------------
双击 exe 或 `python record_gui.py` 打开。界面只负责收集参数和显示日志，
真正的录制/投稿逻辑全部复用 record.run_recording()，和命令行是同一份代码。

界面用 CustomTkinter（圆角卡片 / 扁平按钮 / 深浅色），配色统一走 C() 调色板，
不要在各处写死颜色 —— 主题一切换所有控件都要跟着走。

要点：
  · **多个主播可以同时录**：每个主播一个后台线程，互不影响；
    日志前面带 `[房间号]` 前缀，分得清是谁的。
  · **分段一录完就上传**：交给后台上传队列，录制线程立刻接着录下一段。
  · 未开播时按设定每 30 秒重试一次拉流（默认一直等，直到开播或点停止）。
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk


# 打包成 exe 后，工作目录可能是任意位置；配置和日志按 exe / 脚本所在目录找
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import record as rec                                    # noqa: E402
import bili_uploader as bu                              # noqa: E402

#: 界面设置（每个主播行、目录、主题…）
SETTINGS = APP_DIR / "record_gui.json"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

QUALITY_CHOICES = ["FULL_HD1", "ORIGIN", "HD1", "SD1", "SD2", "LD", "auto"]
QUALITY_LABEL = {"FULL_HD1": "原画/高清（推荐）", "ORIGIN": "原画", "HD1": "高清",
                 "SD1": "标清", "SD2": "较低", "LD": "流畅", "auto": "自动挑最好的"}
SEGMENT_CHOICES = [("30 分钟", 1800), ("60 分钟", 3600),
                   ("120 分钟（推荐）", 7200), ("不分段", 0)]
#: 默认分段时间（2 小时，和命令行 --segment 7200 一致）
SEGMENT_DEFAULT = SEGMENT_CHOICES[2][0]
#: 老版本界面存下来的标签 → 秒数（换过选项文案，读旧设置时要翻译一下）
_SEGMENT_LEGACY = {"1 小时": 3600, "2 小时（推荐）": 7200, "3 小时": 10800,
                   "6 小时": 21600}
DURATION_CHOICES = [("不限（一直录到主播下播）", 0), ("1 小时", 3600),
                    ("2 小时", 7200), ("4 小时", 14400), ("6 小时", 21600),
                    ("8 小时", 28800), ("12 小时", 43200)]


def _quality_label(saved: str) -> str:
    """存下来的清晰度 → 下拉框里那行字。

    老版本存的是 "FULL_HD1  原画/高清（推荐）"（代号 + 文案），
    新界面只显示文案，读旧设置时要翻译一下，否则下拉框会是空白。
    """
    t = (saved or "").strip()
    if t in QUALITY_LABEL.values():
        return t
    for q in QUALITY_CHOICES:
        if t == q or t.startswith(q):
            return QUALITY_LABEL[q]
    return QUALITY_LABEL[QUALITY_CHOICES[0]]


def _segment_label(saved: str = "") -> str:
    """分段时间下拉框的文案。老的 "2 小时（推荐）" 按秒数对到现在的选项上。

    选项改过（以前是 1/2/3/6 小时），老配置里的 "2 小时（推荐）" 现在不在列表里，
    直接塞进只读下拉框会显示不出东西 —— 这里按秒数对回去，对不上就用默认值。
    """
    labels = [s[0] for s in SEGMENT_CHOICES]
    if saved in labels:
        return saved
    sec = _SEGMENT_LEGACY.get(saved or "")
    for label, s in SEGMENT_CHOICES:
        if s == sec:
            return label
    return SEGMENT_DEFAULT


# ─────────────────── 外观 ───────────────────
#: 深色 / 浅色。CTk 的颜色都是 (浅色, 深色) 元组，切换外观时控件自己会跟着变；
#: 只有 Text 的 tag 颜色、进度条这类要手动重刷，用 C1() 取当前模式下的具体值。
DARK = False

_PALETTE = {
    "card": ("#ffffff", "#1e1e22"),
    "card2": ("#f7f8fa", "#232329"),
    "muted": ("#6b7280", "#9aa0a6"),
    "ok": ("#15803d", "#5cd39a"),
    "warn": ("#b45309", "#fbbf24"),
    "err": ("#b91c1c", "#f87171"),
    "sub": ("#e8eaed", "#2a2a30"),
    "sub_hover": ("#dfe2e6", "#33333a"),
    "sub_text": ("#111827", "#e6e6e6"),
    "log_bg": ("#fafafa", "#17171b"),
    "log_fg": ("#374151", "#e6e6e6"),
    "border": ("#dcdfe4", "#33333a"),
}


def C(key: str):
    """当前调色板里的颜色（元组 → 自动跟随深浅色）。"""
    return _PALETTE.get(key, ("#000000", "#ffffff"))


def C1(key: str) -> str:
    """当前模式下的具体颜色（给 tag / 画布这种不吃元组的地方用）。"""
    light, dark = C(key)
    return dark if DARK else light


def open_in_explorer(path: Path) -> None:
    """用系统的默认方式打开文件/目录"""
    try:
        if os.name == "nt":
            os.startfile(str(path))          # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showwarning("打不开", f"{path}\n{e}")


def file_log(text: str) -> None:
    """同时往 exe 同目录的 logs/gui.log 写一份。

    --windowed 打包后没有控制台，界面起不来或者报错时看不到任何东西，
    有这份文件用户就能直接把它发过来。
    """
    try:
        p = APP_DIR / "logs" / "gui.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


class RecorderApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.uploader: rec.UploadWorker | None = None
        self.recording = False
        self.rows: list[dict] = []
        self.up_rows: dict[str, dict] = {}     # 上传页的卡片行
        self._nrun = 0
        self._up_pct = 0
        self._up_chunks = ""
        self._mon: threading.Thread | None = None
        self._qr_img = None                    # 防止二维码图片被 GC
        self._login_win = None                 # 登录弹窗 / 里面的控件（主线程才碰）
        self._qr_lbl = None
        self._qr_hint = None
        self.dark = False                      # 深色（设置里存，重启还在）

        root.title("抖音直播录制 → B 站投稿")
        root.minsize(940, 700)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = 1020, min(860, max(700, sh - 150))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 40)}")
        root.configure(fg_color=("#f4f5f7", "#141416"))
        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode("light")

        self._build_ui()
        self._load_settings()
        self._apply_theme(self.dark)           # 按设置里的深浅色重上一遍
        self._sync_state()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self._drain_queue)
        file_log(f"界面启动 pid={os.getpid()} frozen={getattr(sys, 'frozen', False)} "
                 f"dir={APP_DIR} 主题={'dark' if DARK else 'light'} ui=ctk")

    # ─────────────────── 界面 ───────────────────
    def _card(self, parent, title: str):
        """圆角卡片 + 一行小标题，返回放内容的容器。"""
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=C("card"))
        card.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(card, text=title, font=(FONT, 11, "bold"),
                     text_color=C("muted"), anchor="w").pack(
            fill="x", padx=14, pady=(8, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=10, pady=(0, 8))
        return body

    def _sub_btn(self, parent, text, command=None, width=88, height=30):
        """次要按钮（灰底）—— 主操作用默认的蓝色强调按钮。"""
        return ctk.CTkButton(parent, text=text, command=command, width=width,
                             height=height, corner_radius=8, font=(FONT, 11),
                             fg_color=C("sub"), hover_color=C("sub_hover"),
                             text_color=C("sub_text"))

    def _build_ui(self) -> None:
        # ---- 顶部标题
        head = ctk.CTkFrame(self.root, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 2))
        ctk.CTkLabel(head, text="抖音直播录制", font=(FONT, 19, "bold"),
                     anchor="w").pack(side="left")
        ctk.CTkLabel(head, text="  →  B 站自动投稿", font=(FONT, 13),
                     text_color=C("muted")).pack(side="left")
        self.btn_theme = self._sub_btn(head, "深色", self._toggle_theme, width=64)
        self.btn_theme.pack(side="right")

        # ---- 主播列表
        rooms = self._card(self.root, "主播 · 可以多个同时录制")
        self.rows_frame = ctk.CTkFrame(rooms, fg_color="transparent")
        self.rows_frame.pack(fill="x")
        btn_row = ctk.CTkFrame(rooms, fg_color="transparent")
        btn_row.pack(fill="x", pady=(6, 0))
        self.btn_add = self._sub_btn(btn_row, "+ 添加主播", self.add_row, width=108)
        self.btn_add.pack(side="left")

        # ---- 公共设置
        top = self._card(self.root, "公共设置")
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)

        ctk.CTkLabel(top, text="分段时间（默认）", font=(FONT, 12),
                     text_color=C("muted"), anchor="e", width=104).grid(
            row=0, column=0, sticky="e", padx=(4, 6), pady=4)
        self.var_segment = tk.StringVar(value=SEGMENT_DEFAULT)
        ctk.CTkOptionMenu(top, variable=self.var_segment, values=[s[0] for s in SEGMENT_CHOICES],
                          height=32, corner_radius=8, font=(FONT, 12),
                          fg_color=C("sub"), button_color=C("sub_hover"),
                          text_color=C("sub_text")).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)

        ctk.CTkLabel(top, text="录制时长", font=(FONT, 12), text_color=C("muted"),
                     anchor="e", width=72).grid(row=0, column=2, sticky="e",
                                                padx=(10, 6), pady=4)
        self.var_duration = tk.StringVar(value=DURATION_CHOICES[0][0])
        ctk.CTkOptionMenu(top, variable=self.var_duration,
                          values=[d[0] for d in DURATION_CHOICES],
                          height=32, corner_radius=8, font=(FONT, 12),
                          fg_color=C("sub"), button_color=C("sub_hover"),
                          text_color=C("sub_text")).grid(
            row=0, column=3, sticky="ew", padx=6, pady=4)

        ctk.CTkLabel(top, text="输出目录", font=(FONT, 12), text_color=C("muted"),
                     anchor="e", width=104).grid(row=1, column=0, sticky="e",
                                                 padx=(4, 6), pady=4)
        self.var_dir = tk.StringVar(value=str(rec.DEFAULT_DIR))
        f = ctk.CTkFrame(top, fg_color="transparent")
        f.grid(row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=4)
        f.columnconfigure(0, weight=1)
        ctk.CTkEntry(f, textvariable=self.var_dir, height=32, corner_radius=8,
                     font=(FONT, 12)).grid(row=0, column=0, sticky="ew")
        self._sub_btn(f, "浏览", self.pick_dir, width=64, height=32).grid(
            row=0, column=1, padx=(6, 0))

        # ---- 未开播时的行为
        wait = self._card(self.root, "主播没开播时")
        self.var_wait_mode = tk.StringVar(value="forever")
        r1 = ctk.CTkFrame(wait, fg_color="transparent")
        r1.pack(fill="x")
        ctk.CTkRadioButton(r1, text="一直等（每 30 秒重试一次拉流）",
                           variable=self.var_wait_mode, value="forever",
                           font=(FONT, 12), radiobutton_width=16,
                           radiobutton_height=16).pack(side="left", padx=(4, 14))
        ctk.CTkRadioButton(r1, text="最多等", variable=self.var_wait_mode,
                           value="limit", font=(FONT, 12), radiobutton_width=16,
                           radiobutton_height=16).pack(side="left")
        self.var_wait_min = tk.StringVar(value="30")
        ctk.CTkEntry(r1, textvariable=self.var_wait_min, width=56, height=28,
                     corner_radius=8, font=(FONT, 12)).pack(side="left", padx=6)
        ctk.CTkLabel(r1, text="分钟", font=(FONT, 12), text_color=C("muted")).pack(
            side="left", padx=(0, 16))
        ctk.CTkRadioButton(r1, text="没开播就直接结束", variable=self.var_wait_mode,
                           value="none", font=(FONT, 12), radiobutton_width=16,
                           radiobutton_height=16).pack(side="left")
        self.var_monitor = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(r1, text="下播后继续监控（开播自动接着录）",
                        variable=self.var_monitor, font=(FONT, 11),
                        checkbox_width=18, checkbox_height=18,
                        corner_radius=4).pack(side="right")

        # ---- 投稿
        bili = self._card(self.root, "B 站投稿")
        row1 = ctk.CTkFrame(bili, fg_color="transparent")
        row1.pack(fill="x")
        self.var_upload = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(row1, text="分段录完就自动投稿（同一场直播投成一个视频的分P）",
                        variable=self.var_upload, font=(FONT, 12),
                        checkbox_width=18, checkbox_height=18,
                        corner_radius=4).pack(side="left", pady=2)
        self.lbl_login = ctk.CTkLabel(row1, text="登录状态：未检查", font=(FONT, 12),
                                      text_color=C("muted"))
        self.lbl_login.pack(side="left", padx=14)
        ctk.CTkButton(row1, text="B 站扫码登录", command=self.open_login, width=112,
                      height=30, corner_radius=8, font=(FONT, 11)).pack(side="right")
        self._sub_btn(row1, "编辑配置", self.edit_bili_config).pack(side="right", padx=6)
        self._sub_btn(row1, "补传", self.fix_uploads, width=64).pack(side="right")

        row2 = ctk.CTkFrame(bili, fg_color="transparent")
        row2.pack(fill="x", pady=(6, 0))
        # 开关写在 bili.toml 的 [bili].delete_after_upload（GUI 和命令行共用同一份配置）
        self.var_delete = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row2, text="上传成功后删除本地视频（省硬盘，删了找不回来）",
                        variable=self.var_delete, command=self.on_toggle_delete,
                        font=(FONT, 12), checkbox_width=18, checkbox_height=18,
                        corner_radius=4).pack(side="left", pady=2)
        ctk.CTkLabel(row2, text="只删确认上传成功的那几个文件；没传上去的会留着补传",
                     font=(FONT, 11), text_color=C("muted")).pack(side="left", padx=(12, 4))

        # ---- 操作区
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(8, 4))
        self.btn_start = ctk.CTkButton(bar, text="全部开始", command=self.on_start,
                                       width=104, height=36, corner_radius=10,
                                       font=(FONT, 13, "bold"))
        self.btn_start.pack(side="left")
        self.btn_stop = self._sub_btn(bar, "全部停止", self.on_stop, width=104, height=36)
        self.btn_stop.pack(side="left", padx=10)
        self._sub_btn(bar, "打开录制目录",
                      lambda: open_in_explorer(Path(self.var_dir.get())),
                      width=116, height=36).pack(side="left")
        self.lbl_status = ctk.CTkLabel(bar, text="● 就绪", font=(FONT, 12),
                                       text_color=C("muted"))
        self.lbl_status.pack(side="right")

        # ---- 日志 / 上传（分页）
        self.nb = ctk.CTkTabview(self.root, corner_radius=14, fg_color=C("card"),
                                 height=220, anchor="w",
                                 segmented_button_fg_color=C("card2"),
                                 segmented_button_selected_color=("#2563eb", "#2f6f6a"),
                                 segmented_button_selected_hover_color=("#1d4ed8", "#3a8280"),
                                 text_color=C("sub_text"))
        self.nb.pack(fill="both", expand=True, padx=14, pady=(4, 14))
        tab_rec = self.nb.add("录制日志")
        tab_up = self.nb.add("上传")

        # 分页 1：录制日志（多人同时录时，看开头的房间号区分）
        self.txt = ctk.CTkTextbox(tab_rec, corner_radius=10, wrap="none",
                                  font=(MONO, 12), fg_color=C("log_bg"),
                                  text_color=C("log_fg"), border_width=0)
        self.txt.pack(fill="both", expand=True, padx=2, pady=4)
        self.txt.configure(state="disabled")
        for t, k in (("warn", "warn"), ("ok", "ok"), ("muted", "muted")):
            self.txt.tag_config(t, foreground=C1(k))
        self.txt._textbox.configure(insertbackground=C1("log_fg"))

        # 分页 2：上传（每段一张卡片 + 上传专属日志）
        bar_up = ctk.CTkFrame(tab_up, fg_color="transparent")
        bar_up.pack(fill="x", padx=4, pady=(2, 0))
        self.var_uplog = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar_up, text="显示上传日志", variable=self.var_uplog,
                        command=self._toggle_uplog, font=(FONT, 11),
                        checkbox_width=16, checkbox_height=16,
                        corner_radius=4).pack(side="right")
        self.up_txt = ctk.CTkTextbox(tab_up, corner_radius=10, wrap="none",
                                     font=(MONO, 11), fg_color=C("log_bg"),
                                     text_color=C("log_fg"), border_width=0, height=90)
        self.up_list = ctk.CTkScrollableFrame(tab_up, corner_radius=10,
                                              fg_color=C("card2"))
        self.up_list.pack(fill="both", expand=True, padx=2, pady=(2, 6))
        self.up_txt.configure(state="disabled")
        for t, k in (("warn", "warn"), ("ok", "ok")):
            self.up_txt.tag_config(t, foreground=C1(k))

        self._load_failed_rows()
        self._load_delete_setting()
        self._check_login()

    # ─────────────────── 主题 ───────────────────
    def _toggle_theme(self) -> None:
        self._apply_theme(not self.dark, save=True)

    def _apply_theme(self, dark: bool, save: bool = False) -> None:
        """切深浅色。CTk 控件用的是 (浅,深) 色对，切完自己会重画；
        需要手动处理的是 Text 的 tag 颜色（它只认具体色值）。"""
        global DARK
        DARK = bool(dark)
        self.dark = DARK
        try:
            ctk.set_appearance_mode("dark" if DARK else "light")
        except Exception:
            pass
        for txt in (getattr(self, "txt", None), getattr(self, "up_txt", None)):
            if txt is None:
                continue
            try:
                txt.configure(fg_color=C("log_bg"), text_color=C("log_fg"))
                txt._textbox.configure(insertbackground=C1("log_fg"))
                for t, k in (("warn", "warn"), ("ok", "ok"), ("muted", "muted")):
                    txt.tag_config(t, foreground=C1(k))
            except Exception:
                pass
        for r in getattr(self, "rows", []):
            try:
                r["state"].configure(text_color=C(r.get("state_kind") or "muted"))
            except Exception:
                pass
        if hasattr(self, "btn_theme"):
            try:
                self.btn_theme.configure(text="浅色" if DARK else "深色")
            except Exception:
                pass
        if save:
            self._save_settings()
            self._check_login()          # 让「登录状态」按新主题重上一次色

    # ─────────────────── 主播行 ───────────────────
    def add_row(self, room: str = "", quality: str = "", segment: str = "",
                season=None) -> dict:
        idx = len(self.rows) + 1
        f = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
        f.pack(fill="x", pady=2)

        lbl_no = ctk.CTkLabel(f, text=f"#{idx}", width=26, font=(FONT, 12),
                              text_color=C("muted"), anchor="w")
        lbl_no.pack(side="left")
        var_room = tk.StringVar(value=room)
        e = ctk.CTkEntry(f, textvariable=var_room, height=32, corner_radius=8,
                         font=(FONT, 12), placeholder_text="抖音号 / 房间号 / 直播间链接")
        e.pack(side="left", fill="x", expand=True, padx=(2, 8))

        var_q = tk.StringVar(value=_quality_label(quality))
        cb = ctk.CTkOptionMenu(f, variable=var_q, width=150, height=32,
                               corner_radius=8, font=(FONT, 11),
                               values=[QUALITY_LABEL[q] for q in QUALITY_CHOICES],
                               fg_color=C("sub"), button_color=C("sub_hover"),
                               text_color=C("sub_text"))
        cb.pack(side="left", padx=(0, 6))

        # 每个主播自己的分段时间（主界面「公共设置」里那个只作为新行的默认值）
        var_s = tk.StringVar(value=_segment_label(segment) if segment
                             else self.var_segment.get())
        scb = ctk.CTkOptionMenu(f, variable=var_s, width=118, height=32,
                                corner_radius=8, font=(FONT, 11),
                                values=[s[0] for s in SEGMENT_CHOICES],
                                fg_color=C("sub"), button_color=C("sub_hover"),
                                text_color=C("sub_text"))
        scb.pack(side="left", padx=(0, 6))

        row = {"frame": f, "no": lbl_no, "room": var_room, "quality": var_q,
               "segment": var_s, "seg_combo": scb,
               "season": season if isinstance(season, dict) else None,
               "entry": e, "combo": cb, "thread": None, "stop": None,
               "running": False, "rid": "", "_tag": "", "state_kind": "muted"}

        # 合集：点一下从账号里所有合集里挑一个，投稿成功后自动加进去
        b_season = self._sub_btn(f, self._season_text(row["season"]), width=84,
                                 height=32)
        b_season.configure(command=lambda r=row: self.pick_season(r),
                           font=(FONT, 11))
        b_season.pack(side="left", padx=(0, 6))
        row["btn_season"] = b_season

        b_start = ctk.CTkButton(f, text="开始", width=54, height=32, corner_radius=8,
                                font=(FONT, 12),
                                command=lambda r=row: self.start_row(r))
        b_start.pack(side="left", padx=(0, 4))
        b_stop = self._sub_btn(f, "停止", lambda r=row: self.stop_row(r),
                               width=54, height=32)
        b_stop.pack(side="left", padx=(0, 8))
        row["btn_start"], row["btn_stop"] = b_start, b_stop

        lbl = ctk.CTkLabel(f, text="● 未启动", width=84, font=(FONT, 11),
                           text_color=C("muted"), anchor="w")
        lbl.pack(side="left")
        row["state"] = lbl

        b_del = ctk.CTkButton(f, text="✕", width=30, height=32, corner_radius=8,
                              font=(FONT, 13), fg_color="transparent",
                              hover_color=("#fbe3e3", "#3a2222"),
                              text_color=C("err"),
                              command=lambda r=row: self.del_row(r))
        b_del.pack(side="left")

        self.rows.append(row)
        self._sync_state()
        return row

    def _set_row_state(self, row: dict, text: str, kind: str = "muted") -> None:
        """改这一行的状态字，同时记住颜色语义（切主题时要按它重刷）。"""
        row["state_kind"] = kind
        try:
            row["state"].configure(text=text, text_color=C(kind))
        except Exception:
            pass

    @staticmethod
    def _season_text(season) -> str:
        """合集按钮上的字：没选就写「合集」，选了显示合集名（太长截一下）。"""
        if not season:
            return "合集"
        t = str((season or {}).get("title") or "").strip()
        return (t[:4] + "…") if len(t) > 4 else (t or "合集")

    def pick_season(self, row: dict) -> None:
        """选这个主播要自动加入的合集：拉账号下所有合集，让用户挑一个。"""
        cfg, _ = bu.load_config()
        cred = bu.read_credential(cfg.cred_file) or {}
        if not str(cred.get("sessdata") or "").strip():
            messagebox.showwarning("还没登录 B 站",
                                   "先点「B 站扫码登录」，才能读到你的合集列表")
            return

        win = ctk.CTkToplevel(self.root)
        win.title("选择合集")
        win.geometry("560x560")
        win.transient(self.root)
        win.grab_set()

        ctk.CTkLabel(win, text="这个主播投稿成功后，新视频自动加进所选合集。",
                     font=(FONT, 11), text_color=C("muted")).pack(
            anchor="w", padx=16, pady=(14, 2))
        cur = row.get("season") or {}
        lbl_cur = ctk.CTkLabel(win, text="当前：" + (cur.get("title") or "未选择"),
                               font=(FONT, 13, "bold"), anchor="w")
        lbl_cur.pack(anchor="w", padx=16)

        search = tk.StringVar()
        ctk.CTkEntry(win, textvariable=search, height=32, corner_radius=8,
                     font=(FONT, 12), placeholder_text="输入关键词筛选…").pack(
            fill="x", padx=16, pady=(10, 8))

        listbox = ctk.CTkScrollableFrame(win, corner_radius=10, fg_color=C("card2"))
        listbox.pack(fill="both", expand=True, padx=16)

        status = ctk.CTkLabel(win, text="正在读取合集列表…", font=(FONT, 11),
                              text_color=C("muted"), anchor="w")
        status.pack(fill="x", padx=16, pady=(6, 0))

        state = {"items": [], "picked": None}

        def fill(*_):
            for w in listbox.winfo_children():
                w.destroy()
            kw = search.get().strip()
            for it in state["items"]:
                if kw and kw not in it["title"]:
                    continue
                picked = state["picked"] == it["id"]
                b = ctk.CTkButton(
                    listbox, text=f"{it['title'] or ('（无标题 %s）' % it['id'])}"
                                  f"   ·   {it['count']} 个视频",
                    anchor="w", height=34, corner_radius=8, font=(FONT, 12),
                    fg_color=("#2563eb" if picked else "transparent"),
                    hover_color=C("sub_hover"),
                    text_color=("#ffffff" if picked else C("sub_text")),
                    command=lambda i=it: choose(i))
                b.pack(fill="x", pady=2)

        def choose(it):
            state["picked"] = it["id"]
            lbl_cur.configure(text="当前：" + (it["title"] or "未选择"))
            fill()

        def reload_seasons():
            status.configure(text="正在读取合集列表…", text_color=C("muted"))
            win.update_idletasks()
            try:
                state["items"] = bu.list_seasons(cred)
            except Exception as e:
                status.configure(text=f"读取失败：{e}", text_color=C1("warn"))
                return
            fill()
            status.configure(text=f"共 {len(state['items'])} 个合集（输入可筛选）",
                             text_color=C("muted"))

        def apply_choice():
            sid = state["picked"]
            if not sid:
                messagebox.showinfo("还没选", "先在上面的列表里点一个合集", parent=win)
                return
            item = next((i for i in state["items"] if i["id"] == sid), None)
            if not item:
                return
            row["season"] = item
            row["btn_season"].configure(text=self._season_text(item))
            self.append_log(f"[i] 合集已设置：{item['title']}（id={sid}，"
                            f"投稿成功后自动加入）")
            self._save_settings()
            win.destroy()

        def clear_choice():
            row["season"] = None
            row["btn_season"].configure(text=self._season_text(None))
            self.append_log("[i] 合集已清空：这个主播的视频不再自动加入合集")
            self._save_settings()
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(btns, text="加入这个合集", command=apply_choice, width=112,
                      height=34, corner_radius=9, font=(FONT, 12)).pack(side="left")
        self._sub_btn(btns, "清空（不加合集）", clear_choice, width=124, height=34).pack(
            side="left", padx=8)
        self._sub_btn(btns, "刷新", reload_seasons, width=64, height=34).pack(side="left")
        self._sub_btn(btns, "取消", win.destroy, width=64, height=34).pack(side="right")

        search.trace_add("write", fill)
        reload_seasons()

    def del_row(self, row: dict) -> None:
        """删除一行。**录制中也能删** —— 只停这一路，其它主播不受影响。"""
        if row.get("running"):
            if not messagebox.askokcancel(
                    "这一行正在录",
                    f"{row.get('rid')} 正在录制，确定停掉并删除吗？\n"
                    "（只影响这一行，其它主播继续录）"):
                return
            row["stop"].set()
            if row["thread"] is not None:
                row["thread"].join(timeout=25)
        row["frame"].destroy()
        self.rows = [r for r in self.rows if r is not row]
        for i, r in enumerate(self.rows, 1):
            r["no"].configure(text=f"#{i}")
        self._save_settings()
        self._sync_state()

    # ─────────────────── 设置持久化 ───────────────────
    def _load_settings(self) -> None:
        try:
            d = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        rooms = d.get("rooms") or []
        if not rooms and d.get("room"):
            rooms = [{"room": d.get("room", ""), "quality": d.get("quality", "")}]
        self.dark = (d.get("theme") or "light") == "dark"
        # 先恢复「默认分段时间」：add_row 建行时拿它当兜底
        self.var_segment.set(_segment_label(d.get("segment", SEGMENT_DEFAULT)))
        for r in rooms or [{}]:
            self.add_row(r.get("room", ""), r.get("quality", ""),
                         r.get("segment", ""), r.get("season"))
        if not self.rows:
            self.add_row()
        self.var_duration.set(d.get("duration", DURATION_CHOICES[0][0]))
        if self.var_duration.get() not in [x[0] for x in DURATION_CHOICES]:
            self.var_duration.set(DURATION_CHOICES[0][0])
        self.var_dir.set(d.get("out_dir") or str(rec.DEFAULT_DIR))
        self.var_wait_mode.set(d.get("wait_mode", "forever"))
        self.var_wait_min.set(str(d.get("wait_min", "30")))
        self.var_upload.set(bool(d.get("upload", True)))
        self.var_monitor.set(bool(d.get("monitor", True)))

    def _save_settings(self) -> None:
        try:
            SETTINGS.write_text(json.dumps({
                "rooms": [{"room": r["room"].get().strip(),
                           "quality": r["quality"].get(),
                           "segment": r["segment"].get(),
                           "season": r.get("season")}
                          for r in self.rows],
                "segment": self.var_segment.get(),
                "duration": self.var_duration.get(),
                "out_dir": self.var_dir.get(),
                "wait_mode": self.var_wait_mode.get(),
                "wait_min": self.var_wait_min.get(),
                "upload": self.var_upload.get(),
                "monitor": self.var_monitor.get(),
                "theme": "dark" if self.dark else "light",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ─────────────────── 取值 ───────────────────
    def _quality_code(self, text: str) -> str:
        t = (text or "").strip()
        for q, lab in QUALITY_LABEL.items():      # 先按文案精确匹配（"原画" 别被 "原画/高清" 抢走）
            if t == lab:
                return q
        for q in QUALITY_CHOICES:                 # 老格式 "FULL_HD1  原画/高清（推荐）"
            if t.startswith(q):
                return q
        return QUALITY_CHOICES[0]

    def _segment_seconds(self, text: str = "") -> int:
        """下拉框文案 → 秒数。text 传空就取主界面那个默认值。"""
        label = text or self.var_segment.get()
        for l, sec in SEGMENT_CHOICES:
            if l == label:
                return sec
        return 7200

    def _duration_seconds(self) -> int:
        for label, sec in DURATION_CHOICES:
            if label == self.var_duration.get():
                return sec
        return 0

    def _wait_seconds(self) -> int:
        mode = self.var_wait_mode.get()
        if mode == "none":
            return -1
        if mode == "limit":
            try:
                return max(0, int(float(self.var_wait_min.get()) * 60))
            except Exception:
                return 1800
        return 0

    # ─────────────────── 日志 / 状态 ───────────────────
    def _log_from_thread(self, *a, live: str = "") -> None:
        """录制线程把日志丢进队列，主线程再写控件（Tk 不能跨线程碰）。

        live 非空表示这是进度型日志（「已写 …」），控制台里只留最新一条；
        它只是捎带过去，`append_log` 负责替换，`file_log` 照旧全量落盘。
        """
        self.q.put(("log", (" ".join(map(str, a)), live)))

    def append_log(self, text: str, live: str = "") -> None:
        """写一行到「录制日志」。

        live 非空 = 这是**进度型**日志（「已写 3.20 GB，已录 01:23:45」这类）：
        同一个 key 在控制台只留最新一条 —— 先删掉旧的那行，再把新的接在末尾。
        文件日志（logs/gui.log）照旧全部追加，事后能查完整轨迹。
        """
        tag = ""
        if text.startswith("[!]") or "[x]" in text or "失败" in text:
            tag = "warn"
        elif text.startswith("[√]"):
            tag = "ok"
        self.txt.configure(state="normal")
        if live:
            try:
                old = self.txt.tag_ranges(f"live:{live}")
                if old:
                    self.txt.delete(old[0], old[1])     # 删掉过期的这一条
            except Exception:
                pass
        start = self.txt.index("end-1c")
        line = f"[{time.strftime('%H:%M:%S')}] {text}\n"
        if tag:
            self.txt.insert("end", line, tag)
        else:
            self.txt.insert("end", line)
        if live:
            try:                                        # 给新的一行打上同一个 key
                self.txt.tag_add(f"live:{live}", start, f"{start}+{len(line)}c")
            except Exception:
                pass
        self.txt.see("end")
        self.txt.configure(state="disabled")
        file_log(text)

    def up_log(self, text: str) -> None:
        """「上传」分页的日志（只收后台上传相关的内容，时间戳同样带）"""
        tag = ""
        if text.startswith("[!]") or "[x]" in text or "失败" in text:
            tag = "warn"
        elif text.startswith("[√]"):
            tag = "ok"
        try:
            self.up_txt.configure(state="normal")
            if tag:
                self.up_txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n", tag)
            else:
                self.up_txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
            self.up_txt.see("end")
            self.up_txt.configure(state="disabled")
        except Exception:
            pass                       # 分页还没建好（极少）就别让它炸了上传线程
        file_log(text)

    def _up_row(self, iid, seq, anchor, state, pct) -> None:
        """上传分页的一行：不存在就建一张卡片，存在就原地更新。"""
        w = self.up_rows.get(iid)
        if w is None:
            card = ctk.CTkFrame(self.up_list, corner_radius=9, fg_color=C("card"),
                                height=42, border_width=1, border_color=C("border"))
            card.pack(fill="x", pady=3, padx=2)
            card.pack_propagate(False)
            lbl_seg = ctk.CTkLabel(card, text=seq, width=86, font=(FONT, 11, "bold"),
                                   anchor="w")
            lbl_seg.pack(side="left", padx=(10, 4))
            lbl_anchor = ctk.CTkLabel(card, text=anchor, width=150, font=(FONT, 11),
                                      text_color=C("muted"), anchor="w")
            lbl_anchor.pack(side="left", padx=(0, 4))
            lbl_state = ctk.CTkLabel(card, text=state, width=150, font=(FONT, 11),
                                     anchor="w")
            lbl_state.pack(side="left", padx=(0, 8))
            bar = ctk.CTkProgressBar(card, height=6, corner_radius=3, width=120)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
            lbl_pct = ctk.CTkLabel(card, text=str(pct), width=140, font=(FONT, 10),
                                   text_color=C("muted"), anchor="w")
            lbl_pct.pack(side="left", padx=(0, 8))
            w = {"card": card, "seg": lbl_seg, "anchor": lbl_anchor, "state": lbl_state,
                 "bar": bar, "pct": lbl_pct}
            self.up_rows[iid] = w
        try:
            if seq:
                w["seg"].configure(text=seq)
            if anchor:
                w["anchor"].configure(text=anchor)
            w["state"].configure(text=state)
            w["pct"].configure(text=str(pct))
            digits = "".join(ch for ch in str(pct).split("%")[0] if ch.isdigit())
            pv = int(digits) / 100 if digits else 0
            w["bar"].set(min(1.0, max(0.0, pv)))
            if "已投稿" in state:
                w["state"].configure(text_color=C("ok"))
            elif "失败" in state or "待补传" in state:
                w["state"].configure(text_color=C("warn"))
            else:
                w["state"].configure(text_color=C("sub_text"))
        except Exception:
            pass

    def _toggle_uplog(self) -> None:
        """上传日志默认收起来，把整个分页让给进度列表；调试时再勾出来。"""
        if self.var_uplog.get():
            self.up_txt.pack(side="bottom", fill="x", padx=2, pady=(0, 4))
        else:
            self.up_txt.pack_forget()

    def _load_failed_rows(self) -> None:
        """把以前投稿失败的段先填进「上传」页 —— 点「补传」就会重投它们。"""
        try:
            items = bu.load_failed()
        except Exception:
            items = []
        for i, it in enumerate(items, 1):
            err = (it.get("error") or "失败原因未知").strip()
            if len(err) > 40:
                err = err[:40] + "…"
            label = f"#{it.get('seq', '?')}"
            if it.get("part"):
                label += f" P{it['part']}"
            self._up_row(f"old{i}", label, it.get("anchor") or "",
                         f"待补传（{err}）", "—")

    def _on_upload_event(self, seq, phase, pct, anchor, done=0, total=0,
                         label="") -> None:
        """上传事件 → 那一行卡片。

        ⚠️ 只显示百分比是不够的：B 站固定 10MB 一块，2GB 的文件有 205 块，
        每块才 0.49% —— 前几十兆算出来一直是 0%，看着像卡住。
        所以连着块数一起显示（「0%（2/205 块）」），从第一块就看得出在动。
        """
        iid = f"seq{seq}"
        seg = label or f"#{seq}"
        if phase == "start":
            self._up_row(iid, seg, anchor, "上传中", "0%")
            self._up_pct, self._up_chunks = 0, ""
        elif phase == "progress":
            cell = f"{pct}%（{done}/{total} 块）" if total else f"{pct}%"
            self._up_row(iid, seg, anchor, "上传中", cell)
            self._up_pct = pct
            self._up_chunks = f"{done}/{total}" if total else ""
        elif phase == "ok":
            self._up_row(iid, seg, anchor, "✓ 已投稿", "100%")
            self._up_pct, self._up_chunks = 100, ""
        elif phase == "fail":
            self._up_row(iid, seg, anchor, "✗ 失败（可补传）", "—")
            self._up_chunks = ""
        self._refresh_status()

    def set_status(self, text: str) -> None:
        self.lbl_status.configure(text="● " + text)

    def _show_qr(self, pil) -> None:
        """在主线程把 PIL 图变成 Tk 图片塞进登录弹窗（子线程不能碰 Tk）。"""
        if self._qr_lbl is None:
            return
        try:
            from PIL import ImageTk
            self._qr_img = ImageTk.PhotoImage(pil)
            self._qr_lbl.configure(image=self._qr_img, text="")
            if self._qr_hint is not None:
                self._qr_hint.configure(text="等待扫码…", text_color=C("muted"))
        except Exception as e:
            try:
                self._qr_hint.configure(text=f"二维码显示失败，请用命令行登录：{e}",
                                        text_color=C("warn"))
            except Exception:
                pass

    def _close_login_win(self) -> None:
        win, self._login_win = self._login_win, None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    if isinstance(payload, tuple):
                        text, live = payload
                    else:                      # 老格式：直接是字符串
                        text, live = payload, ""
                    self.append_log(text, live)
                    self._status_from_log(text)
                elif kind == "uplog":
                    self.up_log(payload)
                elif kind == "login":
                    text, color = payload
                    self.lbl_login.configure(text=text, text_color=C(color))
                elif kind == "qr":
                    self._show_qr(payload)
                elif kind == "qhint":
                    text, color = payload
                    if self._qr_hint is not None:
                        try:
                            self._qr_hint.configure(text=text, text_color=C(color))
                        except Exception:
                            pass                # 弹窗已被关掉
                elif kind == "qdone":
                    who = payload
                    if self._qr_hint is not None:
                        try:
                            self._qr_hint.configure(
                                text=f"登录成功：{who}", text_color=C("ok"))
                        except Exception:
                            pass
                    self.lbl_login.configure(text=f"登录状态：正常 · {who}",
                                             text_color=C("ok"))
                    self.append_log(f"[√] B 站登录成功：{who}")
                    self.root.after(1200, self._close_login_win)
                elif kind == "progress":
                    self._on_upload_event(*payload)
                elif kind == "rowdone":
                    row, txt, kind2 = payload
                    try:
                        if row in self.rows:
                            self._set_row_state(row, txt, kind2)
                    except Exception:
                        pass                 # 行已经被删掉了
                elif kind == "nrun":
                    self._nrun = payload
                    self._refresh_status()
        except queue.Empty:
            pass
        try:
            if self.root.winfo_exists():          # 窗口已经关了就别再排下一轮
                self.root.after(150, self._drain_queue)
        except Exception:
            pass

    def _refresh_status(self) -> None:
        """状态栏：几路在录 / 后台上传到百分之多少 / 还剩几段没传"""
        n = self._nrun
        pend = self.uploader.pending() if self.uploader else 0
        done = len(self.uploader.results) if self.uploader else 0
        pct = getattr(self, "_up_pct", 0)
        ch = getattr(self, "_up_chunks", "")
        up = f"{pct}%" + (f"（{ch} 块）" if ch else "")
        if n:
            self.set_status(f"录制中：{n} 路" + (f"（后台上传 {up}）" if self._up_chunks else ""))
        elif pend:
            self.set_status(f"后台上传 {up} · 待传 {pend} 段（已传 {done}）")
        else:
            self.set_status("就绪")

    def _status_from_log(self, line: str) -> None:
        """把日志里的状态同步到「对应那一行」上（多人同时录要分得清是谁）。"""
        for r in self.rows:
            if r.get("running") and r.get("_tag") and line.startswith(f"[{r['_tag']}]"):
                if "秒后再拉一次" in line:
                    mon = r.get("monitor")
                    txt = "● 监控中（等开播）" if mon else "● 等待开播"
                    self._set_row_state(r, txt, "warn")
                elif "开始录制 →" in line or "已写 " in line:
                    self._set_row_state(r, "● 录制中", "ok")
                elif "已交给后台上传" in line:
                    self._set_row_state(r, "● 录制中", "ok")
                break
        if "投稿完成" in line and self._nrun == 0:
            self.set_status("投稿完成")

    # ─────────────────── 录制 ───────────────────
    def _sync_state(self) -> None:
        """录制中也可以增删主播：只锁「正在跑的那一行」，其它行照常能改。"""
        any_running = any(r.get("running") for r in self.rows)
        self.recording = any_running
        self.btn_start.configure(state="normal")     # 随时能把没跑的那些起起来
        self.btn_stop.configure(state="normal" if any_running else "disabled")
        self.btn_add.configure(state="normal")       # 录制中也能添加主播
        for r in self.rows:
            run = bool(r.get("running"))
            r["entry"].configure(state="disabled" if run else "normal")
            r["combo"].configure(state="disabled" if run else "normal")
            r["seg_combo"].configure(state="disabled" if run else "normal")
            r["btn_start"].configure(state="disabled" if run else "normal")
            r["btn_stop"].configure(state="normal" if run else "disabled")

    def _ensure_uploader(self) -> rec.UploadWorker:
        """多个主播共用同一个后台上传队列（谁的段先录完谁先传）。"""
        if self.uploader is None:
            self.uploader = rec.UploadWorker(
                # 上传的日志进「上传」分页，和录制日志分开
                log=lambda *a: self.q.put(("uplog", " ".join(map(str, a)))),
                progress=lambda seq, phase, pct, anchor="", done=0, total=0,
                                label="":
                    self.q.put(("progress",
                                (seq, phase, pct, anchor, done, total, label))))
        # 每次开录都按当前开关刷新一次（用户可能中途改主意）
        self.uploader.no_bili = not self.var_upload.get()
        return self.uploader

    def start_row(self, row: dict) -> bool:
        """起一行。只影响这一路，其它主播不受影响。"""
        if row.get("running"):
            return False
        room = row["room"].get().strip()
        if not room:
            messagebox.showwarning("还没填直播间", "这一行先填抖音号 / 房间号 / 链接")
            return False
        try:
            rid = rec.parse_rid(room)
        except SystemExit as e:
            messagebox.showerror("解析不了这个直播间", f"{room}\n{e}")
            return False
        for other in self.rows:
            if other is not row and other.get("running") and other.get("rid") == rid:
                messagebox.showinfo("已经在录了", f"{rid} 已经在录了")
                return False

        out_dir = Path(self.var_dir.get()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("输出目录不可用", f"{out_dir}\n{e}")
            return False

        tag = room if len(room) <= 14 else room[:14]
        row.update(rid=rid, running=True, _tag=tag,
                   stop=threading.Event())
        row["thread"] = threading.Thread(
            target=rec.run_one_room, name=f"rec-{rid}", daemon=True,
            kwargs=dict(
                rid=rid, tag=tag,
                logf=self._log_from_thread,
                uploader=self._ensure_uploader(), stop_event=row["stop"],
                quality=self._quality_code(row["quality"].get()),
                segment=self._segment_seconds(row["segment"].get()),
                season=row.get("season"),
                duration=self._duration_seconds(),
                out_dir=str(out_dir), wait=self._wait_seconds(), upload=True,
                monitor=self.var_monitor.get()))
        row["monitor"] = self.var_monitor.get()
        row["thread"].start()
        self.append_log(f"===== 开录 [{tag}] {rid} =====")
        self._set_row_state(row, "● 录制中", "ok")
        self._save_settings()
        self._sync_state()
        self._start_monitor()
        return True

    def stop_row(self, row: dict) -> None:
        if not row.get("running"):
            return
        row["stop"].set()
        self._set_row_state(row, "● 停止中…", "warn")
        self.append_log(f"[i] [{row.get('_tag') or row.get('rid')}] 已请求停止，收尾中…")

    def on_start(self) -> None:
        """把没在跑的那些行都起起来（已经在录的不动）。"""
        started = sum(1 for r in list(self.rows)
                      if not r.get("running") and r["room"].get().strip()
                      and self.start_row(r))
        if not started:
            messagebox.showwarning("没有可启动的",
                                   "至少在一行里填抖音号 / 房间号 / 直播间链接")

    def on_stop(self) -> None:
        n = 0
        for r in self.rows:
            if r.get("running"):
                self.stop_row(r)
                n += 1
        if not n:
            self.set_status("当前没有在录的")

    # ─────────────────── 录制线程状态回传 ───────────────────
    def _start_monitor(self) -> None:
        if self._mon is None or not self._mon.is_alive():
            self._mon = threading.Thread(target=self._monitor, name="monitor",
                                         daemon=True)
            self._mon.start()

    def _monitor(self) -> None:
        """盯着线程有没有结束：结束了把行状态改回「已停止」，并刷新计数。"""
        while True:
            time.sleep(0.5)
            alive = 0
            for r in list(self.rows):
                if not r.get("running"):
                    continue
                t = r.get("thread")
                if t is not None and not t.is_alive():
                    r["running"] = False
                    self.q.put(("rowdone", (r, "● 已停止", "muted")))
                else:
                    alive += 1
            self.q.put(("nrun", alive))
            if alive == 0 and not self.threads:
                return

    # ─────────────────── 登录 / 配置 ───────────────────
    def _check_login(self) -> None:
        def work():
            try:
                cfg, _ = bu.load_config()
            except Exception as e:
                self.q.put(("login", (f"配置有问题：{e}", "warn")))
                return
            d = bu.read_credential(cfg.cred_file)
            if not d:
                self.q.put(("login", ("登录状态：未登录（不会投稿）", "warn")))
                return
            self.q.put(("login", ("登录状态：检查中…", "muted")))
            try:
                status, detail = bu.check_cookie_status(d)
            except Exception as e:
                status, detail = "unknown", f"{type(e).__name__}: {e}"

            if status == "ok":
                who = detail.split("（")[0]
                self.q.put(("login", (f"登录状态：正常 · {who}", "ok")))
            elif status == "invalid":
                self.q.put(("login", ("登录状态：已失效，点右边「B 站扫码登录」重登",
                                      "warn")))
                self.q.put(("uplog", f"[!] B 站登录态已失效：{detail}"
                                     "（现在录的段会记成待补传，重登后点「补传」即可）"))
            else:
                self.q.put(("login", ("登录状态：没查出来（网络问题？）", "warn")))

        # 注意：不能在线程里直接 root.after / 改控件 —— Tkinter 不是线程安全的，
        # 从子线程调 after 可能直接抛异常然后被吞掉（标签就永远停在「检查中」）。
        # 统一走 self.q，由主线程的 _drain_queue 落地。
        threading.Thread(target=work, name="cookie-check", daemon=True).start()

    def edit_bili_config(self) -> None:
        open_in_explorer(bu.DEFAULT_CONFIG)

    # ─────────────────── 上传成功后删本地视频 ───────────────────
    def _load_delete_setting(self) -> None:
        """勾选框跟着 bili.toml 走（GUI 和命令行共用同一份配置）。"""
        try:
            cfg, _ = bu.load_config()
            self.var_delete.set(bool(cfg.delete_after_upload))
        except Exception:
            self.var_delete.set(False)

    def on_toggle_delete(self) -> None:
        want = bool(self.var_delete.get())
        try:
            bu.set_config_value("delete_after_upload", "true" if want else "false")
        except Exception as e:
            messagebox.showerror("写配置失败", f"bili.toml 没写进去：{e}")
            self.var_delete.set(not want)
            return
        if self.uploader is not None:
            self.uploader.reload_cfg()
        self.append_log("[i] 上传成功后删除本地视频：" + ("开" if want else "关"))

    def fix_uploads(self) -> None:
        """把以前投稿失败的段重新排队（串行重投，进度看「上传」页）。"""
        def put(t):
            self.q.put(("uplog", t))

        def work():
            try:
                items = bu.load_failed()
                if not items:
                    put("[i] 没有待补传的段")
                    return
                w = self._ensure_uploader()
                n = 0
                for it in items:
                    try:
                        w.submit({"start": it.get("start"), "files": it.get("files") or []},
                                 rid=it.get("rid") or "", anchor=it.get("anchor") or "",
                                 live_title=it.get("live_title") or "",
                                 quality=it.get("quality") or "",
                                 ffmpeg=it.get("ffmpeg") or "",
                                 session=it.get("session"), part=it.get("part") or 0,
                                 season=it.get("season"))
                        n += 1
                    except Exception as e:
                        put(f"[!] 这一段排不进去：{e}")
                if n:
                    put(f"[i] 补传已排队 {n} 段，串行上传中（进度看状态栏）")
                else:
                    put("[i] 没有可补传的（文件可能已不在本地）。")
            except bu.BiliError as e:
                put(f"[x] {e}")
            except Exception as e:
                put(f"[x] 补传出错：{type(e).__name__}: {e}")
        threading.Thread(target=work, name="retry-uploads", daemon=True).start()

    def open_login(self) -> None:
        """扫码登录（二维码显示在弹窗里）。

        ⚠️ Tkinter 不是线程安全的：子线程里**不能**直接改控件、也不能靠
        `root.after` 回主线程（实测会抛异常并被吞掉，表现就是二维码永远不出现）。
        所以后台线程只干「拉二维码 / 轮询」这些纯网络的事，
        所有界面更新一律通过 self.q 交给主线程的 _drain_queue 落地。
        """
        win = ctk.CTkToplevel(self.root)
        win.title("B 站扫码登录")
        win.geometry("380x440")
        win.transient(self.root)
        win.resizable(False, False)

        ctk.CTkLabel(win, text="用 B 站 App 扫码，并在手机上确认",
                     font=(FONT, 13)).pack(pady=(16, 8))
        img_lbl = ctk.CTkLabel(win, text="")
        img_lbl.pack(pady=6)
        hint = ctk.CTkLabel(win, text="正在获取二维码…", font=(FONT, 11),
                            text_color=C("muted"), wraplength=320)
        hint.pack(pady=6)
        self._login_win, self._qr_lbl, self._qr_hint = win, img_lbl, hint

        def close_win():
            self._login_win = None
            try:
                win.destroy()
            except Exception:
                pass
        win.protocol("WM_DELETE_WINDOW", close_win)
        self._sub_btn(win, "关闭", close_win, width=80, height=32).pack(pady=(6, 12))

        def work():
            def hint_msg(t, color="muted"):
                self.q.put(("qhint", (t, color)))

            qr = bu.QrLogin()
            try:
                link = qr.begin()
            except bu.BiliError as e:
                hint_msg(str(e), "warn")
                return

            try:
                import qrcode
                obj = qrcode.QRCode(box_size=5, border=2)
                obj.add_data(link)
                obj.make(fit=True)
                im = obj.make_image(fill_color="black", back_color="white")
                pil = im.get_image() if hasattr(im, "get_image") else im
                # 图片对象必须在主线程创建，这里只把 PIL 图递过去
                self.q.put(("qr", pil))
            except Exception as e:
                hint_msg(f"生成二维码失败，请用命令行登录：{e}", "warn")
                return

            deadline = time.time() + 170        # 二维码有效期约 3 分钟
            while time.time() < deadline:
                try:
                    st = qr.poll()
                except bu.BiliError as e:
                    hint_msg(str(e), "warn")
                    return
                if st == "done":
                    break
                if st == "timeout":
                    hint_msg("二维码已过期，请重新打开", "warn")
                    return
                if st == "conf":
                    hint_msg("已扫码，请在手机上点击确认…")
                time.sleep(2)
            else:
                hint_msg("等太久了，请重新打开", "warn")
                return

            try:
                cfg, _ = bu.load_config()
                cred = qr.save(cfg.cred_file)
                who = qr.verify() or "（昵称未知）"
            except bu.BiliError as e:
                hint_msg(str(e), "warn")
                return
            if not cred.get("sessdata"):
                hint_msg("登录态不完整，请重试", "warn")
                return
            self.q.put(("qdone", who))

        threading.Thread(target=work, name="qrlogin", daemon=True).start()

    # ─────────────────── 其它 ───────────────────
    def pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择录制输出目录",
                                    initialdir=self.var_dir.get() or str(rec.DEFAULT_DIR))
        if d:
            self.var_dir.set(d)

    def on_close(self) -> None:
        running = [r for r in self.rows if r.get("running")]
        if running:
            if not messagebox.askokcancel(
                    "还在录制", f"有 {len(running)} 路正在录制，确定要退出吗？\n"
                                "（会先停止并收尾）"):
                return
            for r in running:
                r["stop"].set()
            for r in running:
                if r["thread"] is not None:
                    r["thread"].join(timeout=25)
        self._save_settings()
        self.root.destroy()


def selftest() -> int:
    """自检：把跑起来真正要用的东西过一遍再退出。

    给 build_record_exe.py 的冒烟测试用 —— 光看「窗口能不能起来」验证不了投稿那一半，
    因为 bilibili_api 的接口配置是 data/api/*.json，打包时最容易漏。
    """
    ok = True
    print(f"Python {sys.version.split()[0]}  frozen={getattr(sys, 'frozen', False)}")
    print(f"目录 {APP_DIR}")

    def check(label, fn):
        nonlocal ok
        try:
            info = fn()
            print(f"  [OK]   {label}" + (f"  {info}" if info else ""))
        except Exception as e:
            ok = False
            print(f"  [FAIL] {label}: {type(e).__name__}: {e}")

    print("第三方库：")

    def ver(name):
        m = __import__(name)
        return getattr(m, "__version__", "")

    def check_import(label, mod, extra=""):
        def _do():
            __import__(mod)
            return extra
        check(label, _do)
    check("customtkinter", lambda: ver("customtkinter"))
    check_import("PIL", "PIL")
    check_import("qrcode", "qrcode")
    check_import("requests", "requests")

    print("bilibili_api：")

    def check_video_uploader():
        from bilibili_api import video_uploader  # noqa: F401
        return "投稿模块可用"
    check("video_uploader", check_video_uploader)

    def check_http_client():
        import asyncio
        from bilibili_api.utils.network import get_client
        return type(get_client()).__name__
    check("bilibili_api 请求客户端", check_http_client)

    print("项目模块：")
    check("record.parse_rid", lambda: rec.parse_rid("live.douyin.com/yall1102"))
    check("record.plan_segment", lambda: f"{rec.plan_segment(None, 7200, 0)}")

    def check_worker():
        w = rec.UploadWorker(no_bili=True)
        return f"后台上传队列可用（pending={w.pending()}）"
    check("record.UploadWorker", check_worker)

    def check_ffmpeg():
        """不只确认「找得到」，还要真的跑一次 —— 内置的 ffmpeg 万一提取坏了
        也应该在这里暴露，而不是等到录制时才发现。"""
        import shutil
        import subprocess
        p = rec.find_ffmpeg()
        if not Path(p).is_file():
            # 源码运行时 find_ffmpeg 返回的是裸命令名 "ffmpeg"，按 PATH 解析；
            # 打包后它返回 _MEIPASS 里的绝对路径，上面就已命中。
            p = shutil.which(p) or p
        if not Path(p).is_file():
            raise RuntimeError(f"找不到 ffmpeg（{p}）—— 打包时应该把它打进 exe，"
                               "源码运行则要装 ffmpeg 或 --ffmpeg 指定路径")
        r = subprocess.run([p, "-version"], capture_output=True, timeout=60)
        first = (r.stdout or b"").decode("utf-8", errors="replace").splitlines()[:1]
        if r.returncode != 0 or "ffmpeg" not in (first[0] if first else ""):
            raise RuntimeError(f"ffmpeg 跑不起来：rc={r.returncode} {first}")
        where = "exe 内置" if getattr(sys, "frozen", False) else "系统 PATH"
        return f"{first[0][:46]}…（{where}）"
    check("ffmpeg", check_ffmpeg)

    def check_cfg():
        cfg, created = bu.load_config()
        return f"{bu.DEFAULT_CONFIG.name} 新建={created} 容器={cfg.container}"
    check("bili_uploader.load_config", check_cfg)

    def check_cred():
        cfg, _ = bu.load_config()
        d = bu.read_credential(cfg.cred_file)
        return "已登录" if d else "未登录（投稿前需要扫码）"
    check("登录态", check_cred)

    def check_cookie_names():
        """回归检查：发出去的 cookie 名必须和 B 站一致（大小写敏感）。

        踩过的坑：用凭据字典里的小写键（sessdata/dedeuserid）当 cookie 名发出去，
        B 站一律回 -101 未登录 —— 表现就是「刚扫码登录成功却马上被判失效」。
        这里打桩 requests，直接看 check_cookie_status 实际发出了哪些名字。
        """
        import requests
        seen = {}

        class _Resp:
            def json(self):
                return {"code": 0, "data": {"isLogin": True, "uname": "stub", "mid": 1}}

        real = requests.get
        try:
            def fake(url, **kw):
                seen.update({k.lower(): k for k in (kw.get("cookies") or {})})
                return _Resp()
            requests.get = fake
            st, _ = bu.check_cookie_status(
                {"sessdata": "x", "bili_jct": "y", "dedeuserid": "1"})
        finally:
            requests.get = real
        if st != "ok":
            raise AssertionError(f"打桩下应为 ok，实际 {st}")
        want = {"sessdata": "SESSDATA", "dedeuserid": "DedeUserID"}
        bad = [f"{k}→{seen.get(k)}" for k, v in want.items() if seen.get(k) != v]
        if bad:
            raise AssertionError(f"cookie 名不对：{bad}（B 站大小写敏感）")
        return "发出的 cookie 名正确（SESSDATA / bili_jct / DedeUserID）"
    check("bili_uploader cookie 名", check_cookie_names)

    print("\nSELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:                                  # 高 DPI 屏上别糊
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = ctk.CTk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
