#!/usr/bin/env python3
"""监视 win2mac 队列，有新消息就弹 macOS 通知。"""
import os
import subprocess
import time

INBOX = os.path.expanduser("~/AgentBridge/win2mac")


def notify(title, body):
    body = body.replace('"', "'").replace("\\", "/")[:300]
    subprocess.run(
        ["osascript", "-e",
         'display notification "%s" with title "%s" sound name "Ping"' % (body, title)],
        capture_output=True,
    )


seen = set(os.listdir(INBOX))
print("watcher up, 已忽略 %d 个历史文件" % len(seen), flush=True)
while True:
    time.sleep(3)
    try:
        cur = {n for n in os.listdir(INBOX) if n.endswith(".md")}
    except FileNotFoundError:
        continue
    for n in sorted(cur - seen):
        try:
            with open(os.path.join(INBOX, n), encoding="utf-8", errors="replace") as f:
                head = " ".join(f.read().split())[:200]
        except OSError:
            head = "(读取失败)"
        notify("AgentBridge: Windows 发来消息", n + " -- " + head)
        print("new: " + n, flush=True)
    seen = cur
