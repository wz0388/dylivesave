# 抖音直播录制 → B 站自动投稿

拉取抖音直播间的视频流，交给 ffmpeg **原样封装成 TS**（`-c copy`，不解码不重编码），
录完**自动投稿到 B 站**，封面统一取视频第一帧。

投稿粒度按**场次**走：**同一场直播**按时间切出来的各段（含断流重连的 `_partN`）
投成**同一个 B 站视频的 P1/P2/P3…**；**关播再开播**算新的一场，**投成新视频**。

只做两件事：**录制** 和 **投稿**。不含弹幕 / 礼物。

单文件 exe，**ffmpeg 已打包在内**，拷到哪都能用。许可说明见 **[第六节](#六第三方组件与许可)**。

---

## 两种用法

| 用法 | 适合谁 | 入口 |
|---|---|---|
| **图形界面（推荐）** | 点开就用 | `RecorderUploader.exe` 或 `python record_gui.py` |
| 命令行 | 挂后台、写脚本 | `python record.py <房间号>` |

---

## 需要什么

**用打包好的 exe**：什么都不用装 —— ffmpeg 已经在里面了。

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

### 主播列表：可以多个同时录

主播列表里填几个就录几个，「全部开始」一次全起，一人一个后台线程，互不影响。
日志每行开头带 `[房间号]`，分得清是谁的。

**每行两样东西是这一路自己的**：

| 行内控件 | 说明 |
|---|---|
| 清晰度 | 这一路拉什么清晰度 |
| 分段时间 | 这一路自己切多长：`30 分钟` / `60 分钟` / `120 分钟` / `不分段` |

「公共设置」里的**「分段时间（默认）」只是新增一行时的默认值** —— 改它不会动已经在列表里的行，
想改某一路就改那一行自己的下拉框（同一场直播的这几段会投成一个视频的 P1/P2…，见第三节）。

录制时长 / 输出目录 / 没开播时的行为仍然所有人共用。

**录制中随时增删主播：**

- 「+ 添加主播」随时能点，新行填上房间号、点它自己的「开始」就录上了，其它主播照常；
- 某一行点「删」只停掉并移除这一路，**其它主播继续录**；
- 正在跑的那一行会锁住房间号和清晰度（改了没用），其它行随便改。

### 录制的行为

- **分段一录完就上传**：一段录完立刻交给后台上传队列，录制线程接着录下一段，
  不用等整场直播结束。上传是**后台串行**的，不阻塞任何一路录制。
  同一场直播的各段会**追加成同一个视频的分P**（第 1 段建稿，后面每段传完就编辑稿件加分P），
  下播后再开播才另起一个视频。
- **下播后自动进入监控状态**：主播下播了不停，每 30 秒查一次，开播了自动接着录下一场，
  适合长期值守。指定了「录制时长」的是一次性任务，录满就停（不监控）。
- **没开播时默认一直等**，每 30 秒重试一次拉流，直到开播或你点停止。
  也可以改成「最多等 N 分钟」或「没开播就直接结束」。
- **停止**：优雅收尾 —— 给 ffmpeg 发中断、把当前 TS 写完整，然后照常投稿。

### 日志分两个分页

| 分页 | 内容 |
|---|---|
| **录制日志** | 录制相关的一切（开播检测、拉流、分段、断流重连、停止…） |
| **上传** | 每段一张进度表（段号 / 主播 / 状态 / 进度）+ 投稿专属日志 |

- 「上传」页的进度表显示 **`12%（25/205 块）`** —— B 站固定 10MB 一块，
  2GB 的录播有 205 块，只看百分比会长时间停在 0%，带上块数从第一块就看得出在动。
- 录制中每 15 秒一行进度，带上**体积和时长**：

  ```
  [20:31:05] [12345]   已写 3.20 GB，已录 01:23:45
  ```

  每个直播间这一行**只保留一条**：新的把旧的顶掉（控制台不会每 15 秒刷一行），
  但 `logs/gui.log` 里**每一次采样都留着** —— 包括等开播时每 30 秒那行重试日志，
  事后要查完整轨迹就看这个文件。
- 投稿失败的段在表里标 `✗ 失败（可补传）`；点「**补传**」按原参数重投。
  重启后启动时也会把 `bili_failed_uploads.json` 里没补上的段填进来。
- **所有日志行都带 `[HH:MM:SS]` 时间戳**；两个分页各写各的，同时也写一份到
  `logs/gui.log`（界面起不来时把这个文件发出来就能定位）。
- 状态栏显示 `录制中：2 路（后台上传 42%（86/205 块））` / `录制结束，等后台上传：…`。

### B 站扫码登录

点「B 站扫码登录」，二维码直接显示在窗口里，手机扫码确认即可，不用命令行。

登录状态显示在界面上，而且是**实测**出来的（不是只看文件里有没有字段）：
`正常 · 昵称` 或 `已失效，点右边「B 站扫码登录」重登`。

### 设置

自动记住，存在 exe 同目录的 `record_gui.json`。

一个例外：**「上传成功后删除本地视频」这个勾选写的是 `bili.toml`**
（`delete_after_upload`），因为它是投稿行为，命令行也要认同一个开关。详见第三节。

> exe 读的是 **exe 同目录** 的 `bili.toml` / `cookie.txt` / `bili_credential.json`，
> 录制产物默认也写到 exe 同目录的 `recordings/`。所以把 exe 放进项目目录里用最省事。

### 自己打包成 exe

```bash
python build_record_exe.py            # 或双击 build_record_exe.bat
```

产物 `dist/RecorderUploader.exe`（单文件，**ffmpeg 已打进去**，约 125MB）。

- 打包脚本自动找 PATH 里的 ffmpeg 并 `--add-binary` 进去；运行时优先用 exe 里那份
  （`find_ffmpeg()` 优先级：手动指定 > exe 内置 > PATH）。
  机器上没装 ffmpeg 也能打，只是打出来的 exe 录不了，得自己装。
- 不想打进去用 `--no-ffmpeg`；想连 ffprobe 一起打用 `--with-ffprobe`（多 80MB 左右）。
- 打包要求解释器**带 tkinter** —— 托管 Python 3.13 没有，脚本会自动挑一个
  「tkinter / PyInstaller / bilibili_api 都齐」的（也可以用 `--python` 指定）。
- 打完后会跑一遍 **exe 自检**（`RecorderUploader.exe --selftest`）：依赖、
  bilibili_api 的接口配置、HTTP 请求客户端、cookie 名映射，
  还会**真的执行一次内置的 `ffmpeg -version`** —— 光看「窗口能不能开」验证不了录制和投稿那两半。
- 报 `The 'typing' package is an obsolete backport ... incompatible with PyInstaller`
  是系统 Python 里装了过时的 `typing` 回填包（PyInstaller 不认）。别去动全局环境，
  建个干净的 venv 打包就行：

  ```bat
  set BUILD_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\pyinstaller-build312\Scripts\python.exe
  python -m venv %USERPROFILE%\.workbuddy\binaries\python\envs\pyinstaller-build312
  %BUILD_PY% -m pip install -r requirements.txt pyinstaller qrcode pillow
  python build_record_exe.py --python %BUILD_PY%
  ```

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

登录态存到 `bili_credential.json`，之后一直复用。**没登录就不会投稿**（跳过并提示，不影响录制）。

> **上传前一定实测一次登录态。** 只检查「字段在不在」是不够的：cookie 过期时字段一个不少、
> 看着完全正常，等你传完几 GB 才在最后一步炸。所以投稿前会打一次 `nav` 接口，
> 失效就当场停下、把这一段记成「待补传」，重新登录后点「补传」即可 ——
> 不会白传一场，也不会把段丢掉。

### 分段录完就上传（不等直播结束）

一段录完的**当下**就交给后台上传队列，录制立刻接着录下一段。
后台队列是**串行**的：谁的段先录完谁先传。

这一点很关键：投稿失败要退避重试、还要防风控节流，如果卡在录制线程里同步做，
等于把录制挂起好几分钟。后台化之后完全不受影响。

### 一场直播 = 一个视频，分段 = 这个视频的分P

默认 `--segment 7200`（每 2 小时切一段）。**同一场直播**（一次连续开播）切出来的各段，
投成**同一个 B 站视频**的分P：

```
李大需 直播回放 2026-09-22 20:00        ← 只有这一个视频
  P1  20:00-22:00
  P2  22:00-00:00
  P3  00:00-02:00
```

分P 的名字默认就是**这一段的开始时间-结束时间**（`part_title_template = "{time}-{end}"`），
结束时间按文件实际时长算出来，不是按分段设置硬加的。B 站播放器自己会在旁边标 P1/P2，
所以模板里不用再写「第 N 段」。

- **第 1 段**用投稿接口建一个新视频（标题/封面/分区/标签都按 `bili.toml`）；
- **后面每段**传完自己的文件，就把「原分P + 新分P」整体重交一遍 ——
  B 站没有「给已有稿件加一个分P」的接口，只能走编辑接口 `archive/edit`。
  标题/简介/分区/标签/封面都按现状原样带上，**不改这些字段，只加分P**。
- 编辑会让稿件**重新送审**，所以新分P 要等审核通过才在播放页出现（期间视频本身还在）。
- 断流重连产生的 `_part2.ts` 也是这一段的连续分P（一段两个文件 = 两个 P）。

**关播再开播就是新的一场**，投成**新视频** —— 同一个主播一晚上开播三次，
就是三个视频，每个视频里各有自己的 P1/P2…。

「一场」是录制循环判定的：一次连续录制（开播 → 下播/停止）算一场；
下播后值守模式每 30 秒探一次，探到开播就重新进一次录制循环，于是开新的一场。
中途断流只要还能重新拉到流，就还是同一场，继续录、继续往同一个视频里加分P。

判定依据记在 exe 同目录的 `bili_sessions.json`（场次 → 稿件 bvid），
所以**重启后补传**也还知道该往哪个视频里加分P。

> 不想这么投？`bili.toml` 里 `multi_part = false` 就退回老行为：**每段一个独立稿件**。

标题模板在 `bili.toml` 里，默认 `"{anchor} 直播回放 {date} {time}"`：

| 占位符 | 含义 |
|---|---|
| `{anchor}` | 主播昵称 |
| `{title}` | 直播间标题 |
| `{date}` `{time}` `{datetime}` | **这一场直播的开始时间**（不是投稿时间，也不是每段的时间） |
| `{quality}` `{room_id}` | 清晰度 / 房间号 |
| `{seg}` `{segments}` | 这是本次录制交出去的第几段 / 总段数（分段即传时总段数未知，填 0） |
| `{part}` `{parts}` | 这一段从第几个分P 起 / 这一段有几个分P |

分P 自己的名字用 `part_title_template`（默认 `"{time}-{end}"`，最多 80 字）：

| 占位符 | 含义 |
|---|---|
| `{time}` / `{start}` | 这一段的开始时间 `20:00` |
| `{end}` | 这一段的结束时间 `22:00`（= 开始 + 文件实际时长） |
| `{dur}` | 这一段多长 `02:00:00` |
| `{part}` | 第几个分P（想写「第 3 段」就把它放回模板里） |

所以播放器的分P 列表里看到的就是 `20:00-22:00` / `22:00-00:00` / `00:00-02:00`。
时长探不出来（文件坏了、ffmpeg 抽不出）时会退化成只写开始时间 `20:00`，不会写成 `20:00-`。

> 用 `multi_part = false` 时**别把 `{time}` 去掉**。B 站不接受短时间内重复标题，
> 同一天的多段只写 `{date}` 会被拒（报「短时间内标题不能相同」）。

### 投稿失败可以补传

投稿失败（比如 406 风控）的段会记到 exe 同目录的 `bili_failed_uploads.json`
（含原始文件路径、段开始时间、段落在哪一场 / 第几个分P、失败原因），两种方式补：

- 界面上点「**补传**」
- 命令行 `python record.py --bili-retry`

补传走同一条后台队列，**按原参数**重投；成功自动从清单划掉，文件不在本地会跳过。
节流（防 406）和进度显示照常生效。点「补传」时会**重新实测登录态** ——
你如果是刚登录完来点补传，不会因为上次的结论被挡在外面。

同一场直播的多个分P 会**按 P 顺序**补：先建稿（P1），再往上面挂 P2/P3。
如果前面的分P 还没补成功，后面的会留在清单里等 —— 不会自己另起一个视频。

> 追加分P 走的是「编辑稿件」接口。B 站偶尔会拒（稿件状态还在流转、接口抖动等），
> 日志里会写清原因（`追加分P失败 …`），这一段会自动进待补传清单，
> 过一会儿点「补传」就行。文件都还在本地，不会白传。

### 上传成功后删掉本地视频（可选）

录播很占硬盘，投完之后本地那份就没用了。`bili.toml` 里：

```toml
delete_after_upload = false    # true = 投稿成功后删除本地录制文件
```

界面上也有同一个开关：**「B 站投稿」那一栏的「上传成功后删除本地视频」**，
勾选/取消会**直接写回 `bili.toml`**（GUI 和命令行共用同一份配置，不会出现两边不一致），
正在跑的上传队列下一次上传就按新值执行。

删除是**保守**的，只在这几种情况下动手：

- **确认投稿成功之后**才删（拿到 bvid）。上传失败、406 风控、登录态失效 —— 一个都不删，
  它们要留着「补传」；
- 只删**这次真正传上去的那几个文件**。被 `min_size_mb` 跳过的空文件、超过 4GB 没传上去的，
  不碰；
- 追加分P 时删的是该段自己的文件，不会碰同一个稿件里已经删掉/还在的旧段；
- 删了只记一行日志：`[i] 已删除本地文件（1823 MB，上传成功）：李大需_20260922_200000_FULL_HD1.ts`。

> ⚠️ 删了找不回来。B 站上的稿件在审核期间不公开，如果你需要本地留档
> （或者怕审核不过），就保持 `false`。

### 不管合集

投稿只管把视频投上去，**不自动归到合集**。要归档到合集，事后跑 `bili_season.py`：

```bash
python bili_season.py add --season 合集名 --latest     # 把最新稿件放进指定合集
```

### 封面统一取第一帧

`cover_position = 0` 就是**视频第一帧**（默认），想换画面填秒数即可。
B 站要求封面必填且不接受空字符串，所以这帧是必须抽的；ffmpeg 抽不出来会退回 opencv。

### TS 直接上传

`container = 'ts'`（默认）：录好的 `.ts` 直接投给 B 站，**由 B 站云端转码**，本地不做处理。
想先在本地无损转封装成 MP4 再传，就设 `container = 'mp4'`（`ffmpeg -c copy`，同样不重编码）。

### 命令行开关

| 参数 | 说明 |
|---|---|
| `--segment N` | 切段长度，默认 `7200`；`0` = 不分段 |
| `--no-bili` | 本次录制不投稿 |
| `--bili` | 本次强制投稿（配置里 `enabled = false` 时也能压过） |
| `--bili-config PATH` | 用别的配置文件 |
| `--bili-login` | 只做扫码登录，设置完就退出 |
| `--bili-retry` | 只补传投稿失败的段，然后退出（不录制） |

---

## 四、文件说明

| 文件 | 说明 |
|---|---|
| `record_gui.py` | 图形界面（tkinter） |
| `build_record_exe.py` / `.bat` | 把界面打包成单文件 exe |
| `record.py` | 录制 + 投稿的核心逻辑：`run_recording()`（单个主播，界面和命令行共用）、`UploadWorker`（后台上传队列）、`record_rooms()`（多主播并发） |
| `record.bat` | 命令行双击入口 |
| `douyin_api.py` | 查开播状态 / 房间信息 / 拉流地址；`parse_rid` 解析房间号 |
| `cookie_store.py` | `cookie.txt` 的读写（整串 / 多行 / JSON / `#` 注释都认） |
| `bili_uploader.py` | 投稿：扫码登录（含 cookie 实测）/ 首帧封面 / TS 直传 / 一场直播追加成同一稿件的多分P / 失败可补传 |
| `bili.toml` | 投稿配置（标题简介模板、分区、标签、节流间隔） |
| `vendor/ab_sign.py` | 纯 Python 的 a_bogus 签名实现（MIT，见第六节） |
| `vendor/ffmpeg/` | ffmpeg 的 GPLv3 许可原文与构建说明（见第六节） |
| `recordings/` | 录制输出目录（**不进仓库**） |
| `cookie.txt` | 抖音 Cookie，自己填（**不进仓库**） |
| `bili_credential.json` | B 站登录态（**不进仓库**） |
| `record_gui.json` / `logs/` | 界面设置与日志（**不进仓库**） |
| `bili_failed_uploads.json` | 投稿失败待补传的清单（**不进仓库**） |
| `bili_sessions.json` | 场次 → 稿件（bvid）对照表：同一场直播的分片靠它追加成同一个视频的分P（**不进仓库**） |
| `tests/` | 分P 归属 / 删除开关 / 进度日志 / 每行分段时间的离线自测（不联网、不真传）：`tests/test_multipart.py`、`test_uploader_queue.py`、`test_delete_after_upload.py`、`test_live_log.py`、`test_row_segment.py` |

### 关于 cookie.txt

拉流接口会带上它，**配了更稳**（不带也能用，但抖音偶发风控时更容易查不到房间信息）。
浏览器打开 `live.douyin.com` → F12 → Network → 任选一条请求 → 复制 Request Headers 里的
`cookie:` 整段 → 粘贴到同目录 `cookie.txt`。

> Cookie 是你的登录凭证，**别提交到仓库、别发给别人**（`.gitignore` 里已经排除了）。

---

## 五、注意

- **合规**：本工具仅用于学习研究，请遵守抖音与哔哩哔哩的用户协议。
  别用单 IP 高频狂连，并发房间数建议控制在个位数。录播内容的版权归主播 / 平台，
  公开传播前请自行确认授权。
- 抖音偶发风控时可能查不到房间信息；「一直等」模式会每 30 秒自动重试，不用管。
- **投稿报 406 是 B 站的风控**（短时间投稿过多触发，HTTP 406），不是程序坏了：
  浏览器打开 member.bilibili.com/platform/upload/video/frame 手动投一个、过一次
  人机验证即可解除；风控有第二阶段，触发了大约 1 小时自动解除。
  `bili.toml` 里的 `bili.min_interval` 可以设两次投稿的最小间隔
  （多主播同时录、或分段很短时建议设 300 秒），就是为了少触发这个。
- 稿件提交到 B 站后要过审核才公开；**追加分P 会让稿件重新送审**，审核期间新分P 不显示。
- 小于 `min_size_mb`（默认 1MB）的片段会被跳过，避免把空文件传上去。
  跳过不会打乱分P 序号 —— 序号是按稿件里现有的分P 数往后排的。
- 某一段投稿失败不影响其它段，日志里会指出是哪一段，之后可以「补传」。
- B 站的上限是**单个文件** ≤4GB、时长 <5 小时，分段（默认 2 小时）就是为它留的余量。
  分P 数量按 B 站投稿页的上限算，一天播 24 小时也就 12 个 P，远没到。
- 一场直播的稿件是「第 1 段建稿时」定的标题/封面/分区/标签，之后不再改
  （追加分P 时原样带回去）；想改标题就去 B 站创作中心改，不影响后续追加。
- 「一场」按录制进程里的一次连续录制算：**程序重启后再录同一场直播，会另起一个视频**。
- `delete_after_upload = true` 之后本地文件**删了就没了**（不进回收站）。投稿失败、
  登录态失效、文件太大没传上去的都不会删，但已经传上去的那份确实会被删掉 ——
  需要本地留档就别开。

---

## 六、第三方组件与许可

### 本仓库自己的代码

**MIT**，见 [LICENSE](LICENSE)。

> ⚠️ 但**发布出去的成品并不只有 MIT 代码** —— 下面的组件里有两个是 GPL，
> 打包进 exe 之后整个发布物得按 GPL 走。详见「GPL 组件带来的义务」。

### 直接依赖

| 组件 | 版本 | 许可 | 用途 | 在 exe 里 |
|---|---|---|---|---|
| [FFmpeg](https://ffmpeg.org/) `essentials_build`（[gyan.dev](https://www.gyan.dev/ffmpeg/builds/)） | 7.1.1 | **GPL v3** | 录制封装 TS、抽封面帧、可选转 MP4 | ✅ 静态版，约 83MB |
| [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) | 17.4.2 | **GPL-3.0-or-later** | B 站投稿（`VideoUploader`） | ✅ |
| [requests](https://github.com/psf/requests) | 2.34 | Apache-2.0 | 抖音接口、登录、登录态检测 | ✅ |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | 0.16.3 | MIT | `bilibili-api` 的 HTTP 客户端 | ✅（内含 `libcurl-impersonate`，MIT） |
| [qrcode-terminal](https://pypi.org/project/qrcode-terminal/) | 0.8 | MIT | 终端扫码登录 | ✅ |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | 8.2 | BSD | 界面里把二维码画成图 | ✅ |
| [Pillow](https://python-pillow.org/) | 12.3 | MIT-CMU（PIL 许可） | 二维码渲染 | ✅ |
| [pycryptodomex](https://github.com/Legrandin/pycryptodome) | 3.23 | BSD / Public Domain | `bilibili-api` 依赖 | ✅ |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) / [lxml](https://lxml.de/) | 4.15 / 6.1 | MIT / BSD-3-Clause | `bilibili-api` 依赖 | ✅ |
| [websocket-client](https://github.com/websocket-client/websocket-client) | 1.7 | Apache-2.0 | `bilibili-api` 依赖 | ✅ |
| [yarl](https://github.com/aio-libs/yarl) / [multidict](https://github.com/aio-libs/multidict) | 1.25 / 6.9 | Apache-2.0 | `bilibili-api` 依赖 | ✅ |
| [certifi](https://github.com/certifi/python-certifi) | 2026.7 | MPL-2.0 | CA 证书 | ✅ |
| [urllib3](https://github.com/urllib3/urllib3) / [idna](https://github.com/kjd/idna) / [charset-normalizer](https://github.com/jawah/charset_normalizer) | 2.8 / 3.20 / 3.5 | MIT / BSD-3-Clause / MIT | 传递依赖 | ✅ |
| [python-dateutil](https://github.com/dateutil/dateutil) | 2.9 | BSD-3-Clause / Apache-2.0 双许可 | 传递依赖 | ✅ |
| [betterproto](https://github.com/danielgtaylor/python-betterproto) | 2.0.0b6 | MIT | `bilibili-api` 依赖 | ✅ |
| [PyInstaller](https://pyinstaller.org/) | 6.22 | GPLv2 **+ bootloader 例外** | 打包成单文件 exe | 只用了引导程序 |

### 源码引入的第三方代码

| 文件 | 来源 | 许可 |
|---|---|---|
| `vendor/ab_sign.py` | [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | **MIT**，原文见 `vendor/LICENSE-ab_sign.txt` |

本仓库只保留录制所需的部分：**没有**引入弹幕相关的签名脚本和 protobuf 定义
（那些来源里有 AGPL-3.0，例如
[saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher)）。
你要自己加回来时，注意它们各自的许可。

### GPL 组件带来的义务

`bilibili-api-python` 是 **GPL-3.0-or-later**；ffmpeg 这份构建是 **GPL v3**
（`--enable-gpl --enable-version3`，见 `vendor/ffmpeg/BUILD-README.txt`）。
两者都被打包进了 exe，所以 **release 里的 `RecorderUploader.exe` 整体按 GPL 分发**：

- **附许可原文**：本仓库 `LICENSE`（MIT）、`vendor/ffmpeg/LICENSE-GPLv3.txt`（GPLv3 全文）、
  `vendor/LICENSE-ab_sign.txt`（MIT）；release 页面也单独附了 GPLv3 全文。
- **提供源码**：本仓库就是全部源码；ffmpeg 的源码对应
  `https://github.com/FFmpeg/FFmpeg/commit/db69d06eee`（gyan.dev 构建说明里给的那个 commit）。
- 要**二次分发**（打包进自己的产品、放到别的下载站），请一并带上上面这些许可原文和源码链接。
- 只在本地自己用 / 学习，不受这些约束 —— 上面讲的是**分发**时的要求。

