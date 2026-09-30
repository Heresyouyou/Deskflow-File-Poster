#!/usr/bin/env python3
"""AgentBridge 消息桥 v2（Mac 端服务）

共享目录：~/AgentBridge/{mac2win,win2mac}/
接口基线（已与 Windows 侧冻结）：
    GET  /api/health
    GET  /api/<queue>?since=<name>
    GET  /api/<queue>/<name>            -> text/markdown
    GET  /api/<queue>/<name>?raw=1      -> application/octet-stream + X-Sha256
    PUT  /api/<queue>/<name>            -> {ok,name,bytes,sha256}
    POST /api/<queue>                   自动命名发消息
    POST /api/<queue>/<name>/ack        移入 <queue>/_done/
所有请求需带 X-Bridge-Token。
"""
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.expanduser("~/AgentBridge"))
import ingest  # noqa: E402  条目落地逻辑（队列轮询与直达推送共用）

ROOT = os.path.expanduser("~/AgentBridge")
TOKEN = "CHANGE_ME_BRIDGE_TOKEN"
BIND = "192.168.10.153"
PORT = 8899

QUEUES = ("mac2win", "win2mac")
MAX_BYTES = 256 * 1024 * 1024          # 单条上限 256 MB
QUEUE_QUOTA = 4 * 1024 * 1024 * 1024   # 每队列 4 GB
PART_TTL = 24 * 3600                   # .part 残留 TTL
DONE_TTL = 7 * 24 * 3600               # _done 归档保留 7 天
CHUNK = 1024 * 1024
LOGFILE = os.path.join(ROOT, "bridge.log")
METADIR = os.path.join(ROOT, ".meta")

for _q in QUEUES:
    os.makedirs(os.path.join(ROOT, _q, "_done"), exist_ok=True)
os.makedirs(METADIR, exist_ok=True)


def log(line):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (stamp, line))
    print("[%s] %s" % (stamp, line), flush=True)


def safe_name(name):
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", unquote(name).strip())
    return (name.lstrip(".") or "msg")[:100]


def queue_dir(queue):
    return os.path.join(ROOT, queue)


def listing(queue, since=None):
    d = queue_dir(queue)
    out = []
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if not os.path.isfile(p) or n.endswith(".part"):
            continue
        if since and n <= since:
            continue
        st = os.stat(p)
        out.append({
            "name": n,
            "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "mtime_ts": st.st_mtime,
        })
    return out


def queue_bytes(queue):
    total = 0
    for root, _dirs, files in os.walk(queue_dir(queue)):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def sha_of(path):
    """带缓存的文件 sha256，缓存键含 mtime 以便失效。"""
    key = os.path.join(METADIR, os.path.basename(path) + ".sha256")
    mtime = int(os.path.getmtime(path))
    try:
        with open(key, encoding="utf-8") as f:
            stamp, value = f.read().split()
        if int(stamp) == mtime:
            return value
    except (OSError, ValueError):
        pass
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    value = h.hexdigest()
    with open(key, "w", encoding="utf-8") as f:
        f.write("%d %s" % (mtime, value))
    return value


def cleaner():
    """定期清理 .part 残留与过期归档。"""
    while True:
        now = time.time()
        for q in QUEUES:
            d = queue_dir(q)
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                p = os.path.join(d, n)
                if os.path.isfile(p) and n.endswith(".part") and now - os.path.getmtime(p) > PART_TTL:
                    os.remove(p)
                    log("clean .part %s/%s (>%dh)" % (q, n, PART_TTL // 3600))
            done = os.path.join(d, "_done")
            if os.path.isdir(done):
                for n in os.listdir(done):
                    p = os.path.join(done, n)
                    if os.path.isfile(p) and now - os.path.getmtime(p) > DONE_TTL:
                        os.remove(p)
                        log("clean _done/%s/%s (>%dd)" % (q, n, DONE_TTL // 86400))
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBridge/2.0"
    protocol_version = "HTTP/1.1"
    timeout = 300
    # 对端实测：小包 + 延迟 ACK 让每条请求白吃 ~40 ms。
    # 根因是 header 与 body 分两次 write（wbufsize=0 → 每次 write 一次 syscall）。
    # 攒到一块再 flush，并关掉 Nagle。
    wbufsize = 64 * 1024
    disable_nagle_algorithm = True

    # ---------- 基础响应 ----------
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.wfile.flush()

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2).encode(),
                   "application/json; charset=utf-8", extra)

    def _ok(self):
        if self.headers.get("X-Bridge-Token") == TOKEN:
            return True
        self._json(401, {"error": "bad or missing X-Bridge-Token"})
        return False

    def _parts(self):
        return [p for p in urlparse(self.path).path.split("/") if p]

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _orig_extra(self, name):
        """原始文件名（PUT 时由 X-Orig-Name-B64 落盘），回给取件方。"""
        try:
            with open(os.path.join(METADIR, name + ".origname"), encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return {"X-Orig-Name-B64": v}
        except OSError:
            pass
        return {}

    # ---------- GET ----------
    def do_GET(self):
        if not self._ok():
            return
        parts = self._parts()
        q = self._query()
        if parts == ["api", "health"]:
            return self._json(200, {"ok": True, "ts": time.time(), "queues": list(QUEUES),
                                    "max_bytes": MAX_BYTES, "quota": QUEUE_QUOTA})
        # 协议 v2 探针：GET /api/push —— 供对端验「接收器在线 + 防火墙放行」
        if parts == ["api", "push"]:
            return self._json(200, {"ok": True, "service": "agentbridge-push", "port": PORT})
        if not parts:
            try:
                with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
                    return self._send(200, f.read().encode(), "text/markdown; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, b"# AgentBridge\n", "text/markdown; charset=utf-8")
        if parts[:1] != ["api"]:
            return self._json(404, {"error": "unknown path", "path": self.path})

        if len(parts) == 2 and parts[1] in QUEUES:
            since = (q.get("since") or [None])[0]
            after = (q.get("after") or [None])[0]
            msgs = listing(parts[1], since)
            if after:
                try:
                    cut = float(after)
                    msgs = [m for m in msgs if m["mtime_ts"] > cut]
                except ValueError:
                    pass
            return self._json(200, {"queue": parts[1], "since": since, "after": after,
                                    "messages": msgs})

        if len(parts) == 3 and parts[1] in QUEUES:
            name = safe_name(parts[2])
            p = os.path.join(queue_dir(parts[1]), name)
            if not os.path.isfile(p) or name.endswith(".part"):
                return self._json(404, {"error": "no such message", "name": name})
            if (q.get("raw") or ["0"])[0] in ("1", "true", "yes"):
                sha = sha_of(p)
                size = os.path.getsize(p)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("X-Sha256", sha)
                for k, v in self._orig_extra(name).items():
                    self.send_header(k, v)
                self.end_headers()
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                self.wfile.flush()
                log("GET  %s/%s raw (%dB sha=%s..)" % (parts[1], name, size, sha[:12]))
                return
            with open(p, encoding="utf-8", errors="replace") as f:
                body = f.read()
            extra = {"X-Sha256": sha_of(p)}
            extra.update(self._orig_extra(name))
            return self._send(200, body.encode(), "text/markdown; charset=utf-8", extra)

        return self._json(404, {"error": "unknown path", "path": self.path})

    # ---------- PUT ----------
    def do_PUT(self):
        if not self._ok():
            return
        parts = self._parts()
        if not (len(parts) == 3 and parts[0] == "api" and parts[1] in QUEUES):
            return self._json(404, {"error": "use PUT /api/<mac2win|win2mac>/<name>"})
        queue = parts[1]
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if total <= 0:
            return self._json(411, {"error": "Content-Length required"})
        if total > MAX_BYTES:
            return self._json(413, {"error": "payload over %d bytes" % MAX_BYTES,
                                    "max_bytes": MAX_BYTES})
        if queue_bytes(queue) + total > QUEUE_QUOTA:
            return self._json(507, {"error": "queue quota exceeded", "quota": QUEUE_QUOTA})

        name = safe_name(parts[2])
        target = os.path.join(queue_dir(queue), name)
        tmp = target + ".part"
        h = hashlib.sha256()
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < total:
                    chunk = self.rfile.read(min(CHUNK, total - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._json(500, {"error": "write failed: %s" % e})

        if got != total:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._json(400, {"error": "short body", "expected": total, "got": got})

        sha = h.hexdigest()
        declared = (self.headers.get("X-Sha256") or "").strip().lower()
        if declared and declared != sha:
            os.remove(tmp)
            log("PUT  %s/%s sha mismatch declared=%s actual=%s" % (queue, name, declared[:12], sha[:12]))
            return self._json(409, {"error": "sha256 mismatch", "expected": declared, "actual": sha})

        os.replace(tmp, target)
        with open(os.path.join(METADIR, name + ".sha256"), "w", encoding="utf-8") as f:
            f.write("%d %s" % (int(os.path.getmtime(target)), sha))

        orig = ""
        raw_orig = self.headers.get("X-Orig-Name-B64")
        if raw_orig:
            try:
                orig = base64.b64decode(raw_orig).decode("utf-8", "replace")
            except Exception:
                orig = "(decode failed)"
        if raw_orig:
            try:
                with open(os.path.join(METADIR, name + ".origname"), "w", encoding="utf-8") as f:
                    f.write(raw_orig.strip())
            except OSError:
                pass
        log("PUT  %s/%s (%dB sha=%s..%s)" % (queue, name, got, sha[:12],
                                             (" orig=%s" % orig) if orig else ""))
        return self._json(200, {"ok": True, "queue": queue, "name": name,
                                "bytes": got, "sha256": sha})

    # ---------- 协议 v2：直达推送 ----------
    def _do_push(self, parts):
        """POST /api/push/<条目名>。名字 URL 编码；必需 X-Sha256；
        非 200 一律 Connection: close（客户端据此决定是否复用连接）。"""
        if len(parts) != 3:
            return self._push_resp(404, {"error": "use POST /api/push/<name>"})
        name = unquote(parts[2]).strip()
        if not name:
            return self._push_resp(400, {"error": "empty name", "name": parts[2]})
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._push_resp(400, {"error": "bad Content-Length", "name": name})
        if total <= 0:
            return self._push_resp(411, {"error": "Content-Length required", "name": name})
        if total > MAX_BYTES:
            return self._push_resp(413, {"error": "payload over %d bytes" % MAX_BYTES,
                                         "name": name, "max_bytes": MAX_BYTES})
        buf = bytearray()
        got = 0
        try:
            while got < total:
                chunk = self.rfile.read(min(CHUNK, total - got))
                if not chunk:
                    break
                buf += chunk
                got += len(chunk)
        except OSError as e:
            return self._push_resp(500, {"error": "read failed: %s" % e, "name": name})
        if got != total:
            return self._push_resp(400, {"error": "short body", "expected": total,
                                         "got": got, "name": name})

        sha_declared = (self.headers.get("X-Sha256") or "").strip() or None
        orig_b64 = self.headers.get("X-Orig-Name-B64") or ""
        try:
            res = ingest.ingest_entry(name, bytes(buf), sha_declared, orig_b64)
        except Exception as e:
            log("PUSH %s 落地异常: %s" % (name, e))
            return self._push_resp(500, {"error": "ingest failed: %s" % e, "name": name})

        code = int(res.get("code") or 500)
        body = {"ok": code == 200, "name": name, "action": res.get("action")}
        if res.get("error"):
            body["error"] = res["error"]
        if res.get("sha256"):
            body["sha256"] = res["sha256"]
        log("PUSH %s (%dB) -> %d %s%s" % (name, got, code, res.get("action") or "",
                                          (" err=%s" % res["error"]) if res.get("error") else ""))
        return self._push_resp(code, body)

    def _push_resp(self, code, obj):
        if code != 200:
            self.close_connection = True
            obj = dict(obj)
            obj.setdefault("connection", "close")
            return self._json(code, obj, {"Connection": "close"})
        return self._json(code, obj)

    # ---------- POST ----------
    def do_POST(self):
        if not self._ok():
            return
        parts = self._parts()
        # 协议 v2：POST /api/push/<条目名> —— 收到即当场落地，不进队列等轮询
        if len(parts) >= 2 and parts[:2] == ["api", "push"]:
            return self._do_push(parts)
        if not (parts[:1] == ["api"] and len(parts) >= 2 and parts[1] in QUEUES):
            return self._json(404, {"error": "unknown path", "path": self.path})
        queue = parts[1]

        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if total > MAX_BYTES:
            return self._json(413, {"error": "payload over %d bytes" % MAX_BYTES})
        body = self.rfile.read(total) if total else b""

        # ack: POST /api/<queue>/<name>/ack
        if len(parts) == 4 and parts[3] == "ack":
            name = safe_name(parts[2])
            src = os.path.join(queue_dir(queue), name)
            if not os.path.isfile(src):
                return self._json(404, {"error": "no such message", "name": name})
            dst = os.path.join(queue_dir(queue), "_done", name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            for suffix in (".sha256", ".origname"):
                try:
                    os.remove(os.path.join(METADIR, name + suffix))
                except OSError:
                    pass
            log("ACK  %s/%s -> _done/" % (queue, name))
            return self._json(200, {"ok": True, "queue": queue, "name": name,
                                    "moved_to": "_done/" + name})

        # 自动命名发消息
        if len(parts) == 2:
            frm = safe_name(self.headers.get("X-From") or queue.split("2")[0])
            name = time.strftime("%Y%m%d-%H%M%S") + "_" + frm + ".md"
            if os.path.exists(os.path.join(queue_dir(queue), name)):
                name = time.strftime("%Y%m%d-%H%M%S") + "_" + frm + "_%03d.md" % (int(time.time() * 1000) % 1000)
            with open(os.path.join(queue_dir(queue), name), "wb") as f:
                f.write(body)
            log("POST %s/%s (%dB)" % (queue, name, len(body)))
            return self._json(200, {"ok": True, "queue": queue, "name": name, "bytes": len(body)})

        return self._json(404, {"error": "unknown path", "path": self.path})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=cleaner, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log("bridge v2 up on http://%s:%d root=%s max=%dMB quota=%dGB" %
        (BIND, PORT, ROOT, MAX_BYTES // 1048576, QUEUE_QUOTA // 1073741824))
    srv.serve_forever()
