# AgentBridge —— Windows 端实现（与 Mac 端同一套协议）

本端是**出站方**：Windows 只主动向 Mac 发请求，Mac 是唯一 HTTP 服务端（`8899`）；
协议 v2 之后 Windows 另外**监听 `8900`**，用来接收 Mac 的直达推送。
对照 Mac 侧详解见 `../../mac端/agentbridge/README.md`。

- 控制层（键鼠）见 `../deskflow/`；本目录只讲双向传输。
- 本端实现是一个**单进程**：`clipwatch.py`（约 1500 行，纯标准库 + `pywin32`）。

---

## 连接信息

| 项 | 值 | 说明 |
|---|---|---|
| 对端（Mac）地址 | `192.168.10.153` | `HOST` |
| 对端（Mac）端口 | `8899` | Mac 唯一 HTTP 服务端：队列 API + `/api/push` |
| 本机（Windows）地址 | `192.168.10.100` | 局域网静态地址（Manual）；网络配置文件名「网络 6」，类别 = 专用 |
| 本机监听端口 | `8900` | `PUSH_PORT`，**只用来接收**对端直推 |
| 出站直推端口 | `8899` | `PEER_PUSH_PORT = PORT` —— 对端把 `/api/push` 挂在现有的 8899 上，没另起 8900 |
| 鉴权头 | `X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN` | 两端必须一致；克隆后自行改回 |
| 服务标识 | `AgentBridge/2.0 Python/3.11.1`（对端） | 本机推送接收器标识 `ClipwatchPush/1.0` |

> **端口不对称是有意的**：Mac 把 `/api/push` 加在既有的 8899 服务上（不另起端口），
> Windows 才需要新开 8900。写死成「两端同端口」会打空，`PEER_PUSH_PORT` 必须单独配。

---

## 队列

对端（Mac `8899`）上一个队列就是一对目录。本端只用两条：

| 队列 | 方向 | 本端动作 |
|---|---|---|
| `win2mac` | Windows → Mac | 本端 **PUT**；直推失败时的兜底通道 |
| `mac2win` | Mac → Windows | 本端 **GET / ack**；对端离线期间积压的条目在这里 |

API（与 Mac 端冻结的基线一致，只加不改）：

```
GET  /api/health
GET  /api/<queue>                      # 列举，返回 {messages:[{name,size,mtime}]}
PUT  /api/<queue>/<name>               # 写入原始字节
GET  /api/<queue>/<name>?raw=1         # 取原始字节（二进制必须 raw=1）
POST /api/<queue>/<name>/ack           # 标记已取走（移入 _done/）
```

### since 的坑（两端都中过）

**不要用 `?since=` 列举。** Mac 侧队列里混着两套命名空间 —— `[0-9]*_mac.md`（普通消息）
与 `file_*` / `cb_*`（载荷），字典序交错时 `since` 会**永久漏掉**某些条目。
本端改用**每 2 秒全量列举 + 本地 `processed` 集合去重**，宁可多一次列举也不丢条目。

---

## 命名约定

| 名称 | 含义 | 上限 |
|---|---|---|
| `file_<YYYYMMDD-HHMMSS>_<清洗原名>` | 文件（目录先打包成 `<目录名>.zip`） | 256 MB / 条，流式 |
| `cb_<YYYYMMDD-HHMMSS-mmm>_<win\|mac>_text.txt` | 剪贴板文本（UTF-8） | 256 KB |
| `cb_<YYYYMMDD-HHMMSS-mmm>_<win\|mac>_image.png` | 剪贴板图片 | 32 MB |
| `xfer_<id>.json` | 多文件清单（只在一次多个文件时发） | — |
| `xfer_<id>.fail.json` | 失败回执 | — |
| `<ts>_win.md` / `<ts>_mac.md` | 设备间普通消息（人/agent 读） | 不 ack，留队列 |
| `<ts>_win-clipack.md` | 剪贴板落地回执 | 不 ack |

- **原始文件名**单独走请求头 `X-Orig-Name-B64`（base64 的 UTF-8 原名），
  队列名里那份是清洗过的（去掉 `\/:*?"<>|` 与控制符、截断到 120 字符）。
- **只有载荷名（`cb_` / `file_` / `xfer_`）走直推**；普通消息直接 PUT 队列
  （见 `PUSH_PREFIXES` / `is_push_payload()`）。理由：普通消息对端 `/api/push` 一律 404，
  直推只会白吃一次往返再降级；消息本身不是延迟敏感件，2 s 轮询足够。

### 防回环

两类回环都要防，两层记账并行：

1. **本机剪贴板**：写剪贴板后记 `(self_seq, self_fp)`，序号+内容指纹都相同才判「自己写的」，跳过。
2. **快路（协议 v2）刚写进去的内容**：接收线程把落地后的指纹放进 `_PUSH_ECHO`，
   主循环读到就吞掉，绝不回发（否则两端互推成环）。

> 图片的指纹必须取「**真正落到剪贴板的内容**」（写完后 `read_clipboard_payload()` 再算 sha1），
> 不能拿收到的 blob 直接算 —— PNG → DIB → PNG 是规范化过程，字节会变。

---

## 落地目录

| 目录 | 用途 |
|---|---|
| `inbox\` | 收到的文件（重名自动加 `(1)`，插在扩展名前） |
| `outbox\` | 投递箱：写稳的文件即发（目录自动打包） |
| `state\clipwatch.json` | 状态：剪贴板序号/指纹、`processed`、`push_seen`（快路已落地条目名，上限 500） |
| `logs\clipwatch.log` | 轮转日志（2 MB × 3） |
| `tmp\` | 传输过程临时件（`push_<uuid>.part`、`<条名>.part`） |

> 这几个目录是**运行时产物，不进本仓**。状态文件用 `os.replace` 原子写。

---

## 超时与重试（实测标定）

| 项 | 值 | 依据 |
|---|---|---|
| `STREAM_BLOCKSIZE` | 256 KB | 实测把上行从 12.3 MB/s（8 KB 分块）拉到 33.9 MB/s |
| 单文件超时 | `30 s + size / 5 MB/s` | 规划速率 5 MB/s，实测 33.9 MB/s，余量约 6.8 倍 |
| 轻量请求超时 | 8 s | 列举 / ack / health |
| 直连超时 | 60 s | 长连接复用，覆盖单条 256 MB 的最坏耗时 |
| 失败重试 | 1 s / 3 s / 9 s | 3 次退避后计入失败列表 |

本机实测（千兆、GBE、网卡 1 Gbps，链路线速约 118 MB/s）：

| 指标 | 值 |
|---|---|
| `ping 192.168.10.153` | 中位 1 ms |
| 轻量请求（复用 keep-alive） | 中位 **2.6 ms**（对端修掉 `wbufsize`/Nagle 之前是 47 ms） |
| 轻量请求（每次新建连接） | 中位 **5.8 ms**（修前 61 ms） |
| 上行（256 KB 分块） | 33.9 MB/s |
| 下行（`?raw=1`） | 34.6 MB/s |
| 2 流并行聚合 | 48.0 MB/s |

---

## Windows 侧常驻（单进程）

`clipwatch.py` 一个进程干完四件事，用 `pythonw.exe` 常驻、无窗口、零 UAC、登录自启。

### 分频主循环

| 节奏 | 常量 | 干什么 | 为什么 |
|---|---|---|---|
| 每 **0.25 s** | `LOCAL_CLIP_SEC` | 本机剪贴板看门狗 + 把快路落地的文件写进剪贴板 | 纯本地、无网络成本，从 2 s 收到 0.25 s |
| 每 **2 s** | `POLL_SEC` | 扫 `outbox` + 全量列举 `mac2win` + 存状态 | 只处理离线积压，快路才是常态 |

### 启动 / 停止

```bat
rem 登录自启：Clipwatch.lnk -> run-clipwatch-hidden.vbs -> run-clipwatch.cmd
D:\KEEPPER\_filebridge\run-clipwatch.cmd
```

幂等由 `clipwatch.py` 内的具名互斥量保证 —— 再启一个实例会自己退出。

### CLI（调试用）

```bat
python.exe clipwatch.py --once            rem 只跑一轮（local + full 各一次）
python.exe clipwatch.py --send <路径...>  rem 一次性发送指定路径
python.exe clipwatch.py --status          rem 打印连通性 + 状态
python.exe clipwatch.py --selftest-clip   rem 剪贴板读写自检
```

### 自检（回归基线）

```bat
python.exe selftest_push.py               rem 起 8900 打真请求，26 项断言
```

覆盖：探针 GET、错 token 401、目录穿越 400、未知命名 404、空 body 413、文本落地 +
echo 指纹、幂等 `duplicate`、图片像素一致、文件 `X-Orig-Name-B64` 还原、清单、
坏 sha 400（且不进 `_PUSH_SEEN`）、超限 413、keep-alive 连推 3 条、拒收后新连接立刻可用。

### 入站放行（协议 v2 必需）

Windows 默认拒绝入站（`DefaultInboundAction = NotConfigured` == Block）。
**不提权加规则，对端直推会被拒，静默降级回队列**（本端只会在日志里留一行
`直达推送不可用（ConnectionRefusedError(10061, ...)）`）：

```powershell
# 需要一次管理员 UAC
powershell -NoProfile -ExecutionPolicy Bypass -File .\add_firewall_rule.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\verify_firewall_rule.ps1
```

实测生效结果：

```
display=KEEPPER Clipwatch Push 8900
enabled=True direction=Inbound action=Allow profile=Any
protocol=TCP localport=8900 remoteaddress=LocalSubnet interfacetype=Any
```

`profile=Any` 是**有意为之**，不要收回成 `Private`：本机网络配置文件名已被 Windows
重建过多次（实测当前是「网络 6」），新建网络默认判为**公用**，一旦如此，
Private-only 的规则会**静默失配**，现象与「对端不再推送」完全一样，
排查成本极高。放宽到 Any 后仍受两重约束：`remoteaddress=LocalSubnet`
（只有同网段可达）与接收器的 `X-Bridge-Token`。

### 开机后仍要保留的项（固化清单）

| 项 | 载体 | 是否随重启保留 |
|---|---|---|
| clipwatch 自启 | 启动文件夹 `Clipwatch.lnk` → `run-clipwatch-hidden.vbs` | ✅ 登录即起 |
| Deskflow 服务端自启 | 启动文件夹 `Deskflow Server.lnk` → `run-server-hidden.vbs` | ✅ 登录即起 |
| 入站放行 8900 | 防火墙规则（持久存储） | ✅ |
| 入站放行 24800 | Deskflow 自带规则 `Deskflow TCP 24800` | ✅ |
| 本机静态地址 | `192.168.10.100/24`（Manual） | ✅ |
| 幂等与去重状态 | `state\clipwatch.json`（`push_seen` / `seen_msgs` / `outbox_done`） | ✅ |
| 剪贴板共享开关 | `Deskflow.conf` 与 `deskflow-server.conf` 两处 `clipboardSharing=false` | ✅ |

开机后的一键核验：

```powershell
rem 1) 两个常驻进程都在（应有 clipwatch.py 与 deskflow-core.exe）
Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name like 'deskflow%'" |
    Select-Object ProcessId,Name,CommandLine
rem 2) 两个端口都在听
netstat -ano | findstr ":8900 :24800"
rem 3) 接收器探针（换成本机地址）
curl.exe -s "http://192.168.10.100:8900/api/push" -H "X-Bridge-Token: <令牌>"
```

> 防火墙规则**读取也需要管理员**（非管理员报 `Access is denied`，System Error 5），
> 所以第 3 步的探针比读规则更实用：探针通 = 监听在 + 本机可达；
> 对端可达性由对端直推的成功日志（`via=push`）体现。

---

## 跨设备剪贴板协议 v1（与 Mac 侧冻结）

优先级 **files > image > text**，三类都靠本进程搬运（Deskflow 只做键鼠）。

| 类别 | 通道 | 上限 |
|---|---|---|
| 文件 | `file_*`（CF_HDROP + `Preferred DropEffect=0x05`） | 256 MB |
| 图片 | `cb_*_image.png`（CF_DIB + 注册 PNG 格式） | 32 MB |
| 文本 | `cb_*_text.txt`（CF_UNICODETEXT） | 256 KB（UTF-8 字节） |

内容指纹 = `kind + ':' + sha1(原始字节)`；超限或名字不认识 → **跳过且不 ack**。

### 写文件的额外处理：延迟写入 + 写后自愈

Deskflow 会 grab 剪贴板所有权并 `clearContents`，把刚写的 CF_HDROP 清掉（实测 1–4 s 内
只剩 `Deskflow Ownership`）。清空**不定时**，所以固定延迟复查不可靠：

| 常量 | 值 | 含义 |
|---|---|---|
| `HEAL_WATCH_SEC` | 12.0 | 写后复查窗口 |
| `HEAL_POLL_SEC` | 1.0 | 复查采样间隔 |
| `HEAL_STABLE_HITS` | 6 | 连续命中本批多少次才判「保住了」 |
| `HEAL_MAX_REWRITES` | 2 | 最多重写次数（不与 Deskflow 无限对打） |

并且带守卫：只有 `read_clipboard_payload() is not None` 时才自愈，避免覆盖用户新复制的内容。

---

## 跨设备直达推送协议 v2（与 Mac 侧冻结）

**接收端从「被动轮询」改成「谁有货谁直接 POST 对端」**。核心判断：发送方本来就是主动 PUT，
慢的是接收方「怎么知道有货」——所以只改接收端。

### 契约（一条 HTTP 请求）

```
POST /api/push/<条目名>            # 名字必须 URL 编码：quote(name, safe='')，含中文
X-Bridge-Token: <token>            # 必需
Content-Length: <字节数>            # 必需
X-Sha256: <hex sha256>             # 必需
X-Orig-Name-B64: <base64 原名>      # 可选
Content-Type: application/octet-stream
```

| 码 | 含义 | 发送方该做什么 |
|---|---|---|
| 200 | **已真正落地**（`action` = `clipboard`/`inbox`/`manifest`/`duplicate`） | 完成，不必写队列 |
| 400 | 坏名字 / body 短了 / sha 不符 | 退回队列 |
| 401 | token 不对 | 退回队列（并报警） |
| 404 | 名字不认识 / 路由不对 | 退回队列 |
| 413 | 超限 | 退回队列 |
| 422 | 落地失败（去重登记已撤销） | 可重推 |
| 500 | 内部错 | 退回队列 |

**探针**：`GET /api/push`（带 token）→ `200 {"ok":true,"service":"clipwatch-push","port":8900}`，
不产生条目。

### 两条硬要求

1. **幂等**：按条目名去重，重复推送回 `200 action=duplicate`，不再落第二遍。
2. **连接复用**：接收器是 `HTTP/1.1` keep-alive；**非 200 回复必须带 `Connection: close`
   并置 `close_connection = True`** —— 否则未读的 body 会被当成下一个请求行解析，
   服务端抛 `REQUEST_URI_TOO_LONG` + `ConnectionAbortedError [WinError 10053]`。

### 本端发送侧策略

```
push(name) -> 200 完成
          -> 异常/非 200  ->  PushUnavailable  ->  PUT /api/win2mac/<name>  （队列降级）
```

队列从此只做**离线兜底 + 审计**。实测降级日志：

```
[cli] 直达推送不可用（ConnectionRefusedError(10061, '由于目标计算机积极拒绝，无法连接。')），
      push-fallback-test.txt 退回队列
[cli] 已发送 push-fallback-test.txt (34 B, 0.059s, via=queue)
[cli] 已发送 push-direct-test.txt  (30 B, 0.768s, via=push)     <- 端口修对之后
```

`via=push|queue` 每次都会打进日志，便于对账。

---

## 根治：关闭 Deskflow 剪贴板共享

Deskflow 的剪贴板转发**不支持文件（furl）**，且会 grab 所有权 + `clearContents`，
把本进程写好的内容清掉。所以两端 `clipboardSharing` 必须保持 `false`，
Deskflow 只做键鼠，三类剪贴板全部走 AgentBridge。

> Windows 侧**真正生效的开关在 `deskflow-server.conf` 的 `section: options`**，
> 不是 GUI 的 `[internalConfig]`（那边改了不起作用）。见 `../deskflow/`。

---

## 手工 / 联调用

```bat
rem 列举对端 mac2win（轻量请求，复用连接）
curl.exe -s -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" http://192.168.10.153:8899/api/mac2win

rem 下载（二进制，必须 raw=1；Invoke-RestMethod 会按 UTF-8 解码毁掉 PNG）
curl.exe -s -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" -o out.png "http://192.168.10.153:8899/api/mac2win/cb_..._mac_image.png?raw=1"

rem 上传（中文名要 percent-encoding）
curl.exe -s -X PUT -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" ^
  --data-binary "@D:\path\file.bin" "http://192.168.10.153:8899/api/win2mac/file_20260930-000000_file.bin"

rem 标记已取走
curl.exe -s -X POST -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" ^
  "http://192.168.10.153:8899/api/mac2win/<name>/ack"

rem 对端直推探针（验证对端 /api/push 在线）
curl.exe -s -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" http://192.168.10.153:8899/api/push

rem 本机接收器探针（验证 8900 在听 + 防火墙放行）
curl.exe -s -H "X-Bridge-Token: CHANGE_ME_BRIDGE_TOKEN" http://192.168.10.100:8900/api/push
```

PowerShell 下载二进制请用 `Invoke-WebRequest ... -OutFile`（不要 `Invoke-RestMethod`）。

---

## Windows 侧速查

```bat
rem 看日志
type D:\KEEPPER\_filebridge\logs\clipwatch.log

rem 看状态（连通性 + 序号/指纹/processed）
python.exe D:\KEEPPER\_filebridge\clipwatch.py --status

rem 重启常驻
taskkill /F /IM pythonw.exe /FI "WINDOWTITLE eq *"   rem 谨慎：会杀掉所有 pythonw
rem 更稳：先按命令行过滤出 clipwatch 那一个 PID 再杀
wmic process where "name='pythonw.exe'" get ProcessId,CommandLine | findstr clipwatch
taskkill /F /PID <PID>
D:\KEEPPER\_filebridge\run-clipwatch.cmd

rem 确认 8900 在听（LISTENING）
netstat -ano | findstr :8900
```

---

## 排障

| 现象 | 原因 | 解决 |
|---|---|---|
| 日志反复 `直达推送不可用（ConnectionRefusedError 10061）` | 对端 `/api/push` 没起 / 端口写错 / 本机入站被防火墙挡 | 先 `curl <对端>:8899/api/push`；再确认 `PEER_PUSH_PORT=8899` 与 `PUSH_PORT=8900` 没搞混 |
| 对端能推我，我推不了对端 | 本方 8900 监听正常，但对端把 `/api/push` 挂在 8899 而本方出站打的是 8900 | 改 `PEER_PUSH_PORT` |
| 收到 `400 bad name` | 条目名没 URL 编码（含中文/空格） | `quote(name, safe='')` |
| 收到 `403`/`404`/`ConnectionAbortedError 10053` | 拒收后没读 body，残留 body 被当成下一个请求 | 非 200 一律 `Connection: close` + `close_connection=True` |
| 收到的 PNG 打不开 / 比原文件大 | 用普通 GET 而非 `?raw=1`，服务端按 UTF-8 解码了二进制 | 一律 `?raw=1` |
| 剪贴板刚写就没了（只剩 `Deskflow Ownership`） | Deskflow 还在共享剪贴板 | 两端 `clipboardSharing=false` 并重启 Deskflow |
| 两端互相回推、内容打乒乓 | 防回环指纹没登记，或图片指纹用了收到的 blob | 写完后取「真正落到剪贴板的内容」算指纹（见「防回环」） |
| 同一条目被落地两次 | 快路已落地，队列里那份又被当新条目处理 | `_PUSH_SEEN` 幂等去重 + `poll_inbox` 的去重守卫 |
| 普通消息在对端看不见 | 兜底分支把普通消息也 ack 进 `_done/` | 只对载荷名（`cb_`/`file_`/`xfer_`）落地；普通消息留队列 |
| 非管理员读防火墙报 `Access is denied`（System Error 5） | 读规则也要管理员 | 提权跑 `verify_firewall_rule.ps1` |