#!/usr/bin/env python3
"""条目落地逻辑：队列轮询（inboxwatch）与直达推送（8899 /api/push）共用。

一条「条目」= (name, blob, sha_declared, orig_b64)。
`ingest_entry` 返回 {"code":..., "action":..., "error":..., "sha256":...}，
码与语义严格对齐协议 v2 第二节：200 落地 / 400 载荷坏 / 404 名字不认识 /
413 超限 / 422 落地失败（撤销去重登记，允许对端重推）。

幂等：按条目名去重（`.push_dedupe.json`，重启后仍有效）；重复推送回
200 action=duplicate，不重复写剪贴板。
"""
import base64
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.expanduser("~/AgentBridge"))
import bridge_lib as B  # noqa: E402

INBOX = os.path.expanduser("~/Downloads/KEEPPER-Files/inbox")
LOG = os.path.join(B.ROOT, "inboxwatch.log")
CONF = os.path.join(B.ROOT, "inbox.conf.json")
CLIPWRITE = os.path.join(B.ROOT, "clipwrite.js")
CLIPSET = os.path.join(B.ROOT, "clipset.js")
CLIPREAD = os.path.join(B.ROOT, "clipread.js")

# 写后核验（不用固定短延迟：对端实测 Deskflow 的回收延迟 1 s / 4 s / 不发生三种都出现过）
HEAL_WATCH_SEC = 12.0     # 核验窗口，大于实测最长回收延迟 4 s
HEAL_POLL_SEC = 1.0       # 窗口内每秒复查一次
HEAL_STABLE_HITS = 6      # 连续 6 次命中本批文件即判定保住（6 s）

PREFIX_RE = re.compile(r"^file_\d{8}-\d{6}_")
CB_RE = re.compile(r"^cb_\d{8}-\d{6}-\d{3}_(mac|win)_(text|image)\.(txt|png)$")
XFR_RE = re.compile(r"^xfer_.*\.json$")
TEXT_MAX = 256 * 1024
IMAGE_MAX = 32 * 1024 * 1024
FILE_MAX = 256 * 1024 * 1024

DEDUPE = os.path.join(B.ROOT, ".push_dedupe.json")
DEDUPE_CAP = 500
DRAG_INBOX = os.path.join(B.ROOT, ".drag_inbox.jsonl")   # 入站 drag_* 信令 spool（供 dragwatch.py 对账）
DRAG_INBOX_CAP = 2 * 1024 * 1024
_lock = threading.Lock()


def is_payload(name):
    """载荷名白名单：cb_ / file_ / xfer_。普通消息 <ts>_win.md 不属于载荷。

    inboxwatch 的队列过滤与 /api/push 的受理判断共用本函数
    （对端 165013 的第 4 点建议：两处判断不能各写一份）。
    """
    return bool(CB_RE.match(name) or XFR_RE.match(name)
                or B.DRAG_RE.match(name) or name.startswith("file_"))


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


# ---------- 去重登记 ----------

def _dedupe_load():
    try:
        with open(DEDUPE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _dedupe_save(lst):
    with open(DEDUPE, "w", encoding="utf-8") as f:
        json.dump(lst[-DEDUPE_CAP:], f)


def dedupe_hit(name):
    with _lock:
        return name in _dedupe_load()


def dedupe_add(name):
    with _lock:
        lst = _dedupe_load()
        if name not in lst:
            lst.append(name)
        _dedupe_save(lst)


def dedupe_drop(name):
    with _lock:
        _dedupe_save([n for n in _dedupe_load() if n != name])


def drag_spool(name, obj):
    """把入站 drag_* 信令追加到 spool（一行一条 JSON），dragwatch.py 增量读取对账。

    单行一次 write()，读者按 \n 切整行，避免读到半行。
    """
    line = json.dumps({"name": name, "obj": obj, "ts": time.time()}, ensure_ascii=False)
    with _lock:
        try:
            if os.path.getsize(DRAG_INBOX) > DRAG_INBOX_CAP:
                os.replace(DRAG_INBOX, DRAG_INBOX + ".old")
        except OSError:
            pass
        with open(DRAG_INBOX, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------- 小工具 ----------

def want_clipboard():
    try:
        with open(CONF, encoding="utf-8") as f:
            return bool(json.load(f).get("write_clipboard"))
    except (OSError, ValueError):
        return False


def unique_path(name):
    """落地路径：本目录是临时中转（5 分钟 TTL 自动清），同名直接覆盖，
    不再产生 报告(1).pdf 这类副本。"""
    os.makedirs(INBOX, exist_ok=True)
    p = os.path.join(INBOX, name)
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return p


def orig_name(header_b64, bridge_name):
    if header_b64:
        try:
            return base64.b64decode(header_b64).decode("utf-8")
        except Exception:
            pass
    return PREFIX_RE.sub("", bridge_name) or bridge_name


def write_clipboard(paths):
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", CLIPWRITE, json.dumps(paths)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log("写剪贴板失败: %s" % (r.stderr or "").strip()[:200])
            return False
        log("已写剪贴板 %d 项: %s" % (len(paths), (r.stdout or "").strip()))
        return True
    except Exception as e:
        log("写剪贴板异常: %s" % e)
        return False


def apply_clip(kind, blob):
    """把对端发来的文本/图片写进本机剪贴板（走 clipset.js，自带记账）。"""
    tmp = None
    try:
        if kind == "text":
            try:
                txt = blob.decode("utf-8")
            except UnicodeDecodeError as e:
                log("文本不是合法 UTF-8: %s" % e)
                return None
            req = {"kind": "text", "text": txt}
        else:
            tmp = "/tmp/ab-in-clip-%d-%d.png" % (os.getpid(), int(time.time() * 1000))
            with open(tmp, "wb") as f:
                f.write(blob)
            req = {"kind": "image", "path": tmp}

        req["fp"] = "%s:%s" % (kind, hashlib.sha1(blob).hexdigest())
        r = subprocess.run(["osascript", "-l", "JavaScript", CLIPSET, json.dumps(req)],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out.startswith("{"):
            log("写剪贴板(%s)失败: %s" % (kind, (r.stderr or out).strip()[:200]))
            return None
        res = json.loads(out)
        if not res.get("ok"):
            log("写剪贴板(%s)未成功: %s" % (kind, res.get("err") or out))
            return None
        return res
    except Exception as e:
        log("写剪贴板(%s)异常: %s" % (kind, e))
        return None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def clip_paths():
    """读当前剪贴板里的文件列表（realpath 归一化）。"""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", CLIPREAD],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return [os.path.realpath(p) for p in json.loads(r.stdout or "{}").get("paths", [])]
    except Exception:
        return None


RESULT_OK = "写入生效（复查窗口内未被清）"
RESULT_WIPED = "写入被清掉（复查窗口内丢失）"
RESULT_OVERWRITTEN = "写入被用户其它内容覆盖（非 Deskflow 清空）"


def send_clipack(result, paths, rewrites=0):
    """按对端约定回一条 <ts>_mac-clipack.md 回执。"""
    name = "%s_mac-clipack.md" % time.strftime("%Y%m%d-%H%M%S")
    body = ("# 剪贴板落地回执\n\n"
            "- 结果: %s\n"
            "- 自愈重写次数: %d\n"
            "- 落地文件数: %d\n"
            "- 文件: %s\n"
            "- 回执时刻: %s\n") % (result, rewrites, len(paths),
                                    ", ".join(os.path.basename(p) for p in paths),
                                    time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        B.put_bytes("mac2win", name, body.encode("utf-8"), log=log)
        log("已回剪贴板落地回执 %s（%s）" % (name, result))
    except Exception as e:
        log("回执发送失败: %s" % e)


def verify_and_receipt(paths):
    """写后窗口内持续核验，不回写。判定结果回一条回执给对端。"""
    want = sorted(os.path.realpath(p) for p in paths)
    hits = 0
    result = None
    deadline = time.time() + HEAL_WATCH_SEC
    while time.time() < deadline:
        time.sleep(HEAL_POLL_SEC)
        cur = clip_paths()
        if cur is None:
            continue
        if not cur:
            result = RESULT_WIPED
            break
        if sorted(cur) != want:
            result = RESULT_OVERWRITTEN
            break
        hits += 1
        if hits >= HEAL_STABLE_HITS:
            result = RESULT_OK
            log("剪贴板写后核验通过：本批 %d 个文件稳定 %d s 未被清" % (len(paths), hits))
            break
    if result is None:
        result = RESULT_OK if sorted(clip_paths() or []) == want else RESULT_WIPED
    send_clipack(result, paths)


# ---------- 剪贴板后台写入（串行 worker，绝不阻塞 HTTP handler）----------
# 背景：/api/push 曾在 handler 里同步跑 osascript 写 NSPasteboard，对端实测一次
# 小文本要 6.73 s 才拿到 200；并发请求还会各自 spawn osascript 争抢剪贴板。
# 现在 handler 只做「校验 + 去重登记 + 入队」立刻回 200，写剪贴板交单条后台线程
# 串行处理（同时避免 NSPasteboard 争用）。
_clip_q = queue.Queue()
_clip_lock = threading.Lock()
_clip_started = False


def _clip_worker():
    while True:
        job = _clip_q.get()
        try:
            _run_clip_job(job)
        except Exception as e:
            log("剪贴板后台任务异常: %s" % e)
        finally:
            _clip_q.task_done()


def _ensure_clip_worker():
    global _clip_started
    if _clip_started:
        return
    with _clip_lock:
        if not _clip_started:
            threading.Thread(target=_clip_worker, daemon=True).start()
            _clip_started = True


def enqueue_clip(job):
    _ensure_clip_worker()
    _clip_q.put(job)


def _run_clip_job(job):
    """真正碰 NSPasteboard 只在这里发生；失败则撤销去重登记，允许对端重推。"""
    kind = job[0]
    if kind == "cb":
        _, name, ckind, blob = job
        if not want_clipboard():
            log("%s 收到剪贴板 %s，但 write_clipboard=false" % (name, ckind))
            return
        res = apply_clip(ckind, blob)
        if res:
            log("落地 %s -> 本机剪贴板（%s %dB cc=%s）"
                % (name, ckind, len(blob), res.get("cc")))
            return
        log("落地 %s 写剪贴板失败，撤销去重登记供对端重推" % name)
        dedupe_drop(name)
        notify("AgentBridge 剪贴板落地失败", name)
        return
    if kind == "files":
        _, paths = job
        if not want_clipboard():
            return
        if write_clipboard(paths):
            threading.Thread(target=verify_and_receipt, args=(list(paths),), daemon=True).start()


# ---------- 统一入口 ----------

def _err(code, error):
    return {"code": code, "action": None, "error": error}


def ingest_entry(name, blob, sha_declared=None, orig_b64=""):
    """落地一条条目。调用方把返回的 code 透传给对端 / 决定是否 ack。"""
    if dedupe_hit(name):
        log("条目 %s 已在去重表里，action=duplicate" % name)
        return {"code": 200, "action": "duplicate",
                "sha256": hashlib.sha256(blob).hexdigest()}

    actual = hashlib.sha256(blob).hexdigest()
    if sha_declared and sha_declared.lower() != actual:
        log("!! %s sha256 不符 declared=%s actual=%s，不落地、不登记"
            % (name, sha_declared[:12], actual[:12]))
        notify("AgentBridge 校验失败", name)
        return _err(400, "sha256 mismatch")

    # ---- 跨设备剪贴板 ----
    mcb = CB_RE.match(name)
    if mcb:
        if mcb.group(1) != "win":
            return _err(404, "unsupported name")
        kind = mcb.group(2)
        limit = TEXT_MAX if kind == "text" else IMAGE_MAX
        if len(blob) == 0 or len(blob) > limit:
            log("剪贴板 %s 超限或为空（%dB / 上限 %dB）" % (kind, len(blob), limit))
            return _err(413, "over limit")
        if not want_clipboard():
            log("%s 收到剪贴板 %s，但 write_clipboard=false" % (name, kind))
            return _err(422, "clipboard writing disabled")
        dedupe_add(name)
        enqueue_clip(("cb", name, kind, bytes(blob)))
        log("受理 %s（剪贴板 %s %dB），写入已交后台" % (name, kind, len(blob)))
        return {"code": 200, "action": "clipboard", "sha256": actual}

    # ---- 跨屏拖拽信令：drag_<id>_{begin,sync,drop,cancel}.json ----
    if B.DRAG_RE.match(name):
        try:
            obj = json.loads(blob.decode("utf-8", "replace"))
        except Exception as e:
            log("拖拽信令 %s 解析失败: %s" % (name, e))
            return _err(400, "bad drag json")
        dedupe_add(name)
        drag_spool(name, obj)
        log("受理拖拽信令 %s" % name)
        return {"code": 200, "action": "drag", "sha256": actual}

    # ---- 清单：只记账 ----
    if XFR_RE.match(name):
        try:
            man = json.loads(blob.decode("utf-8", "replace"))
            log("清单 %s：%d 项 -> %s"
                % (name, man.get("count", 0),
                   ", ".join(i.get("orig", "?") for i in man.get("items", []))))
        except Exception as e:
            log("清单 %s 解析失败: %s" % (name, e))
        dedupe_add(name)
        return {"code": 200, "action": "manifest", "sha256": actual}

    # ---- 文件载荷 ----
    if name.startswith("file_"):
        if len(blob) == 0 or len(blob) > FILE_MAX:
            log("文件 %s 超限或为空（%dB）" % (name, len(blob)))
            return _err(413, "over limit")
        dst = unique_path(orig_name(orig_b64, name))
        try:
            with open(dst, "wb") as f:
                f.write(blob)
        except OSError as e:
            log("落地 %s 失败: %s" % (dst, e))
            return _err(422, "write failed: %s" % e)
        dedupe_add(name)
        log("落地 %s -> %s (%dB sha=%s..)"
            % (name, os.path.basename(dst), len(blob), actual[:12]))
        notify("AgentBridge 收到文件", os.path.basename(dst))
        enqueue_clip(("files", [dst]))
        return {"code": 200, "action": "inbox", "sha256": actual}

    # ---- 非载荷名 ----
    # 命名不像载荷（file_/cb_/xfer_）时有两种来历：
    #   队列轮询：不可能走到这里（inboxwatch 已按 is_payload 过滤）；
    #   直达推送：回 404「名字不认识」，与对端语义对称，由对端退回队列，
    #             普通消息 <ts>_win.md 最终仍会进 win2mac 队列等人读，不丢。
    log("拒绝 %s：名字不像载荷（非 cb_/file_/xfer_）" % name)
    return _err(404, "unsupported name")
