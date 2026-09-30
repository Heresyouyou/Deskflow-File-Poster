---
name: "deskflow-win-trust-mac-client"
description: "Windows 端 Deskflow 服务端信任 Mac 客户端的修复手册：Mac 报 'waiting for hello' 后 Timed out、服务端开了 Require client certificates 时使用。Invoke when Mac 端 Deskflow 连不上、需要把 Mac 客户端证书指纹加入 trusted-clients，或要关闭客户端证书校验。"
---

# Deskflow 服务端：让 Windows 信任 Mac 客户端

> 本手册供 **Windows（Server）** 侧的 Trae 使用，用于修复 Mac 客户端连不上本机 Deskflow 服务端的问题。
> 前提：两端已在同一局域网，服务端已监听 `0.0.0.0:24800`。

---

## 一、现象与判定

Mac 客户端日志停在：

```
INFO: connected to secure socket
INFO: server tls certificate info: /CN=Deskflow
DEBUG1: connected, waiting for hello
WARNING: failed to connect to server: Timed out      <= 每 2 秒重试一次，永远到不了 hello
```

且 Mac 侧 `lsof -nP -iTCP:24800` 显示连接**一直 ESTABLISHED**，服务端既不回 hello 也不断开。
反向验证（Mac 侧执行，不带客户端证书时服务端会回 TLS alert `certificate_required`）：

```bash
(sleep 6 | openssl s_client -connect <服务端IP>:24800 -quiet > /dev/null 2>/tmp/e.txt); grep -i alert /tmp/e.txt
# 命中 "SSL alert number 116" => 服务端强制要求客户端证书
```

**结论：服务端开启了客户端证书校验，而 Mac 的客户端证书指纹不在服务端白名单里。**

- 服务端 `security/tlsEnabled=true`（默认）
- 服务端 `security/checkPeerFingerprints=true`（默认；GUI 里就是 **Require client certificates** 勾选状态）
- 两者同时为真时，服务端 `SecurityLevel = PeerAuth`，会校验对端证书 SHA-256 指纹

**为什么是「静默挂住」而不是报错**：Deskflow 源码中 `SecureSocket::disconnect()` 只发送内部事件、**不关闭 TCP socket**，所以指纹校验失败时客户端看到的是「连接挂着、没有数据」，最终只能超时。

> 注意：Mac 侧客户端手册 `deskflow-mac-client` 里写的「客户端证书：不要求」与实测不符——服务端默认是**要求**的。以本文档为准。

---

## 二、修复（二选一）

### 方案 A：把 Mac 客户端指纹加入服务端白名单（推荐，保留双向校验）

Mac 客户端证书指纹（SHA-256，冒号分隔）：

```
12:AA:7A:9C:73:D5:56:85:11:14:37:8E:65:E3:3E:11:33:6B:13:E5:7D:94:C4:CE:4C:5D:94:BA:B0:3C:2D:0D
```

1. 定位服务端数据目录下的指纹库文件 `tls\trusted-clients`
   - 非便携模式（默认）：`C:\ProgramData\Deskflow\tls\trusted-clients`
   - 便携模式：指纹库在 `<Deskflow.exe 所在目录>\settings\tls\trusted-clients`
   - 不确定时用端口反查进程可执行文件路径：

```bat
netstat -ano | findstr :24800
wmic process where "ProcessId=<PID>" get ExecutablePath
```

2. 目录不存在则创建，文件内容追加**一行**（小写 hex、无冒号、行尾换行）：

```
v2:sha256:12aa7a9c73d556851114378e65e33e11336b13e57d94c4ce4c5d94bab03c2d0d
```

命令行一行搞定（需管理员权限）：

```bat
mkdir "C:\ProgramData\Deskflow\tls" 2>nul
echo v2:sha256:12aa7a9c73d556851114378e65e33e11336b13e57d94c4ce4c5d94bab03c2d0d>> "C:\ProgramData\Deskflow\tls\trusted-clients"
```

3. **不需要重启服务端**：该文件在每次连接时重新读取，让 Mac 端重连即可（Mac 端每 2 秒自动重试一次）。

### 方案 B：关闭「要求客户端证书」

如果不想维护指纹白名单：

1. 打开 Deskflow 界面 -> **设置 / Settings -> 安全 / Security**
2. **取消勾选 `Require client certificates`**（对应配置键 `security/checkPeerFingerprints=false`）
3. 保存后**重启 Deskflow 服务端**（该值只在监听 socket 建立时读取，必须重启才生效）

TLS 依然开启（连接仍加密），只是不再校验客户端证书。

### 方案 C：GUI 弹窗直接信任

若服务端跑的是 Deskflow GUI，首次有客户端连接时会弹「新的客户端指纹」确认框 —— 点**接受**即自动写入 `trusted-clients`，等价于方案 A。

---

## 三、验证

Windows 侧日志应依次出现：

```
accepted secure socket
accepted client connection
saying hello as Barrier, protocol v1.8
```

Mac 侧日志应出现：

```
INFO: connected to secure socket
DEBUG1: connected, waiting for hello
=> 不再出现 "Timed out"，连接保持 ESTABLISHED 且不再每 2 秒重连
```

Mac 侧自查：

```bash
lsof -nP -iTCP:24800               # 连接稳定 ESTABLISHED
grep -c "Timed out" /tmp/df.log    # 该计数不再增长
```

另外：服务端首次见到名为 `MAC` 的屏幕时，GUI 会询问是否把新客户端加入屏幕布局（`New client wants to connect`）——需要点接受，Mac 才会被真正纳入跨屏布局。

---

## 四、注意事项

- 指纹**随 Mac 端证书重新生成而失效**：Mac 侧一旦删除 `~/Library/Deskflow/tls/deskflow.pem` 并重生成，必须重新同步指纹。
- 客户端配置文件里的屏幕名来自 `[core] computerName`，本次约定为 **`MAC`**（大小写敏感），服务端屏幕布局里的名字必须与之一致。
- Deskflow 服务端同时只稳定服务一个客户端，测试时不要留多个客户端进程。
- 排查顺序建议：`TCP 可达 -> TLS 握手成功 -> 指纹校验 -> hello 交换 -> 屏幕名匹配`。
