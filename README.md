# Deskflow-File-Poster

同一局域网内两台设备（**Mac + Windows**）的一套协同方案，分两层：

| 层 | 作用 | 本仓位置 | 本机实际路径 |
|---|---|---|---|
| **控制** | Deskflow 软件 KVM：一套键鼠跨屏控制 Mac 与 Windows | `mac端/deskflow/` | `~/Library/Deskflow/`、`/Applications/Deskflow.app` |
| **双向传输** | AgentBridge：文件 / 文本 / 图片三类内容的跨设备互传 | `mac端/agentbridge/` | `~/AgentBridge/` |

本仓目前只收录 **Mac 端**的实现与配置；Windows 端由对端设备自行部署（下文给出对接口径）。

---

## 一、为什么是两层

Deskflow 只负责**键鼠**（KVM）。它的剪贴板转发不支持文件（furl），且实测会出现
「写入后 1–4 s 被回收」的回灌问题，因此本方案：

- **关闭 Deskflow 的剪贴板共享**（`clipboardSharing=false`），让它只做键鼠；
- 剪贴板与文件互传**全部走 AgentBridge** 这条自建 HTTP 通道，
  由 Mac 当服务端（`8899`），Windows 只发出站请求。

## 二、目录结构

```
Deskflow-File-Poster/
├── README.md                     ← 本文件：本地文件说明 + 使用方法
└── mac端/
    ├── agentbridge/              ← 双向传输：Mac 端全部源码
    │   ├── README.md             ←   API 基线 / 命名约定 / 协议 v1+v2 / 排障（详解）
    │   ├── bridge_server.py      ←   HTTP 服务端（8899，含 /api/push 直达推送）
    │   ├── bridge_lib.py         ←   公共库：PUT/列举/raw 下载/直推+降级
    │   ├── ingest.py             ←   条目落地逻辑（剪贴板/文件/清单，两条路径共用）
    │   ├── inboxwatch.py         ←   win2mac 队列轮询兜底 → 落地 → ack
    │   ├── outboxwatch.py        ←   投递箱写稳即发
    │   ├── clipwatch.py / .js    ←   剪贴板守护（文件/文本/图片 → 发往 Windows）
    │   ├── clipset.js            ←   写本机剪贴板（接收侧写入原语，含防回环记账）
    │   ├── clipread.js           ←   读本机剪贴板（写后核验）
    │   ├── clipwrite.js          ←   写 furl 列表（文件互传）
    │   ├── clipsend.js           ←   联调自测用
    │   ├── watcher.py            ←   早期 .md 通知（可不用）
    │   └── inbox.conf.json       ←   开关：是否把收到的内容写本机剪贴板
    └── deskflow/                 ← 控制：Deskflow Mac 端配置与手册
        ├── Deskflow.conf         ←   本机实际配置（屏幕名 MAC、已关剪贴板共享）
        ├── 01-Mac客户端配置手册.md
        ├── 02-Windows端信任Mac客户端手册.md
        └── 安装包/
            └── deskflow-1.26.0-macos-x86_64.dmg
```

## 三、使用方法

### A. Deskflow（控制层）

1. **安装**
   - 本仓自带安装包：`mac端/deskflow/安装包/deskflow-1.26.0-macos-x86_64.dmg`
     （**Intel x86_64** 版，macOS 直接双击挂载安装）。
   - **Apple Silicon（arm64）** 请到官方 Release 下载对应架构：
     <https://github.com/deskflow/deskflow/releases>
     （文件名形如 `deskflow-<版本>-macos-arm64.dmg`；本机为 x86_64 故收录的是 x86_64 包）。
   - 版本以本机实测为准：**Deskflow 1.26.0**。
2. **配置**：把 `mac端/deskflow/Deskflow.conf` 放到 `~/Library/Deskflow/Deskflow.conf`，
   按需改 `remoteHost`（Windows 的局域网 IP）。要点：
   - `computerName=MAC` —— **屏幕名必须是大写 `MAC`**，Windows 端按此名信任本机；
   - `coreMode=1`（客户端模式）、`port=24800`、`remoteHost=<Windows IP>`；
   - `clipboardSharing=false` —— 有意关闭，剪贴板交给 AgentBridge。
3. **权限授权**：首次运行需在
   「系统设置 → 隐私与安全性 → **辅助功能** / **输入监控**」中勾选 Deskflow，
   否则键鼠事件注入会被系统拦截。
4. **Windows 端**：需把 Mac 的客户端证书指纹加入信任白名单（或关闭客户端证书校验），
   否则表现为 **TLS 握手成功但无数据传输、随后超时**。步骤见
   `mac端/deskflow/02-Windows端信任Mac客户端手册.md`。
5. 排障（跨屏划不过去 / 权限被拦 / 指纹不匹配 / 屏幕名不识别）见 `01-` 手册。

### B. AgentBridge（双向传输）

> 详解（API 基线、队列、命名约定、协议 v1 剪贴板 / v2 直达推送、失败案例）见
> **`mac端/agentbridge/README.md`**，此处只给最短上手路径。

1. **先改密钥**（见下文「安全提示」），把三个文件里的
   `CHANGE_ME_BRIDGE_TOKEN` 换成你自己的令牌。
2. **放置代码**：将 `mac端/agentbridge/` 拷到 `~/AgentBridge/`，并建好目录：
   ```bash
   mkdir -p ~/AgentBridge/{mac2win,win2mac}/_done ~/Downloads/KEEPPER-Inbox ~/Downloads/KEEPPER-Outbox
   ```
3. **改连接信息**：`bridge_server.py` 的 `BIND`（本机 IP）、`bridge_lib.py` 的
   `BRIDGE`（本机 `http://<Mac IP>:8899`）与 `PEER_PUSH`（对端 `http://<Win IP>:8900`）。
4. **启动**（四块常驻）：
   ```bash
   cd ~/AgentBridge
   nohup python3 bridge_server.py </dev/null >> server.out 2>&1 &
   nohup python3 clipwatch.py    </dev/null >> clipwatch.out 2>&1 &
   nohup python3 outboxwatch.py  </dev/null >> outboxwatch.out 2>&1 &
   nohup python3 inboxwatch.py   </dev/null >> inboxwatch.out 2>&1 &
   ```
   停止/重启见 `mac端/agentbridge/README.md` 末尾「Mac 侧速查」。
5. **验证**：
   ```bash
   B=http://127.0.0.1:8899; T="X-Bridge-Token: <你的令牌>"
   curl -s "$B/api/health"     -H "$T"      # 存活
   curl -s "$B/api/push"       -H "$T"      # 直达推送接收器探针
   ```
6. **日常使用**
   - 发文件（Mac→Windows）：把文件丢进 `~/Downloads/KEEPPER-Outbox/`；
   - 收文件（Windows→Mac）：落在 `~/Downloads/KEEPPER-Inbox/`（重名自动加 `(1)`）；
   - 剪贴板：复制文本 / 图片 / 文件即自动互传（受 `inbox.conf.json` 的
     `write_clipboard` 控制是否写入本机剪贴板）；
   - 给对端 agent 发消息：`.md` 放进 `~/AgentBridge/mac2win/`，对端读 `win2mac/`。

## 四、安全提示

- **仓库为公开仓库，源码中的共享密钥已脱敏**：原本硬编码的 `X-Bridge-Token`
  已统一替换为占位符 **`CHANGE_ME_BRIDGE_TOKEN`**（涉及 `bridge_server.py`、
  `bridge_lib.py`、`agentbridge/README.md`）。**克隆后必须自行改掉**，
  且不要把真实令牌提交回来。
- 通道设计上**仅限局域网 `192.168.10.0/24`**，不应暴露到公网；如需变动请同时
  收紧防火墙规则与令牌强度。
- 队列目录 `mac2win/`、`win2mac/` 下的 `.md` 是**设备间往来消息**，
  其中可能含本机 IP、路径等信息，**请勿把运行时产生的队列内容一并提交到公开仓库**。

## 五、本机实测环境

| 项 | 值 |
|---|---|
| Mac | Intel **x86_64**（arm64 需另下 Deskflow 包） |
| Deskflow | 1.26.0 |
| Python | 3.11（无需第三方依赖，仅标准库 + `osascript`/JXA） |
| 局域网 | `192.168.10.0/24`，Mac `192.168.10.153:8899`，Windows `192.168.10.100:8900` |
