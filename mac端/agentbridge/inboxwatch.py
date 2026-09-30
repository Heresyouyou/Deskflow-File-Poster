#!/usr/bin/env python3
"""win2mac -> 本机（队列轮询兜底路径）。

协议 v2 起，正常流程由对端直推 8899 的 /api/push 当场落地（见 ingest.py /
bridge_server.py）。本进程只负责**兜底**：仍有条目落进 win2mac 队列时
（对端直推失败降级、或对端只写队列），轮询取回 -> ingest_entry -> 按返回码 ack。

落地细节全部在 ingest.py，这里只做「下载 / 去重失败计数 / ack」。
"""
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/AgentBridge"))
import bridge_lib as B  # noqa: E402
import ingest  # noqa: E402

QUEUE = "win2mac"
POLL = 2.0
MAX_FAILS = 3
FAILS = {}          # name -> 连续失败次数；>=3 不再重试，留队列给人工

log = ingest.log


def main():
    os.makedirs(ingest.INBOX, exist_ok=True)
    log("inboxwatch 启动（队列轮询兜底，%ss，写剪贴板=%s，直推为主路径）"
        % (POLL, ingest.want_clipboard()))

    while True:
        try:
            msgs = B.listing(QUEUE)
        except Exception as e:
            log("列举失败（跳过本轮）: %s" % e)
            time.sleep(POLL)
            continue

        for m in msgs:
            name = m["name"]
            # 只处理「载荷」；普通消息 <ts>_win.md 留在队列里给人读，不进 _done
            if not ingest.is_payload(name):
                continue
            if FAILS.get(name, 0) >= MAX_FAILS:
                continue
            try:
                sha_declared, orig_b64, blob = B.get_raw(QUEUE, name)
            except Exception as e:
                log("下载 %s 失败，本轮中断，下轮重试: %s" % (name, e))
                break

            try:
                res = ingest.ingest_entry(name, blob, sha_declared, orig_b64)
            except Exception as e:
                log("落地 %s 异常（不 ack）: %s" % (name, e))
                res = {"code": 500, "error": str(e)}
            code = int(res.get("code") or 500)

            if code == 200:
                FAILS.pop(name, None)
                B.ack(QUEUE, name, log)
            else:
                FAILS[name] = FAILS.get(name, 0) + 1
                if FAILS[name] <= 1:
                    log("%s 落地失败 code=%s %s（不 ack，已失败 %d/%d）"
                        % (name, code, res.get("error") or "", FAILS[name], MAX_FAILS))

        time.sleep(POLL)


if __name__ == "__main__":
    main()
