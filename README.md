# Deskflow-File-Poster

同一局域网内 **Mac + Windows** 两台设备的协同方案：**一套键鼠跨屏控制**，
外加一条**自建 HTTP 通道**做文件 / 文本 / 图片的双向互传。

分两层，职责不重叠：

| 层 | 作用 | Mac 端位置 | Windows 端位置 |
|---|---|---|---|
| **控制** | Deskflow 软件 KVM：一套键鼠跨屏控制两台机器 | `mac端/deskflow/` | `Windows端/deskflow/` |
| **双向传输** | AgentBridge：文件 / 文本 / 图片跨设备互传 | `mac端/agentbridge/` | `Windows端/agentbridge/` |

两端各自独立部署，协议一致、可互操作；哪一端先装都行，装到两端才有完整效果。

---

## 一、整体架构

```
        ┌──────────────────────────── Mac ────────────────────────────┐
        │                                                             │
  键鼠  │  Deskflow 客户端 (coreMode=1)                                │
 ──────►│    屏幕名 MAC · clipboardSharing=false                      │
        │                                                             │
        │  AgentBridge 服务端  http://192.168.10.153:8899             │
        │    ├── /api/health · /api/<queue>…   ← 协议 v1 队列基线      │
        │    └── /api/push/<name> · /api/push  ← 协议 v2 直达推送      │
        │    ├── clipwatch.py    剪贴板守护（本机复制 → 发对端）        │
        │    ├── inboxwatch.py   队列轮询兜底（2 s）→ 落地 → ack       │
        │    └── outboxwatch.py  投递箱写稳即发                        │
        └─────────────────────────┬───────────────────────────────────┘
                                  │  局域网 192.168.10.0/24
                                  │  头 X-Bridge-Token 鉴权
                                  │  v2 直达：Mac→Win:8900 / Win→Mac:8899
        ┌─────────────────────────┴───────────────────────────────────┐
        │  Deskflow 服务端 (coreMode=0)  屏幕名 DESKTOP-DTEKPKA        │
        │  AgentBridge 客户端 http://192.168.10.100:8900              │
        │    clipwatch.py（出站轮询 + 剪贴板守护 + 协议 v2 接收器）      │
        └──────────────────────────── Windows ────────────────────────┘
```

**单向发起**：Mac 常驻监听（`8899`，含 v2 接收器），Windows 只发出站请求
（监听 `8900` 仅用于接收 Mac 的直推），不需要在 Mac 上开任何指向 Windows 的入站口。

**两条送达路径**（同一套落地逻辑，见 `ingest.py`）：

1. **直推（快路）** —— 载荷名（`cb_` / `file_` / `xfer_`）先 `POST /api/push/<name>`
   到对端，收到即**当场落地**，正常往返亚秒级；
2. **队列（兜底）** —— 直推失败（对端不在线 / 404 / 超限）则退回本机队列，
   由对端 2 s 轮询取走。**普通消息**（`<ts>_mac.md` 等）不走直推，直接进队列。

---

## 二、功能清单

| 能力 | 说明 | 快路 |
|---|---|---|
| **跨屏键鼠** | Deskflow 软件 KVM，一套键鼠在两台机器间滑屏，无硬件切换器 | — |
| **文件互传** | Mac→Win 丢进投递箱；Win→Mac 落到收件箱（重名自动加 `(1)`） | 直推 |
| **文本剪贴板** | 复制文本即互传（上限 256 KB），可选自动写入对端剪贴板 | 直推 |
| **图片剪贴板** | 复制图片即互传（上限 32 MB） | 直推 |
| **文件剪贴板** | 复制文件（furl）即互传，多文件先发 `xfer_<id>.json` 清单 | 直推 + 清单 |
| **设备间消息** | 两端 AI agent 互发 `.md` 消息（如回执、说明），走队列不占直推 | 队列 |
| **落地幂等** | 按条目名去重，重复投递返回 `action=duplicate`，不重复写 | — |
| **完整性校验** | 头 `X-Sha256` 端到端校验；文件用 `.part` 临时文件 + 原子重命名 | — |
| **防回环** | 本地写入记账 `.clip_self.json`，避免自己写进去的又被当成新复制发出去 | — |
| **写后核验** | 写入剪贴板后 6 s 稳定性核验，失败不 ack、最多重试 3 次 | — |

---

## 三、为什么拆两层

Deskflow 只负责**键鼠**。它自带的剪贴板转发**不支持文件（furl）**，且实测会出现
「写入后 1–4 s 被对端回灌覆盖」的问题，跨机复制文件时表现为内容莫名丢失。

因此本方案**有意关闭 Deskflow 的剪贴板共享**（两端 `clipboardSharing=false`），
让它专心做 KVM；**剪贴板与文件互传全部交给 AgentBridge** ——
一条可控、可校验、可观测、可降级的自建 HTTP 通道。

代价是多跑三个常驻进程；换来的是：内容类型不受限（文本 / 图片 / 任意文件）、
有完整性校验、有幂等、有失败降级、出问题能看日志定位。

---

## 四、目录索引

```
Deskflow-File-Poster/
├── README.md                     ← 本文件：整体框架与功能
├── mac端/                        ← Mac 侧：部署指南见 mac端/README.md
│   ├── README.md
│   ├── agentbridge/              ←   双向传输源码（另有 README.md 详解协议）
│   └── deskflow/                 ←   控制层配置 + 手册 + 安装包
└── Windows端/                    ← Windows 侧：部署指南见 Windows端/README.md
    ├── README.md
    ├── agentbridge/              ←   双向传输源码（另有 README.md 详解协议）
    └── deskflow/                 ←   控制层配置 + 手册 + 安装包
```

**按角色选读**：

| 你的角色 | 先看 |
|---|---|
| 只想了解这套方案是什么 | 本文件（读完即可） |
| 要部署 Mac 端 | [`mac端/README.md`](mac端/README.md) |
| 要部署 Windows 端 | [`Windows端/README.md`](Windows端/README.md) |
| 要改协议 / 排查传输问题 | `mac端/agentbridge/README.md`、`Windows端/agentbridge/README.md` |

---

## 五、快速开始

部署顺序没有硬性要求，**先装哪台都行**；两台都装完才有完整效果。

1. **控制层（两端各做一次）** —— 装 Deskflow，配好屏幕名与模式：
   - Windows 作**服务端**（`coreMode=0`，屏幕名 `DESKTOP-DTEKPKA`）；
   - Mac 作**客户端**（`coreMode=1`，屏幕名必须是大写 **`MAC`**，指向 Windows IP）；
   - 两端都设 `clipboardSharing=false`；
   - Mac 端需在「系统设置 → 隐私与安全性」里授权**辅助功能**与**输入监控**；
   - 若 Windows 服务端开启了客户端证书校验，需把 Mac 客户端证书指纹加入信任，
     否则表现为 **TLS 握手成功但无数据传输、随后超时**。
2. **传输层（两端各做一次）** —— 按各自端级 README 部署 AgentBridge，
   **确保两端的 `X-Bridge-Token` 一致**。
3. **验证** —— 两端各跑一次探针：
   ```bash
   # Mac 本机
   curl -s http://127.0.0.1:8899/api/health -H "X-Bridge-Token: <令牌>"
   curl -s http://127.0.0.1:8899/api/push   -H "X-Bridge-Token: <令牌>"
   # Windows 本机
   curl -s http://127.0.0.1:8900/api/push   -H "X-Bridge-Token: <令牌>"
   ```
   各返回 `{"ok":true,…}` 即接收器在线、防火墙放行。
4. 具体命令、目录准备、启停方式，见对应端级 README。

---

## 六、两端对齐的关键约定

部署/改动时务必两端同步，否则会出现「一端改了另一端不认」。

| 约定 | 值 |
|---|---|
| **鉴权** | 所有请求带 `X-Bridge-Token`（GET 也要）；两端令牌必须一致 |
| **端口** | Mac 监听 `8899`（含 v2 接收器）；Windows 监听 `8900`（接收 Mac 直推） |
| **出站目标** | Mac→Windows 打 `8900`；Windows→Mac 打 `8899` |
| **队列** | `mac2win/`（Mac 发给 Windows）、`win2mac/`（Windows 发给 Mac），各带 `_done/` |
| **命名** | `file_<ts>_<原名>`、`xfer_<id>.json`、`cb_<ts-mmm>_<mac\|win>_<text\|image>.<txt\|png>`、`<ts>_<mac\|win>.md` |
| **直推白名单** | `cb_` / `file_` / `xfer_` 三种前缀；其余一律走队列（非载荷直推返回 `404 unsupported name`） |
| **上限** | 文本 256 KB / 图片 32 MB / 文件 256 MB；单条 256 MB、队列总量 4 GB |
| **错误响应** | 非 200 一律带 `Connection: close`；响应码 200/400/401/404/413/422/500 |
| **密钥脱敏** | 两端源码中的令牌统一为占位符 `CHANGE_ME_BRIDGE_TOKEN`，克隆后自行替换 |

协议细节（v1 队列基线 + v2 直达推送的全量字段与状态码）见两端
`agentbridge/README.md`，两份同源。

---

## 七、安全提示

- **本仓为公开仓库，源码中的共享密钥已脱敏**：原本硬编码的 `X-Bridge-Token` 已统一
  替换为占位符 **`CHANGE_ME_BRIDGE_TOKEN`**。**克隆后必须自行改掉**，
  且不要把真实令牌提交回来。
- 通道设计上**仅限局域网 `192.168.10.0/24`**，不应暴露到公网；
  如需扩大范围请同时收紧防火墙规则并提高令牌强度。
- **运行时产物不要提交**：队列目录 `mac2win/`、`win2mac/`（含 `_done/`）、
  `*.log` / `*.out`、投递箱与收件箱目录，可能含本机 IP、路径、设备往来消息。

## 八、本机实测环境

| 项 | 值 |
|---|---|
| 控制层 | Deskflow 1.26.0（Mac 客户端 `coreMode=1` / Windows 服务端 `coreMode=0`） |
| 屏幕名 | Mac `MAC`（**必须大写**）；Windows `DESKTOP-DTEKPKA` |
| 传输层 | AgentBridge（Python 3.11，仅标准库 + `osascript`/JXA，无第三方依赖） |
| 局域网 | `192.168.10.0/24`；Mac `192.168.10.153:8899`，Windows `192.168.10.100:8900` |
