# -*- coding: utf-8 -*-
"""
抖音直播录制 → B 站自动投稿 · 图形界面（PySide6 + Fluent Widgets）
------------------------------------------------------------------
双击 exe 或 `python record_gui.py` 打开。界面只负责收集参数和显示日志，
真正的录制/投稿逻辑全部复用 record.run_recording()，和命令行是同一份代码。

界面用 PySide6 + qfluentwidgets（Win11 Fluent 风格）：
  · 左侧导航（录制 / 上传 / 日志），内容是圆角卡片；
  · 主题跟随系统，也能在「公共设置」里手动切深/浅色；
  · 录制线程通过 Qt 信号把日志/进度送回界面（跨线程 emit 自动排队，线程安全）。

要点：
  · **多个主播可以同时录**：每个主播一个后台线程，互不影响；
    日志前面带 `[房间号]` 前缀，分得清是谁的。
  · **分段一录完就上传**：交给后台上传队列，录制线程立刻接着录下一段。
  · 未开播时按设定每 30 秒重试一次拉流（默认一直等，直到开播或点停止）。
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidgetItem, QMessageBox,
                               QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox,
                            FluentIcon as FIF, FluentWindow, InfoBar, InfoBarPosition,
                            LineEdit, ListWidget, MessageBoxBase,
                            PrimaryPushButton, ProgressBar, PushButton, RadioButton,
                            ScrollArea, SearchLineEdit, SimpleCardWidget,
                            StrongBodyLabel, SwitchButton, TextEdit, Theme,
                            TitleLabel, ToolButton, isDarkTheme, setTheme,
                            setThemeColor)


# 打包成 exe 后，工作目录可能是任意位置；配置和日志按 exe / 脚本所在目录找
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import douyin_api as dy                                  # noqa: E402
import record as rec                                     # noqa: E402
import bili_uploader as bu                              # noqa: E402
import qqbot as qb                                      # noqa: E402

#: 界面设置（每个主播行、目录、主题…）
SETTINGS = APP_DIR / "record_gui.json"

QUALITY_CHOICES = ["FULL_HD1", "ORIGIN", "HD1", "SD1", "SD2", "LD", "auto"]
QUALITY_LABEL = {"FULL_HD1": "原画/高清（推荐）", "ORIGIN": "原画", "HD1": "高清",
                 "SD1": "标清", "SD2": "较低", "LD": "流畅", "auto": "自动挑最好的"}
SEGMENT_CHOICES = [("30 分钟", 1800), ("60 分钟", 3600),
                   ("120 分钟（推荐）", 7200), ("不分段", 0)]
SEGMENT_DEFAULT = SEGMENT_CHOICES[2][0]
_SEGMENT_LEGACY = {"1 小时": 3600, "2 小时（推荐）": 7200, "3 小时": 10800,
                   "6 小时": 21600}
DURATION_CHOICES = [("不限（一直录到主播下播）", 0), ("1 小时", 3600),
                    ("2 小时", 7200), ("4 小时", 14400), ("6 小时", 21600),
                    ("8 小时", 28800), ("12 小时", 43200)]

#: 状态文字的颜色（深浅色各一套；QLabel 的 stylesheet 只认具体色值）
_STATE_COLORS = {
    "light": {"muted": "#6b7280", "ok": "#15803d", "warn": "#b45309",
              "info": "#2563eb", "err": "#b91c1c"},
    "dark": {"muted": "#9aa0a6", "ok": "#5cd39a", "warn": "#fbbf24",
             "info": "#60a5fa", "err": "#f87171"},
}


def C(kind: str) -> str:
    """按当前主题取一个具体颜色。"""
    return _STATE_COLORS["dark" if isDarkTheme() else "light"].get(kind, "#000000")


def _quality_label(saved: str) -> str:
    """存下来的清晰度 → 下拉框里那行字（老配置是 "FULL_HD1  原画/高清（推荐）"）。"""
    t = (saved or "").strip()
    if t in QUALITY_LABEL.values():
        return t
    for q in QUALITY_CHOICES:
        if t == q or t.startswith(q):
            return QUALITY_LABEL[q]
    return QUALITY_LABEL[QUALITY_CHOICES[0]]


def _segment_label(saved: str = "") -> str:
    """分段时间下拉框的文案。老的 "2 小时（推荐）" 按秒数对到现在的选项上。"""
    labels = [s[0] for s in SEGMENT_CHOICES]
    if saved in labels:
        return saved
    sec = _SEGMENT_LEGACY.get(saved or "")
    for label, s in SEGMENT_CHOICES:
        if s == sec:
            return label
    return SEGMENT_DEFAULT


def open_in_explorer(path: Path) -> None:
    """用系统的默认方式打开文件/目录"""
    try:
        if os.name == "nt":
            os.startfile(str(path))          # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        QMessageBox.warning(None, "打不开", f"{path}\n{e}")


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


def _write_log_line(edit, line: str, fmt, live: str, live_map: dict,
                    follow: bool) -> None:
    """往日志框写一行，并按「是否跟随最新」决定滚动。

    ⚠️ 就地替换「已写」那行时**绝不能动滚动位置** —— 之前统一 setTextCursor +
    ensureCursorVisible，光标停在那行（往往在文档前部），视图就被拽回顶部了。
    """
    sb = edit.verticalScrollBar()
    keep = sb.value()
    replaced = False
    try:
        blk = live_map.get(live) if live else None
        if blk is not None and blk.isValid():
            cur = QTextCursor(blk)
            cur.setPosition(blk.position())
            cur.setPosition(blk.position() + max(0, blk.length() - 1),
                            QTextCursor.MoveMode.KeepAnchor)
            cur.insertText(line, fmt)
            live_map[live] = cur.block()
            replaced = True
        else:
            cur = QTextCursor(edit.document())
            cur.movePosition(QTextCursor.MoveOperation.End)
            if edit.toPlainText():
                cur.insertBlock()
            cur.insertText(line, fmt)
            if live:
                live_map[live] = cur.block()
    except Exception:
        return
    try:
        if replaced or not follow:
            sb.setValue(min(keep, sb.maximum()))     # 保持用户当前看到的位置
        else:
            edit.moveCursor(QTextCursor.MoveOperation.End)   # 跟随最新
    except Exception:
        pass


class Bridge(QObject):
    """录制线程 → 界面线程的信号桥。

    Qt 的信号是**排队**投递的：从 Python 线程 emit，槽函数会在 GUI 线程执行，
    所以槽里可以直接碰控件（这点和 Tkinter 不一样，不用再自己搞队列轮询）。
    """
    log = Signal(str, str)          # 文本, live key
    uplog = Signal(str)
    login = Signal(str, str)        # 文本, ok/warn/muted
    qr = Signal(object)             # {"raw": bytes, "w": int, "h": int}
    qhint = Signal(str, str)
    qdone = Signal(str)
    progress = Signal(int, str, int, str, int, int, str)
    botlog = Signal(str)            # 机器人自己的日志（连接/断线/报错）
    notice = Signal(str)            # 需要推给 QQ 的事件（上传卡住等）
    anchor_name = Signal(int, str)  # 解析到主播昵称（row 的 id, 昵称）
    ui_call = Signal(object)        # 机器人线程要改界面时，把函数丢过来在 GUI 线程执行


class RoomRow(QFrame):
    """主播列表里的一行。"""

    def __init__(self, app, idx: int, parent=None):
        super().__init__(parent)
        self.app = app
        self.idx = idx
        self.running = False
        self.rid = ""
        self.tag = ""
        self.anchor = ""
        self.mode = "idle"
        self.cover = ""           # 这个主播专属的封面（空 = 用全局/抽帧）
        self.out_dir = ""         # 这个主播自己的录制目录（空 = 用公共设置）
        self.bili_config = ""     # 这个主播自己的 bili.toml（空 = 用全局配置）
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.season: dict | None = None
        self.state_kind = "muted"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(2)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.no = CaptionLabel(f"#{idx}")
        self.no.setFixedWidth(22)
        lay.addWidget(self.no)

        self.room = LineEdit()
        self.room.setPlaceholderText("抖音号 / 房间号 / 直播间链接")
        self.room.setClearButtonEnabled(True)
        lay.addWidget(self.room, 1)

        self.quality = ComboBox()
        self.quality.addItems([QUALITY_LABEL[q] for q in QUALITY_CHOICES])
        self.quality.setFixedWidth(162)
        lay.addWidget(self.quality)

        self.segment = ComboBox()
        self.segment.addItems([s[0] for s in SEGMENT_CHOICES])
        self.segment.setFixedWidth(150)
        lay.addWidget(self.segment)

        self.btn_season = PushButton("合集", self)
        self.btn_season.setFixedWidth(80)
        self.btn_season.clicked.connect(lambda: app.pick_season(self))
        lay.addWidget(self.btn_season)

        self.btn_start = PrimaryPushButton("开始", self)
        self.btn_start.setFixedWidth(60)
        self.btn_start.clicked.connect(lambda: app.start_row(self))
        lay.addWidget(self.btn_start)

        self.btn_stop = PushButton("停止", self)
        self.btn_stop.setFixedWidth(60)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(lambda: app.stop_row(self))
        lay.addWidget(self.btn_stop)

        self.btn_del = ToolButton(FIF.CLOSE, self)
        self.btn_del.setFixedSize(30, 30)
        self.btn_del.setToolTip("删除这一行（录制中也能删，只停这一路）")
        self.btn_del.clicked.connect(lambda: app.del_row(self))
        lay.addWidget(self.btn_del)
        outer.addLayout(lay)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(22, 0, 0, 0)
        bottom.setSpacing(10)
        self.name = BodyLabel("")
        self.name.setToolTip("主播昵称（添加后自动解析；开播日志里也会更新）")
        bottom.addWidget(self.name)
        self.state = CaptionLabel("● 未启动")
        self.progress = CaptionLabel("")
        self.progress.setFont(QFont("Microsoft YaHei UI", 9))
        bottom.addWidget(self.state)
        bottom.addWidget(self.progress, 1)
        outer.addLayout(bottom)

        self._progress = ""
        self.set_state("● 未启动", "muted")

    def set_progress(self, text: str) -> None:
        self._progress = (text or "").strip()
        self.progress.setText(self._progress)
        self.progress.setStyleSheet(f"color: {C('muted')};")
        page = getattr(self.app, "anchor_pages", {}).get(id(self))
        if page is not None:
            page.set_progress(self._progress)

    def values(self) -> dict:
        return {
            "room": self.room.text().strip(),
            "quality": self.quality.currentText(),
            "segment": self.segment.currentText(),
            "season": self.season,
            "cover": self.cover,
            "out_dir": self.out_dir,
            "bili_config": self.bili_config,
        }

    def set_state(self, text: str, kind: str = "muted") -> None:
        self.state_kind = kind
        self.state.setText(text)
        self.state.setStyleSheet(f"color: {C(kind)};")
        page = getattr(self.app, "anchor_pages", {}).get(id(self))
        if page is not None:
            page.set_state(text, kind)

    def set_anchor(self, name: str) -> None:
        """设置主播昵称：总监控那一行、主播页标题、左侧导航一起更新。"""
        self.anchor = (name or "").strip()
        self.name.setText(self.anchor)
        page = getattr(self.app, "anchor_pages", {}).get(id(self))
        if page is not None:
            page.refresh_title()

    def set_season(self, season) -> None:
        self.season = season
        title = (season or {}).get("title") or ""
        self.btn_season.setText((title[:4] + "…") if len(title) > 4 else (title or "合集"))
        self.btn_season.setToolTip(title or "点这里选一个合集，投稿成功后自动加入")

    def set_running(self, running: bool) -> None:
        self.running = running
        self.room.setEnabled(not running)
        self.quality.setEnabled(not running)
        self.segment.setEnabled(not running)
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        page = getattr(self.app, "anchor_pages", {}).get(id(self))
        if page is not None:               # 主播页上的按钮跟着一起变
            page.btn_start.setEnabled(not running)
            page.btn_stop.setEnabled(running)



class AnchorPage(QWidget):
    """一个主播自己的分页：状态 + 封面 + 这个主播的日志 + 这个主播的上传进度。"""

    def __init__(self, app, row):
        super().__init__()
        self.app = app
        self.row = row
        self._live: dict[str, object] = {}
        self.up_rows: dict[str, UploadRow] = {}


        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(20, 10, 20, 20)
        lay.setSpacing(10)

        self.title = TitleLabel("主播")
        lay.addWidget(self.title)

        head = QWidget(holder)
        h = QHBoxLayout(head)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self.state = CaptionLabel("● 未启动")
        self.progress = CaptionLabel("")
        h.addWidget(self.state)
        h.addWidget(self.progress, 1)
        lay.addWidget(head)

        # ---- 开始 / 停止（不用回总监控页点）
        act = QWidget(holder)
        ha = QHBoxLayout(act)
        ha.setContentsMargins(0, 0, 0, 0)
        ha.setSpacing(10)
        self.btn_start = PrimaryPushButton(FIF.PLAY, "开始录制", act)
        self.btn_start.clicked.connect(lambda: app.start_row(row))
        ha.addWidget(self.btn_start)
        self.btn_stop = PushButton(FIF.PAUSE, "停止", act)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(lambda: app.stop_row(row))
        ha.addWidget(self.btn_stop)
        ha.addStretch(1)
        lay.addWidget(act)

        # ---- 独立配置（留空 = 跟公共设置走）
        lay.addWidget(StrongBodyLabel("独立配置（留空 = 跟随公共设置）"))

        dir_row = QWidget(holder)
        hd = QHBoxLayout(dir_row)
        hd.setContentsMargins(0, 0, 0, 0)
        hd.setSpacing(10)
        lbd = BodyLabel("录制目录")
        lbd.setFixedWidth(64)
        hd.addWidget(lbd)
        self.ed_dir = LineEdit()
        self.ed_dir.setPlaceholderText("留空 = 用公共设置里的输出目录")
        self.ed_dir.setClearButtonEnabled(True)
        self.ed_dir.editingFinished.connect(self._dir_changed)
        hd.addWidget(self.ed_dir, 1)
        d1 = PushButton("浏览", dir_row)
        d1.clicked.connect(lambda: app.pick_anchor_dir(row))
        hd.addWidget(d1)
        d2 = PushButton("用公共", dir_row)
        d2.clicked.connect(lambda: app.clear_anchor_dir(row))
        hd.addWidget(d2)
        lay.addWidget(dir_row)

        cfg_row = QWidget(holder)
        hcfg = QHBoxLayout(cfg_row)
        hcfg.setContentsMargins(0, 0, 0, 0)
        hcfg.setSpacing(10)
        lbc = BodyLabel("B 站配置")
        lbc.setFixedWidth(64)
        hcfg.addWidget(lbc)
        self.ed_cfg = LineEdit()
        self.ed_cfg.setPlaceholderText("留空 = 全局 bili.toml；可指定另一个账号/分区/标题模板")
        self.ed_cfg.setClearButtonEnabled(True)
        self.ed_cfg.editingFinished.connect(self._cfg_changed)
        hcfg.addWidget(self.ed_cfg, 1)
        c1 = PushButton("选择/新建", cfg_row)
        c1.clicked.connect(lambda: app.pick_anchor_config(row))
        hcfg.addWidget(c1)
        c2 = PushButton("用公共", cfg_row)
        c2.clicked.connect(lambda: app.clear_anchor_config(row))
        hcfg.addWidget(c2)
        lay.addWidget(cfg_row)

        # ---- 这个主播专属的封面（留空就用全局的）
        cover_row = QWidget(holder)
        hc = QHBoxLayout(cover_row)
        hc.setContentsMargins(0, 0, 0, 0)
        hc.setSpacing(10)
        lb = BodyLabel("封面")
        lb.setFixedWidth(40)
        hc.addWidget(lb)
        self.ed_cover = LineEdit()
        self.ed_cover.setPlaceholderText("留空 = 用全局封面 / 抽视频第一帧")
        self.ed_cover.setClearButtonEnabled(True)
        self.ed_cover.editingFinished.connect(self._cover_changed)
        hc.addWidget(self.ed_cover, 1)
        b1 = PushButton("选择图片", cover_row)
        b1.clicked.connect(lambda: app.pick_anchor_cover(row))
        hc.addWidget(b1)
        b2 = PushButton("用全局", cover_row)
        b2.clicked.connect(lambda: app.clear_anchor_cover(row))
        hc.addWidget(b2)
        lay.addWidget(cover_row)

        # ---- 这个主播的上传进度
        lay.addWidget(StrongBodyLabel("上传进度"))
        self.up_box = QWidget(holder)
        self.up_lay = QVBoxLayout(self.up_box)
        self.up_lay.setContentsMargins(0, 0, 0, 0)
        self.up_lay.setSpacing(6)
        self.up_hint = CaptionLabel("还没有上传记录")
        self.up_lay.addWidget(self.up_hint)
        lay.addWidget(self.up_box)

        # ---- 这个主播的日志
        hlog = QHBoxLayout()
        hlog.addWidget(StrongBodyLabel("日志"))
        hlog.addStretch(1)
        hlog.addWidget(CaptionLabel("跟随最新"))
        self.sw_follow = SwitchButton()
        self.sw_follow.setOnText("开")
        self.sw_follow.setOffText("关")
        self.sw_follow.setChecked(bool(getattr(app, "follow_log", True)))
        self.sw_follow.checkedChanged.connect(lambda on: app.set_follow(on))
        hlog.addWidget(self.sw_follow)
        lay.addLayout(hlog)
        self.log_edit = TextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        self.log_edit.setPlaceholderText("这个主播的日志会出现在这里")
        self.log_edit.setMinimumHeight(320)
        lay.addWidget(self.log_edit, 1)

        scroll.setWidget(holder)
        outer.addWidget(scroll)

    # ---- 标题
    def refresh_title(self) -> None:
        name = self.row.anchor or self.row.room.text().strip() or "（空）"
        text = f"#{self.row.idx} {name}"
        if len(text) > 16:
            text = text[:15] + "…"
        self.title.setText(text)
        try:                                # 左边导航上的名字也跟着变
            item = self.app.navigationInterface.widget(self.objectName())
            if item is not None and hasattr(item, "setText"):
                item.setText(text)
        except Exception:
            pass

    # ---- 封面
    def _dir_changed(self) -> None:
        self.row.out_dir = self.ed_dir.text().strip()
        self.app._save_settings()

    def _cfg_changed(self) -> None:
        self.row.bili_config = self.ed_cfg.text().strip()
        self.app._save_settings()

    def _cover_changed(self) -> None:
        self.row.cover = self.ed_cover.text().strip()
        self.app._save_settings()

    def set_cover(self, path: str) -> None:
        self.row.cover = path or ""
        self.ed_cover.setText(self.row.cover)
        self.app._save_settings()

    # ---- 状态 / 进度
    def set_state(self, text: str, kind: str = "muted") -> None:
        self.state.setText(text)
        self.state.setStyleSheet(f"color: {C(kind)};")

    def set_progress(self, text: str) -> None:
        self.progress.setText(text or "")

    # ---- 日志（本页独立折叠「已写」）
    def append_log(self, text: str, live: str = "") -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        kind = ""
        if text.startswith("[!]") or "[x]" in text or "失败" in text:
            kind = "warn"
        elif text.startswith("[√]"):
            kind = "ok"
        fmt = QTextCharFormat()
        if kind:
            fmt.setForeground(QColor(C(kind)))
        _write_log_line(self.log_edit, line, fmt, live, self._live,
                        getattr(self.app, "follow_log", True))

    # ---- 上传进度卡片
    def up_row(self, iid: str, seg, anchor, state, pct) -> None:
        w = self.up_rows.get(iid)
        if w is None:
            w = UploadRow(self.up_box)
            self.up_lay.addWidget(w)
            self.up_rows[iid] = w
            self.up_hint.hide()
        w.update_row(seg, anchor, state, pct)


class UploadRow(SimpleCardWidget):
    """上传页的一行：段 / 主播 / 状态 / 进度条 / 百分比。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(12)

        self.seg = StrongBodyLabel("—")
        self.seg.setFixedWidth(84)
        lay.addWidget(self.seg)

        self.anchor = CaptionLabel("")
        self.anchor.setFixedWidth(140)
        lay.addWidget(self.anchor)

        self.state = BodyLabel("—")
        self.state.setFixedWidth(150)
        lay.addWidget(self.state)

        self.bar = ProgressBar()
        self.bar.setValue(0)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar, 1)

        self.pct = CaptionLabel("—")
        self.pct.setFixedWidth(130)
        lay.addWidget(self.pct)

    def update_row(self, seg, anchor, state, pct) -> None:
        if seg:
            self.seg.setText(seg)
        if anchor:
            self.anchor.setText(anchor)
        self.state.setText(state)
        self.pct.setText(str(pct))
        digits = "".join(ch for ch in str(pct).split("%")[0] if ch.isdigit())
        self.bar.setValue(int(digits) if digits else 0)
        if "已投稿" in state:
            self.state.setStyleSheet(f"color: {C('ok')};")
        elif "失败" in state or "待补传" in state:
            self.state.setStyleSheet(f"color: {C('warn')};")
        else:
            self.state.setStyleSheet("")


class SeasonDialog(MessageBoxBase):
    """合集选择：搜索框 + 列表。"""

    def __init__(self, items: list[dict], current: dict | None, parent=None):
        super().__init__(parent)
        self.picked: dict | None = None
        self.items = items
        self.titleLabel = StrongBodyLabel("选择合集", self)

        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("输入关键词筛选…")
        self.search.setClearButtonEnabled(True)

        self.list = ListWidget(self)
        self.list.setMinimumHeight(320)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(CaptionLabel("这个主播投稿成功后，新视频自动加进所选合集。"))
        self.viewLayout.addWidget(self.search)
        self.viewLayout.addWidget(self.list)

        self.yesButton.setText("加入这个合集")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(580)

        self.search.textChanged.connect(self._fill)
        self._fill()
        if current:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole)["id"] == current.get("id"):
                    self.list.setCurrentRow(i)
                    break

    def _fill(self) -> None:
        kw = self.search.text().strip()
        self.list.clear()
        for it in self.items:
            if kw and kw not in it["title"]:
                continue
            item = QListWidgetItem(f"{it['title'] or ('（无标题 %s）' % it['id'])}"
                                  f"    ·    {it['count']} 个视频")
            item.setData(Qt.ItemDataRole.UserRole, it)
            self.list.addItem(item)

    def accept(self) -> None:
        item = self.list.currentItem()
        if item is None:
            InfoBar.warning("还没选", "先在列表里点一个合集",
                            duration=2000, position=InfoBarPosition.TOP, parent=self)
            return
        self.picked = item.data(Qt.ItemDataRole.UserRole)
        super().accept()


class LoginDialog(MessageBoxBase):
    """扫码登录：二维码显示在弹窗里。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = StrongBodyLabel("B 站扫码登录", self)
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setMinimumSize(240, 240)
        self.hint = CaptionLabel("正在获取二维码…")

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(BodyLabel("用 B 站 App 扫码，并在手机上确认"))
        self.viewLayout.addWidget(self.img, 0, Qt.AlignmentFlag.AlignHCenter)
        self.viewLayout.addWidget(self.hint)
        self.yesButton.hide()
        self.cancelButton.setText("关闭")
        self.widget.setMinimumWidth(380)

    def set_hint(self, text: str, kind: str = "muted") -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color: {C(kind)};")

    def set_pixmap(self, pix: QPixmap) -> None:
        self.img.setPixmap(pix)


def _initial_theme() -> Theme:
    """读设置里的主题。必须在 QApplication 之后、建窗口之前 setTheme，
    否则 FluentWindow 的 mica 底色会停在系统主题上（出现深底配浅色字的鬼样子）。"""
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return Theme.DARK if d.get("theme") == "dark" else Theme.LIGHT
    except Exception:
        return Theme.LIGHT


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.rows: list[RoomRow] = []

        self.uploader: rec.UploadWorker | None = None
        self._up_pct = 0
        self._up_chunks = ""
        self._live_blocks: dict[str, object] = {}
        self._login_dialog: LoginDialog | None = None
        self._pending_qr: dict | None = None
        self.bot: qb.QQBot | None = None            # QQ 机器人（没启用时是 None）
        self.bot_cfg = qb.load_config(APP_DIR)
        self.follow_log = True          # 日志是否自动滚到最新（可在日志页关掉）

        self.settings = self._read_settings()

        self.bridge = Bridge()
        self.bridge.log.connect(self.append_log)
        self.bridge.uplog.connect(self.up_log)
        self.bridge.login.connect(self._on_login)
        self.bridge.qr.connect(self._on_qr)
        self.bridge.qhint.connect(self._on_qhint)
        self.bridge.qdone.connect(self._on_qdone)
        self.bridge.progress.connect(self._on_upload_event)
        self.bridge.botlog.connect(self._on_bot_log)
        self.bridge.notice.connect(self._on_bot_notice)
        self.bridge.anchor_name.connect(self._on_anchor_name)
        self.bridge.ui_call.connect(lambda fn: fn())

        self.setWindowTitle("抖音直播录制 → B 站投稿")
        # 不开 mica：它把系统壁纸透进窗口，深浅色一切换就会出现「深底浅字」的半成品
        try:
            self.setMicaEffectEnabled(False)
        except Exception:
            pass
        self.resize(1120, 800)
        self._init_pages()
        self._init_nav()
        self._apply_page_bg()
        self._load_settings()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)

        # 延迟删除：启动先清一次（上次没跑到期的），之后每 10 分钟一次
        self._sweep_deletes()
        self.sweep_timer = QTimer(self)
        self.sweep_timer.timeout.connect(self._sweep_deletes)
        self.sweep_timer.start(10 * 60 * 1000)

        file_log(f"界面启动 pid={os.getpid()} frozen={getattr(sys, 'frozen', False)} "
                 f"dir={APP_DIR} ui=qt")

    # ─────────────────── 页面骨架 ───────────────────
    def _scroll_page(self, name: str, title: str):
        page = QWidget()
        page.setObjectName(name)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("holder")
        holder.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(20, 10, 20, 20)
        lay.setSpacing(12)
        lay.addWidget(TitleLabel(title))
        scroll.setWidget(holder)
        outer.addWidget(scroll)
        return page, lay

    def _init_pages(self) -> None:
        self.record_page, self.rec_lay = self._scroll_page("recordPage", "总监控")
        self._build_rooms_card(self.rec_lay)
        self._build_action_bar(self.rec_lay)
        self._build_common_card(self.rec_lay)
        self._build_wait_card(self.rec_lay)
        self._build_bili_card(self.rec_lay)
        self.rec_lay.addStretch(1)

        self.bot_page, self.bot_lay = self._scroll_page("botPage", "机器人")
        self._build_bot_page(self.bot_lay)
        self.bot_lay.addStretch(1)

        self.log_page, self.log_lay = self._scroll_page("logPage", "总日志")
        bar = QWidget(self.log_page)
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(8)
        hb.addWidget(CaptionLabel("老的日志在上面，新的在下面；关掉就不会自动滚动了"))
        hb.addStretch(1)
        hb.addWidget(CaptionLabel("跟随最新"))
        self.sw_follow = SwitchButton()
        self.sw_follow.setOnText("开")
        self.sw_follow.setOffText("关")
        self.sw_follow.checkedChanged.connect(self.set_follow)
        hb.addWidget(self.sw_follow)
        self.log_lay.addWidget(bar)

        self.log_edit = TextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 10))
        self.log_edit.setMinimumHeight(430)
        self.log_lay.addWidget(self.log_edit, 1)
        self.status_label = CaptionLabel("● 就绪")
        self.log_lay.addWidget(self.status_label)

        #: 每个主播一页（row → AnchorPage），add_row/del_row 里维护
        self.anchor_pages: dict[int, AnchorPage] = {}

    def _apply_page_bg(self) -> None:
        """页面底色。qfluentwidgets 只给自己的控件上色，裸 QWidget 会跟系统调色板走
        （系统深色时就是黑底 + 浅色卡片，特别难看），所以这里显式刷一遍。"""
        bg = "#1e1e1e" if isDarkTheme() else "#f0f4f9"
        pages = [self.record_page, self.bot_page, self.log_page]
        pages += [pg for pg in getattr(self, "anchor_pages", {}).values()]
        for page in pages:
            page.setStyleSheet(f"#{page.objectName()}{{background:{bg};}}")
            page.setAutoFillBackground(True)
            # ScrollArea 的 viewport 是裸 QWidget，会跟系统调色板走 —— 不给它单独上色，
            # 系统是深色主题时整块内容区就是灰黑色（卡片亮、底色暗，特别脏）
            for sa in page.findChildren(ScrollArea):
                sa.setStyleSheet(
                    f"QScrollArea{{background:{bg};border:none;}}"
                    f"QScrollArea>QWidget>QWidget{{background:{bg};}}")

    def _init_nav(self) -> None:
        self.addSubInterface(self.record_page, FIF.VIDEO, "总监控")
        self.addSubInterface(self.log_page, FIF.DOCUMENT, "总日志")
        self.addSubInterface(self.bot_page, FIF.ROBOT, "机器人")
        # 主播分页在 add_row 里动态加，排在后面
        self.navigationInterface.setExpandWidth(168)
        self.navigationInterface.setCollapsible(False)

    # ─────────────────── 卡片 ───────────────────
    def _card(self, parent_lay, title: str):
        card = CardWidget(self)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(StrongBodyLabel(title))
        head.addStretch(1)
        lay.addLayout(head)
        parent_lay.addWidget(card)
        return card, lay, head

    def _build_rooms_card(self, parent_lay) -> None:
        card, lay, head = self._card(parent_lay, "主播 · 可以多个同时录制")
        self.btn_add = PushButton(FIF.ADD, "添加主播", card)
        self.btn_add.clicked.connect(lambda: self.add_row())
        head.addWidget(self.btn_add)

        self.rows_box = QWidget(card)
        self.rows_lay = QVBoxLayout(self.rows_box)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(2)
        lay.addWidget(self.rows_box)

    def _build_common_card(self, parent_lay) -> None:
        card, lay, head = self._card(parent_lay, "公共设置")

        grid = QWidget(card)
        g = QHBoxLayout(grid)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(10)

        g.addWidget(BodyLabel("分段时间（默认）"))
        self.cmb_segment = ComboBox()
        self.cmb_segment.addItems([s[0] for s in SEGMENT_CHOICES])
        self.cmb_segment.setFixedWidth(160)
        g.addWidget(self.cmb_segment)

        g.addSpacing(12)
        g.addWidget(BodyLabel("录制时长"))
        self.cmb_duration = ComboBox()
        self.cmb_duration.addItems([d[0] for d in DURATION_CHOICES])
        self.cmb_duration.setFixedWidth(240)
        g.addWidget(self.cmb_duration)

        g.addSpacing(12)
        g.addWidget(BodyLabel("深色模式"))
        self.sw_dark = SwitchButton()
        self.sw_dark.setOnText("开")
        self.sw_dark.setOffText("关")
        self.sw_dark.checkedChanged.connect(self._on_theme_switch)
        g.addWidget(self.sw_dark)
        g.addStretch(1)
        lay.addWidget(grid)

        row = QWidget(card)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lbl = BodyLabel("输出目录")
        lbl.setFixedWidth(76)
        h.addWidget(lbl)
        self.ed_dir = LineEdit()
        self.ed_dir.setClearButtonEnabled(True)
        h.addWidget(self.ed_dir, 1)
        btn = PushButton("浏览", row)
        btn.clicked.connect(self.pick_dir)
        h.addWidget(btn)
        btn_open = PushButton(FIF.FOLDER, "打开", row)
        btn_open.clicked.connect(lambda: open_in_explorer(Path(self.ed_dir.text())))
        h.addWidget(btn_open)
        lay.addWidget(row)

        row2 = QWidget(card)
        h3 = QHBoxLayout(row2)
        h3.setContentsMargins(0, 0, 0, 0)
        h3.setSpacing(10)
        h3.addWidget(BodyLabel("录完"))
        self.ed_local_delete = LineEdit()
        self.ed_local_delete.setFixedWidth(64)
        self.ed_local_delete.setPlaceholderText("0")
        self.ed_local_delete.setToolTip(
            "录完多少小时后删本地文件；0 = 不删。\n"
            "和 B 站投稿无关 —— 不上传、纯本地留档也照样按这个清理。")
        self.ed_local_delete.editingFinished.connect(self._save_local_delete)
        h3.addWidget(self.ed_local_delete)
        h3.addWidget(BodyLabel("小时后删除本地文件"))
        h3.addSpacing(8)
        h3.addWidget(CaptionLabel("0 = 不删；和投稿无关，还在排队补传的文件不会被删"))
        h3.addStretch(1)
        lay.addWidget(row2)

    def _build_wait_card(self, parent_lay) -> None:
        card, lay, head = self._card(parent_lay, "主播没开播时")
        row = QWidget(card)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        self.rb_forever = RadioButton("一直等（每 30 秒重试一次拉流）")
        self.rb_limit = RadioButton("最多等")
        self.rb_none = RadioButton("没开播就直接结束")
        for rb in (self.rb_forever, self.rb_limit, self.rb_none):
            h.addWidget(rb)
        self.sp_min = LineEdit()
        self.sp_min.setFixedWidth(64)
        h.addWidget(self.sp_min)
        h.addWidget(BodyLabel("分钟"))
        h.addStretch(1)
        h.addWidget(BodyLabel("下播后继续监控"))
        self.sw_monitor = SwitchButton()
        self.sw_monitor.setOnText("开")
        self.sw_monitor.setOffText("关")
        h.addWidget(self.sw_monitor)
        lay.addWidget(row)

    def _build_bili_card(self, parent_lay) -> None:
        card, lay, head = self._card(parent_lay, "B 站投稿")

        top = QWidget(card)
        h = QHBoxLayout(top)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self.ck_upload = CheckBox("分段录完就自动投稿（同一场直播投成一个视频的分P）")
        self.ck_upload.setChecked(True)
        h.addWidget(self.ck_upload)
        self.lbl_login = CaptionLabel("登录状态：未检查")
        h.addWidget(self.lbl_login)
        h.addStretch(1)
        self.btn_retry = PushButton("补传", top)
        self.btn_retry.clicked.connect(self.fix_uploads)
        h.addWidget(self.btn_retry)
        self.btn_cfg = PushButton("编辑配置", top)
        self.btn_cfg.clicked.connect(self.edit_bili_config)
        h.addWidget(self.btn_cfg)
        self.btn_login = PrimaryPushButton(FIF.FINGERPRINT, "B 站扫码登录", top)
        self.btn_login.clicked.connect(self.open_login)
        h.addWidget(self.btn_login)
        lay.addWidget(top)

        bottom = QWidget(card)
        h2 = QHBoxLayout(bottom)
        h2.setContentsMargins(0, 0, 0, 0)
        h2.setSpacing(12)
        self.ck_delete = CheckBox("上传成功后删除本地视频")
        self.ck_delete.toggled.connect(self.on_toggle_delete)
        h2.addWidget(self.ck_delete)
        h2.addSpacing(10)
        h2.addWidget(BodyLabel("延后"))
        self.ed_delay = LineEdit()
        self.ed_delay.setFixedWidth(64)
        self.ed_delay.setPlaceholderText("0")
        self.ed_delay.setToolTip("填小时数，比如 6 就是传完 6 小时后自动删；0 = 立刻删")
        self.ed_delay.editingFinished.connect(self._save_delete_delay)
        h2.addWidget(self.ed_delay)
        h2.addWidget(BodyLabel("小时（0 = 立即）"))
        h2.addSpacing(10)
        h2.addWidget(CaptionLabel("只删确认上传成功的那几个文件；到点自动清，程序重启也不会漏"))
        h2.addStretch(1)
        lay.addWidget(bottom)

        cover_row = QWidget(card)
        h4 = QHBoxLayout(cover_row)
        h4.setContentsMargins(0, 0, 0, 0)
        h4.setSpacing(10)
        lbl_cover = BodyLabel("封面")
        lbl_cover.setFixedWidth(40)
        h4.addWidget(lbl_cover)
        self.ed_cover = LineEdit()
        self.ed_cover.setPlaceholderText("留空 = 自动取视频第一帧；也可以指定一张 jpg/png")
        self.ed_cover.setClearButtonEnabled(True)
        h4.addWidget(self.ed_cover, 1)
        btn_pick_cover = PushButton("选择图片", card)
        btn_pick_cover.clicked.connect(self.pick_cover)
        h4.addWidget(btn_pick_cover)
        btn_auto_cover = PushButton("用第一帧", card)
        btn_auto_cover.clicked.connect(self.clear_cover)
        h4.addWidget(btn_auto_cover)
        lay.addWidget(cover_row)

    def _build_action_bar(self, parent_lay) -> None:
        bar = QWidget(self)
        h = QHBoxLayout(bar)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(10)
        self.btn_start_all = PrimaryPushButton(FIF.PLAY, "全部开始", bar)
        self.btn_start_all.clicked.connect(self.on_start)
        h.addWidget(self.btn_start_all)
        self.btn_stop_all = PushButton(FIF.PAUSE, "全部停止", bar)
        self.btn_stop_all.setEnabled(False)
        self.btn_stop_all.clicked.connect(self.on_stop)
        h.addWidget(self.btn_stop_all)
        h.addStretch(1)
        self.lbl_status = CaptionLabel("● 就绪")
        h.addWidget(self.lbl_status)
        parent_lay.addWidget(bar)

    # ─────────────────── QQ 机器人 ───────────────────
    def _build_bot_page(self, parent_lay) -> None:
        card, lay, head = self._card(parent_lay, "QQ 机器人 · 通知 + 管理")
        self.lbl_bot_state = CaptionLabel("未启用")
        head.addWidget(self.lbl_bot_state)
        head.addSpacing(12)
        head.addWidget(CaptionLabel("启用"))
        self.btn_bot_clear = PushButton("清空消息", card)
        self.btn_bot_clear.setFixedWidth(88)
        self.btn_bot_clear.clicked.connect(lambda: self.bot_log.clear())
        head.addWidget(self.btn_bot_clear)
        self.sw_bot = SwitchButton()
        self.sw_bot.setOnText("开")
        self.sw_bot.setOffText("关")
        self.sw_bot.checkedChanged.connect(lambda _: self._apply_bot())
        head.addWidget(self.sw_bot)

        # --- 凭证
        r1 = QWidget(card)
        h1 = QHBoxLayout(r1)
        h1.setContentsMargins(0, 0, 0, 0)
        h1.setSpacing(10)
        l1 = BodyLabel("AppID")
        l1.setFixedWidth(76)
        h1.addWidget(l1)
        self.ed_appid = LineEdit()
        h1.addWidget(self.ed_appid, 1)
        l2 = BodyLabel("AppSecret")
        l2.setFixedWidth(80)
        h1.addWidget(l2)
        self.ed_secret = LineEdit()
        self.ed_secret.setEchoMode(QLineEdit.EchoMode.Password)
        h1.addWidget(self.ed_secret, 1)
        lay.addWidget(r1)

        # --- 推到哪儿（openid，不是 QQ 号）
        r2 = QWidget(card)
        h2 = QHBoxLayout(r2)
        h2.setContentsMargins(0, 0, 0, 0)
        h2.setSpacing(10)
        l3 = BodyLabel("群 openid")
        l3.setFixedWidth(76)
        h2.addWidget(l3)
        self.ed_groups = LineEdit()
        self.ed_groups.setPlaceholderText("多个用逗号隔开；先在群里 @ 一下机器人，从日志里抄")
        h2.addWidget(self.ed_groups, 1)
        l4 = BodyLabel("私聊 openid")
        l4.setFixedWidth(88)
        h2.addWidget(l4)
        self.ed_users = LineEdit()
        self.ed_users.setPlaceholderText("可选")
        h2.addWidget(self.ed_users, 1)
        lay.addWidget(r2)

        # --- 绑定码：openid 抄不到，就发这条指令过去，发哪儿绑哪儿
        r2b = QWidget(card)
        h2b = QHBoxLayout(r2b)
        h2b.setContentsMargins(0, 0, 0, 0)
        h2b.setSpacing(10)
        l6 = BodyLabel("绑定码")
        l6.setFixedWidth(76)
        h2b.addWidget(l6)
        self.ed_code = LineEdit()
        self.ed_code.setReadOnly(True)
        h2b.addWidget(self.ed_code, 1)
        self.btn_copy = PushButton("复制指令", r2b)
        self.btn_copy.setToolTip("复制「#绑定 <绑定码>」，粘到群里或私聊发给机器人")
        self.btn_copy.clicked.connect(self._copy_bind_cmd)
        h2b.addWidget(self.btn_copy)
        self.btn_newcode = PushButton("重新生成", r2b)
        self.btn_newcode.clicked.connect(self._regen_code)
        h2b.addWidget(self.btn_newcode)
        lay.addWidget(r2b)

        # --- 谁能操作
        r3 = QWidget(card)
        h3 = QHBoxLayout(r3)
        h3.setContentsMargins(0, 0, 0, 0)
        h3.setSpacing(10)
        h3.addWidget(BodyLabel("允许机器人操作"))
        self.sw_manage = SwitchButton()
        self.sw_manage.setOnText("允许")
        self.sw_manage.setOffText("只读")
        h3.addWidget(self.sw_manage)
        h3.addSpacing(16)
        l5 = BodyLabel("管理员 openid")
        l5.setFixedWidth(96)
        h3.addWidget(l5)
        self.ed_admins = LineEdit()
        self.ed_admins.setPlaceholderText("授权出去的人；绑定的人（master）不用填，自动算")
        h3.addWidget(self.ed_admins, 1)
        lay.addWidget(r3)

        # --- 通知开关
        r4 = QWidget(card)
        h4 = QHBoxLayout(r4)
        h4.setContentsMargins(0, 0, 0, 0)
        h4.setSpacing(14)
        self.ck_live_on = CheckBox("开播提醒")
        self.ck_live_off = CheckBox("下播提醒")
        self.ck_up_ok = CheckBox("投稿完成")
        self.ck_up_fail = CheckBox("投稿失败")
        for w in (self.ck_live_on, self.ck_live_off, self.ck_up_ok, self.ck_up_fail):
            h4.addWidget(w)
        h4.addStretch(1)
        lay.addWidget(r4)

        # --- 按钮 + 说明
        r5 = QWidget(card)
        h5 = QHBoxLayout(r5)
        h5.setContentsMargins(0, 0, 0, 0)
        h5.setSpacing(10)
        self.btn_bot_save = PrimaryPushButton("保存并应用", r5)
        self.btn_bot_save.clicked.connect(self._save_bot_ui)
        h5.addWidget(self.btn_bot_save)
        btn_stop = PushButton("断开", r5)
        btn_stop.clicked.connect(lambda: (self.sw_bot.setChecked(False), self._apply_bot()))
        h5.addWidget(btn_stop)
        h5.addStretch(1)
        self.lbl_bot_stats = CaptionLabel("")
        h5.addWidget(self.lbl_bot_stats)
        lay.addWidget(r5)

        lay.addWidget(CaptionLabel(
            "绑定：把机器人拉进群（或加好友），把「#绑定 绑定码」发过去 —— "
            "在群里发就绑到群，私聊发就绑到私聊，上面的 openid 会自动填好。\n"
            "管理：再发「#添加 房间号」「#删除 房间号」「#列表」「#状态」。"))
        lay.addWidget(StrongBodyLabel("机器人消息（收到的 + 连接日志）"))
        self.bot_log = TextEdit()
        self.bot_log.setReadOnly(True)
        self.bot_log.setFont(QFont("Consolas", 9))
        self.bot_log.setFixedHeight(200)
        lay.addWidget(self.bot_log)

    def _load_bot_ui(self) -> None:
        c = self.bot_cfg
        self.sw_bot.blockSignals(True)          # 回填开关别顺手把机器人启动一遍
        self.ed_appid.setText(c.app_id)
        self.ed_secret.setText(c.client_secret)
        self.ed_groups.setText("，".join(c.groups))
        self.ed_users.setText("，".join(c.users))
        self.ed_admins.setText("，".join(c.admins))
        self.sw_manage.setChecked(bool(c.allow_manage))
        self.sw_bot.setChecked(bool(c.enabled))
        self.ck_live_on.setChecked(c.notify_live_on)
        self.ck_live_off.setChecked(c.notify_live_off)
        self.ck_up_ok.setChecked(c.notify_upload_ok)
        self.ck_up_fail.setChecked(c.notify_upload_fail)
        self.ed_code.setText(qb.ensure_bind_code(c))
        self.sw_bot.blockSignals(False)

    @staticmethod
    def _split_ids(text: str) -> list[str]:
        out = []
        for part in (text or "").replace("，", ",").replace(" ", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
        return out

    def _read_bot_ui(self) -> qb.BotConfig:
        c = self.bot_cfg
        c.app_id = self.ed_appid.text().strip()
        c.client_secret = self.ed_secret.text().strip()
        c.groups = self._split_ids(self.ed_groups.text())
        c.users = self._split_ids(self.ed_users.text())
        c.admins = self._split_ids(self.ed_admins.text())
        c.allow_manage = self.sw_manage.isChecked()
        c.enabled = self.sw_bot.isChecked()
        c.notify_live_on = self.ck_live_on.isChecked()
        c.notify_live_off = self.ck_live_off.isChecked()
        c.notify_upload_ok = self.ck_up_ok.isChecked()
        c.notify_upload_fail = self.ck_up_fail.isChecked()
        return c

    def _save_bot_ui(self) -> None:
        cfg = self._read_bot_ui()
        try:
            qb.save_config(APP_DIR, cfg)
        except Exception as e:
            InfoBar.error("保存失败", str(e), duration=4000,
                          position=InfoBarPosition.TOP, parent=self)
            return
        self.append_log(f"[i] 机器人配置已保存（{'启用' if cfg.enabled else '未启用'}，"
                        f"{len(cfg.groups)} 个群 / {len(cfg.users)} 个私聊）")
        self._apply_bot()

    def _apply_bot(self) -> None:
        """按当前界面设置启动/重启/停止机器人。"""
        cfg = self._read_bot_ui()
        if self.bot is not None:
            try:
                self.bot.stop()
            except Exception:
                pass
            self.bot = None
        if not cfg.enabled:
            self.lbl_bot_state.setText("未启用")
            return
        if not cfg.app_id or not cfg.client_secret:
            self.lbl_bot_state.setText("缺少 AppID / AppSecret")
            return
        self.bot = qb.QQBot(cfg, log=lambda t: self.bridge.botlog.emit(t),
                            handler=self._bot_command,
                            on_bind=self._on_bind, on_unbind=self._on_unbind,
                            on_grant=self._on_grant,
                            on_screenshot=self._on_screenshot,
                            on_bili_login=self._on_bili_login,
                            on_status=lambda s: self.bridge.botlog.emit(f"[状态] {s}"))
        self.bot.start()
        self.lbl_bot_state.setText("启动中…")

    def _copy_bind_cmd(self) -> None:
        code = self.ed_code.text().strip() or qb.ensure_bind_code(self.bot_cfg)
        QApplication.clipboard().setText(f"#绑定 {code}")
        InfoBar.success("已复制", f"把这句发到群里或私聊： #绑定 {code}",
                        duration=4000, position=InfoBarPosition.TOP, parent=self)

    def _regen_code(self) -> None:
        import uuid
        self.bot_cfg.bind_code = str(uuid.uuid4())
        self.ed_code.setText(self.bot_cfg.bind_code)
        try:
            qb.save_config(APP_DIR, self._read_bot_ui())
        except Exception:
            pass
        self.append_log("[i] 绑定码已重新生成（旧的作废）")

    def _on_bind(self, kind: str, oid: str, user: str = "") -> str:
        """机器人在群里/私聊里收到「#绑定 <码>」→ 回 GUI 线程记下来。"""
        return self._ui_call(lambda: self._on_bind_ui(kind, oid, user))

    def _on_bind_ui(self, kind: str, oid: str, user: str = "") -> str:
        c = self.bot_cfg
        if kind == "group":
            if oid not in c.groups:
                c.groups.append(oid)
            where = "这个群"
        else:
            if oid not in c.users:
                c.users.append(oid)
            where = "这个私聊"
        # 绑定的人是 master：天然能管主播，也能 @ 别人授权
        if user and user not in c.masters:
            c.masters.append(user)
        self.ed_groups.setText("，".join(c.groups))
        self.ed_users.setText("，".join(c.users))
        try:
            qb.save_config(APP_DIR, self._read_bot_ui())
        except Exception:
            pass
        self._bot_append(f"已绑定{where}：{oid}（master={user or '未知'}）")
        return (f"绑定成功，以后的开播/投稿通知都发到{where}。\n"
                "你是管理员（master），可以 @某人 发「#授权」让他也能管主播")

    def _on_grant(self, oid: str, add: bool) -> str:
        return self._ui_call(lambda: self._on_grant_ui(oid, add))

    def _on_grant_ui(self, oid: str, add: bool) -> str:
        c = self.bot_cfg
        if add:
            if oid in c.admins or oid in c.masters:
                return "这个人本来就有权限"
            c.admins.append(oid)
            self._bot_append(f"已授权：{oid}")
            return f"已授权：{oid}，他现在也能 #添加 / #删除 主播了"
        if oid in c.masters:
            return "master 的权限不能收回（换人请重新绑定）"
        if oid not in c.admins:
            return "这个人本来就没有权限"
        c.admins.remove(oid)
        self._bot_append(f"已取消授权：{oid}")
        return f"已取消授权：{oid}"

    def _on_unbind(self, kind: str, oid: str) -> str:
        return self._ui_call(lambda: self._on_unbind_ui(kind, oid))

    def _on_unbind_ui(self, kind: str, oid: str) -> str:
        c = self.bot_cfg
        lst = c.groups if kind == "group" else c.users
        if oid not in lst:
            return "这里本来就没绑过"
        lst.remove(oid)
        self.ed_groups.setText("，".join(c.groups))
        self.ed_users.setText("，".join(c.users))
        try:
            qb.save_config(APP_DIR, self._read_bot_ui())
        except Exception:
            pass
        self._bot_append(f"已解绑：{oid}")
        return "已解绑，不再往这里发通知"

    def _on_bot_log(self, text: str) -> None:
        if text.startswith("[状态] "):
            self.lbl_bot_state.setText(text[5:])
            bot = self.bot
            if bot is not None:
                st = bot.stats
                self.lbl_bot_stats.setText(
                    f"收到 {st['events']} 条 · 指令 {st['commands']} · 发出 {st['sent']}")
            return
        self._bot_append(text)

    def _resolve_anchor_name(self, row: RoomRow) -> None:
        """添加/启动时顺手查一下主播昵称（抖音接口），查到就填进总监控那一行。"""
        raw = row.room.text().strip()
        if not raw or row.anchor:
            return
        try:
            rid = rec.parse_rid(raw)
        except Exception:
            return
        if not rid:
            return

        def work():
            try:
                info = dy.fetch_room_info(rid)
                name = (getattr(info, "anchor", "") or "").strip()
            except Exception:
                return
            if name:
                self.bridge.anchor_name.emit(id(row), name)

        threading.Thread(target=work, name="anchor-name", daemon=True).start()

    def _on_anchor_name(self, row_id: int, name: str) -> None:
        for r in self.rows:
            if id(r) == row_id:
                if name and r.anchor != name:
                    r.set_anchor(name)
                break

    def _on_bili_login(self, idx: int, target) -> str:
        """QQ 的 #登录bili 命令：给第 idx 个主播扫码登录（用它自己的 bili.toml/凭据）。"""
        info = self._ui_call(lambda: self._login_target(idx), timeout=10)
        if not isinstance(info, dict):
            return str(info)
        threading.Thread(target=self._bili_login_worker, args=(info, target),
                         name="bili-login", daemon=True).start()
        return ""

    def _login_target(self, idx: int):
        if idx < 1 or idx > len(self.rows):
            return f"只有 {len(self.rows)} 个主播，没有第 {idx} 个"
        row = self.rows[idx - 1]
        return {"cfg": row.bili_config or "", "label": self._row_label(row)}

    def _bot_say(self, target, text: str) -> None:
        bot = getattr(self, "bot", None)
        if bot is not None:
            bot.send_text_to(target, text)
        self.bridge.uplog.emit(f"[i] {text}")

    def _bot_send_image(self, target, path: str, caption: str) -> None:
        bot = getattr(self, "bot", None)
        if bot is not None:
            bot.send_image_to(target, path, caption)

    def _bili_login_worker(self, info: dict, target) -> None:
        """后台线程：取二维码 → 发图 → 轮询 → 存到该主播的凭据文件。"""
        label = info.get("label") or "主播"
        qr = bu.QrLogin()
        try:
            link = qr.begin()
        except Exception as e:
            self._bot_say(target, f"获取二维码失败：{e}")
            return
        try:
            import qrcode
            img = qrcode.make(link).convert("RGB")
            path = Path(tempfile.gettempdir()) / f"bili_login_{int(time.time())}.png"
            img.save(str(path))
        except Exception as e:
            self._bot_say(target, f"生成二维码图片失败：{e}")
            return
        self._bot_send_image(target, str(path),
                             f"B 站扫码登录 · {label}\n用 B 站 App 扫一扫并点确认")
        deadline = time.time() + 165
        while time.time() < deadline:
            try:
                st = qr.poll()
            except Exception as e:
                self._bot_say(target, f"查询扫码状态失败：{e}")
                return
            if st == "done":
                break
            if st == "timeout":
                self._bot_say(target, f"二维码过期了，重新发「#登录bili {label}」那一行对应的序号")
                return
            time.sleep(2)
        else:
            self._bot_say(target, "等太久了（超过 2 分半），重新发一次 #登录bili 吧")
            return
        try:
            cfg, _ = bu.load_config(info["cfg"]) if info.get("cfg") else bu.load_config()
            cred = qr.save(cfg.cred_file)
            who = qr.verify() or "（昵称未知）"
        except Exception as e:
            self._bot_say(target, f"登录失败：{e}")
            return
        if not (cred or {}).get("sessdata"):
            self._bot_say(target, "登录态不完整，请重试")
            return
        self._bot_say(target, f"✅ 登录成功：{who}\n已绑定到 {label}\n凭据：{cfg.cred_file.name}")
        self.bridge.uplog.emit(f"[√] B 站登录成功：{who} → {label}（{cfg.cred_file}）")

    def _on_screenshot(self, idx: int):
        """QQ 的 #截图 命令：切到那个主播的分页、截整窗，返回 (路径, 标题)。"""
        res = self._ui_call(lambda: self._screenshot_ui(idx), timeout=15.0)
        return res if isinstance(res, tuple) else ("", str(res))

    def _screenshot_ui(self, idx: int):
        if idx < 1 or idx > len(self.rows):
            return "", f"只有 {len(self.rows)} 个主播，没有第 {idx} 个"
        row = self.rows[idx - 1]
        page = self.anchor_pages.get(id(row))
        if page is None:
            return "", "这个主播还没有分页"
        try:
            self.switchTo(page)
            QApplication.processEvents()
            pm: QPixmap = self.grab()          # 带左侧导航的整窗
            name = row.anchor or row.room.text().strip() or str(row.rid or idx)
            path = Path(tempfile.gettempdir()) / f"anchor_{idx}_{int(time.time())}.png"
            if not pm.save(str(path), "PNG"):
                return "", "截图保存失败"
            title = f"#{idx} {name}"
            self.append_log(f"[i] 已截图：{title}")
            return str(path), title
        except Exception as e:
            return "", f"截图出错：{type(e).__name__}: {e}"

    def _on_bot_notice(self, text: str) -> None:
        """上传卡住之类的事件：机器人页记一笔，同时推 QQ。"""
        self._bot_append(text)
        self._bot_notify(text)

    def _bot_append(self, text: str) -> None:
        """往机器人页的消息区追加一行（收到的消息 / 连接日志都放这儿）。"""
        cur = QTextCursor(self.bot_log.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.bot_log.setTextCursor(cur)
        self.bot_log.ensureCursorVisible()

    # ---- 机器人指令 → 界面（跨线程：丢回 GUI 线程执行并等结果）
    def _ui_call(self, fn, timeout: float = 8.0):
        box: dict = {}
        done = threading.Event()

        def job():
            try:
                box["v"] = fn()
            except Exception as e:                     # noqa: BLE001
                box["v"] = f"出错了：{type(e).__name__}: {e}"
            finally:
                done.set()

        self.bridge.ui_call.emit(job)
        if not done.wait(timeout):
            return "界面没响应（可能正在忙），稍后再试"
        return box.get("v") or "（没有回复）"

    def _bot_command(self, cmd: str, arg: str, user: str) -> str:
        return self._ui_call(lambda: self._bot_command_ui(cmd, arg, user))

    def _bot_command_ui(self, cmd: str, arg: str, user: str) -> str:
        """在 GUI 线程里真正执行机器人指令。"""
        if cmd == "列表":
            if not self.rows:
                return "还没添加主播"
            lines = []
            for i, r in enumerate(self.rows, 1):
                flag = "  ● 录制中" if r.running else ""
                season = f"  合集《{r.season['title']}》" if r.season else ""
                lines.append(f"#{i} {self._row_label(r)}{flag}{season}")
            return "现在的主播：\n" + "\n".join(lines)

        if cmd == "状态":
            if not self.rows:
                return "还没添加主播"
            rec_n = 0
            lines = []
            for i, r in enumerate(self.rows, 1):
                room = r.room.text().strip()
                rid = r.rid or room or "?"
                label = self._row_label(r)
                if not r.running:
                    lines.append(f"#{i} {label} · 未在录")
                    continue
                if r.mode == "recording":
                    rec_n += 1
                    st = "录制中"
                else:
                    st = "监控中（等开播）"
                # 具体进度：「已写 3.20 GB · 已录 01:23:45」（没有就不显示）
                prog = getattr(r, "_progress", "") or ""
                parts = [st] + ([prog] if prog else [])
                # 这个主播最近一段的投稿状态（有就带上）
                page = self.anchor_pages.get(id(r))
                if page is not None and page.up_rows:
                    last = list(page.up_rows.values())[-1]
                    ustate = last.state.text()
                    if "上传中" in ustate:
                        parts.append(f"投稿 {last.pct.text()}")
                    elif "已投稿" in ustate:
                        parts.append("已投稿")
                    elif "失败" in ustate:
                        parts.append("投稿失败")
                lines.append(f"#{i} {label} · " + " · ".join(parts))
            up = f"；后台上传 {self._up_pct}%（{self._up_chunks} 块）" if self._up_chunks else ""
            head = f"{len(self.rows)} 个主播，{rec_n} 路录制中{up}"
            return head + "\n" + "\n".join(lines)

        if cmd == "添加":
            parts = arg.split()
            if not parts:
                return "用法：添加 房间号 [清晰度]"
            room = parts[0]
            quality = parts[1] if len(parts) > 1 else ""
            try:
                rid = rec.parse_rid(room)
            except SystemExit as e:
                return f"解析不了这个直播间：{e}"
            for r in self.rows:
                if r.room.text().strip() and r.room.text().strip() == room:
                    return f"{self._row_label(r)} 已经在列表里了（第 {r.idx} 行）"
            if quality and quality not in QUALITY_LABEL.values():
                quality = ""
            row = self.add_row(room, quality, "", None)
            self.append_log(f"[i] QQ 机器人添加了主播：{rid} → 第 {row.idx} 行")
            self._save_settings()
            return (f"已添加：{self._row_label(row)}（第 {row.idx} 行"
                    + (f"，清晰度 {row.quality.currentText()}" if quality else "")
                    + "）\n昵称正在解析，几秒后发「状态」就能看到")

        if cmd == "删除":
            key = arg.strip().lstrip("#")
            if not key:
                return "用法：删除 房间号（或 删除 #2 按序号）"
            target = None
            for r in self.rows:
                if key.isdigit() and r.idx == int(key):
                    target = r
                    break
                if r.room.text().strip() == key or (r.rid and r.rid == key):
                    target = r
                    break
            if target is None:
                return f"没找到：{key}（发「列表」看看）"
            who = self._row_label(target)
            self.del_row(target)
            self.append_log(f"[i] QQ 机器人删除了主播：{who}")
            return f"已删除：{who}"

        return "这个指令我还不支持"

    def _bot_notify(self, text: str) -> None:
        """所有推送统一带上当时的时间（年月日 时分秒），事后翻记录知道是什么时候的事。"""
        bot = getattr(self, "bot", None)
        if bot is None:
            return
        stamp = time.strftime("%m-%d %H:%M:%S")
        bot.notify(f"[{stamp}] {text}")

    @staticmethod
    def _row_label(r) -> str:
        """给 QQ 回复用的「主播名（房间号）」；名字还没解析出来就只给房间号。"""
        rid = (r.rid or r.room.text().strip() or "").strip()
        name = (r.anchor or "").strip()
        if name and name != rid:
            return f"{name}（{rid}）"
        return rid or "（空）"

    def _page_for(self, key: str):
        """按 tag / 主播名 / 房间号 找这个主播的分页。"""
        key = (key or "").strip()
        if not key:
            return None
        for r in self.rows:
            if key in (r.tag, r.anchor, str(r.rid)):
                return self.anchor_pages.get(id(r))
        return None

    def _route_log(self, text: str) -> None:
        """日志开头是 [房间号] / [主播名] 的，顺带写进那个主播自己的分页。"""
        if not text.startswith("["):
            return
        end = text.find("]")
        if end <= 1:
            return
        page = self._page_for(text[1:end])
        if page is not None:
            page.append_log(text)

    def _update_progress(self, live: str, text: str) -> None:
        """把「已写 x，已录 hh:mm:ss」刷新到对应那行的状态格（rec:{rid} → 那一行）。"""
        if not live.startswith("rec:"):
            return
        rid = live[4:]
        try:
            body = (text or "").strip()
            if "已写" in body and "已录" in body:
                written = body.split("已写", 1)[1].split("已录", 1)[0].strip(" ，,、")
                elapsed = body.split("已录", 1)[1].strip(" ，,、")
                disp = f"已写 {written} · 已录 {elapsed}"
            else:
                disp = body
        except Exception:
            disp = (text or "").strip()
        for r in self.rows:
            if r.running and str(r.rid) == rid:
                r.set_progress(disp)
                return

    def _status_from_log(self, line: str) -> None:
        """从日志把每一路的状态区分成「录制中 / 监控中（等开播）」，顺便抓主播名。"""
        for r in self.rows:
            if not r.running:
                continue
            tag = f"[{r.tag or r.rid}]"
            if not line.startswith(tag):
                continue
            body = line[len(tag):]
            if "主播：" in body and "·" in body:
                try:
                    r.set_anchor(body.split("主播：", 1)[1].split("·")[0].strip())
                except Exception:
                    pass
                r.mode = "recording"
                r.set_state("● 录制中", "ok")
            elif "开始录制 →" in body or "  已写 " in body:
                r.mode = "recording"
                r.set_state("● 录制中", "ok")
            elif "进入监控状态" in body or "等主播开播" in body:
                r.mode = "waiting"
                r.set_state("● 监控中", "info")
            elif "秒后再拉一次" in body:
                r.mode = "waiting"
                r.set_state("● 等待开播", "warn")

    def _maybe_notify_from_log(self, text: str) -> None:
        """从日志里认「开播 / 下播」—— 录制线程的日志是现成的，不用改 record.py。"""
        cfg = self.bot_cfg
        if "主播：" in text and "·" in text and cfg.notify_live_on:
            try:
                name = text.split("主播：", 1)[1].split("·")[0].strip()
                title = text.split("·", 1)[1].strip()
            except Exception:
                return
            self._bot_notify(f"【开播】{name}\n{title}")
        elif "重查发现主播已下播" in text and cfg.notify_live_off:
            self._bot_notify("【下播】主播已下播，正在收尾并投稿")

    # ─────────────────── 主题 ───────────────────
    def _on_theme_switch(self, dark: bool) -> None:
        setTheme(Theme.DARK if dark else Theme.LIGHT)
        self._save_settings()
        for r in self.rows:
            r.set_state(r.state.text(), r.state_kind)
            r.progress.setStyleSheet(f"color: {C('muted')};")
        for pg in getattr(self, "anchor_pages", {}).values():
            pg.set_state(pg.state.text(), "muted" if "未启动" in pg.state.text() else "ok")
            pg.progress.setStyleSheet(f"color: {C('muted')};")
        self._apply_page_bg()
        self._check_login()

    # ─────────────────── 主播行 ───────────────────
    def add_row(self, room: str = "", quality: str = "", segment: str = "",
                season=None, cover: str = "", out_dir: str = "",
                bili_config: str = "") -> RoomRow:
        row = RoomRow(self, len(self.rows) + 1)
        row.room.setText(room or "")
        row.quality.setCurrentText(_quality_label(quality))
        row.segment.setCurrentText(_segment_label(segment) if segment
                                   else self.cmb_segment.currentText())
        row.set_season(season)
        row.cover = (cover or "").strip()
        row.out_dir = (out_dir or "").strip()
        row.bili_config = (bili_config or "").strip()
        self.rows_lay.addWidget(row)
        self.rows.append(row)

        # 每个主播一个分页：自己的日志 + 自己的上传进度 + 自己的封面
        page = AnchorPage(self, row)
        page.setObjectName(f"anchorPage{id(row)}")
        page.ed_cover.setText(row.cover)
        page.ed_dir.setText(row.out_dir)
        page.ed_cfg.setText(row.bili_config)
        page.refresh_title()
        self.anchor_pages[id(row)] = page
        title = f"#{row.idx} " + (room[:8] if room else "新主播")
        self.addSubInterface(page, FIF.PEOPLE, title)
        row.room.editingFinished.connect(page.refresh_title)
        self._apply_page_bg()
        self._resolve_anchor_name(row)

        self._sync_state()
        return row

    def del_row(self, row: RoomRow) -> None:
        """删除一行。**录制中也能删** —— 只停这一路，其它主播不受影响。"""
        if row.running:
            ret = QMessageBox.question(
                self, "这一行正在录",
                f"{row.rid} 正在录制，确定停掉并删除吗？\n（只影响这一行，其它主播继续录）")
            if ret != QMessageBox.StandardButton.Yes:
                return
            if row.stop_event is not None:
                row.stop_event.set()
            if row.thread is not None:
                row.thread.join(timeout=25)
        page = self.anchor_pages.pop(id(row), None)
        if page is not None:
            try:
                self.removeInterface(page)
            except Exception:
                pass
            page.deleteLater()
        row.setParent(None)
        row.deleteLater()
        self.rows.remove(row)
        for i, r in enumerate(self.rows, 1):
            r.idx = i
            r.no.setText(f"#{i}")
            pg = self.anchor_pages.get(id(r))
            if pg is not None:
                pg.refresh_title()
        self._save_settings()
        self._sync_state()

    def pick_season(self, row: RoomRow) -> None:
        cfg, _ = bu.load_config()
        cred = bu.read_credential(cfg.cred_file) or {}
        if not str(cred.get("sessdata") or "").strip():
            InfoBar.warning("还没登录 B 站", "先点「B 站扫码登录」，才能读到合集列表",
                            duration=3000, position=InfoBarPosition.TOP, parent=self)
            return
        try:
            items = bu.list_seasons(cred)
        except Exception as e:
            InfoBar.error("读合集失败", str(e), duration=5000,
                          position=InfoBarPosition.TOP, parent=self)
            return
        if not items:
            InfoBar.info("没有合集", "这个账号下还没有创建过合集", duration=3000,
                         position=InfoBarPosition.TOP, parent=self)
            return
        dlg = SeasonDialog(items, row.season, self)
        if dlg.exec() and dlg.picked:
            row.set_season(dlg.picked)
            self.append_log(f"[i] 合集已设置：{dlg.picked['title']}"
                            f"（id={dlg.picked['id']}，投稿成功后自动加入）")
            self._save_settings()

    # ─────────────────── 设置 ───────────────────
    def _read_settings(self) -> dict:
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_settings(self) -> None:
        d = self.settings
        rooms = d.get("rooms") or []
        if not rooms and d.get("room"):
            rooms = [{"room": d.get("room", ""), "quality": d.get("quality", "")}]
        self.cmb_segment.setCurrentText(_segment_label(d.get("segment", SEGMENT_DEFAULT)))
        self.cmb_duration.setCurrentText(d.get("duration", DURATION_CHOICES[0][0]))
        if self.cmb_duration.currentText() not in [x[0] for x in DURATION_CHOICES]:
            self.cmb_duration.setCurrentIndex(0)
        self.ed_dir.setText(d.get("out_dir") or str(rec.DEFAULT_DIR))
        mode = d.get("wait_mode", "forever")
        ({"forever": self.rb_forever, "limit": self.rb_limit,
          "none": self.rb_none}.get(mode) or self.rb_forever).setChecked(True)
        self.sp_min.setText(str(d.get("wait_min", "30")))
        self.ck_upload.setChecked(bool(d.get("upload", True)))
        self.sw_monitor.setChecked(bool(d.get("monitor", True)))
        self.sw_dark.setChecked(self.settings.get("theme") == "dark")
        self.follow_log = bool(d.get("follow_log", True))
        try:
            self.sw_follow.blockSignals(True)
            self.sw_follow.setChecked(self.follow_log)
            self.sw_follow.blockSignals(False)
        except Exception:
            pass
        self.local_delete_hours = float(d.get("local_delete_hours") or 0)
        self.ed_local_delete.setText(f"{self.local_delete_hours:g}")
        for r in rooms or [{}]:
            self.add_row(r.get("room", ""), r.get("quality", ""),
                         r.get("segment", ""), r.get("season"),
                         r.get("cover", ""), r.get("out_dir", ""),
                         r.get("bili_config", ""))
        if not self.rows:
            self.add_row()
        self._load_delete_setting()
        self._load_failed_rows()
        self._load_bot_ui()
        self._apply_bot()
        self._check_login()

    def _save_settings(self) -> None:
        try:
            SETTINGS.write_text(json.dumps({
                "rooms": [r.values() for r in self.rows],
                "segment": self.cmb_segment.currentText(),
                "duration": self.cmb_duration.currentText(),
                "out_dir": self.ed_dir.text(),
                "wait_mode": ("limit" if self.rb_limit.isChecked() else
                              "none" if self.rb_none.isChecked() else "forever"),
                "wait_min": self.sp_min.text(),
                "upload": self.ck_upload.isChecked(),
                "monitor": self.sw_monitor.isChecked(),
                "theme": "dark" if isDarkTheme() else "light",
                "local_delete_hours": float(getattr(self, "local_delete_hours", 0) or 0),
                "follow_log": bool(getattr(self, "follow_log", True)),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ─────────────────── 取值 ───────────────────
    def _quality_code(self, text: str) -> str:
        t = (text or "").strip()
        for q, lab in QUALITY_LABEL.items():
            if t == lab:
                return q
        for q in QUALITY_CHOICES:
            if t.startswith(q):
                return q
        return QUALITY_CHOICES[0]

    def _segment_seconds(self, text: str = "") -> int:
        label = text or self.cmb_segment.currentText()
        for l, sec in SEGMENT_CHOICES:
            if l == label:
                return sec
        return 7200

    def _duration_seconds(self) -> int:
        for label, sec in DURATION_CHOICES:
            if label == self.cmb_duration.currentText():
                return sec
        return 0

    def _wait_seconds(self) -> int:
        if self.rb_none.isChecked():
            return -1
        if self.rb_limit.isChecked():
            try:
                return max(0, int(float(self.sp_min.text()) * 60))
            except Exception:
                return 1800
        return 0

    # ─────────────────── 日志 ───────────────────
    def append_log(self, text: str, live: str = "") -> None:
        """写一行到「录制日志」。

        live 非空 = 进度型日志（「已写 …」），同一个直播间只留最新一条：
        找到那一行所在的 block，就地替换文字；block 没了就重新追加。
        """
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        kind = ""
        if text.startswith("[!]") or "[x]" in text or "失败" in text:
            kind = "warn"
        elif text.startswith("[√]"):
            kind = "ok"
        self._put(self.log_edit, line, kind, live)
        file_log(text)
        self._maybe_notify_from_log(text)
        self._status_from_log(text)
        self._route_log(text)
        if live:
            self._update_progress(live, text)

    def up_log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        kind = ""
        if text.startswith("[!]") or "[x]" in text or "失败" in text:
            kind = "warn"
        elif text.startswith("[√]"):
            kind = "ok"
        if hasattr(self, "log_edit"):
            self._put(self.log_edit, line, kind, "")
        self._route_log(text)
        file_log(text)

    def _put(self, edit: TextEdit, line: str, kind: str, live: str) -> None:
        fmt = QTextCharFormat()
        if kind:
            fmt.setForeground(QColor(C(kind)))
        _write_log_line(edit, line, fmt, live, self._live_blocks, self.follow_log)

    # ─────────────────── 上传进度（挂到各自的主播页） ───────────────────
    def _up_row(self, iid: str, seg, anchor, state, pct) -> None:
        page = self._page_for(anchor) if anchor else None
        if page is None:
            return                     # 认不出是谁的，只留在总日志里
        page.up_row(iid, seg, anchor, state, pct)

    def _load_failed_rows(self) -> None:
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

    def _on_upload_event(self, seq, phase, pct, anchor, done=0, total=0, label="") -> None:
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
            if self.bot_cfg.notify_upload_ok:
                self._bot_notify(f"【投稿完成】{seg} {anchor}".strip())
        elif phase == "fail":
            self._up_row(iid, seg, anchor, "✗ 失败（可补传）", "—")
            self._up_chunks = ""
            if self.bot_cfg.notify_upload_fail:
                self._bot_notify(f"【投稿失败】{seg} {anchor}（可在面板点「补传」）".strip())
        self._refresh_status()

    # ─────────────────── 状态 ───────────────────
    def set_status(self, text: str) -> None:
        self.lbl_status.setText("● " + text)
        self.status_label.setText("● " + text)

    def _refresh_status(self) -> None:
        n = sum(1 for r in self.rows if r.running)
        pend = self.uploader.pending() if self.uploader else 0
        done = len(self.uploader.results) if self.uploader else 0
        up = f"{self._up_pct}%" + (f"（{self._up_chunks} 块）" if self._up_chunks else "")
        if n:
            self.set_status(f"录制中：{n} 路" + (f"（后台上传 {up}）" if self._up_chunks else ""))
        elif pend:
            self.set_status(f"后台上传 {up} · 待传 {pend} 段（已传 {done}）")
        else:
            self.set_status("就绪")

    def _tick(self) -> None:
        """0.5 秒一次：把已经结束的录制线程收尾，刷新按钮状态。"""
        changed = False
        for r in self.rows:
            if r.running and r.thread is not None and not r.thread.is_alive():
                r.set_running(False)
                r.mode = "idle"
                r.set_progress("")
                r.set_state("● 已停止", "muted")
                changed = True
        if changed:
            self._sync_state()
            self._refresh_status()

    def _sync_state(self) -> None:
        self.btn_stop_all.setEnabled(any(r.running for r in self.rows))
        for r in self.rows:
            r.set_running(r.running)

    # ─────────────────── 录制 ───────────────────
    def _ensure_uploader(self) -> rec.UploadWorker:
        if self.uploader is None:
            self.uploader = rec.UploadWorker(
                log=lambda *a: self.bridge.uplog.emit(" ".join(map(str, a))),
                progress=lambda seq, phase, pct, anchor="", done=0, total=0,
                                label="": self.bridge.progress.emit(
                                    seq, phase, pct, anchor, done, total, label),
                notify=lambda text: self.bridge.notice.emit(text))
        self.uploader.no_bili = not self.ck_upload.isChecked()
        return self.uploader

    def _log_from_thread(self, *a, live: str = "") -> None:
        self.bridge.log.emit(" ".join(map(str, a)), live)

    def start_row(self, row: RoomRow) -> bool:
        if row.running:
            return False
        room = row.room.text().strip()
        if not room:
            InfoBar.warning("还没填直播间", "这一行先填抖音号 / 房间号 / 链接",
                            duration=3000, position=InfoBarPosition.TOP, parent=self)
            return False
        try:
            rid = rec.parse_rid(room)
        except SystemExit as e:
            InfoBar.error("解析不了这个直播间", f"{room}\n{e}", duration=5000,
                          position=InfoBarPosition.TOP, parent=self)
            return False
        for other in self.rows:
            if other is not row and other.running and other.rid == rid:
                InfoBar.info("已经在录了", f"{rid} 已经在录了", duration=3000,
                             position=InfoBarPosition.TOP, parent=self)
                return False

        out_dir = Path(row.out_dir or self.ed_dir.text()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            InfoBar.error("输出目录不可用", f"{out_dir}\n{e}", duration=5000,
                          position=InfoBarPosition.TOP, parent=self)
            return False

        tag = room if len(room) <= 14 else room[:14]
        row.rid = rid
        row.tag = tag
        row.anchor = ""
        row.mode = "waiting"
        row.set_progress("")
        row.stop_event = threading.Event()
        row.set_running(True)
        row.thread = threading.Thread(
            target=rec.run_one_room, name=f"rec-{rid}", daemon=True,
            kwargs=dict(
                rid=rid, tag=tag, logf=self._log_from_thread,
                uploader=self._ensure_uploader(), stop_event=row.stop_event,
                quality=self._quality_code(row.quality.currentText()),
                segment=self._segment_seconds(row.segment.currentText()),
                season=row.season, duration=self._duration_seconds(),
                out_dir=str(out_dir), wait=self._wait_seconds(), upload=True,
                monitor=self.sw_monitor.isChecked(),
                on_segment_done=self._on_segment_done,
                cover=row.cover or "", bili_config=row.bili_config or ""))
        row.thread.start()
        self.append_log(f"===== 开录 [{tag}] {rid} =====")
        row.set_state("● 等待开播", "warn")
        self._save_settings()
        self._sync_state()
        return True

    def stop_row(self, row: RoomRow) -> None:
        if not row.running or row.stop_event is None:
            return
        row.stop_event.set()
        row.set_state("● 停止中…", "warn")
        self.append_log(f"[i] [{row.tag or row.rid}] 已请求停止，收尾中…")

    def on_start(self) -> None:
        started = 0
        for r in list(self.rows):
            if not r.running and r.room.text().strip() and self.start_row(r):
                started += 1
        if not started:
            InfoBar.warning("没有可启动的", "至少在一行里填抖音号 / 房间号 / 直播间链接",
                            duration=3000, position=InfoBarPosition.TOP, parent=self)

    def on_stop(self) -> None:
        n = 0
        for r in self.rows:
            if r.running:
                self.stop_row(r)
                n += 1
        if not n:
            self.set_status("当前没有在录的")

    # ─────────────────── 登录 / 配置 ───────────────────
    def _on_login(self, text: str, kind: str) -> None:
        self.lbl_login.setText(text)
        self.lbl_login.setStyleSheet(f"color: {C(kind)};")

    def _check_login(self) -> None:
        def work():
            try:
                cfg, _ = bu.load_config()
            except Exception as e:
                self.bridge.login.emit(f"配置有问题：{e}", "warn")
                return
            d = bu.read_credential(cfg.cred_file)
            if not d:
                self.bridge.login.emit("登录状态：未登录（不会投稿）", "warn")
                return
            self.bridge.login.emit("登录状态：检查中…", "muted")
            try:
                status, detail = bu.check_cookie_status(d)
            except Exception as e:
                status, detail = "unknown", f"{type(e).__name__}: {e}"
            if status == "ok":
                who = detail.split("（")[0]
                self.bridge.login.emit(f"登录状态：正常 · {who}", "ok")
            elif status == "invalid":
                self.bridge.login.emit("登录状态：已失效，点右边「B 站扫码登录」重登", "warn")
                self.bridge.uplog.emit(f"[!] B 站登录态已失效：{detail}"
                                       "（现在录的段会记成待补传，重登后点「补传」即可）")
            else:
                self.bridge.login.emit("登录状态：没查出来（网络问题？）", "warn")

        threading.Thread(target=work, name="cookie-check", daemon=True).start()

    def edit_bili_config(self) -> None:
        open_in_explorer(bu.DEFAULT_CONFIG)

    def _load_delete_setting(self) -> None:
        try:
            cfg, _ = bu.load_config()
            self.ck_delete.blockSignals(True)
            self.ck_delete.setChecked(bool(cfg.delete_after_upload))
            self.ck_delete.blockSignals(False)
            self.ed_delay.setText(f"{float(cfg.delete_delay_hours or 0):g}")
            self.ed_cover.setText(str(getattr(cfg, "cover_file", "") or ""))
        except Exception:
            pass

    def _save_delete_delay(self) -> None:
        """把「延后 x 小时」写回 bili.toml（和命令行共用同一份配置）。"""
        raw = (self.ed_delay.text() or "").strip() or "0"
        try:
            hours = max(0.0, float(raw))
        except ValueError:
            InfoBar.warning("要填数字", "小时数只能填数字，比如 6 或 0.5",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
            self._load_delete_setting()
            return
        try:
            bu.set_config_value("delete_delay_hours", f"{hours:g}")
        except Exception as e:
            InfoBar.error("写配置失败", f"bili.toml 没写进去：{e}", duration=5000,
                          position=InfoBarPosition.TOP, parent=self)
            return
        self.ed_delay.setText(f"{hours:g}")
        if self.uploader is not None:
            self.uploader.reload_cfg()
        self.append_log("[i] 上传成功后删除："
                        + ("立即删" if hours <= 0 else f"延后 {hours:g} 小时"))

    def set_follow(self, on: bool) -> None:
        """日志是否自动跟到最新。总日志页和每个主播页共用这一个状态。"""
        self.follow_log = bool(on)
        switches = [getattr(self, "sw_follow", None)]
        switches += [pg.sw_follow for pg in getattr(self, "anchor_pages", {}).values()]
        for sw in switches:
            if sw is None:
                continue
            try:
                sw.blockSignals(True)
                sw.setChecked(self.follow_log)
                sw.blockSignals(False)
            except Exception:
                pass
        self._save_settings()

    def _save_local_delete(self) -> None:
        """「录完 x 小时删本地」——纯录制侧设置，和投稿无关。"""
        raw = (self.ed_local_delete.text() or "").strip() or "0"
        try:
            hours = max(0.0, float(raw))
        except ValueError:
            InfoBar.warning("要填数字", "小时数只能填数字，比如 6 或 0.5",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
            hours = getattr(self, "local_delete_hours", 0.0)
        self.local_delete_hours = hours
        self.ed_local_delete.setText(f"{hours:g}")
        self._save_settings()
        self.append_log("[i] 录完自动删本地："
                        + ("不删" if hours <= 0 else f"{hours:g} 小时后删"))

    def _on_segment_done(self, files: list[str]) -> None:
        """某一段录完了（录制线程回调）：按设置排进延迟删除。

        ⚠️ 这里在录制线程里，只能做纯 Python 的事（写队列文件 / 发信号），
        不能碰 Qt 控件 —— 日志走 bridge。
        """
        hours = float(getattr(self, "local_delete_hours", 0) or 0)
        if hours <= 0 or not files:
            return
        try:
            bu.schedule_delete(files, hours, log=self._log_from_thread)
        except Exception as e:
            self._log_from_thread(f"[!] 安排延迟删除失败：{type(e).__name__}: {e}")

    def pick_anchor_dir(self, row: RoomRow) -> None:
        page = self.anchor_pages.get(id(row))
        d = QFileDialog.getExistingDirectory(
            self, f"给 {row.room.text().strip() or '这个主播'} 选录制目录",
            row.out_dir or self.ed_dir.text() or str(rec.DEFAULT_DIR))
        if d and page is not None:
            page.row.out_dir = d
            page.ed_dir.setText(d)
            self._save_settings()
            self.append_log(f"[i] [{row.room.text().strip()}] 录制目录：{d}")

    def clear_anchor_dir(self, row: RoomRow) -> None:
        page = self.anchor_pages.get(id(row))
        if page is not None:
            page.row.out_dir = ""
            page.ed_dir.setText("")
            self._save_settings()
            self.append_log(f"[i] [{row.room.text().strip()}] 录制目录改回公共设置")

    def pick_anchor_config(self, row: RoomRow) -> None:
        """给这个主播单独指定一份 bili.toml（可以完全是另一个账号/分区）。"""
        page = self.anchor_pages.get(id(row))
        start = row.bili_config or str(APP_DIR / "bili_anchor.toml")
        f, _ = QFileDialog.getSaveFileName(
            self, f"给 {row.room.text().strip() or '这个主播'} 选 bili.toml",
            start, "配置文件 (*.toml)")
        if not f:
            return
        p = Path(f)
        if not p.exists():
            # 新文件：先按当前全局配置复制一份，省得从模板一条条改
            try:
                src = bu.DEFAULT_CONFIG
                if src.is_file():
                    p.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                    self.append_log(f"[i] 已按全局配置创建：{p}")
                else:
                    bu.write_config_template(p)
            except Exception as e:
                InfoBar.error("建配置失败", str(e), duration=5000,
                              position=InfoBarPosition.TOP, parent=self)
                return
        if page is not None:
            page.row.bili_config = str(p)
            page.ed_cfg.setText(str(p))
            self._save_settings()
            self.append_log(f"[i] [{row.room.text().strip()}] 独立投稿配置：{p}")

    def clear_anchor_config(self, row: RoomRow) -> None:
        page = self.anchor_pages.get(id(row))
        if page is not None:
            page.row.bili_config = ""
            page.ed_cfg.setText("")
            self._save_settings()
            self.append_log(f"[i] [{row.room.text().strip()}] 投稿配置改回全局")

    def pick_anchor_cover(self, row: RoomRow) -> None:
        page = self.anchor_pages.get(id(row))
        f, _ = QFileDialog.getOpenFileName(
            self, f"给 {row.room.text().strip() or '这个主播'} 选封面",
            (row.cover or "") if row.cover else "",
            "图片 (*.jpg *.jpeg *.png *.webp)")
        if f and page is not None:
            page.set_cover(f)
            self.append_log(f"[i] [{row.room.text().strip()}] 封面：{f}")

    def clear_anchor_cover(self, row: RoomRow) -> None:
        page = self.anchor_pages.get(id(row))
        if page is not None:
            page.set_cover("")
            self.append_log(f"[i] [{row.room.text().strip()}] 封面改回全局设置")

    def pick_cover(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片（建议 16:10，jpg/png）",
            self.ed_cover.text() or "", "图片 (*.jpg *.jpeg *.png *.webp)")
        if not f:
            return
        self._save_cover(f)

    def clear_cover(self) -> None:
        self.ed_cover.setText("")
        self._save_cover("")

    def _save_cover(self, path: str) -> None:
        """封面路径写回 bili.toml（单引号 = 字面量，Windows 路径不用转义）。"""
        path = (path or "").strip()
        try:
            bu.set_config_value("cover_file", f"'{path}'")
        except Exception as e:
            InfoBar.error("写配置失败", f"bili.toml 没写进去：{e}", duration=5000,
                          position=InfoBarPosition.TOP, parent=self)
            self._load_delete_setting()
            return
        self.ed_cover.setText(path)
        self.append_log("[i] 封面：" + (path if path else "自动取视频第一帧"))

    def _sweep_deletes(self) -> None:
        """把到点的延迟删除清掉（启动时一次，之后每 10 分钟一次）。"""
        try:
            protect = self.uploader.protect_paths() if self.uploader else set()
            bu.sweep_delete_queue(self.append_log, protect=protect)
        except Exception as e:
            self.append_log(f"[!] 清理延迟删除出错：{type(e).__name__}: {e}")

    def on_toggle_delete(self, checked: bool) -> None:
        try:
            bu.set_config_value("delete_after_upload", "true" if checked else "false")
        except Exception as e:
            InfoBar.error("写配置失败", f"bili.toml 没写进去：{e}", duration=5000,
                          position=InfoBarPosition.TOP, parent=self)
            self.ck_delete.blockSignals(True)
            self.ck_delete.setChecked(not checked)
            self.ck_delete.blockSignals(False)
            return
        if self.uploader is not None:
            self.uploader.reload_cfg()
        self.append_log("[i] 上传成功后删除本地视频：" + ("开" if checked else "关"))

    def fix_uploads(self) -> None:
        def work():
            try:
                items = bu.load_failed()
                if not items:
                    self.bridge.uplog.emit("[i] 没有待补传的段")
                    return
                w = self._ensure_uploader()
                n = 0
                for it in items:
                    w.submit({"start": it.get("start"), "files": it.get("files") or []},
                             rid=it.get("rid") or "", anchor=it.get("anchor") or "",
                             live_title=it.get("live_title") or "",
                             quality=it.get("quality") or "",
                             ffmpeg=it.get("ffmpeg") or "",
                             session=it.get("session"), part=it.get("part") or 0,
                             season=it.get("season"))
                    n += 1
                self.bridge.uplog.emit(f"[i] 补传已排队 {n} 段，串行上传中（进度看状态栏）")
            except bu.BiliError as e:
                self.bridge.uplog.emit(f"[x] {e}")
            except Exception as e:
                self.bridge.uplog.emit(f"[x] 补传出错：{type(e).__name__}: {e}")

        threading.Thread(target=work, name="retry-uploads", daemon=True).start()

    # ─────────────────── 扫码登录 ───────────────────
    def open_login(self) -> None:
        if self._login_dialog is not None:
            self._login_dialog.raise_()
            return
        dlg = LoginDialog(self)
        self._login_dialog = dlg
        dlg.finished.connect(lambda _: setattr(self, "_login_dialog", None))

        def work():
            def hint(t, kind="muted"):
                self.bridge.qhint.emit(t, kind)

            qr = bu.QrLogin()
            try:
                link = qr.begin()
            except bu.BiliError as e:
                hint(str(e), "warn")
                return
            try:
                import qrcode
                obj = qrcode.QRCode(box_size=6, border=2)
                obj.add_data(link)
                obj.make(fit=True)
                img = obj.make_image(fill_color="black", back_color="white").convert("RGB")
                w, h = img.size
                self.bridge.qr.emit({"raw": img.tobytes("raw", "RGB"), "w": w, "h": h})
            except Exception as e:
                hint(f"生成二维码失败，请用命令行登录：{e}", "warn")
                return

            deadline = time.time() + 170
            while time.time() < deadline:
                try:
                    st = qr.poll()
                except bu.BiliError as e:
                    hint(str(e), "warn")
                    return
                if st == "done":
                    break
                if st == "timeout":
                    hint("二维码已过期，请重新打开", "warn")
                    return
                if st == "conf":
                    hint("已扫码，请在手机上点击确认…")
                time.sleep(2)
            else:
                hint("等太久了，请重新打开", "warn")
                return
            try:
                cfg, _ = bu.load_config()
                cred = qr.save(cfg.cred_file)
                who = qr.verify() or "（昵称未知）"
            except bu.BiliError as e:
                hint(str(e), "warn")
                return
            if not cred.get("sessdata"):
                hint("登录态不完整，请重试", "warn")
                return
            self.bridge.qdone.emit(who)

        threading.Thread(target=work, name="qrlogin", daemon=True).start()
        dlg.show()

    def _on_qr(self, payload) -> None:
        if self._login_dialog is None or not payload:
            return
        try:
            img = QImage(payload["raw"], payload["w"], payload["h"],
                         payload["w"] * 3, QImage.Format.Format_RGB888)
            self._login_dialog.set_pixmap(QPixmap.fromImage(img))
            self._login_dialog.set_hint("等待扫码…", "muted")
        except Exception as e:
            self._login_dialog.set_hint(f"二维码显示失败：{e}", "warn")

    def _on_qhint(self, text: str, kind: str) -> None:
        if self._login_dialog is not None:
            self._login_dialog.set_hint(text, kind)

    def _on_qdone(self, who: str) -> None:
        if self._login_dialog is not None:
            self._login_dialog.set_hint(f"登录成功：{who}", "ok")
        self._on_login(f"登录状态：正常 · {who}", "ok")
        self.append_log(f"[√] B 站登录成功：{who}")

    # ─────────────────── 其它 ───────────────────
    def pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择录制输出目录",
                                             self.ed_dir.text() or str(rec.DEFAULT_DIR))
        if d:
            self.ed_dir.setText(d)

    def closeEvent(self, e) -> None:
        running = [r for r in self.rows if r.running]
        if running:
            ret = QMessageBox.question(
                self, "还在录制",
                f"有 {len(running)} 路正在录制，确定要退出吗？\n（会先停止并收尾）")
            if ret != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            for r in running:
                if r.stop_event is not None:
                    r.stop_event.set()
            for r in running:
                if r.thread is not None:
                    r.thread.join(timeout=25)
        if self.bot is not None:
            try:
                self.bot.stop()
            except Exception:
                pass
        self._save_settings()
        e.accept()


def selftest() -> int:
    """自检：把跑起来真正要用的东西过一遍再退出。"""
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
    check("PySide6", lambda: __import__("PySide6").__version__)

    def qfw_version():
        import qfluentwidgets
        return getattr(qfluentwidgets, "__version__", "已安装")
    check("qfluentwidgets", qfw_version)
    for mod in ("PIL", "qrcode", "requests"):
        check(mod, lambda m=mod: (__import__(m), "")[1])

    print("bilibili_api：")

    def check_video_uploader():
        from bilibili_api import video_uploader  # noqa: F401
        return "投稿模块可用"
    check("video_uploader", check_video_uploader)

    def check_http_client():
        from bilibili_api.utils.network import get_client
        return type(get_client()).__name__
    check("bilibili_api 请求客户端", check_http_client)

    def check_bot():
        import aiohttp
        cfg = qb.BotConfig(app_id="", client_secret="")
        bot = qb.QQBot(cfg, log=lambda *a: None)
        return f"机器人模块可用（aiohttp {aiohttp.__version__}，{bot.status}）"
    check("qqbot", check_bot)

    print("项目模块：")
    check("record.parse_rid", lambda: rec.parse_rid("live.douyin.com/yall1102"))
    check("record.plan_segment", lambda: f"{rec.plan_segment(None, 7200, 0)}")

    def check_worker():
        w = rec.UploadWorker(no_bili=True)
        return f"后台上传队列可用（pending={w.pending()}）"
    check("record.UploadWorker", check_worker)

    def check_ffmpeg():
        import shutil
        import subprocess
        p = rec.find_ffmpeg()
        if not Path(p).is_file():
            p = shutil.which(p) or p
        if not Path(p).is_file():
            raise RuntimeError(f"找不到 ffmpeg（{p}）")
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
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("RecorderUploader")
    setTheme(_initial_theme())
    setThemeColor("#0f766e")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
