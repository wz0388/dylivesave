# -*- coding: utf-8 -*-
"""
把 record_gui.py 打包成单文件 exe（PyInstaller）
------------------------------------------------------------------
用哪个 Python 很关键：**必须有 tkinter**。托管 Python 3.13 没有 tkinter，
所以脚本会在若干候选里挑一个「tkinter / PyInstaller / bilibili_api 都齐」的解释器，
默认优先本项目用过的那个 build venv。

用法:
    python build_record_exe.py                 # 单文件、不弹控制台
    python build_record_exe.py --console       # 带控制台，界面打不开时用它看报错
    python build_record_exe.py --python <python.exe>

产物：dist/RecorderUploader.exe
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "RecorderUploader"      # 用 ASCII 名：中文路径在 shell / 命令行里容易出问题
ENTRY = "record_gui.py"

#: 挑解释器时要能 import 成功的几样
PROBE = ("import tkinter, PyInstaller, bilibili_api, requests, qrcode, PIL; print('ok')")


def _decode(b: bytes) -> str:
    """子进程输出可能是 UTF-8，也可能是 GBK（跟控制台代码页有关），挨个试"""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("latin-1", errors="replace")


def candidates(explicit: str = "") -> list[Path]:
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit))
    envs = Path.home() / ".workbuddy" / "binaries" / "python" / "envs"
    # 本项目之前打包用过的那个（托管 3.13 没有 tkinter，不能用）
    for name in ("pyinstaller-build312", "pyinstaller-build"):
        p = envs / name / "Scripts" / "python.exe"
        if p.is_file():
            out.append(p)
    for p in sorted(envs.glob("*/Scripts/python.exe")):
        if p not in out:
            out.append(p)
    exe = shutil.which("python")
    if exe:
        out.append(Path(exe))
    out.append(Path(sys.executable))
    return out


def pick_python(explicit: str = "") -> Path | None:
    tried = []
    for p in candidates(explicit):
        if not p.is_file():
            continue
        try:
            r = subprocess.run([str(p), "-c", PROBE], capture_output=True,
                               text=True, timeout=180)
        except Exception as e:
            tried.append(f"{p} → 起不来（{e}）")
            continue
        if r.returncode == 0:
            return p
        last = (r.stderr or "").strip().splitlines()
        tried.append(f"{p} → {last[-1] if last else '探测失败'}")
    if explicit:
        sys.exit(f"指定的解释器不满足要求：{explicit}")
    print("没找到合适的解释器，试过的：")
    for t in tried:
        print("   ", t)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="打包 record_gui.py 成单文件 exe")
    ap.add_argument("--python", default="", help="指定用于打包的解释器")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口（打不开界面时用它看报错）")
    ap.add_argument("--name", default=NAME, help=f"产物名，默认 {NAME}")
    ap.add_argument("--no-ffmpeg", action="store_true",
                    help="不把 ffmpeg 打进 exe（改用系统 PATH 里的 ffmpeg）")
    ap.add_argument("--with-ffprobe", action="store_true",
                    help="连 ffprobe 一起打进去（只是录完打印一下编码信息，多 80MB 左右）")
    a = ap.parse_args()

    py = pick_python(a.python)
    if py is None:
        sys.exit(
            "\n没有可用的解释器。要求：tkinter + PyInstaller + bilibili-api-python 都得有。\n"
            "  · 托管 Python 3.13 没有 tkinter，不能用它打包；\n"
            "  · 用基于系统 Python 3.12 的 venv：先 pip install pyinstaller "
            "-r requirements.txt，再用 --python 指过来。"
        )
    print(f"打包用的解释器：{py}")

    shutil.rmtree(HERE / "build", ignore_errors=True)
    shutil.rmtree(HERE / "dist", ignore_errors=True)

    # ---- 把 ffmpeg 打进 exe
    #      ffmpeg 是静态编译的单文件（无 dll 依赖），直接 --add-binary 放到解包根目录，
    #      运行时的 find_ffmpeg() 会优先用 _MEIPASS/ffmpeg.exe —— exe 拷到哪都能录。
    bundled = []
    if not a.no_ffmpeg:
        ff = shutil.which("ffmpeg")
        if not ff:
            print("！PATH 里没找到 ffmpeg：打出来的 exe 要靠系统里装好的 ffmpeg 才能录")
        else:
            mb = Path(ff).stat().st_size / 1024 / 1024
            print(f"   打进 exe：ffmpeg.exe（{mb:.0f} MB）← {ff}")
            bundled.append(("ffmpeg.exe", ff, mb))
            if a.with_ffprobe:
                fp = shutil.which("ffprobe")
                if fp:
                    mb2 = Path(fp).stat().st_size / 1024 / 1024
                    print(f"   打进 exe：ffprobe.exe（{mb2:.0f} MB）")
                    bundled.append(("ffprobe.exe", fp, mb2))

    args = [
        str(py), "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", a.name,
        # bilibili_api 的接口配置是 data/api/*.json，不 collect 就会漏，
        # 表现是「界面能开，一投稿就报错」
        "--collect-all", "bilibili_api",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "qrcode_terminal",
        "--paths", str(HERE),
    ]
    for _name, _path, _mb in bundled:
        args += ["--add-binary", f"{_path};."]
    args.append("--console" if a.console else "--windowed")
    args.append(str(HERE / ENTRY))

    print("开始打包（第一次要一两分钟）…")
    r = subprocess.run(args, cwd=str(HERE))
    if r.returncode != 0:
        sys.exit(f"打包失败，退出码 {r.returncode}")

    exe = HERE / "dist" / f"{a.name}.exe"
    if not exe.is_file():
        sys.exit("打包好像成功了，但没找到 exe")

    print(f"\n✓ 生成 {exe}  （{exe.stat().st_size / 1024 / 1024:.1f} MB）")

    # 冒烟：跑 exe 的自检（能验证依赖和配置都正常，而不只是「窗口能开」）
    print("\n跑一遍 exe 自检 …")
    try:
        # 注意：exe 里 print 出来的中文默认可能是 GBK（跟控制台代码页有关），
        # 直接用 utf-8 解会 UnicodeDecodeError。强制它输出 utf-8，再兜一层。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([str(exe), "--selftest"], capture_output=True,
                           env=env, timeout=420)
        out = _decode(r.stdout)
        print(out)
        if r.returncode != 0:
            print(_decode(r.stderr))
            sys.exit("exe 自检没通过")
    except Exception as e:
        print(f"（自检没能跑起来：{e}）")

    print("把 exe 放到项目目录里双击即可。它读的是 **exe 同目录** 的 "
          "bili.toml / cookie.txt / bili_credential.json，")
    print("录制产物默认也写到 exe 同目录的 recordings/。")


if __name__ == "__main__":
    main()
