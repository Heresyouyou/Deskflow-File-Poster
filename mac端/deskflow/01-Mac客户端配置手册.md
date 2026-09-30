---
name: "deskflow-mac-client"
description: "macOS 端 Deskflow 客户端配置手册：让 Mac 直接使用 Windows 主机的键盘鼠标（同一局域网内的软件 KVM 跨屏）。Invoke when 要在 Mac 上配置/排查 Deskflow、跨屏划不过去、权限被系统拦截、TLS 指纹不匹配、屏幕名不识别。"
---

# Deskflow 客户端配置手册（macOS）

本手册供**控制端 / 客户端**（Mac）使用。Mac 侧由你（Mac 上的 Trae）带着用户一步步做。

---

## 0. 先搞清楚谁是谁

这次的架构里，**Mac 不提供键鼠，只是接收键鼠事件**。

| 角色 | 机器 | 说明 |
|---|---|---|
| **Server（服务端）** | Windows `192.168.10.100` | 物理键鼠插在这台机器上，由它广播输入 |
| **Client（客户端）** | 本机（Mac） | 被动接收，光标划到边缘时接管 |

### 两台机器怎么连上（先确认可达性）

```
Mac ──WiFi/LAN──> 同一局域网（192.168.10.0/24）──> Windows 192.168.10.100
```

**这是「同网段跨屏」方案，前提是 Mac 和 Windows 在同一个二层网络里。**

- 如果 Mac 走的是校园网 / 别的网段（比如 `192.168.123.x`），**Deskflow 连不上**，属于预期行为 —— 那种场景请用 ToDesk。
- 先把 Mac 接到 Windows 所在的同一台 K2P 路由器（AP 模式）下，拿到 `192.168.10.x` 地址。

自查命令：

```bash
ipconfig getifaddr en0        # Wi-Fi 地址
ifconfig | grep "inet "       # 所有 IPv4
ping -c 3 192.168.10.100      # 必须通
```

`ping` 不通就先别装软件，先把网络接对。

---

## 一、服务端事实（Mac 侧做配置时的对照表）

这些值由 Windows 侧固定，Mac 侧必须**逐项对上**，错一项就连不上。

| 项 | 值 | 说明 |
|---|---|---|
| 服务端 IP | `192.168.10.100` | 手动填，不要依赖自动发现 |
| 端口 | `24800` | Deskflow/Synergy 默认，TCP |
| 服务端屏幕名 | `DESKTOP-DTEKPKA` | 布局里在**左** |
| 客户端屏幕名 | `MAC` | **必须完全一致，大小写敏感** |
| 通信协议 | `Barrier` | 两端必须选同一种 |
| 布局 | Windows 在左，Mac 在右 | 鼠标从 Windows **右边缘**划出 → 进 Mac |
| 客户端证书 | 不要求 | 勾选项保持关闭 |
| 服务端证书指纹 | `94:6F:E8:7A:A8:F8:77:13:7A:BD:8B:D8:DF:4F:0D:76:1E:83:32:4E:39:41:CE:0B:CE:60:AC:A7:17:C1:98:FB` | SHA-256，首次连接核对用 |

---

## 二、安装 Deskflow

### 2.1 下载（按 CPU 选一个）

| Mac 类型 | 文件 |
|---|---|
| Apple Silicon（M1/M2/M3/M4） | `deskflow-1.26.0-macos-arm64.dmg` |
| Intel | `deskflow-1.26.0-macos-x86_64.dmg` |

官方发布页：`https://github.com/deskflow/deskflow/releases/tag/v1.26.0`

先确认架构：

```bash
uname -m      # arm64 或 x86_64
```

如果 GitHub 直连慢，把下载地址套上镜像前缀：

```
https://ghfast.top/https://github.com/deskflow/deskflow/releases/download/v1.26.0/<文件名>
```

### 2.2 安装

1. 打开 `.dmg`，把 **Deskflow.app** 拖进「应用程序」
2. **首次打开会被 Gatekeeper 拦**（应用未经 Apple 公证）：
   - 右键 Deskflow.app → 「打开」→ 再点一次「打开」
   - 或 系统设置 → 隐私与安全性 → 底部「仍要打开」

---

## 三、授予系统权限（最容易漏的一步）

**没有权限，Deskflow 能连上，但鼠标键盘完全没反应** —— 症状很像"连不上"，实际是权限没给。

打开 **系统设置 → 隐私与安全性**，逐项添加并勾选 `Deskflow`：

| 权限项 | 英文 | 为什么必须给 |
|---|---|---|
| **辅助功能** | Accessibility | 没有它无法注入鼠标/键盘事件 |
| **输入监控** | Input Monitoring | 没有它读不到本机输入状态（修饰键、Secure Input 检测） |
| **本地网络**（macOS 15+ 才有） | Local Network | 没有它连不上同网段的服务端 |

> 勾选后**必须退出 Deskflow 再重新打开**，权限才会生效。

自查：

```bash
# 能看到 Deskflow 被授权
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "select client,service,auth_value from access where client like '%deskflow%'" 2>/dev/null \
  || echo "（读不到 TCC 库属正常，改用人眼看系统设置界面）"
```

---

## 四、配置为客户端（核心步骤）

打开 Deskflow，切到 **Client / 客户端**模式，按下表填：

| 界面项 | 填什么 |
|---|---|
| 模式 | **Client**（不要选 Server） |
| 屏幕名 / Screen name | `MAC` ← **一个字母都不能错** |
| 服务端地址 / Server IP | `192.168.10.100` |
| 端口 / Port | `24800` |
| 协议 / Protocol | **Barrier** |
| Require client certificate | **不勾** |
| 自动更新 | 可关 |

然后点 **Start / 启动**。

### 关于屏幕名（连不上时的头号原因）

Deskflow 的服务端**按名字认机器**。如果 Mac 报上去的名字不是 `MAC`，Windows 侧日志会直接报：

```
unknown screen name `xxx'
```

此时只有两个办法：

1. **改 Mac 的屏幕名**（首选）—— 就填 `MAC`
2. 或者到 Windows 侧把这个新名字加进布局（需要改 `deskflow-server.conf`）

---

## 五、首次连接的 TLS 指纹

Deskflow 1.26 **默认开启 TLS**，连接是加密的。第一次连上时会弹一个指纹确认框。

- 核对指纹是否为：

```
94:6F:E8:7A:A8:F8:77:13:7A:BD:8B:D8:DF:4F:0D:76:1E:83:32:4E:39:41:CE:0B:CE:60:AC:A7:17:C1:98:FB
```

- 一致 → 点 **Trust / 信任**
- 不一致 → **不要信任**，先回 Windows 侧确认证书有没有被重置过

> 如果弹窗里的格式是分段十六进制但分组不同，数一下**是不是 32 组**（SHA-256）。数量对不上说明用了另一种摘要算法，以 Windows 界面显示的为准。

服务端如果不要求客户端证书（本次就是这种配置），Mac 侧无需额外导入任何证书文件。

---

## 六、验证（按顺序做）

| # | 动作 | 期望结果 |
|---|---|---|
| 1 | `ping -c 3 192.168.10.100` | 通 |
| 2 | 系统设置里 Deskflow 的辅助功能 / 输入监控 | 已勾选 |
| 3 | Deskflow 客户端启动 | 状态显示已连接 / Connected |
| 4 | 把 Windows 的鼠标往**屏幕最右边缘**推 | 光标出现在 Mac 屏幕上 |
| 5 | 在 Mac 上操作键鼠 | 能正常点、能打字 |
| 6 | 把 Mac 光标往**左边缘**推 | 光标回到 Windows |
| 7 | 在 Windows 上复制文字，到 Mac 粘贴 | 能粘贴（剪贴板共享） |

第 4 步能过，整条链路就是通的。

---

## 七、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| 一直连不上 / Connecting… | 不在同一网段 | `ping 192.168.10.100`，不通就换网络 |
| 连上了但键鼠没反应 | 辅助功能 / 输入监控没给 | 见第三节，给完**重启 Deskflow** |
| 日志报 `unknown screen name` | Mac 屏幕名不是 `MAC` | 见第四节 |
| 日志报 `fingerprint does not match` | 指纹没信任 / 证书变了 | 见第五节 |
| 协议错误 / 一直握手失败 | 两端协议不一致 | 两端都选 **Barrier** |
| macOS 15+ 连不上、无任何报错 | 「本地网络」权限没给 | 系统设置 → 隐私与安全性 → 本地网络 |
| 划到边缘卡住、来回跳 | Mac 接了多显示器 | Deskflow 会把多屏压成一块虚拟屏导致边缘判定出错 —— **只接单屏**使用 |
| 进 Mac 后光标乱飘 | 分辨率/缩放差异大 | 把两端缩放比例调成接近；或用 `switchDelay` 增加切换延迟 |
| 打字延迟明显 | 走了 Wi-Fi 且信号差 | 换有线，或靠近 AP |
| 和 ToDesk / Moonlight 抢输入 | 两个软件同时抓键鼠 | **同一时间只开一个** |

### 看日志

Mac 侧日志在 GUI 的日志面板里；无界面运行时看：

```bash
ls -lt ~/Library/Logs/ 2>/dev/null | head -20
```

---

## 八、无界面运行（可选）

如果不想每次手动开 GUI：

```bash
/Applications/Deskflow.app/Contents/MacOS/deskflow-core client \
  -s ~/Library/Application\ Support/Deskflow/Deskflow.conf
```

> 服务端地址、屏幕名这些都存在 settings 文件里 —— **先用 GUI 配好并成功连过一次**，再用命令行方式复用同一份配置。
> `-s` 后面填 GUI 实际使用的配置文件路径（可在 GUI 的「Settings」里看到）。

---

## 九、边界与注意事项

1. **只适用于同网段。** 跨网段（Mac 在校园网、Windows 在 NAS 后面）时 Deskflow 不可用，那种场景用 ToDesk。
2. **Deskflow 与串流互斥。** Deskflow 抓的是本地输入，Moonlight / ToDesk 抓的是远程输入，同时开会互相抢占，表现为输入错乱。**一次只开一个。**
3. **Windows 侧是 headless 服务。** Windows 上跑的是 `deskflow-core.exe server`（无界面，登录自启，监听 `0.0.0.0:24800`）。Mac 这边完全不需要碰 Windows 的界面。
4. **UAC 提权窗口的控制。** Windows 侧是普通用户权限运行，因此**无法控制以管理员身份运行的窗口（UAC 弹窗）**。这是没有安装系统服务的结果，日常使用不受影响。
5. **屏幕名与布局是硬约束。** `MAC` / `DESKTOP-DTEKPKA` 这类名字、左右方位，改任何一处都要两端同步改。

---

## 十、参数速查

| 项 | 值 |
|---|---|
| 服务端 | `192.168.10.100:24800` |
| 协议 | Barrier |
| 客户端屏幕名 | `MAC` |
| 服务端屏幕名 | `DESKTOP-DTEKPKA` |
| 布局 | Windows(左) — Mac(右) |
| 剪贴板共享 | 开，上限 3 MB |
| 服务端证书指纹 | `94:6F:E8:7A:A8:F8:77:13:7A:BD:8B:D8:DF:4F:0D:76:1E:83:32:4E:39:41:CE:0B:CE:60:AC:A7:17:C1:98:FB` |
| 必需权限 | 辅助功能、输入监控、（macOS15+）本地网络 |