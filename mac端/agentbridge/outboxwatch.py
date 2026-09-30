#!/usr/bin/env python3
"""Outbox → Windows。

监视 ~/Downloads/KEEPPER-Outbox/：丢进去的文件/目录，待大小与 mtime 连续两次采样不变
（写稳）后发到 mac2win，随后移入 Outbox/.sent/ 留痕。
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.expanduser("~/AgentBridge"))
import bridge_lib as B  # noqa: E402

OUTBOX = os.path.expanduser("~/Downloads/KEEPPER-Outbox")
SENT = os.path.join(OUTBOX, ".sent")
LOG = os.path.join(B.ROOT, "outboxwatch.log")
INTERVAL = 2.0


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def scan():
    """返回 {路径: (大小, mtime)}，跳过隐藏项与垃圾文件。"""
    out = {}
    try:
        names = os.listdir(OUTBOX)
    except OSError:
        return out
    for n in names:
        if n.startswith("."):
            continue
        p = os.path.join(OUTBOX, n)
        if B.is_junk(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        out[p] = (st.st_size, int(st.st_mtime))
    return out


def main():
    os.makedirs(SENT, exist_ok=True)
    log("outboxwatch 启动，监视 %s（%.0fs 一轮，需连续两轮稳定）" % (OUTBOX, INTERVAL))
    prev = {}
    while True:
        time.sleep(INTERVAL)
        cur = scan()
        for p, sig in sorted(cur.items()):
            if prev.get(p) != sig:
                continue                      # 还没写稳，下轮再看
            log("检测到写稳: %s (%dB)" % (os.path.basename(p), sig[0]))
            try:
                n = B.send_paths([p], log=log)
            except Exception as e:
                log("发送失败，保留在 Outbox 下轮重试: %s" % e)
                continue
            if n:
                try:
                    shutil.move(p, os.path.join(SENT, os.path.basename(p)))
                    log("已移入 .sent/: %s" % os.path.basename(p))
                except OSError as e:
                    log("移入 .sent/ 失败（文件已发出）: %s" % e)
        prev = cur


if __name__ == "__main__":
    main()
