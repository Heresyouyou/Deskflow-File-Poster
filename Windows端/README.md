# Deskflow-File-Poster —— Windows 端

本目录是 [mac端](../mac端/) 的对端实现：同一套 **Deskflow 控制层 + AgentBridge 双向传输** 方案中
**Windows 侧**的全部代码与配置。两端协议一致，可各自独立部署。

| 层 | 作用 | 本仓位置 | 本机实际路径 |
|---|---|---|---|
| **控制** | Deskflow 软件 KVM：一套键鼠跨屏控制 Mac 与 Windows | `Windows端/deskflow/` | `D:\KEEPPER\_deskflow\` |
| **双向传输** | AgentBridge：文件 / 文本 / 图片三类内容的跨设备互传 | `Windows端/agentbridge/` | `D:\KEEPPER\_filebridge\` |

> 为什么拆成两层、Deskflow 剪贴板为何被有意关闭，见[根 README](../README.md) 第一节，
> 此处不再重复。

---

## 目录结构

```
Windows端/
├── README.md                     ← 本文件：Windows 端总览 + 最短上手路径
├── agentbridge/                  ← 双向传输：Windows 端全部源码
│   ├── README.md                 ←   详解：连接信息 / 队列 / 命名约定 / 协议 v1+v2 / 排障
│   ├── clipwatch.py              ←   常驻主程序（单进程）：
│   │                                 出站轮询 + 剪贴板守护 + 投递箱监听
│   │                                 + 协议 v2 直达推送接收器（监听 8900）
│   ├── run-clipwatch.cmd         ←   带控制台启动（调试用）
│   ├── run-clipwatch-hidden.vbs  ←   无窗口后台启动（日常用）
│   ├── selftest_push.py          ←   协议 v2 接收器回归自检（26 项断言）
│   ├── add_firewall_rule.ps1     ←   放行入站 8900（需一次管理员 UAC）
│   └── verify_firewall_rule.ps1  ←   核验防火墙规则是否生效
└── deskflow/                     ← 控制：Deskflow Windows 服务端配置与手册
    ├── Deskflow.conf             ←   实际生效配置（桌面版启动器 -s 指定的那份）
    ├── deskflow-server.conf      ←   Barrier 风格配置（另一份，同样需关剪贴板）
    ├── 01-Windows服务端配置手册.md
    ├── 02-Windows端信任Mac客户端手册.md
    └── 安装包/
        └── deskflow-1.26.0-win-x64.msi
```

---

## 使用方法

### A. Deskflow（控制层）

1. **安装**
   - 本仓自带安装包：`Windows端/deskflow/安装包/deskflow-1.26.0-win-x64.msi`
     （Windows x64，双击安装或 `msiexec /i` 静默安装）。
   - 其它架构 / 版本请到官方 Release 下载：
     <https://github.com/deskflow/deskflow/releases>
     （文件名形如 `deskflow-<版本>-win-x64.msi` / `-arm64.msi`）。
   - 版本以本机实测为准：**Deskflow 1.26.0**。
2. **配置**：把 `Windows端/deskflow/Deskflow.conf` 放到便携版
   `settings\Deskflow.conf`（或安装版的同名配置位置）。要点：
   - `computerName=DESKTOP-DTEKPKA` —— 本机屏幕名，Mac 侧按此名互认；
   - `coreMode=0`（**服务端模式**，Windows 是这套 KVM 的服务端）；
   - `clipboardSharing=false` —— 有意关闭，剪贴板交给 AgentBridge；
   - `screens\N\name` 里列出对端 Mac 的屏幕名（Mac 侧须为**大写 `MAC`**）。
   - 另有一份 `deskflow-server.conf`（Barrier 风格）也带 `clipboardSharing`，
     **两份都要设为 `false`**，避免某个启动参数切换到另一份时又被打开。
3. **信任 Mac 客户端**：若服务端开启了客户端证书校验，需把 Mac 客户端证书指纹加入
   `trusted-clients`（或关闭该校验），否则表现为 **TLS 握手成功但无数据传输、随后超时**。
   步骤见 `Windows端/deskflow/02-Windows端信任Mac客户端手册.md`。
4. 服务端启动、入站端口、验证与常见坑见 `Windows端/deskflow/01-Windows服务端配置手册.md`。

### B. AgentBridge（双向传输）

> 详解（API 基线、队列、命名约定、协议 v1 剪贴板 / v2 直达推送、超时标定、排障）见
> **`Windows端/agentbridge/README.md`**，此处只给最短上手路径。

1. **先改密钥**：把 `clipwatch.py` 里的 `CHANGE_ME_BRIDGE_TOKEN`
   换成与 Mac 端一致的令牌。
2. **改连接信息**：`clipwatch.py` 顶部的 `HOST` / `PEER_HOST`（Mac 的局域网 IP）、
   `PUSH_PORT`（本机监听，默认 `8900`）、`PEER_PUSH_PORT`（对端 `/api/push` 端口，
   Mac 挂在既有 `8899`）。
3. **放行入站**（协议 v2 必需，一次性，需管理员 UAC）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\agentbridge\add_firewall_rule.ps1 -Port 8900
   powershell -ExecutionPolicy Bypass -File .\agentbridge\verify_firewall_rule.ps1
   ```
   不开这一条时，协议 v2 直达推送会被本机防火墙静默丢弃，
   发送侧会退回协议 v1 队列（功能仍可用，只是多一次轮询延迟）。
4. **启动**（单进程常驻，日常用无窗口方式）：
   ```cmd
   :: 后台无窗口
   wscript "D:\KEEPPER\_filebridge\run-clipwatch-hidden.vbs"
   :: 或带控制台调试
   D:\KEEPPER\_filebridge\run-clipwatch.cmd
   ```
   停止：结束 `pythonw.exe` / `python.exe` 上的 `clipwatch.py` 进程后按上面命令重启。
5. **验证**：
   ```powershell
   curl.exe -s "http://192.168.10.153:8899/api/health" -H "X-Bridge-Token: <令牌>"
   curl.exe -s "http://127.0.0.1:8900/api/push"     -H "X-Bridge-Token: <令牌>"
   ```
   第二条是**本端直达推送接收器**的探针，返回 `{"ok":true,"service":"clipwatch-push",...}` 即正常。
6. **回归自检**（改完接收器代码后跑一遍）：
   ```cmd
   python selftest_push.py
   ```
7. **日常使用**
   - 发文件（Windows→Mac）：把文件丢进投递箱 `D:\KEEPPER\_filebridge\outbox\`；
   - 收文件（Mac→Windows）：落在收件箱 `D:\KEEPPER\_filebridge\inbox\`（重名自动加 `(1)`）；
   - 剪贴板：复制文本 / 图片 / 文件即自动互传；
   - 给对端 agent 发消息：`.md` 写进 `D:\KEEPPER\_agentbridge\` 后投放队列。

---

## 安全提示

- **仓库为公开仓库，源码中的共享密钥已脱敏**：原本硬编码的 `X-Bridge-Token`
  已统一替换为占位符 **`CHANGE_ME_BRIDGE_TOKEN`**（涉及 `clipwatch.py`、
  `agentbridge/README.md`）。**克隆后必须自行改掉**，且不要把真实令牌提交回来。
- 通道设计上**仅限局域网 `192.168.10.0/24`**，不应暴露到公网；防火墙放行规则
  已限定 `RemoteAddress=LocalSubnet` + `Profile=Private`，如需变动请同时收紧。
- 队列目录 `inbox/`、`outbox/`、`logs/`、`state/` 是**运行时产物**，
  其中可能含本机 IP、路径等信息，**请勿把运行时内容一并提交到公开仓库**。

## 本机实测环境

| 项 | 值 |
|---|---|
| Windows | x64，桌面版；局域网地址 `192.168.10.100`（静态，网络配置文件 = 专用） |
| Deskflow | 1.26.0（便携版，`deskflow-core.exe server -s settings\Deskflow.conf`） |
| Python | 3.11（仅标准库；`selftest_push.py` 额外用 `Pillow` 造测试图） |
| 局域网 | `192.168.10.0/24`，Mac `192.168.10.153:8899`，Windows `192.168.10.100:8900` |