#!/usr/bin/env python3
"""跨屏拖拽 · Mac 源端控制器（协议 v1 阶段1：Mac → Win）。

由 clipgate 随 Deskflow 连接门控拉起 / 终止（连接态才侦测拖拽）。

流程：
  1) dragwatch.js 观测 NSDragPboard → 判定「开始拖拽文件」（判据已实测成立）；
  2) 生成 drag_id，发 drag_<id>_begin.json（含 items / ptr / screen）到 Windows 8900；
  3) 后台把载荷（file_* / xfer_*）直推 Windows 8900，不等松手
     （Windows 依 armed begin 清单把命中项落 dragstage 暂存区）；
  4) 收 Windows drag_<id>_drop.json 对账（经 8899 → ingest → .drag_inbox.jsonl）；
  5) 中断 / 鼠标未到目标 → TTL(30s) 到期或新一轮拖拽顶替 → 发 drag_<id>_cancel.json。

所有出站信令与载荷一律直推（禁用队列轮询）；未 armed 收到任何 drag_* 只记日志。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.expanduser("~/AgentBridge"))
import bridge_lib as B  # noqa: E402

DAEMON = os.path.join(B.ROOT, "dragwatch.js")
LOG = os.path.join(B.ROOT, "dragwatch.log")
SPOOL = os.path.join(B.ROOT, ".drag_inbox.jsonl")

TTL = 30.0            # armed 会话存活上限（与 Windows 侧 armed TTL 对齐）
DEBOUNCE = 1.5        # 同一文件名集合在此窗口内重复上报视为同一次拖拽开始
SPOOL_POLL = 0.1      # 入站信令轮询间隔
TICK = 0.5            # 会话 TTL 巡检间隔


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- 会话状态 ----------------
_SESS = {"id": None, "items": [], "deadline": 0.0, "begun_ts": 0.0,
         "key": None, "key_ts": 0.0, "stage": None}
_LOCK = threading.Lock()


def _clear_session():
    with _LOCK:
        _SESS.update(id=None, items=[], deadline=0.0, begun_ts=0.0, stage=None)


def send_signal(drag_id, kind, obj):
    try:
        name, res = B.push_drag_signal(drag_id, kind, obj, log=log)
        log("→ %s action=%s" % (name, (res or {}).get("action") or "?"))
        return True
    except Exception as e:
        log("发 %s_%s 失败: %s" % (drag_id, kind, e))
        return False


def cancel_session(reason):
    with _LOCK:
        sid = _SESS["id"]
        _SESS.update(id=None, items=[], deadline=0.0, begun_ts=0.0, stage=None)
    if sid:
        send_signal(sid, "cancel", {"id": sid, "reason": reason,
                                    "ts": int(time.time())})


# ---------------- 载荷规划 + 预传 ----------------

def plan_items(paths, drag_id):
    """把本地路径规划成载荷项：目录 ditto 打包成 file_*（.zip），文件名写入 begin 清单。

    返回 (items, temps)。items[].name 即预传用的 file_<ts>_<orig> 名（协议绑定规则）。
    """
    ts = B.timestamp()
    items, temps = [], []
    for p in paths:
        p = p.rstrip("/") or p
        if os.path.isdir(p):
            try:
                z = B.package_dir(p, log)
            except (subprocess.CalledProcessError, OSError) as e:
                log("打包失败 %s: %s" % (p, e))
                continue
            temps.append(z)
            items.append({"kind": "dir", "src": p, "send": z, "orig": os.path.basename(z)})
        elif os.path.isfile(p):
            items.append({"kind": "file", "src": p, "send": p, "orig": os.path.basename(p)})
        else:
            log("跳过（不存在或不可读）: %s" % p)

    used = set()
    for i, it in enumerate(items):
        nm = "file_%s_%s" % (ts, B.safe(it["orig"]))
        if nm in used:
            nm = "file_%s_%02d_%s" % (ts, i + 1, B.safe(it["orig"]))
        used.add(nm)
        it["name"] = nm
        it["size"] = os.path.getsize(it["send"])
        it["sha256"] = B.sha256_of(it["send"])
    return items, temps


def prefetch(drag_id, items, temps):
    """后台把载荷直推 Windows 8900。多文件先发 xfer 清单，再逐项直推。
    单项失败只记日志、继续传其余项（不让一件坏掉拖垮整批）。"""
    failed = []
    try:
        if len(items) > 1:
            try:
                digest = hashlib.sha256("|".join(it["name"] for it in items).encode()).hexdigest()[:6]
                xid = "%s-%s" % (B.timestamp(), digest)
                manifest = {
                    "id": xid, "ts": B.timestamp(), "source": "mac", "drag_id": drag_id,
                    "count": len(items),
                    "items": [{"name": it["name"], "orig": it["orig"], "kind": it["kind"],
                               "size": it["size"], "sha256": it["sha256"]} for it in items],
                }
                blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                B.push_bytes("xfer_%s.json" % xid, blob, log=log)
                log("预传清单 xfer_%s.json（%d 项，drag_id=%s）" % (xid, len(items), drag_id))
            except Exception as e:
                log("预传清单失败 drag_id=%s: %s" % (drag_id, e))

        t0 = time.time()
        for it in items:
            try:
                with open(it["send"], "rb") as f:
                    blob = f.read()
                B.push_bytes(it["name"], blob, orig_name=it["orig"],
                             sha256=it["sha256"], log=log)
                log("预传 %s (%dB)" % (it["name"], it["size"]))
            except Exception as e:
                failed.append(it["name"])
                log("预传失败 %s: %s" % (it["name"], e))
        log("预传结束 drag_id=%s：成功 %d/%d，用时 %.3fs%s"
            % (drag_id, len(items) - len(failed), len(items), time.time() - t0,
               ("；失败: " + ", ".join(failed)) if failed else ""))
    finally:
        for t in temps:
            shutil.rmtree(os.path.dirname(t), ignore_errors=True)


def start_session(paths, ptr, screen):
    drag_id = B.new_drag_id()
    items, temps = plan_items(paths, drag_id)
    if not items:
        log("本次拖拽无可发送载荷，跳过")
        return
    begin = {
        "id": drag_id, "ts": int(time.time()), "source": "mac",
        "ptr": ptr, "screen": screen,
        "count": len(items),
        "items": [{"name": it["name"], "orig": it["orig"], "kind": it["kind"],
                   "size": it["size"], "sha256": it["sha256"]} for it in items],
    }
    if not send_signal(drag_id, "begin", begin):
        for t in temps:
            shutil.rmtree(os.path.dirname(t), ignore_errors=True)
        return
    with _LOCK:
        _SESS.update(id=drag_id, items=items, deadline=time.time() + TTL,
                     begun_ts=time.time(), key=tuple(sorted(paths)), key_ts=time.time())
    log("armed 会话 %s：%d 项（%s），TTL %.0fs"
        % (drag_id, len(items), ", ".join(it["orig"] for it in items), TTL))
    threading.Thread(target=prefetch, args=(drag_id, items, temps), daemon=True).start()


# ---------------- 入站信令（对账） ----------------
_spos = [0]


def handle_signal(name, obj):
    m = B.DRAG_RE.match(name)
    if not m:
        return
    sid, kind = m.group(1), m.group(2)
    with _LOCK:
        cur = _SESS["id"]
        begun = _SESS["begun_ts"]
    if sid != cur:
        log("忽略 %s：未 armed（当前会话 %s）" % (name, cur or "无"))
        return
    if kind == "drop":
        log("对账成功 %s：落点 x=%s y=%s screen=%s；begin→drop 用时 %.3fs"
            % (sid, obj.get("x"), obj.get("y"), obj.get("screen"),
               (time.time() - begun) if begun else -1))
        _clear_session()
    elif kind == "cancel":
        log("收到目标端取消 %s，解除会话" % sid)
        _clear_session()
    elif kind == "sync":
        log("收到进度 %s: %s" % (sid, obj.get("progress")))
    else:
        log("收到未处理信令 %s" % name)


def spool_loop():
    try:
        _spos[0] = os.path.getsize(SPOOL)
    except OSError:
        _spos[0] = 0
    while True:
        time.sleep(SPOOL_POLL)
        try:
            sz = os.path.getsize(SPOOL)
        except OSError:
            continue
        if sz < _spos[0]:
            _spos[0] = 0
        if sz == _spos[0]:
            continue
        try:
            with open(SPOOL, "rb") as f:
                f.seek(_spos[0])
                data = f.read()
        except OSError:
            continue
        nl = data.rfind(b"\n")
        if nl < 0:
            continue
        chunk = data[:nl + 1]
        _spos[0] += len(chunk)
        for raw in chunk.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
                handle_signal(rec.get("name", ""), rec.get("obj") or {})
            except Exception as e:
                log("解析入站信令失败: %s" % e)


def tick_loop():
    while True:
        time.sleep(TICK)
        with _LOCK:
            sid, deadline = _SESS["id"], _SESS["deadline"]
        if sid and time.time() > deadline:
            log("会话 %s TTL 到期未收到 drop，发 cancel" % sid)
            cancel_session("ttl")


# ---------------- JXA 守护 ----------------
def spawn():
    return subprocess.Popen(["osascript", "-l", "JavaScript", DAEMON],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)


def main():
    threading.Thread(target=spool_loop, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    proc = spawn()
    log("dragwatch 启动，JXA 守护 pid=%d（TTL %.0fs）" % (proc.pid, TTL))
    while True:
        line = proc.stdout.readline()
        if not line:
            log("JXA 守护退出，3 秒后重启")
            proc.wait()
            time.sleep(3)
            proc = spawn()
            log("JXA 守护重启 pid=%d" % proc.pid)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            log("忽略非 JSON 行: %s" % line[:120])
            continue
        kind = ev.get("event")
        if kind == "drag":
            paths = [p for p in ev.get("paths", []) if not B.is_junk(p)]
            if not paths:
                continue
            key = tuple(sorted(paths))
            with _LOCK:
                if key == _SESS["key"] and (time.time() - _SESS["key_ts"]) < DEBOUNCE:
                    _SESS["key_ts"] = time.time()
                    continue
            if _SESS["id"]:
                cancel_session("superseded")
            start_session(paths, ev.get("ptr"), ev.get("screen"))
        elif kind == "clear":
            log("拖拽板变化但非文件载荷（cc=%s），忽略" % ev.get("cc"))
        elif kind == "start":
            log("守护就绪 cc=%s pid=%s" % (ev.get("cc"), ev.get("pid")))
        elif kind == "error":
            log("守护报错: %s" % ev.get("err"))


if __name__ == "__main__":
    main()
