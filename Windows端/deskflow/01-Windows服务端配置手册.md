# 01 · Windows 服务端配置手册（Deskflow）

本手册供 **Windows（Server）** 侧使用。Mac 侧见 `mac端/deskflow/01-Mac客户端配置手册.md`，
信任客户端指纹见同目录 `02-Windows端信任Mac客户端手册.md`。

> **本方案里 Deskflow 只做键鼠。** 剪贴板共享两端都关掉（见第四节），
> 文件 / 文本 / 图片三类内容全部走 AgentBridge（`../agentbridge/`）。

---

## 一、角色与事实对照表

| 项 | 值 | 说明 |
|---|---|---|
| **Server（服务端）** | Windows `192.168.10.100` | 物理键鼠插在这台机器上，由它广播输入 |
| **Client（客户端）** | Mac `192.168.10.153` | 被动接收，光标划到边缘时接管 |
| 服务端监听 | `0.0.0.0:24800` | Deskflow/Synergy 默认端口，TCP |
| 服务端屏幕名 | `DESKTOP-DTEKPKA` | 布局里在**上** |
| 客户端屏幕名 | `MAC` | 布局里在**下**；**大小写敏感，必须完全一致** |
| 通信协议 | `Barrier` | 两端必须选同一种 |
| 布局 | Mac 挂在 Windows 的**正下方左侧**，宽度只占 **37%**（左边缘对齐） | 只有 Windows 下边缘的**左侧 37%** 划出才进 Mac 的**上边缘**；Mac **上边缘**划出任一点 → 回 Windows 下边缘左侧 37% |
| 剪贴板共享 | **关闭** | 有意为之，交给 AgentBridge |

**两端必须在同一二层网络（同网段）**，这是「同网段跨屏」方案的前提。
Mac 若走校园网 / 别的网段（如 `192.168.123.x`），Deskflow 连不上属于预期行为。

---

## 二、部署形态：便携版 + 无界面常驻

本端**不装 MSI、不装系统服务**，用便携版解压后以命令行 `deskflow-core.exe server`
无界面常驻（登录自启）。代价是**普通用户权限运行，无法控制以管理员身份运行的窗口（UAC 弹窗）**，
日常使用不受影响。

```
D:\KEEPPER\_deskflow\
├── run-server.cmd                <- 启动器（登录自启链的最后一环）
├── run-server-hidden.vbs         <- 无窗口拉起 run-server.cmd
├── core-server.log               <- 服务端 stdout/stderr 落盘处
├── deskflow-1.26.0-win-x64.msi   <- 官方安装包（本仓 安装包/ 下有副本）
└── portable\
    └── deskflow-1.26.0-win-x64-portable\
        ├── deskflow-core.exe     <- 真正跑的服务端
        ├── deskflow.exe          <- GUI（本方案不用）
        ├── Qt6*.dll / *140.dll   <- 运行库
        └── settings\
            ├── Deskflow.conf           <- 本机实际使用的配置（GUI 格式）
            ├── deskflow-server.conf    <- Barrier 风格的服务器配置（section: 格式）
            └── tls\                    <- 服务端证书与 trusted-clients
```

启动链：`Deskflow Server.lnk`（登录自启）→ `run-server-hidden.vbs` → `run-server.cmd`（无窗口）。

---

## 三、启动

`run-server.cmd` 全文要点：

```bat
set BASE=D:\KEEPPER\_deskflow\portable\deskflow-1.26.0-win-x64-portable

rem 幂等守卫：已在跑就直接退出，避免 24800 端口冲突 + 日志被冲掉
tasklist /FI "IMAGENAME eq deskflow-core.exe" /NH 2>nul | find /I "deskflow-core.exe" >nul
if not errorlevel 1 exit /b 0

cd /d "%BASE%"
"%BASE%\deskflow-core.exe" server -s "%BASE%\settings\Deskflow.conf" > "D:\KEEPPER\_deskflow\core-server.log" 2>&1
```

三个必须照抄的点：

1. **幂等守卫不能去掉。** 第二个实例会抢 24800 并把日志清空，看起来像"服务端莫名重启"。
2. **`run-server.cmd` 里只写 ASCII 注释。** `cmd.exe` 按 ANSI 解码 `.cmd` 文件，
   中文注释会把后面几行解析坏（这是踩过的坑）。
3. **`cd /d "%BASE%"` 不能省。** Deskflow 按工作目录找 Qt 插件与 `settings\`。

启动后自查：

```bat
netstat -ano | findstr :24800                 rem 期望 0.0.0.0:24800 LISTENING
type D:\KEEPPER\_deskflow\core-server.log     rem 期望含 "clipboard sharing is disabled"
```

---

## 四、配置文件：两份 conf 的关系（重要）

`settings\` 下有两份文件，**都带 `clipboardSharing` 这个键，但格式不同**：

| 文件 | 格式 | 本方案中的角色 |
|---|---|---|
| `Deskflow.conf` | GUI 存储：`[core]` / `[internalConfig]` | **启动器用 `-s` 指定的就是它**；它再用 `externalConfig` 把布局转交给下一份 |
| `deskflow-server.conf` | Barrier 风格：`section: screens` / `links` / `options` | **当前实际生效的布局与选项来源**（能写"部分边缘"，GUI 格式不能） |

> **为什么要转交**：GUI 格式（`[internalConfig]` + `numColumns/screens\N`）只能表达
> "整条边相连"，而本方案要求「Mac 只占 Windows 宽度 37%」，需要**部分边缘**的区间语法 ——
> 那只有 Barrier 风格配置写得出。所以在 `Deskflow.conf` 里设
> `externalConfig=true` + `externalConfigFile=<…>/settings/deskflow-server.conf`，
> 由后者提供 `section: links`。
> GUI 格式里那份网格（`screens\1` / `screens\6`）此时**不再生效**，留着只是为了
> 万一哪天用 GUI 打开；**改布局要改的是 `deskflow-server.conf`**。

> **不要只改一份。** 两边都设 `clipboardSharing=false`，否则一旦启动器指向的文件变了，
> 行为会跟着变，而且现象是"剪贴板又被回灌了"，很难联想到配置。

`Deskflow.conf` 关键片段：

```ini
[core]
computerName=DESKTOP-DTEKPKA
coreMode=0

[internalConfig]
clipboardSharing=false
clipboardSharingSize=3072
externalConfig=true
externalConfigFile=<本机路径>/settings/deskflow-server.conf
screens\1\name=DESKTOP-DTEKPKA
screens\2\name=
screens\3\name=
screens\4\name=
screens\5\name=
screens\6\name=MAC
```

`deskflow-server.conf` 关键片段：

```ini
section: screens
	DESKTOP-DTEKPKA:
	MAC:
end

section: links
	DESKTOP-DTEKPKA:
		down(0,37) = MAC(0,100)
	MAC:
		up(0,100) = DESKTOP-DTEKPKA(0,37)
end

section: options
	protocol = barrier
	clipboardSharing = false
	clipboardSharingSize = 3072
	switchCorners = none
	switchCornerSize = 0
end
```

**为什么一定要关剪贴板共享**：Deskflow 的剪贴板转发不支持文件（furl），
且会 grab 所有权并 `clearContents` —— 实测把我们写进去的 CF_HDROP 在 1–4 s 内清成
只剩 `Deskflow Ownership`，用户此时粘贴拿不到文件。关掉之后由 AgentBridge 接管三类内容。

改完**必须重启服务端**（该值在建立监听与处理剪贴板时读取，热改不生效）：

```bat
taskkill /F /IM deskflow-core.exe
D:\KEEPPER\_deskflow\run-server.cmd
type D:\KEEPPER\_deskflow\core-server.log | findstr /I "clipboard"
rem 期望：NOTE: clipboard sharing is disabled
```

### 改「鼠标从哪一侧进出」与「占多宽」

跨屏方向与**连接宽度**由服务端的**生效配置**决定，客户端不用改。
本方案生效的是 `deskflow-server.conf`（见第四节），关键就是 `section: links`：

- 方向：`left` / `right` / `up` / `down`。
- **宽度**：方向后面可以跟一个**区间** `(起点%,终点%)`，表示只把这条边的
  这一段连过去；`=` 右边同样带区间，表示对端用哪一段来接。
  **两边区间写 0–100 就是整条边相连。**

**当前布局**是「Mac 挂在 Windows 正下方左侧、只占宽度 37%」，写法就是：

```ini
section: links
	DESKTOP-DTEKPKA:
		down(0,37) = MAC(0,100)      # Windows 下边缘 左 37% → Mac 整个上边缘
	MAC:
		up(0,100) = DESKTOP-DTEKPKA(0,37)
end
```

`0,37` 即左侧 37%。于是只有从 Windows 下边缘的**左 37%** 划出才会进 Mac，
Mac 上边缘任意点划出则回到 Windows 下边缘的左 37% —— 效果上 Mac 就"挂"在
Windows 的左下角、宽度只占 37%。

想调整宽度就改这两个 `37`（例如整条边相连写成 `down(0,100) = MAC(0,100)`）；
想换方向就把 `down`/`up` 换成 `left`/`right` 并相应调整区间。

> 为什么不用 GUI 格式的 `screens\N\name`：它只能表达"整条边相连"，
> 写不出部分边缘，所以必须走 `externalConfig`（见第四节）。

改完**必须重启服务端**。核验不用盯着鼠标试——把指针推到屏幕边缘，
服务端会打日志：

```
INFO: switch from "DESKTOP-DTEKPKA" to "MAC" at 1364,0
INFO: leaving screen
```

`at x,y` 是**落点在对端屏幕上的坐标**：y≈0 说明「从上边缘进入」，
x≈0 是「从左边缘进入」。配合**负向验证**更可靠：
在 Windows 下边缘的 **70%** 处划出应当**没有**任何日志（因为超出 0–37% 区间），
在 **30%** 处划出则会打出上面这行 —— 一次就能确认区间宽度对不对。

---

## 五、入站与端口

服务端对外只需要 **24800/TCP 入站**（Deskflow 自身）。本机实测该规则由 Deskflow
安装时自带，名为 `Deskflow TCP 24800`，作用域是 `profile=Any / remoteaddress=Any`，
**已经放行，无需手工再加**。核验：

```powershell
# 需管理员：读防火墙规则也要提权，否则报 Access is denied（System Error 5）
Get-NetFirewallRule -DisplayName 'Deskflow TCP 24800' |
    Select-Object DisplayName,Enabled,Direction,Action,Profile
```

只有在确实缺失时才手工补，并用 `-Profile Any` 而不是 `Private`：

```powershell
# 需管理员
New-NetFirewallRule -DisplayName 'Deskflow Server 24800' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 24800 -RemoteAddress LocalSubnet -Profile Any
```

> 为什么不用 `Private`：Windows 会重建网络配置（本机实测已到「网络 6」），
> 新位置默认判为**公用**，`Private` 规则会**静默失配** —— 表现为 Mac 连不上、
> 但日志里没有任何「被拒绝」的痕迹。`-RemoteAddress LocalSubnet` 已经把范围
> 限死在同网段，比网络位置分类更可靠。

> 另有 AgentBridge 直推用的 **8900/TCP**，与 Deskflow 无关，
> 见 `../agentbridge/add_firewall_rule.ps1`（同样是 `-Profile Any` + `LocalSubnet`）。

Deskflow 1.26 默认开 TLS。服务端证书与信任库在 `settings\tls\`；
首次连接客户端时会校验对端指纹，**Mac 连不上、日志停在 `waiting for hello` 后 Timed out**
就是这个环节 —— 处理步骤见 `02-Windows端信任Mac客户端手册.md`。

---

## 六、验证

按顺序做，任一步不过就停在那里：

| # | 动作 | 期望 |
|---|---|---|
| 1 | `netstat -ano \| findstr :24800` | `0.0.0.0:24800 LISTENING` |
| 2 | `type core-server.log \| findstr /I clipboard` | `NOTE: clipboard sharing is disabled` |
| 3 | Mac 侧启动客户端 | 状态 **Connected** |
| 4 | 看服务端日志 | `accepted secure socket` → `accepted client connection` → `saying hello as Barrier` |
| 5 | 把 Windows 鼠标推到**下边缘左侧 37% 内** | 光标从 Mac 的**上边缘**进入 |
| 6 | 在 Mac 上点、打字 | 正常响应 |
| 7 | 把 Mac 光标推到**上边缘** | 光标回到 Windows 下边缘左侧 37% 内 |
| 8 | 在 Windows 下边缘 **70% 处**（超出 37% 区间）划出 | **不切换**，日志无新增 |

服务端日志里跨屏切换长这样（正常现象，不是报错）：

```
INFO: switch from "DESKTOP-DTEKPKA" to "MAC" at 1364,0
INFO: leaving screen
INFO: switch from "MAC" to "DESKTOP-DTEKPKA" at 1376,896
INFO: entering screen
```

另外：服务端首次见到名为 `MAC` 的屏幕时，如果跑的是 GUI 会问「新客户端是否加入布局」——
点接受才会真正纳入。本方案是无界面命令行，布局以配置文件为准。

---

## 七、常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 服务端"莫名重启"/日志被清空 | 启动了第二个实例，抢 24800 | 保留 `run-server.cmd` 的幂等守卫 |
| 改了 conf 不起作用 | 改的是另一份 conf，或没重启 | 两份都改 + 重启服务端 |
| 布局退回"整条边相连"、区间失效 | 改的是 `Deskflow.conf` 里的网格；或 GUI 打开过把 `externalConfig` 关掉了 | 布局以 `deskflow-server.conf` 为准，并确认 `Deskflow.conf` 里 `externalConfig=true` |
| 剪贴板又被回灌、写进去的文件 1–4 s 消失 | 某一份 conf 里 `clipboardSharing` 还是 true | 见第四节 |
| `run-server.cmd` 报语法错 | 里面写了中文注释（cmd 按 ANSI 解码） | 注释只用 ASCII |
| Mac 报 `waiting for hello` 后 Timed out | 服务端要求客户端证书，指纹不在白名单 | 见 `02-` 手册 |
| 日志报 `unknown screen name` | 客户端屏幕名不是 `MAC` | 两端同步屏幕名 |
| 划到边缘卡住 / 来回跳 | Mac 接了多显示器 | Deskflow 把多屏压成一块虚拟屏，**只接单屏** |
| UAC 提权窗口控制不了 | 服务端是普通用户权限 | 无解（这是不装系统服务的代价），日常不影响 |
| 和串流软件抢输入 | Moonlight/ToDesk 也在抓键鼠 | **同一时间只开一个** |

---

## 八、参数速查

| 项 | 值 |
|---|---|
| 服务端监听 | `0.0.0.0:24800` |
| 协议 | Barrier |
| 服务端屏幕名 | `DESKTOP-DTEKPKA`（上） |
| 客户端屏幕名 | `MAC`（下） |
| 布局 | Mac 挂在 Windows **正下方左侧**，宽度占 **37%**：`down(0,37) = MAC(0,100)` |
| 生效配置 | `deskflow-server.conf`（由 `Deskflow.conf` 的 `externalConfig=true` 转交） |
| 剪贴板共享 | **false**（两份 conf 都要） |
| 启动器 | `run-server.cmd`（登录自启：`Deskflow Server.lnk` → `run-server-hidden.vbs`） |
| 日志 | `core-server.log` |
| 版本 | Deskflow 1.26.0（portable，x64） |