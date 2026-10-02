#!/usr/bin/env python3
"""跨屏拖拽 · Mac 目标端落点投递器（协议 v1：阶段 A Finder 落点 + 阶段 B 其它 App 粘贴）。

背景（Mac 侧探针实测结论）：
  Deskflow 只把「指针坐标」转发给 Mac，**不转发**从 Windows 起手的拖拽的按键状态。
  故 Mac 无法从本机事件判断「松手」——改由 Windows 在松手瞬间发 drag_<id>_drop.json，
  Mac 收到后**立即读取本机光标位置**（Deskflow 已把光标停在落点）做命中。

流程：
  1) 入站 drag_<id>_begin.json(source=win) → armed（TTL 30s）；
  2) 入站 drag_<id>_drop.json → 读本机光标 → 判定目标：
       - Finder（pid→bundle=com.apple.finder）：用 finderat.applescript 取包含该点的
         窗口 target 目录；若无窗口命中则判为桌面 ~/Desktop → **copy** 落盘（阶段 A）；
       - 其它已知 App（有 bundle + pid）：**写剪贴板 furl → 激活该 App → 合成 ⌘V**
         投递（微信 / QQ 等，阶段 B）；源文件路径取 inbox；
       - 无光标 / 认不出 App → 兜底 copy 到 ~/Downloads；
  3) 回执 drag_<id>_sync.json 到 Windows 8900（phase=done|fail）。

由 clipgate 随 Deskflow 连接门控拉起 / 终止。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.expanduser("~/AgentBridge"))
import bridge_lib as B  # noqa: E402

ROOT = B.ROOT
LOG = os.path.join(ROOT, "dropwatch.log")
SPOOL = os.path.join(ROOT, ".drag_inbox.jsonl")
DROPAT = os.path.join(ROOT, "dropat.js")
FINDERAT = os.path.join(ROOT, "finderat.applescript")
APPPASTE = os.path.join(ROOT, "apppaste.js")
INBOX = os.path.expanduser("~/Downloads/KEEPPER-Files/inbox")

TTL = 30.0          # armed 会话存活上限（与 Windows / dragwatch 对齐）
SPOOL_POLL = 0.1
TICK = 0.5
WAIT_FILE = 3.0     # drop 到达后，等 inbox 文件就位的最长时间
FALLBACK = os.path.expanduser("~/Downloads")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, body):
    body = body.replace('"', "'").replace("\\", "/")[:200]
    try:
        subprocess.run(["osascript", "-e",
                        'display notification "%s" with title "%s" sound name "Ping"'
                        % (body, title)], capture_output=True, timeout=10)
    except Exception:
        pass


# ---------------- 会话状态 ----------------
_SESS = {"id": None, "items": [], "deadline": 0.0}
_LOCK = threading.Lock()


def _clear():
    with _LOCK:
        _SESS.update(id=None, items=[], deadline=0.0)


def send_sync(drag_id, obj):
    try:
        name, res = B.push_drag_signal(drag_id, "sync", obj, log=log)
        log("→ %s action=%s" % (name, (res or {}).get("action") or "?"))
    except Exception as e:
        log("回执 %s_sync 失败: %s" % (drag_id, e))


# ---------------- 落点查询 ----------------
def query_cursor():
    """读本机光标 + AX 命中（pid/app/bundle）。返回 dict 或 None。"""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", DROPAT],
                           capture_output=True, text=True, timeout=15)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out.startswith("{"):
            log("dropat.js 失败: %s" % ((r.stderr or out).strip()[:200]))
            return None
        return json.loads(out)
    except Exception as e:
        log("dropat.js 异常: %s" % e)
        return None


def finder_dir(fx, fy):
    """给定 AX 屏坐标，返回包含该点的 Finder 窗口 target 目录；无命中返回 None。"""
    try:
        r = subprocess.run(["osascript", FINDERAT, str(fx), str(fy)],
                           capture_output=True, text=True, timeout=15)
        p = (r.stdout or "").strip()
        return p or None
    except Exception as e:
        log("finderat.applescript 异常: %s" % e)
        return None


def resolve_target(info):
    """返回投递计划 dict：{kind: finder|app|fallback, ...}。"""
    hit = info.get("hit") or {}
    fx, fy = hit.get("ax"), hit.get("ay")
    bundle = info.get("bundle")
    app = info.get("app")
    pid = info.get("pid")
    if fx is None or fy is None:
        return {"kind": "fallback", "path": FALLBACK, "why": "no-cursor"}
    if bundle == "com.apple.finder" or app == "Finder":
        p = finder_dir(fx, fy)
        if p:
            return {"kind": "finder", "path": p}
        desk = os.path.expanduser("~/Desktop")
        return {"kind": "finder", "path": desk, "why": "desktop"}
    if bundle and pid:
        # 阶段 B：非 Finder 的已知 App → 走「剪贴板 furl + ⌘V」投递
        return {"kind": "app", "bundle": bundle, "pid": pid, "app": app}
    return {"kind": "fallback", "path": FALLBACK, "app": app or bundle}


# ---------------- 投递 ----------------
def app_paste(paths, pid, bundle):
    """写剪贴板 furl → 激活目标 App → 合成 ⌘V。返回 (ok, res)。"""
    req = {"paths": list(paths), "pid": pid, "bundle": bundle}
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", APPPASTE, json.dumps(req)],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, {"err": str(e)}
    out = (r.stdout or "").strip()
    if not out.startswith("{"):
        return False, {"err": (r.stderr or out).strip()[:200]}
    try:
        d = json.loads(out)
    except ValueError:
        return False, {"err": out[:200]}
    return bool(d.get("ok")), d


def gather_sources(items):
    """等 inbox 里对应载荷就位，返回 (srcs, missing)。srcs = [(orig, path), ...]。"""
    srcs, missing = [], []
    for it in items:
        orig = it.get("orig") or it.get("name")
        if not orig:
            continue
        src = os.path.join(INBOX, orig)
        deadline = time.time() + WAIT_FILE
        while not os.path.isfile(src) and time.time() < deadline:
            time.sleep(0.2)
        if os.path.isfile(src):
            srcs.append((orig, src))
        else:
            missing.append(orig)
            log("inbox 缺文件 %s（等 %.1fs 未就位）" % (orig, WAIT_FILE))
    return srcs, missing


def land_to_dir(srcs, target):
    """把 srcs copy 进目录（同名覆盖），返回 (landed, failed)。"""
    os.makedirs(target, exist_ok=True)
    landed, failed = 0, []
    for orig, src in srcs:
        try:
            dst = os.path.join(target, orig)
            shutil.copy2(src, dst)
            landed += 1
            log("投递 %s -> %s" % (orig, dst))
        except OSError as e:
            failed.append(orig)
            log("投递失败 %s: %s" % (orig, e))
    return landed, failed


def dispatch(drag_id, obj, items):
    """收到 Windows drop 触发：读光标 → 定目标 → 投递（落盘或 ⌘V）→ 回执。"""
    info = query_cursor()
    if info is None or not info.get("ok"):
        log("落点查询失败 drag_id=%s info=%s" % (drag_id, info))
        send_sync(drag_id, {"id": drag_id, "ts": int(time.time()), "phase": "fail",
                            "why": "cursor-query-failed", "landed": 0})
        return
    hit = info.get("hit") or {}
    plan = resolve_target(info)
    kind = plan.get("kind")
    log("落点 drag_id=%s cursor=(%s,%s) app=%s(%s) -> %s"
        % (drag_id, hit.get("ax"), hit.get("ay"), info.get("app"),
           info.get("bundle"), plan))

    srcs, missing = gather_sources(items)

    if kind == "app":
        paths = [p for _, p in srcs]
        ok, res = app_paste(paths, plan.get("pid"), plan.get("bundle"))
        log("App 投递 drag_id=%s app=%s(%s) ok=%s res=%s"
            % (drag_id, plan.get("app"), plan.get("bundle"), ok, res))
        meta = {"kind": "app", "app": plan.get("app"),
                "bundle": plan.get("bundle"), "result": res}
        landed = 0
        if ok:
            landed = len(paths)
        elif paths:
            # 粘贴失败兜底落盘，避免文件只留在 5 分钟中转站里被清掉
            landed, failed = land_to_dir(srcs, FALLBACK)
            missing += failed
            if landed:
                meta["fallback"] = FALLBACK
                log("App 投递失败，已兜底落到 %s" % FALLBACK)
    else:
        landed, failed = land_to_dir(srcs, plan["path"])
        missing += failed
        meta = plan

    phase = "done" if landed and not missing else ("done" if landed else "fail")
    send_sync(drag_id, {
        "id": drag_id, "ts": int(time.time()), "phase": phase,
        "landed": landed, "missing": missing,
        "target": meta, "x": hit.get("ax"), "y": hit.get("ay"),
    })
    if landed:
        where = meta.get("path") or meta.get("app") or meta.get("bundle") or "?"
        notify("跨屏拖拽已投递", "%d 个文件 → %s" % (landed, where))


# ---------------- 入站信令 ----------------
def handle(name, obj):
    m = B.DRAG_RE.match(name)
    if not m:
        return
    sid, kind = m.group(1), m.group(2)
    with _LOCK:
        cur = _SESS["id"]
    if kind == "begin":
        if (obj.get("source") or "").lower() != "win":
            return   # 只接管 win 源；mac 源由 dragwatch.py 负责
        items = obj.get("items") or []
        with _LOCK:
            _SESS.update(id=sid, items=items, deadline=time.time() + TTL)
        log("armed %s：%d 项（%s），TTL %.0fs"
            % (sid, len(items), ", ".join(i.get("orig", "?") for i in items), TTL))
        return
    if kind == "cancel":
        if sid == cur:
            log("收到 cancel %s，解除 armed" % sid)
            _clear()
        return
    if kind == "drop":
        if sid != cur:
            log("忽略 drop %s：未 armed（当前 %s）" % (sid, cur or "无"))
            return
        with _LOCK:
            items = list(_SESS["items"])
        _clear()
        threading.Thread(target=dispatch, args=(sid, dict(obj), items), daemon=True).start()
        return


_pos = [0]


def spool_loop():
    try:
        _pos[0] = os.path.getsize(SPOOL)
    except OSError:
        _pos[0] = 0
    while True:
        time.sleep(SPOOL_POLL)
        try:
            sz = os.path.getsize(SPOOL)
        except OSError:
            continue
        if sz < _pos[0]:
            _pos[0] = 0
        if sz == _pos[0]:
            continue
        try:
            with open(SPOOL, "rb") as f:
                f.seek(_pos[0])
                data = f.read()
        except OSError:
            continue
        nl = data.rfind(b"\n")
        if nl < 0:
            continue
        chunk = data[:nl + 1]
        _pos[0] += len(chunk)
        for raw in chunk.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
                handle(rec.get("name", ""), rec.get("obj") or {})
            except Exception as e:
                log("解析入站信令失败: %s" % e)


def tick_loop():
    while True:
        time.sleep(TICK)
        with _LOCK:
            sid, dl = _SESS["id"], _SESS["deadline"]
        if sid and time.time() > dl:
            log("会话 %s TTL 到期未收到 drop，解除 armed" % sid)
            _clear()


def main():
    threading.Thread(target=spool_loop, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    log("dropwatch 启动（inbox=%s，TTL %.0fs，fallback=%s，apppaste=%s）"
        % (INBOX, TTL, FALLBACK, os.path.basename(APPPASTE)))
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
