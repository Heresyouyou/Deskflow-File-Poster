#!/usr/bin/env python3
"""AgentBridge 门控（Mac 侧）。

只在 Deskflow 与 Windows 真正连上（TCP 192.168.10.100:24800 ESTABLISHED）期间
才开启依赖「跨屏」的功能：

    连接建立 → inbox.conf.json write_clipboard=true（收剪贴板）
              + 拉起 clipwatch.py（发剪贴板）
              + 拉起 dragwatch.py（跨屏拖拽源端侦测）
              + 拉起 dropwatch.py（跨屏拖拽目标端落点投递）
    连接断开 → write_clipboard=false + 终止 clipwatch.py / dragwatch.py

文件 / 消息 / 8899 服务不受影响，始终常驻。
由 launchd 的 com.agentbridge.clipgate 托管（KeepAlive）。
"""
import json
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.expanduser("~/AgentBridge")
LOGDIR = os.path.join(ROOT, "logs")
LOG = os.path.join(ROOT, "clipgate.log")
CONF = os.path.join(ROOT, "inbox.conf.json")
PY = "/usr/local/bin/python3"

PEER_HOST = "192.168.10.100"
PEER_PORT = "24800"
POLL = 2.0        # 连接检测间隔
KILL_WAIT = 5.0   # 等子进程退出的上限

# 受门控的工作进程（脚本名）。连接期拉起，断开发终止。
WORKERS = ("clipwatch.py", "dragwatch.py", "dropwatch.py")
_children = {}    # 脚本名 -> Popen


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def deskflow_connected():
    """Deskflow 客户端到服务端 24800 是否 ESTABLISHED（lsof 只列已建立连接）。"""
    try:
        r = subprocess.run(
            ["lsof", "-nP", "-iTCP@%s:%s" % (PEER_HOST, PEER_PORT),
             "-sTCP:ESTABLISHED", "-t"],
            capture_output=True, text=True, timeout=10)
        return bool((r.stdout or "").strip())
    except Exception as e:
        log("检测 Deskflow 连接失败: %s" % e)
        return False


def want_clipboard():
    try:
        with open(CONF, encoding="utf-8") as f:
            return bool(json.load(f).get("write_clipboard"))
    except (OSError, ValueError):
        return False


def set_clipboard(flag):
    """原子改写 inbox.conf.json（ingest.want_clipboard 每轮重读，即时生效）。"""
    if want_clipboard() == flag:
        return
    try:
        with open(CONF, encoding="utf-8") as f:
            conf = json.load(f)
    except (OSError, ValueError):
        conf = {}
    conf["write_clipboard"] = flag
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONF)
    log("write_clipboard -> %s" % flag)


def worker_running(script):
    c = _children.get(script)
    return c is not None and c.poll() is None


def kill_strays(script):
    """只清「即将拉起的那个脚本」的遗留（避免误杀已在跑的另一路 worker）。"""
    stem = script[:-3] if script.endswith(".py") else script
    for pat in ("AgentBridge/" + script, "AgentBridge/" + stem + ".js"):
        try:
            subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=10)
        except Exception:
            pass


def start_worker(script):
    if worker_running(script):
        return
    kill_strays(script)
    os.makedirs(LOGDIR, exist_ok=True)
    devnull = open(os.devnull, "w")
    errf = open(os.path.join(LOGDIR, script.replace(".py", ".err.log")), "a")
    # start_new_session：worker 及其子进程（如 JXA 守护）同属一个进程组，
    # 断开时整组终止，不留孤儿 osascript。
    _children[script] = subprocess.Popen([PY, os.path.join(ROOT, script)], cwd=ROOT,
                                         stdout=devnull, stderr=errf, start_new_session=True)
    errf.close()
    log("拉起 %s pid=%d" % (script, _children[script].pid))


def stop_worker(script):
    c = _children.get(script)
    if c is None:
        return
    if c.poll() is None:
        try:
            os.killpg(os.getpgid(c.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + KILL_WAIT
        while c.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if c.poll() is None:
            try:
                os.killpg(os.getpgid(c.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    log("%s 已退出 pid=%d" % (script, c.pid))
    _children[script] = None


def on_term(_sig, _frm):
    for w in WORKERS:
        stop_worker(w)
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    for w in WORKERS:
        kill_strays(w)
    log("clipgate 启动（检测 %s:%s ESTABLISHED，轮询 %ss，workers=%s）"
        % (PEER_HOST, PEER_PORT, POLL, ",".join(WORKERS)))
    state = None
    while True:
        conn = deskflow_connected()
        if conn != state:
            state = conn
            log("Deskflow %s → %s跨设备功能"
                % ("已连接" if conn else "已断开", "开启" if conn else "关闭"))
        # 每轮按目标态幂等收敛（set_clipboard 值相同时短路），
        # 这样外部误改 conf 或 worker 意外退出都能自愈。
        if state:
            set_clipboard(True)
            for w in WORKERS:
                if not worker_running(w):
                    log("%s 不在运行，拉起" % w)
                    start_worker(w)
        else:
            for w in WORKERS:
                if _children.get(w) is not None:
                    stop_worker(w)
            set_clipboard(False)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
