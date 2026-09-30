# Deskflow-File-Poster —— Mac 端

本目录是 [Windows端](../Windows端/) 的对端实现：同一套
**Deskflow 控制层 + AgentBridge 双向传输** 方案中 **Mac 侧**的全部代码与配置。
两端协议一致，可各自独立部署。

| 层 | 作用 | 本仓位置 | 本机实际路径 |
|---|---|---|---|
| **控制** | Deskflow 软件 KVM：一套键鼠跨屏控制 Mac 与 Windows | `mac端/deskflow/` | `~/Library/Deskflow/`、`/Applications/Deskflow.app` |
| **双向传输** | AgentBridge：文件 / 文本 / 图片三类内容的跨设备互传 | `mac端/agentbridge/` | `~/AgentBridge/` |

> 为什么拆成两层、Deskflow 剪贴板为何被有意关闭，见[根 README](../README.md) 第一节，
> 此处不再重复。

---

## 目录结构

```
mac端/
├── README.md                     ← 本文件：Mac 端总览 + 最短上手路径
├── agentbridge/                  ← 双向传输：Mac 端全部源码
│   ├── README.md                 ←   详解：连接信息 / 队列 / 命名约定 / 协议 v1+v2 / 排障
│   ├── bridge_server.py          ←   HTTP 服务端（监听 8899，含 /api/push 直达推送 + 探针）
│   ├── bridge_lib.py             ←   公共库：PUT / 列举 / raw 下载 / 直推 + 降级链
│   ├── ingest.py                 ←   条目落地（剪贴板 / 文件 / 清单，两条路径共用）
│   ├── inboxwatch.py             ←   win2mac 队列轮询兜底 → 落地 → ack
│   ├── outboxwatch.py            ←   投递箱写稳即发
│   ├── clipwatch.py              ←   剪贴板守护（常驻主程序，调 clipwatch.js）
│   ├── clipwatch.js              ←   剪贴板变化检测（JXA，文件 / 文本 / 图片分流）
│   ├── clipset.js                ←   写本机剪贴板（接收侧写入原语，含防回环记账）
│   ├── clipread.js               ←   读本机剪贴板（写后核验用）
│   ├── clipwrite.js              ←   写 furl 列表（文件互传）
│   ├── clipsend.js               ←   联调自测用
│   ├── watcher.py                ←   早期 .md 通知（可选，不启用不影响）
│   └── inbox.conf.json           ←   开关：是否把收到的内容写本机剪贴板
└── deskflow/                     ← 控制：Deskflow Mac 客户端配置与手册
    ├── Deskflow.conf             ←   实际生效配置（屏幕名 MAC，已关剪贴板共享）
    ├── 01-Mac客户端配置手册.md
    ├── 02-Windows端信任Mac客户端手册.md
    └── 安装包/
        └── deskflow-1.26.0-macos-x86_64.dmg
```

> `02-Windows端信任Mac客户端手册.md` 与 `Windows端/deskflow/` 下的同名文件**逐字节一致**，
> 是同一份手册的两处副本（这一步由 Windows 侧操作，故两端都放一份，谁都不会漏看）。

---

## 使用方法

### A. Deskflow（控制层）

1. **安装**
   - 本仓自带安装包：`mac端/deskflow/安装包/deskflow-1.26.0-macos-x86_64.dmg`
     （**Intel x86_64**，双击挂载后拖入「应用程序」）。
   - **Apple Silicon（arm64）请勿用这个包**，到官方 Release 下载对应架构：
     <https://github.com/deskflow/deskflow/releases>
     （文件名形如 `deskflow-<版本>-macos-arm64.dmg`）。
   - 版本以本机实测为准：**Deskflow 1.26.0**。
2. **配置**：把 `mac端/deskflow/Deskflow.conf` 放到 `~/Library/Deskflow/Deskflow.conf`。要点：
   - `computerName=MAC` —— **屏幕名必须是大写 `MAC`**，Windows 服务端按此名互认，
     写成小写或带前后缀会连不上；
   - `coreMode=1`（**客户端模式**，Windows 是这套 KVM 的服务端）；
   - `remoteHost=<Windows 局域网 IP>`、`port=24800`；
   - `clipboardSharing=false` —— 有意关闭，剪贴板交给 AgentBridge。
3. **权限授权**（必须，否则键鼠事件注入会被系统静默拦截）：
   「系统设置 → 隐私与安全性 → **辅助功能**」与「**输入监控**」里勾选 Deskflow，改完重启 App。
4. **Windows 端配合**：若服务端开启了客户端证书校验，需把 Mac 客户端证书指纹
   （`~/Library/Deskflow/tls/deskflow.pem`）加入 `trusted-clients`（或关闭该校验），
   否则表现为 **TLS 握手成功但无数据传输、随后超时**。
   步骤见 `mac端/deskflow/02-Windows端信任Mac客户端手册.md`。
5. 跨屏划不过去 / 权限被拦 / 指纹不匹配 / 屏幕名不识别等排障见
   `mac端/deskflow/01-Mac客户端配置手册.md`。

### B. AgentBridge（双向传输）

> 详解（API 基线、队列、命名约定、协议 v1 剪贴板 / v2 直达推送、超时标定、排障）见
> **`mac端/agentbridge/README.md`**，此处只给最短上手路径。

1. **先改密钥**：把 `bridge_server.py`、`bridge_lib.py` 里的 `CHANGE_ME_BRIDGE_TOKEN`
   换成与 Windows 端一致的令牌。
2. **改连接信息**：`bridge_server.py` 顶部的 `BIND`（本机局域网 IP）；
   `bridge_lib.py` 顶部的 `BRIDGE`（本机 `http://<Mac IP>:8899`）与
   `PEER_PUSH`（对端 `http://<Windows IP>:8900`）。
3. **放置代码并建目录**：
   ```bash
   mkdir -p ~/AgentBridge/{mac2win,win2mac}/_done
   mkdir -p ~/Downloads/KEEPPER-Inbox ~/Downloads/KEEPPER-Outbox
   ```
4. **启动**（四块常驻）：
   ```bash
   cd ~/AgentBridge
   nohup python3 bridge_server.py </dev/null >> server.out    2>&1 &
   nohup python3 clipwatch.py     </dev/null >> clipwatch.out 2>&1 &
   nohup python3 outboxwatch.py   </dev/null >> outboxwatch.out 2>&1 &
   nohup python3 inboxwatch.py    </dev/null >> inboxwatch.out 2>&1 &
   ```
5. **验证**：
   ```bash
   B=http://<Mac IP>:8899; T="X-Bridge-Token: <令牌>"
   curl -s "$B/api/health" -H "$T"   # 存活（含 max_bytes / quota）
   curl -s "$B/api/push"   -H "$T"   # 本端直达推送接收器探针
   ```
   第二条返回 `{"ok":true,"service":"agentbridge-push","port":8899}` 即正常。
   （对端的对应探针是 `http://<Windows IP>:8900/api/push`，返回 `service":"clipwatch-push"`。）

   > 注意 `bridge_server.py` 的 `BIND` 绑定的是**本机局域网 IP**（如 `192.168.10.153`），
   > 因此**用 `127.0.0.1` 请求会被拒绝**（curl 退出码 7）。探针请用上面的局域网 IP 访问。
6. **日常使用**
   - 发文件（Mac→Windows）：把文件丢进 `~/Downloads/KEEPPER-Outbox/`；
   - 收文件（Windows→Mac）：落在 `~/Downloads/KEEPPER-Inbox/`（重名自动加 `(1)`）；
   - 剪贴板：复制文本 / 图片 / 文件即自动互传；
     是否**写入本机剪贴板**由 `inbox.conf.json` 的 `write_clipboard` 控制（改完即时生效）；
   - 给对端 agent 发消息：`.md` 放进 `~/AgentBridge/mac2win/`，对端读它的 `inbox/`。
7. **停止 / 重启**（改了 `bridge_server.py` / `bridge_lib.py` / `ingest.py` 必须重启对应块）：
   ```bash
   pkill -f bridge_server.py; pkill -f clipwatch.py; pkill -f clipwatch.js
   pkill -f outboxwatch.py;   pkill -f inboxwatch.py
   ```
   重启命令同第 4 步。

---

## 安全提示

- **仓库为公开仓库，源码中的共享密钥已脱敏**：原本硬编码的 `X-Bridge-Token`
  已统一替换为占位符 **`CHANGE_ME_BRIDGE_TOKEN`**（涉及 `bridge_server.py`、
  `bridge_lib.py`、`agentbridge/README.md`）。**克隆后必须自行改掉**，
  且不要把真实令牌提交回来。
- 通道设计上**仅限局域网 `192.168.10.0/24`**，不应暴露到公网；如需变动请同时收紧
  防火墙规则与令牌强度。
- 队列目录 `mac2win/`、`win2mac/` 及其 `_done/`、以及 `*.log` / `*.out` 都是**运行时产物**，
  其中可能含本机 IP、路径、设备往来消息，**请勿把运行时内容一并提交到公开仓库**。

## 本机实测环境

| 项 | 值 |
|---|---|
| Mac | Intel **x86_64**（arm64 请另下 Deskflow 包）；局域网地址 `192.168.10.153` |
| Deskflow | 1.26.0（客户端模式 `coreMode=1`，屏幕名 `MAC`） |
| Python | 3.11（仅标准库；剪贴板读写走 `osascript`/JXA，无第三方依赖） |
| 局域网 | `192.168.10.0/24`，Mac `192.168.10.153:8899`，Windows `192.168.10.100:8900` |
