#!/usr/bin/env python3
"""AgentBridge 公共库（Mac 侧）：命名、目录打包、PUT 发送、列举、raw 下载。

接口基线见 README.md（已与 Windows 侧冻结）。
超时系数用 Windows 侧实测吞吐（43.4 MB/s，保守按 5 MB/s 规划）：
30s + size_bytes / 5 MB/s，重试 3 次、退避 1/3/9 秒。
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BRIDGE = "http://192.168.10.153:8899"
PEER_PUSH = "http://192.168.10.100:8900"   # 协议 v2：Windows 侧直达推送接收器
DIRECT_PUSH = True                          # Mac->Windows 先直推，失败/404 退回本机队列
TOKEN = "CHANGE_ME_BRIDGE_TOKEN"
ROOT = os.path.expanduser("~/AgentBridge")

JUNK_RE = re.compile(r"(^\.DS_Store$)|(^\._)|(^\.localized$)|(\.crdownload$)|(\.part$)|(\.download$)")
# 协议 v2：只有「载荷」才值得直推；普通消息 <ts>_mac.md / <ts>_mac-clipack.md
# 对端接收器不认（回 404），直推只会白吃一次往返，直接走本机队列。
PUSH_PAYLOAD_RE = re.compile(
    r"^(cb_\d{8}-\d{6}-\d{3}_(mac|win)_(text|image)\.(txt|png)"
    r"|file_\d{8}-\d{6}_.+|xfer_.+\.json)$")
NAME_RE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]")

TIMEOUT_BASE = 30.0      # 秒
TIMEOUT_PER_MB = 0.2     # 秒 / MB（= 5 MB/s 规划速率）
RETRY = 3
BACKOFF = (1, 3, 9)      # 秒


def timestamp(t=None):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t))


def is_junk(path):
    return bool(JUNK_RE.search(os.path.basename(path.rstrip("/"))))


def safe(name):
    name = NAME_RE.sub("_", name).lstrip(".") or "item"
    return name[:80]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_push_payload(name):
    return bool(PUSH_PAYLOAD_RE.match(name))


def q(name):
    """名字里可能有中文，URL 路径段必须百分号编码。"""
    return urllib.parse.quote(name, safe="")


def _open(url, data=None, method="GET", headers=None, timeout=60):
    req = urllib.request.Request(BRIDGE + url, data=data, method=method)
    req.add_header("X-Bridge-Token", TOKEN)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def push_bytes(name, blob, orig_name=None, sha256=None, timeout=None, log=print):
    """协议 v2：把条目直推到对端（Windows 8900 /api/push/<名字>）。

    收到即对端当场落地，省掉「写本机队列 + 对端轮询」的那 2 s。
    任何非 200 / 连不上都抛异常，由调用方退回队列写入。
    """
    if timeout is None:
        timeout = TIMEOUT_BASE + TIMEOUT_PER_MB * (len(blob) / (1024.0 * 1024.0))
    headers = {
        "X-Bridge-Token": TOKEN,
        "X-Sha256": sha256 or hashlib.sha256(blob).hexdigest(),
        "Content-Type": "application/octet-stream",
    }
    if orig_name:
        headers["X-Orig-Name-B64"] = base64.b64encode(orig_name.encode("utf-8")).decode("ascii")
    req = urllib.request.Request("%s/api/push/%s" % (PEER_PUSH, q(name)), data=blob, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.status != 200:
            raise RuntimeError("push HTTP %s %s" % (r.status, body[:200]))
        return json.loads(body or b"{}")


def put_bytes(queue, name, blob, orig_name=None, sha256=None, timeout=None, log=print):
    """发送一条。mac2win 方向先直推对端，失败再 PUT 本机队列（协议 v2 降级链）。"""
    if timeout is None:
        timeout = TIMEOUT_BASE + TIMEOUT_PER_MB * (len(blob) / (1024.0 * 1024.0))
    if DIRECT_PUSH and queue == "mac2win" and is_push_payload(name):
        try:
            res = push_bytes(name, blob, orig_name, sha256, timeout, log)
            log("直推 %s (%dB) -> action=%s，未写队列"
                % (name, len(blob), res.get("action") or "?"))
            return res
        except Exception as e:
            log("直推 %s 失败（%s），退回本机 %s 队列" % (name, e, queue))
    headers = {}
    if orig_name:
        headers["X-Orig-Name-B64"] = base64.b64encode(orig_name.encode("utf-8")).decode("ascii")
    if sha256:
        headers["X-Sha256"] = sha256
    last = None
    for attempt in range(RETRY):
        try:
            with _open("/api/%s/%s" % (queue, q(name)), blob, "PUT", headers, timeout) as r:
                body = r.read()
                if r.status != 200:
                    raise RuntimeError("HTTP %s %s" % (r.status, body[:200]))
            return json.loads(body)
        except Exception as e:  # 连接失败 / 超时 / 非 200
            last = e
            log("PUT %s/%s 第 %d/%d 次失败: %s" % (queue, name, attempt + 1, RETRY, e))
            if attempt + 1 < RETRY:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise RuntimeError("PUT %s/%s 连续 %d 次失败: %s" % (queue, name, RETRY, last))


def listing(queue, since=None):
    url = "/api/%s" % queue
    if since:
        url += "?since=" + urllib.parse.quote(since)
    with _open(url, timeout=15) as r:
        return json.loads(r.read())["messages"]


def get_raw(queue, name, timeout=600):
    """下载二进制，返回 (X-Sha256, X-Orig-Name-B64, bytes)。"""
    with _open("/api/%s/%s?raw=1" % (queue, q(name)), timeout=timeout) as r:
        return (r.headers.get("X-Sha256") or "", r.headers.get("X-Orig-Name-B64") or "", r.read())


def get_text(queue, name, timeout=30):
    with _open("/api/%s/%s" % (queue, q(name)), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def ack(queue, name, log=print):
    try:
        with _open("/api/%s/%s/ack" % (queue, q(name)), b"", "POST", timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log("ACK %s/%s 失败: HTTP %s" % (queue, name, e.code))
        return None


def package_dir(path, log=print):
    """ditto 打包目录，返回临时 zip 完整路径（调用方负责删临时目录）。"""
    path = path.rstrip("/")
    base = os.path.basename(path)
    tmpdir = tempfile.mkdtemp(prefix="abbzip-")
    zip_path = os.path.join(tmpdir, base + ".zip")
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", path, zip_path], check=True)
    return zip_path


def send_paths(paths, log=print):
    """把本地路径（文件/目录混合）发到 mac2win。目录先 ditto 打包，两端一律不自动解包。"""
    ts = timestamp()
    items = []
    temps = []
    for p in paths:
        p = p.rstrip("/") or p
        if os.path.isdir(p):
            try:
                z = package_dir(p, log)
            except (subprocess.CalledProcessError, OSError) as e:
                log("打包失败 %s: %s" % (p, e))
                continue
            temps.append(z)
            items.append({"kind": "dir", "src": p, "send": z, "orig": os.path.basename(z)})
        elif os.path.isfile(p):
            items.append({"kind": "file", "src": p, "send": p, "orig": os.path.basename(p)})
        else:
            log("跳过（不存在或不可读）: %s" % p)
    if not items:
        return 0
    try:
        used = set()
        for i, it in enumerate(items):
            nm = "file_%s_%s" % (ts, safe(it["orig"]))
            if nm in used:
                nm = "file_%s_%02d_%s" % (ts, i + 1, safe(it["orig"]))
            used.add(nm)
            it["bridge_name"] = nm
            it["size"] = os.path.getsize(it["send"])
            it["sha256"] = sha256_of(it["send"])

        if len(items) > 1:
            digest = hashlib.sha256("|".join(it["bridge_name"] for it in items).encode()).hexdigest()[:6]
            xid = "%s-%s" % (ts, digest)
            manifest = {
                "id": xid, "ts": ts, "source": "mac", "count": len(items),
                "items": [{"name": it["bridge_name"], "orig": it["orig"], "kind": it["kind"],
                           "size": it["size"], "sha256": it["sha256"]} for it in items],
            }
            blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            put_bytes("mac2win", "xfer_%s.json" % xid, blob, log=log)
            log("清单 xfer_%s.json 已发（%d 项）" % (xid, len(items)))

        for it in items:
            with open(it["send"], "rb") as f:
                blob = f.read()
            r = put_bytes("mac2win", it["bridge_name"], blob, it["orig"], it["sha256"], log=log)
            log("已发 %s (%dB sha=%s..)" % (it["bridge_name"], it["size"], (r.get("sha256") or "")[:12]))
        return len(items)
    finally:
        for t in temps:
            shutil.rmtree(os.path.dirname(t), ignore_errors=True)
