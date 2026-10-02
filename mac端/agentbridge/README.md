# AgentBridge —— 两台设备之间的 Agent 消息桥（v3）

Mac（192.168.10.153）当服务端，Windows 侧零安装（curl.exe / PowerShell 即可）。
v3 = v2 冻结基线 +「原始文件名回传、?after 过滤、Mac 侧三块 watcher」。

## 连接信息

| 项 | 值 |
|---|---|
| 地址 | `http://192.168.10.153:8899` |
| 鉴权头 | `X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN`（所有请求，含 GET） |
| 单条上限 | 256 MB |
| 队列上限 | 4 GB（超限 507） |
| 协议 | HTTP/1.1 keep-alive |
| 仅限 | 192.168.10.0/24 |

## 队列

| 队列 | 方向 | Windows 侧动作 |
|---|---|---|
| `mac2win` | Mac → Windows | 只读 |
| `win2mac` | Windows → Mac | 只写 |

Mac 真实路径：`~/AgentBridge/mac2win/`、`~/AgentBridge/win2mac/`

## API（冻结基线 + v3 只加不改）

```
GET  /api/health                    存活探测（含 max_bytes / quota）
GET  /api/<queue>                   列举
GET  /api/<queue>?since=<name>      增量列举（按文件名字典序）
GET  /api/<queue>?after=<epoch>     增量列举（按 mtime，推荐）
GET  /api/<queue>/<name>            读取原文（text/markdown + X-Sha256）
GET  /api/<queue>/<name>?raw=1      二进制直出（application/octet-stream
                                    + Content-Length + X-Sha256）
PUT  /api/<queue>/<name>            写入任意字节流 -> {ok,name,bytes,sha256}
                                    头: X-Orig-Name-B64（原名，base64(UTF-8)）
                                        X-Sha256（可选，发送端声明，不符即 409）
POST /api/<queue>                   自动命名发消息（body 必须是 --data-binary）
POST /api/<queue>/<name>/ack        标记已被取走，移入 _done/
POST /api/push/<name>               v2 直达推送：收到即当场落地，不进队列（见下）
GET  /api/push                      v2 探针：{"ok":true,"service":"agentbridge-push","port":8899}
```

列举返回的每条含 `name / size / mtime / mtime_ts`（`mtime_ts` 供 `?after` 用）。

`X-Orig-Name-B64` 在 PUT 时落到 `.meta/<name>.origname`，取件方 GET（text 与 raw）
时原样回传，ack 时随 `.sha256` 一起清理。

### since 的坑（两端都中过）

队列里有两套命名空间：普通消息 `<ts>_win.md` 以数字开头，`file_` / `xfer_`
以字母开头。字典序下 `2` < `f`，所以「所有消息」永远排在「所有载荷」之前：
游标一旦推进到某个 `file_` 名，之后时间更晚的消息会被 `since` 永久过滤掉，静默丢消息。

结论：**不要用 `since`**。要么全量列举（ack 后条目即离开列举，天然幂等），
要么改用 `?after=<上次最后一条的 mtime_ts>`。

## 命名约定

| 名称 | 含义 |
|---|---|
| `file_<YYYYMMDD-HHMMSS>_<原名>` | 文件载荷 |
| `xfer_<id>.json` | 多文件清单（仅多文件时发） |
| `xfer_<id>.fail.json` | 多文件失败回执 |
| `cb_<YYYYMMDD-HHMMSS-mmm>_<mac\|win>_text.txt` | 跨设备剪贴板：文本（UTF-8 无 BOM） |
| `cb_<YYYYMMDD-HHMMSS-mmm>_<mac\|win>_image.png` | 跨设备剪贴板：图片（PNG 原始字节） |
| `<YYYYMMDD-HHMMSS>_<mac\|win>.md` | 普通消息 |
| `_done/<name>` | 已被接收方取走（不在列举中） |
| `<name>.part` | 传输中，对 watcher 不可见 |

## 落地目录

| 端 | 收 | 发 |
|---|---|---|
| Mac | `~/Downloads/KEEPPER-Files/inbox/` | `~/Downloads/KEEPPER-Files/outbox/` |
| Windows | `D:\KEEPPER\_filebridge\inbox\` | `D:\KEEPPER\_filebridge\outbox\` |

Mac 侧文件互传（接收 / 发送 / 拖拽预传暂存）全部集中在 `~/Downloads/KEEPPER-Files/`：
`inbox/`（接收落地）、`outbox/`（投递发送，其下 `.sent/` 留痕）、`.dragstage/`（拖拽暂存）。
该目录是**临时中转站**：目录内所有文件满 **5 分钟**由 `bridge_server.py` 的 `files_cleaner`
自动删除；同名落地**直接覆盖**，不再产生 `(1)` 副本。消息通道（`mac2win`/`win2mac` 队列）不受影响。

## 超时与重试（实测标定）

Windows 侧实测 64 MiB 下载 1.545 s，吞吐 43.4 MB/s，sha256 一致。
按保守 5 MB/s 规划：`timeout = 30s + size_bytes / 5MB/s`，重试 3 次，退避 1/3/9 s。

## Mac 侧常驻进程

各服务由 launchd 托管（`~/Library/LaunchAgents/com.agentbridge.*.plist`）。
**`ProcessType` 必须是 `Interactive`**：用 `Background` 会被 App Nap 节流，
剪贴板写入从 ≈0.3 s 拖到 ≈12 s。

| 进程 | 作用 | 存活策略 | 日志 |
|---|---|---|---|
| `bridge_server.py` | 服务端（8899，`ThreadingHTTPServer`） | 常驻 | `bridge.log` |
| `clipgate.py` | 跨设备功能门控（剪贴板 + 拖拽，随 Deskflow 连接开关） | 常驻 | `clipgate.log` |
| `clipwatch.py`（内含 `clipwatch.js` / `clipset.js`） | 剪贴板出现文件/文本/图片 → 发往 Windows | **受 `clipgate` 门控** | `clipwatch.log` |
| `dragwatch.py`（内含 `dragwatch.js`） | 跨屏拖拽源端：观测 `NSDragPboard` → `drag_begin` + 预传载荷 | **受 `clipgate` 门控** | `dragwatch.log` |
| `dropwatch.py`（内含 `dropat.js` / `finderat.applescript` / `apppaste.js`） | 跨屏拖拽目标端：收 Windows `drop` → 读本机光标 → Finder 落盘 / 其它 App 剪贴板粘贴 | **受 `clipgate` 门控** | `dropwatch.log` |
| `outboxwatch.py` | `KEEPPER-Files/outbox` 写稳即发（发完移入 `.sent/`） | 常驻 | `outboxwatch.log` |
| `inboxwatch.py` | win2mac 载荷落地 `KEEPPER-Files/inbox` / 写剪贴板 → 写后核验 → 回执 → ack | 常驻 | `inboxwatch.log` |

### 剪贴板共享门控（`clipgate.py`）

跨设备剪贴板只在 **Deskflow 与 Windows 真正连上时**（`192.168.10.100:24800`
处于 `ESTABLISHED`，用 `lsof` 检测，每 2 s 一轮）才开启：

- **连上** → `inbox.conf.json` 的 `write_clipboard=true`（收方向）＋ 拉起 `clipwatch.py`（发方向）＋ `dragwatch.py`（拖拽源端）＋ `dropwatch.py`（拖拽目标端）；
- **断开** → `write_clipboard=false`（收方向拒收，对端收到 422）＋ 整组终止 `clipwatch.py` / `dragwatch.py` / `dropwatch.py` 及其 JXA 子进程。

文件 / 消息 / 8899 服务不受影响，始终常驻。`clipgate` 每轮幂等收敛，
外部误改 conf 或 `clipwatch` 意外退出都能自愈（非 `clipgate` 途径死亡留下的孤儿 JXA 也会在拉起前清掉）。

要点：
- 剪贴板读写一律走 JXA（`osascript -l JavaScript`），Mac 上 PyObjC 不可用。
- 变化判定用 `NSPasteboard.changeCount`，对应 Windows 的 `GetClipboardSequenceNumber`。
- 防回环：自己写入剪贴板前后把 `{pending,before,cc}` 记进 `.clip_self.json`，
  `clipwatch.js` 读到同值即跳过，不做内容比对。
- 目录两端各自打包、**两端一律不自动解包**（Mac 用 `ditto -c -k --sequesterRsrc --keepParent`）。
- 重名冲突：保留原名 + 序号插在扩展名前（`报告(1).pdf`）。
- `.part` 残留 24 h TTL，`_done/` 归档 7 天 TTL，服务端每小时扫一次。

### 开关：inboxwatch 写剪贴板

`~/AgentBridge/inbox.conf.json` 的 `write_clipboard`（每轮重读，改完即时生效）。**现由 `clipgate.py` 按 Deskflow 连接状态自动维护**（连上 `true` / 断开 `false`），通常无需手动改。

写入后会跑一次**写后核验**（`clipread.js` 读回当前剪贴板文件列表）：

- 12 s 窗口内每 1 s 复查一次，连续 6 次命中本批文件 → 判定「写入生效」；
- 三态判定：写入生效 / 写入被清掉 / 被用户其它内容覆盖；
- 回一条 `<ts>_mac-clipack.md` 到 `mac2win` 给对端对账（对端用 `-win-clipack.md`）；
- Mac 侧**只核验不重写** —— 回收源已根除，重写没有对手。

### 跨设备剪贴板协议 v1（与 Windows 侧冻结）

Deskflow 只做键鼠，剪贴板的三类内容（文件 / 文本 / 图片）走本通道，全自动双向。
**仅在 Deskflow 处于连接状态时启用**（见上文 `clipgate.py` 门控）。

| 内容 | 条目名 | body | 上限 |
|---|---|---|---|
| 文字 | `cb_<ts-mmm>_<mac\|win>_text.txt` | UTF-8 纯文本，无 BOM | 256 KB |
| 图片 | `cb_<ts-mmm>_<mac\|win>_image.png` | PNG 原始字节 | 32 MB |
| 文件 | `file_*` / `xfer_*.json` | 不变 | 256 MB |

命名正则（两端共用）：`^cb_(\d{8}-\d{6}-\d{3})_(win|mac)_(text|image)\.(txt|png)$`，毫秒固定 3 位。

要点：

- **取回必须 `?raw=1`**：明文形式服务端按 UTF-8 解码，二进制图片会被替换字符毁掉。
- 优先级 **files > image > text**：带文件的复制通常同时带一份文本表示，按文本发会丢文件语义。
- 超限 / 名字不认识的条目：**跳过且不 ack**，留在队列交给人工。
- 防回环两层：`clipset.js` 写剪贴板时把 `{cc, fp}` 记进 `.clip_self.json`（守护据 cc 跳过），
  再加一道**内容指纹**（`kind + ':' + sha1(原始字节)`）——剪贴板序号会被无关进程扰动，只看序号会误报。
- 单程延迟 0–2 s（两端 2 s 轮询），双向最坏约 4 s。
- **注意**：本机 shell 的 `LANG` 为空时 `pbcopy` 写中文会落成空内容（不是通道问题），
  自测请用 `osascript -e 'set the clipboard to "..."'` 或先 `export LANG=en_US.UTF-8`。

### 跨设备直达推送协议 v2（与 Windows 侧冻结）

v1 的延迟瓶颈在**接收方**：发送方本来就主动 PUT，但接收方只能 2 s 轮询自己的队列，
所以「写队列 → 对端轮询」天然带 0–2 s。v2 让发送方**直接推到对端**，对端收到即落地。

- 接收端：本机 `8899` 上加 `POST /api/push/<条目名>`（采纳对端建议，**不另起端口**）
- 发送端：`bridge_lib.put_bytes` 对 `mac2win` 方向**先直推对端 8900**，
  200 即完成、不写队列；连不上 / 非 200 → 退回本机 `mac2win` 队列（降级链）
- 名字 URL 编码；必需头 `X-Bridge-Token` / `Content-Length` / `X-Sha256`；可选 `X-Orig-Name-B64`
- 响应码：200 落地（`action` ∈ `clipboard|inbox|manifest|duplicate`）/
  400 载荷坏 / 401 鉴权 / 404 名字不认识（**非载荷名一律 404**，与对端语义对称）/
  413 超限 / 422 落地失败（撤销去重，允许重推）/ 500
- **白名单只对载荷生效**：`is_push_payload()`（`cb_`/`file_`/`xfer_`）才直推；
  普通消息 `<ts>_mac.md` / `<ts>_mac-clipack.md` 直接 PUT 队列，不发直推、不白吃 404。
  接收侧 `ingest.is_payload()` 是**同一个判断**，两处共用（避免以后加前缀漏改一处）。
- 硬要求：**幂等**（按条目名去重 `.push_dedupe.json`，重启后仍有效 → `action=duplicate`）；
  **非 200 一律 `Connection: close`**；上限分类（文本 256 KB / 图片 32 MB / 文件 256 MB）

落地逻辑抽到 `ingest.py`，由两条路径共用：`bridge_server.py` 的 `/api/push`（主路径、
当场落地）与 `inboxwatch.py` 的队列轮询（**兜底**）。`inboxwatch.py` 已瘦成
「轮询 → get_raw → ingest_entry → 按码 ack」，且**只处理载荷**（`cb_`/`xfer_`/`file_`），
普通消息 `<ts>_win.md` 留在队列里给人读、不进 `_done/`。

### 跨屏拖拽协议 v1（阶段1：Mac → Win，与 Windows 侧冻结）

目标：Mac Finder 拖文件 → 跨 Deskflow 屏 → Windows 松手 → 文件出现在「下载」。
键鼠由 Deskflow 跨屏统一，载荷由本通道预传，两端用 `drag_id` 关联。

- 源端（Mac）判据：**剪贴板路线** —— 后台 JXA 观测 `NSDragPboard.changeCount`，
  拖拽开始时 `NSFilenamesPboardType` 直接给出被拖路径（已实测确认）。
  ⚠️ 本机拖拽「结束」**不会**使 `NSDragPboard` 再变化 → 结束/取消由对端 `drag_drop` 回执 + 本端 TTL 兜底。
- 信令：`drag_<id>_{begin,sync,drop,cancel}.json`，一律直推（`begin` → Windows `8900`；
  目标端 `drop` → Mac `8899`）；载荷 `file_*`/`xfer_*` 复用现有通道，预传到目标端 `dragstage` 暂存区。
- 目标端落地方新增 `action=dragstage`；`is_payload()` 白名单加 `drag_`（**两端同源判断**），v2 既有 action 不变。
- `armed` TTL **30 s**；收 `drag_cancel` 立即解除；未 armed 收到任何 `drag_*` 只记日志、不报错。
- 入站 `drag_*` 由 `ingest.ingest_entry` 受理后写 spool `~/AgentBridge/.drag_inbox.jsonl`（一行一条），
  `dragwatch.py` 增量读取对账（`drag_drop` 落点 / `cancel`）。
- 暂存目录：Mac `~/Downloads/KEEPPER-Files/.dragstage/<drag_id>/`；
  Windows `%LOCALAPPDATA%\KEEPPER\dragstage\<drag_id>\`。松手后再搬到目标目录，**半包绝不进目标**。

| 文件 | 作用 |
|---|---|
| `dragwatch.js` | JXA 守护：轮询 `NSDragPboard`，变化且有文件载荷 → 向 stdout 打一行 JSON |
| `dragwatch.py` | 源端控制器：`drag_begin` + 后台预传 + TTL 兜底 `cancel` + 收 `drop` 对账 |

### 跨屏拖拽协议 v1 · 阶段 A（Win → Mac 落点投递）

目标：Windows 拖文件 → 跨屏到 Mac → 在 Mac 松手的位置投递（拖到 Finder 窗口就落该文件夹）。

**探针实测结论（`dropprobe.js`）**：Deskflow 只把「指针坐标」转发给 Mac，
**不转发**从 Windows 起手的拖拽的按键状态（Mac 侧全程 `btn=0`，只看到指针连续移动，
看不到按下/松开）。→ Mac 无法从本机事件判断「何时松手」。

**故改由 Windows 触发落点**：Windows 松手瞬间发 `drag_<id>_drop.json`，
Mac 收到后**立即读本机光标位置**（Deskflow 已把光标停在落点）做命中，再投递。

- 信令方向（修正 163017 第 4/6 点）：
  - `drag_<id>_begin.json`：**Win→Mac**，`source:"win"`（Mac 据此 arm）；
  - `drag_<id>_drop.json`：**Win→Mac**，松手落点在 Mac 屏时发（越快越好）；
  - `drag_<id>_cancel.json`：**Win→Mac**，取消 / 落回源屏；
  - `drag_<id>_sync.json`：**Mac→Win** 回执，`{"phase":"done|fail","landed":N,"target":{...},"x","y"}`。
- Mac 命中与投递（阶段 A 仅 Finder）：`dropat.js` 读光标 + `pid→bundle`；
  若为 Finder（`com.apple.finder`）→ `finderat.applescript` 取包含该点的窗口 `target` 目录，
  无窗口命中判为桌面 `~/Desktop`；非 Finder 的已知 App → 走**阶段 B**（见下）；
  认不出 App → 兜底 `~/Downloads`。
  再把 inbox 里对应载荷投递（落盘同名覆盖 / 粘贴），回 `sync` 回执。
- 载荷映射：inbox 落地文件名 = begin `items[].orig`（即原始文件名，见 `ingest.unique_path`）。
- 权限：`dropat.js` 的 AX `AXUIElementCopyElementAtPosition` 需「辅助功能」；
  `finderat.applescript` 控制 Finder 需「自动化」。均已授权。

| 文件 | 作用 |
|---|---|
| `dropat.js` | JXA：读本机光标 + `CGWindowListCopyWindowInfo` 命中窗口归属 App（`pid`/`app`/`bundle`），AX hit-test 兜底 |
| `finderat.applescript` | 给定屏坐标 → 包含该点的 Finder 窗口 `target` 目录 |
| `dropwatch.py` | 目标端控制器：win 源 begin → armed → drop → 命中 → 投递 → `sync` 回执 |

### 跨屏拖拽协议 v1 · 阶段 B（Win → Mac 落到微信等 App）

目标：Windows 拖文件 → 跨屏到 Mac → 在**非 Finder 的 App 窗口**（微信 / QQ 等）上松手，
文件作为「粘贴内容」进入该 App。

方式（`apppaste.js`）：命中光标下 App 且拿得到 `bundle` + `pid` 时，按顺序做三步 ——

1. 把 **inbox 里的落地文件**（`~/Downloads/KEEPPER-Files/inbox/<orig>`）以 file-URL（furl）
   写进剪贴板，并按 `clipwrite.js` 同样的格式记账到 `.clip_self.json`，
   防止 `clipwatch` 把它当成用户复制再回灌 Windows（否则会形成回环）；
2. 用 `NSRunningApplication` 激活目标 App（优先 `pid`，其次 `bundle`）；
3. 等目标 App 真正到前台（最多 ~2.4 s）后，合成 **⌘V**（`CGEventCreateKeyboardEvent`
   keycode 9 + `kCGEventFlagMaskCommand`，`CGEventPost(kCGHIDEventTap)`）。
   目标 App 没到前台则**不合成**，回 `err=not-frontmost`，绝不误粘到别的 App。

- 兜底：`apppaste.js` 失败时，`dropwatch.py` 把文件 copy 到 `~/Downloads`，
  避免只留在 5 分钟中转站里被清掉；`sync` 回执的 `target.fallback` 会标出该路径。
- 权限：写剪贴板无需特殊权限；激活 App 与合成按键都走「辅助功能」
  （`AXIsProcessTrusted` 与 `CGEventPost` 同源）。已实测：写 furl → 激活 → ⌘V 全链路可用。
- 已知不确定：微信 Mac 是否接受「粘贴文件」取决于其版本（部分版本只认拖放）。
  若微信不吃，会走兜底落到 `~/Downloads`，`sync` 回执可据此判断。

| 文件 | 作用 |
|---|---|
| `apppaste.js` | JXA：写 furl（记账）→ 激活目标 App → 合成 ⌘V |

> 落点识别为什么不用 AX：`AXUIElementCopyElementAtPosition` 依赖目标 App 响应 AX，微信等非原生 App 会返回 `-25208 (NotImplemented)`、偶发 `-25211 (APIDisabled)`，导致落点识别失败。改用窗口服务器快照（`CGWindowListCopyWindowInfo`，屏幕前→后排序）取包含光标的 layer-0 窗口归属 App，纯本地快照、不依赖任何 App。
> 注意 JXA 坑：`$.kCGWindow*` / `$.kCGWindowListOption*` 常量取不到（是 `Ref`），字典键必须用字面字符串，option 位用字面整数。

### 根治：关闭 Deskflow 剪贴板共享

背景：Deskflow 只注册文本/图片/HTML 三种剪贴板转换器，furl 不在其中，
其 `Clipboard::marshall` 产出空数据却仍接管 ownership，把我们写入的 furl
在 1–4 s 内回收成只剩 `Deskflow Ownership`。对端用 `GetClipboardSequenceNumber`
独立监测拿到铁证，且清空延迟**不定时**（1 s / 4 s / 不发生 三种都出现过）。

自愈只是与它抢所有权，胜率不可控，因此改为根治：

- 文件 `~/Library/Deskflow/Deskflow.conf` → `[internalConfig] clipboardSharing=false`
- 备份 `Deskflow.conf.bak-151840`；改完 `pkill -9 Deskflow` 再 `open -a Deskflow`
- 关前：cc=117 / 118 / 119 / 122 四次写入**全被回收**
- 关后：cc=128 写入后 T+5 / 15 / 30 s 复查 cc 恒定、文件数不变；
  cc=129 核验通过并回执 `20260930-152139_mac-clipack.md`
- 代价：两机之间**不再能复制粘贴文本/图片**（跨机 ⌘C/⌘V 失效）；
  本通道的文件互传三条路径（剪贴板 furl / 投递箱 / 右键发送）不受影响
- Windows 侧需在服务端同步关闭，已发 `20260930-152226_mac.md` 通知对端

## 手工/联调用

```bash
# 模拟用户在 Finder 里复制文件，触发 clipwatch 发送（不记账，用于自测）
osascript -l JavaScript ~/AgentBridge/clipsend.js '["/路径/a.bin","/路径/b.txt"]'

# 直接写剪贴板且记账（这条会被 clipwatch 认为是自己写的，不重发）
osascript -l JavaScript ~/AgentBridge/clipwrite.js '["/路径/a.bin"]'

# 阶段 B 手工自测：把文件「粘贴」进某个已运行的 App（会激活该 App 并合成 ⌘V）
osascript -l JavaScript ~/AgentBridge/apppaste.js '{"paths":["/路径/a.bin"],"bundle":"com.apple.TextEdit"}'
```

## Windows 侧速查

```powershell
$H = @{ "X-Bridge-Token" = "CHANGE_ME_BRIDGE_TOKEN" }
$B = "http://192.168.10.153:8899"
Invoke-RestMethod "$B/api/health" -Headers $H
Invoke-RestMethod "$B/api/mac2win?after=<上次最后一条的 mtime_ts>" -Headers $H

# 下载（二进制，必须用 curl.exe 或 httpclient，Invoke-RestMethod 会破坏二进制）
# 中文名要做 percent-encoding
curl.exe -s -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" -o out.bin "$B/api/mac2win/<name>?raw=1"

# 上传（原名走 X-Orig-Name-B64）
curl.exe -s -X PUT "$B/api/mac2win/<name>" -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" --data-binary "@local.bin"

# 标记已取走
curl.exe -s -X POST "$B/api/mac2win/<name>/ack" -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN"
```

## Mac 侧速查

```bash
B=http://192.168.10.153:8899; T="X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN"
ls -la ~/AgentBridge/win2mac/                      # Windows 发来的
curl -s "$B/api/win2mac?after=<mtime_ts>" -H "$T"   # 增量拉取
tail -f ~/AgentBridge/{bridge,clipgate,clipwatch,outboxwatch,inboxwatch}.log
ls -A ~/AgentBridge/mac2win/ ~/AgentBridge/win2mac/_done/ | grep -c "\.part$"

# 用 launchd 重启对应块（改完 .py 后重启；clipwatch 由 clipgate 拉起，不要单独重启）
U=$(id -u)
for n in server clipgate inboxwatch outboxwatch; do
  launchctl kickstart -k gui/$U/com.agentbridge.$n
done
launchctl list | grep agentbridge          # 核验都在跑
```
