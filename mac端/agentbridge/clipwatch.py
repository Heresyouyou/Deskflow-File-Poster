#!/usr/bin/env python3
"""Mac 侧剪贴板 → Windows（files + text + image 三类）。

常驻 JXA 守护 clipwatch.js 内部轮询 changeCount，只在变化时给出一行 JSON；
本进程负责过滤垃圾文件、目录 ditto 打包、串行 PUT 到 mac2win。
文本/图片按跨设备剪贴板协议 v1 走 cb_<ts-mmm>_mac_text.txt / _mac_image.png；
自己在剪贴板里出现的条目（含 inboxwatch 写入的）由 .clip_self.json 记账 + 内容指纹两层跳过。
"""
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

DAEMON = os.path.join(B.ROOT, "clipwatch.js")
LOG = os.path.join(B.ROOT, "clipwatch.log")
STATE = os.path.join(B.ROOT, ".clipwatch_state.json")
SELF_STATE = os.path.join(B.ROOT, ".clip_self.json")

# 跨设备剪贴板协议 v1（与 Windows 侧冻结）：files > image > text
CB_RE = re.compile(r"^cb_\d{8}-\d{6}-\d{3}_(mac|win)_(text|image)\.(txt|png)$")
TEXT_MAX = 256 * 1024          # 文本上限 256 KB（UTF-8 字节）
IMAGE_MAX = 32 * 1024 * 1024   # 图片上限 32 MB（PNG 字节）


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"sent_cc": []}


_STATE_LOCK = threading.Lock()


def save_state(st):
    """读循环与发送线程都会写 state，加锁避免并发写坏 STATE。"""
    st["sent_cc"] = st.get("sent_cc", [])[-50:]
    try:
        with _STATE_LOCK:
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump(st, f)
    except OSError:
        pass


def fingerprint(kind, blob):
    """内容指纹 = kind + ':' + sha1(原始字节)（协议 §4）。"""
    return "%s:%s" % (kind, hashlib.sha1(blob).hexdigest())


def cb_name(kind):
    """cb_<YYYYMMDD-HHMMSS-mmm>_mac_<text|image>.<txt|png>，毫秒固定 3 位。"""
    t = time.time()
    ms = int(round((t - int(t)) * 1000))
    if ms > 999:
        ms = 999
    return "cb_%s-%03d_mac_%s.%s" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(t)),
                                     ms, kind, "png" if kind == "image" else "txt")


def self_fp():
    """读 .clip_self.json 里最近一次自己写入的内容指纹（第二层防回环）。"""
    try:
        with open(SELF_STATE, encoding="utf-8") as f:
            return json.load(f).get("fp")
    except (OSError, ValueError):
        return None


def send_clip_event(st, kind, path):
    """把守护落好的文本/图片临时文件发到 mac2win。超限跳过并记日志。"""
    limit = TEXT_MAX if kind == "text" else IMAGE_MAX
    try:
        size = os.path.getsize(path)
    except OSError as e:
        log("剪贴板 %s 载荷不可读: %s" % (kind, e))
        return
    if size == 0:
        log("剪贴板 %s 载荷为空，跳过" % kind)
        return
    if size > limit:
        log("剪贴板 %s 超限（%dB > %dB），跳过不发送" % (kind, size, limit))
        return

    with open(path, "rb") as f:
        blob = f.read()
    fp = fingerprint(kind, blob)
    if st.get("last_fp") == fp or self_fp() == fp:
        log("剪贴板 %s 内容指纹未变（自己写入的回声），跳过" % kind)
        return

    name = cb_name(kind)
    try:
        r = B.put_bytes("mac2win", name, blob, sha256=hashlib.sha256(blob).hexdigest(), log=log)
        st["last_fp"] = fp
        save_state(st)
        log("已发 %s（%s %dB sha256=%s..）"
            % (name, kind, size, (r.get("sha256") or "")[:12]))
    except Exception as e:
        log("发送剪贴板 %s 失败: %s" % (kind, e))


def spawn():
    return subprocess.Popen(["osascript", "-l", "JavaScript", DAEMON],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)


SEND_Q = queue.Queue()


def sender_loop(st):
    """所有出站网络 IO 都在这里做，与看门狗读循环解耦：
    对端不可达时单条 push 最长 30s×3 重试，也不会冻住本地剪贴板检测。"""
    while True:
        job = SEND_Q.get()
        kind = job[0]
        try:
            if kind == "clip":
                _, ekind, path = job
                try:
                    send_clip_event(st, ekind, path)
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            elif kind == "files":
                _, paths = job
                try:
                    n = B.send_paths(paths, log=log)
                    log("本批发送完成，%d 项" % n)
                except Exception as e:
                    log("发送失败: %s" % e)
        except Exception as e:
            log("发送线程异常: %s" % e)
        finally:
            SEND_Q.task_done()


def main():
    st = load_state()
    threading.Thread(target=sender_loop, args=(st,), daemon=True).start()
    proc = spawn()
    log("clipwatch 启动，JXA 守护 pid=%d" % proc.pid)
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
        if kind == "start":
            log("JXA 守护就绪 changeCount=%s pid=%s" % (ev.get("cc"), ev.get("pid")))
            continue
        if kind == "error":
            log("守护报错: %s" % ev.get("err"))
            continue
        if kind == "self":
            log("changeCount=%s 是自己写入的，跳过（防回环）" % ev.get("cc"))
            continue
        if kind in ("image", "text"):
            cc = ev.get("cc")
            if cc in st.get("sent_cc", []):
                continue
            st.setdefault("sent_cc", []).append(cc)
            save_state(st)
            # 网络 IO 交发送线程：读循环（本地看门狗）不被慢 push 冻住
            SEND_Q.put(("clip", kind, ev.get("path")))
            continue

        if kind != "files":
            continue

        cc = ev.get("cc")
        if cc in st.get("sent_cc", []):
            continue

        paths = [p for p in (ev.get("paths") or []) if not B.is_junk(p)]
        st.setdefault("sent_cc", []).append(cc)
        save_state(st)
        if not paths:
            log("changeCount=%s 只有垃圾文件，跳过" % cc)
            continue

        log("剪贴板出现 %d 项: %s"
            % (len(paths), ", ".join(os.path.basename(p.rstrip("/")) for p in paths)))
        SEND_Q.put(("files", paths))


if __name__ == "__main__":
    main()
