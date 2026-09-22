# 抖音直播录制 → B 站自动投稿

拉取抖音直播间的视频流，交给 ffmpeg **原样封装成 TS**（`-c copy`，不解码不重编码），
**录完自动投稿到 B 站**，放进每个主播各自的合集，封面统一取视频第一帧。

只做两件事：**录制** 和 **上传**。不含弹幕 / 礼物 / 界面。

---

## 需要什么

- **Python 3.11+**（配置用 `tomllib` 读，3.11 起才有）
- **ffmpeg** 在 PATH 里（或 `--ffmpeg` 指定路径）
- 装依赖：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 一、录制

```bash
# 一直录到 Ctrl+C（默认每 2 小时自动切一段）
python record.py 805104115974

# 录 5 分钟自动停
python record.py 805104115974 --duration 300

# 主播还没开播？守着等（每 30 秒查一次，最多等 10 分钟）
python record.py 805104115974 --wait 600

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
| `--dir` | 输出目录，默认 `recordings/` |
| `--name` | 文件名前缀，默认用主播昵称 |
| `--ffmpeg` | ffmpeg 路径，默认用 PATH 里的 |
| `--wait` | 未开播时最多等多少秒 |
| `--no-reconnect` | 断流后不自动重连 |
| `--list` | 只列出可用清晰度然后退出 |

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

## 二、录完自动投稿到 B 站

录制结束（直播结束 / 到时长 / Ctrl+C）后自动投稿。配置在 **`bili.toml`**。

### 一次性设置：扫码登录

```bash
python record.py --bili-login
```

终端打印二维码 → B 站 App 扫码 → 手机确认。登录态存到 `bili_credential.json`，之后一直复用。
**没登录就不会投稿**（跳过并提示，不影响录制）。

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

### 每个主播一个合集

```toml
[season]
enabled = true
name_template = "{anchor}"     # {anchor}=主播昵称 → 每人一个同名合集
create_if_missing = true        # 合集不存在就自动新建，再放进去
desc_template = "{anchor} 的直播回放合集"

[season.anchors]                # 想给个别主播指定别的合集名，在这里覆盖
# "黄同学书屋" = "黄同学书屋的直播回放"
```

- 想让所有主播进同一个合集，就把 `name_template` 写成固定名字（`name_template = '直播回放'`）。
- 新建合集**需要封面**，直接用这次投稿的封面；B 站对新建合集有人工审核，不会立刻显示。
- **加合集失败不影响稿件本身** —— 稿件已经提交成功了，日志里会提示。

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
| `--bili-season NAME` | 本次投稿指定合集名（覆盖配置） |
| `--bili-login` | 只做扫码登录，设置完就退出 |

---

## 三、文件说明

| 文件 | 说明 |
|---|---|
| `record.py` | 录制 + 录完投稿的命令行入口 |
| `record.bat` | 双击入口（填房间号和时长即可） |
| `douyin_api.py` | 查开播状态 / 房间信息 / 拉流地址；`parse_rid` 解析房间号 |
| `cookie_store.py` | `cookie.txt` 的读写（支持整串 / 多行 / JSON / `#` 注释） |
| `bili_uploader.py` | 投稿到 B 站：登录 / 首帧封面 / TS 直传 / 多分P / 自动进合集 |
| `bili.toml` | B 站投稿配置（每个主播的合集名在这里配） |
| `vendor/ab_sign.py` | 纯 Python 的 a_bogus 签名实现（见文末许可） |
| `recordings/` | 录制输出目录（**不进仓库**） |
| `cookie.txt` | 抖音 Cookie，自己填（**不进仓库**） |
| `bili_credential.json` | B 站登录态，`--bili-login` 生成（**不进仓库**） |

### 关于 cookie.txt

拉流接口会带上它，**配了更稳**（不带也能用，但抖音偶发风控时更容易查不到房间信息）。
浏览器打开 `live.douyin.com` → F12 → Network → 任选一条请求 → 复制 Request Headers 里的
`cookie:` 整段 → 粘贴到同目录 `cookie.txt`。文件格式很宽松：一整串、多行、`{"cookie": "..."}`
的 JSON 都认，`#` 开头是注释。

> Cookie 是你的登录凭证，**别提交到仓库、别发给别人**（`.gitignore` 里已经排除了）。

---

## 四、注意

- **合规**：本工具仅用于学习研究，请遵守抖音与哔哩哔哩的用户协议。
  别用单 IP 高频狂连，并发房间数建议控制在个位数。
- 抖音偶发风控时可能查不到房间信息，重试即可，不会崩。
- 稿件提交到 B 站后要过审核才公开；审核期间加不进合集是正常的。
- 小于 `min_size_mb`（默认 1MB）的片段会被跳过，避免把空文件传上去。
- 某一段投稿失败不影响其它段，日志里会指出是哪一段。

---

## 五、第三方代码与许可

- `vendor/ab_sign.py` —— a_bogus 的纯 Python 实现，来自
  [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)，**MIT**，
  许可证原文见 `vendor/LICENSE-ab_sign.txt`。

本仓库只包含录制所需的部分，所以没有引入其他第三方签名脚本 / protobuf 定义。
若你要在本项目里重新加入弹幕相关代码，注意那些来源的许可（例如
[saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) 是 **AGPL-3.0**）。
