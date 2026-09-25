# 抖音直播录制 → B 站自动投稿

拉取抖音直播间的视频流，交给 ffmpeg **原样封装成 TS**（`-c copy`，不解码不重编码），
**录完自动投稿到 B 站**，封面统一取视频第一帧（不管合集）。

只做两件事：**录制** 和 **上传**。不含弹幕 / 礼物。

---

## 两种用法

| 用法 | 适合谁 | 入口 |
|---|---|---|
| **图形界面（推荐）** | 点开就用 | `RecorderUploader.exe` 或 `python record_gui.py` |
| 命令行 | 挂后台、写脚本 | `python record.py <房间号>` |

---

## 需要什么

**用打包好的 exe**：什么都不用装 —— **ffmpeg 已经打进 exe 里了**，拷到哪都能用。

**从源码跑**：

- **Python 3.11+**（配置用 `tomllib` 读，3.11 起才有）
- **ffmpeg** 在 PATH 里（或 `--ffmpeg` 指定路径）
- 装依赖：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 一、图形界面

双击 `RecorderUploader.exe`（或 `python record_gui.py`）。

**可以多个主播同时录**：主播列表里填几个就录几个，「全部开始」一次全起，
一人一个后台线程，互不影响；日志每行开头带 `[房间号]`，分得清是谁的。
每行能单独设清晰度，分段时间 / 时长 / 目录 / 没开播时的行为这些是所有人共用的。

**录制中随时增删主播**：

- 「+ 添加主播」随时能点，新加的行填上房间号点它的「开始」就录上了，其它主播照常；
- 某一行点「删」只停掉并移除这一路，**其它主播继续录**；
- 正在跑的那一行会锁住房间号和清晰度（改了也没用），其它行随便改。

- **分段一录完就上传**：一段录完立刻交给后台上传队列，录制线程接着录下一段，
  不用等整场直播结束。**上传进度显示在状态栏**（`后台上传 42%`），不刷日志。
- **下播后自动进入监控状态**：主播下播了不停，每 30 秒查一次，开播了自动接着录下一场，
  适合长期值守。指定了「录制时长」的是一次性任务，录满就停（不监控）。
- **没开播时默认一直等，每 30 秒重试一次拉流**，直到开播或你点停止。
  也可以改成「最多等 N 分钟」或「没开播就直接结束」。
- **B 站扫码登录**：点「B 站扫码登录」，二维码直接显示在界面里，手机扫码确认即可，
  不用命令行。
- **停止**：优雅收尾 —— 给 ffmpeg 发中断、把当前 TS 写完整，然后照常投稿。
- 设置自动记住，存在 exe 同目录的 `record_gui.json`。
- 日志同时写一份到 exe 同目录的 `logs/gui.log`：界面起不来时把这个文件发出来就能定位。

> exe 读的是 **exe 同目录** 的 `bili.toml` / `cookie.txt` / `bili_credential.json`，
> 录制产物默认也写到 exe 同目录的 `recordings/`。所以把 exe 放进项目目录里用最省事。

### 自己打包成 exe

```bash
python build_record_exe.py            # 或双击 build_record_exe.bat
```

产物 `dist/RecorderUploader.exe`（单文件，**ffmpeg 已打进去**，约 60MB）。

- 打包脚本会自动找 PATH 里的 ffmpeg 并 `--add-binary` 进去；运行时优先用
  exe 里那份（`find_ffmpeg()` 的优先级：手动指定 > exe 内置 > PATH）。
  机器上没装 ffmpeg 也能打，只是打出来的 exe 录不了，得自己装。
- 不想打进去用 `--no-ffmpeg`；想连 ffprobe 一起打用 `--with-ffprobe`（多 80MB 左右）。

打包要求解释器**带 tkinter** —— 托管 Python 3.13 没有，所以脚本会在候选里自动挑一个
「tkinter / PyInstaller / bilibili_api 都齐」的（也可以用 `--python` 指定）。

打完后脚本会跑一遍 **exe 自检**（`RecorderUploader.exe --selftest`），把依赖、
bilibili_api 的接口配置、HTTP 请求客户端都验一遍，还会**真的执行一次内置的
`ffmpeg -version`** —— 光看「窗口能不能开」验证不了投稿和录制那两半。

---

## 二、录制（命令行）

```bash
# 一直录到 Ctrl+C（默认每 2 小时自动切一段）
python record.py 805104115974

# 多个主播同时录（填几个都行）
python record.py 805104115974 224951713326 yall1102

# 录 5 分钟自动停
python record.py 805104115974 --duration 300

# 没开播就一直守着，每 30 秒拉一次流（默认行为）
python record.py 805104115974

# 没开播就直接退出，不守着等
python record.py 805104115974 --no-wait

# 只看这个直播间有哪些清晰度
python record.py 805104115974 --list

# 选清晰度 / 换协议 / 指定目录
python record.py 805104115974 --quality SD1
python record.py 805104115974 --protocol hls
python record.py 805104115974 --dir D:\录播
```

也可以双击 **`record.bat`**，按提示填房间号和时长。

| 参数 | 说明 |
|---|---|
| `--quality` | `FULL_HD1`(默认，原画) / `ORIGIN` / `HD1` / `SD1` / `SD2` / `LD` / `auto` |
| `--protocol` | `flv`(默认，直连最稳) / `hls`（拉不到会自动回退 flv） |
| `--duration` | 录制秒数，`0` = 一直录到 Ctrl+C |
| `--segment` | 每多少秒切一段，**默认 7200（2 小时）**，`0` = 不分段 |
| `--wait` | 没开播时最多等多少秒。**默认 0 = 一直等**（每 30 秒重试一次） |
| `--no-monitor` | 下播后就退出。默认：没指定时长时，下播后每 30 秒查一次，开播自动接着录 |
| `--no-wait` | 没开播就立刻退出 |
| `--dir` | 输出目录，默认 `recordings/` |
| `--name` | 文件名前缀，默认用主播昵称 |
| `--ffmpeg` | ffmpeg 路径，默认用 PATH 里的 |
| `--no-reconnect` | 断流后不自动重连 |
| `--list` | 只列出可用清晰度然后退出 |
| `--gui` | 打开图形界面 |

### 文件怎么命名

```
recordings/
  李大需_20260922_200000_FULL_HD1.ts        ← 第 1 段（名字里是这段的开始时间）
  李大需_20260922_220000_FULL_HD1.ts        ← 第 2 段（新段重新起名）
  李大需_20260922_220000_FULL_HD1_part2.ts  ← 第 2 段中途断流重连，接在后面
```

- **断流会自动重连**，重连后写 `_part2.ts`，不覆盖前面的。
- 切到新的一段时，用**新段的开始时间**重新起名，所以文件名本身就能看出分段。
- 拉流地址带 `expire`/`sign` 会失效，所以长录制用的是「取地址 → 录 → 断了再取新地址」的循环；
  切段时也会顺手重取一次。

---

## 三、录完自动投稿到 B 站

录制结束（直播结束 / 到时长 / Ctrl+C / 界面点停止）后自动投稿。配置在 **`bili.toml`**。

### 一次性设置：扫码登录

界面里点「B 站扫码登录」；命令行是：

```bash
python record.py --bili-login
```

登录态存到 `bili_credential.json`，之后一直复用。
**没登录就不会投稿**（跳过并提示，不影响录制）。
界面右上角会显示实测出来的登录状态（`正常 · 昵称` / `已失效，点右边「B 站扫码登录」重登`）；
上传前还会再用 `nav` 接口实测一次 —— 见下面「不管合集」里那段说明。

### 分段录完就上传（不等直播结束）

一段录完的**当下**就交给后台上传队列，录制立刻接着录下一段 —— 不用等整场结束。
上传是**后台串行**的：谁的段先录完谁先传，不会阻塞任何一路录制。

这一点很关键：投稿失败要退避重试、还要防风控节流，如果卡在录制线程里同步做，
等于把录制挂起 7 分钟。后台化之后完全不受影响。

### 每 2 小时切一段，每段投成独立稿件

默认 `--segment 7200`，于是 6 小时的直播会变成 3 个 B 站稿件，标题里带**该段自己的开始时间**：

```
李大需 直播回放 2026-09-22 20:00     ← 第 1 段
李大需 直播回放 2026-09-22 22:00     ← 第 2 段
李大需 直播回放 2026-09-23 00:00     ← 第 3 段
```

这同时也绕开了 B 站「单文件 ≤4GB、时长 <5 小时」的限制。

标题模板在 `bili.toml` 里，默认 `"{anchor} 直播回放 {date} {time}"`：

| 占位符 | 含义 |
|---|---|
| `{anchor}` | 主播昵称 |
| `{title}` | 直播间标题 |
| `{date}` `{time}` `{datetime}` | **这一段录制的开始时间**（不是投稿时间） |
| `{quality}` `{room_id}` | 清晰度 / 房间号 |
| `{seg}` `{segments}` | 这是第几段 / 总段数 |

> ⚠️ **别把 `{time}` 去掉**。B 站不接受短时间内重复标题，同一天的多段只写 `{date}`
> 会被拒（报「短时间内标题不能相同」）。

### 不管合集

投稿只管把视频投上去，**不自动归到合集**。要归档到合集，事后跑 `bili_season.py`：

```bash
python bili_season.py add --season 合集名 --latest     # 把最新稿件放进指定合集
```

- **投稿本身失败（比如 406 风控）有兜底**：失败的段会记到 exe 同目录的
  `bili_failed_uploads.json`（含原始文件路径、开始时间和失败原因），点界面上的
  「**补传**」或 `python record.py --bili-retry` 就按原参数重投；成功自动划掉，
  文件不在本地会跳过。节流（防 406）和进度显示在补传时照常生效。
- **上传前会实测一次 B 站登录态**（打 `nav` 接口，字段齐全 ≠ cookie 还有效）。
  失效时不会白传一场：当场的段直接记成「待补传」，重新登录后点「补传」即可。
  界面右上角会显示登录状态（正常 · 昵称 / 已失效）。

### 封面统一取第一帧

`cover_position = 0` 就是**视频第一帧**（默认）。想换画面填秒数即可。
B 站要求封面必填且不接受空字符串，所以这帧是必须抽的；ffmpeg 抽不出来会退回 opencv。

### TS 直接上传

`container = 'ts'`（默认）：录好的 `.ts` 直接投给 B 站，**由 B 站云端转码**，本地不做任何处理。
想先在本地无损转封装成 MP4 再传，就设 `container = 'mp4'`（`ffmpeg -c copy`，同样不重编码）。

### 命令行开关

| 参数 | 说明 |
|---|---|
| `--segment N` | 切段长度，默认 `7200`；`0` = 不分段 |
| `--no-bili` | 本次录制不投稿 |
| `--bili` | 本次强制投稿（配置里 `enabled = false` 时也能压过） |
| `--bili-config PATH` | 用别的配置文件 |
| `--bili-login` | 只做扫码登录，设置完就退出 |

---

## 四、文件说明

| 文件 | 说明 |
|---|---|
| `record_gui.py` | 图形界面（tkinter） |
| `build_record_exe.py` / `.bat` | 把界面打包成单文件 exe |
| `record.py` | 录制 + 投稿的核心逻辑：`run_recording()`（单个主播，界面和命令行共用）、
              `UploadWorker`（后台上传队列）、`record_rooms()`（多主播并发） |
| `record.bat` | 命令行双击入口 |
| `douyin_api.py` | 查开播状态 / 房间信息 / 拉流地址；`parse_rid` 解析房间号 |
| `cookie_store.py` | `cookie.txt` 的读写（整串 / 多行 / JSON / `#` 注释都认） |
| `bili_uploader.py` | 投稿：扫码登录（含 cookie 实测）/ 首帧封面 / TS 直传 / 多分P / 投稿出问题可补传 |
| `bili.toml` | 投稿配置（标题/简介模板、分区、标签、节流间隔） |
| `vendor/ab_sign.py` | 纯 Python 的 a_bogus 签名实现（见文末许可） |
| `recordings/` | 录制输出目录（**不进仓库**） |
| `cookie.txt` | 抖音 Cookie，自己填（**不进仓库**） |
| `bili_credential.json` | B 站登录态（**不进仓库**） |
| `record_gui.json` | 界面记住的设置（**不进仓库**） |

### 关于 cookie.txt

拉流接口会带上它，**配了更稳**（不带也能用，但抖音偶发风控时更容易查不到房间信息）。
浏览器打开 `live.douyin.com` → F12 → Network → 任选一条请求 → 复制 Request Headers 里的
`cookie:` 整段 → 粘贴到同目录 `cookie.txt`。

> Cookie 是你的登录凭证，**别提交到仓库、别发给别人**（`.gitignore` 里已经排除了）。

---

## 五、注意

- **合规**：本工具仅用于学习研究，请遵守抖音与哔哩哔哩的用户协议。
  别用单 IP 高频狂连，并发房间数建议控制在个位数。
- 抖音偶发风控时可能查不到房间信息；「一直等」模式会每 30 秒自动重试，不用管。
- **投稿报 406 是 B 站的风控**（短时间投稿过多触发，HTTP 406），不是程序坏了：
  浏览器打开 member.bilibili.com/platform/upload/video/frame 手动投一个、过一次
  人机验证即可解除；风控有第二阶段，触发了大约 1 小时自动解除。
  `bili.toml` 里的 `[bili] min_interval` 可以设两次投稿的最小间隔
  （多主播同时录、或分段很短时建议设 300 秒），就是为了少触发这个。
- 稿件提交到 B 站后要过审核才公开。
- 小于 `min_size_mb`（默认 1MB）的片段会被跳过，避免把空文件传上去。
- 某一段投稿失败不影响其它段，日志里会指出是哪一段。

---

## 六、第三方代码与许可

- `vendor/ab_sign.py` —— a_bogus 的纯 Python 实现，来自
  [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)，**MIT**，
  许可证原文见 `vendor/LICENSE-ab_sign.txt`。

本仓库只包含录制所需的部分，所以没有引入其他第三方签名脚本 / protobuf 定义。
若你要在本项目里重新加入弹幕相关代码，注意那些来源的许可（例如
[saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) 是 **AGPL-3.0**）。
