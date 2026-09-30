# -*- coding: utf-8 -*-
"""协议 v2 接收器本机自检（回归基线）：起 8900，逐项打真请求并断言。

覆盖：探针 GET、鉴权、目录穿越、不认识的命名、文本、图片、幂等重推、
      文件落地 + X-Orig-Name-B64、清单、坏 sha、超限。
"""
import hashlib
import http.client
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipwatch as cw

cw.setup_logging(verbose_console=True)
srv = cw.start_push_server()
assert srv, 'listener failed'
time.sleep(0.3)

HOST, PORT = '127.0.0.1', cw.PUSH_PORT
FAIL = []


def check(label, got, want):
    ok = got == want
    print('%s %-28s got=%r want=%r' % ('PASS' if ok else 'FAIL', label, got, want))
    if not ok:
        FAIL.append(label)


def req(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    d = r.read()
    conn.close()
    return r.status, d


def post(name, blob, token=cw.TOKEN, sha=None, orig=None):
    h = {'X-Bridge-Token': token, 'Content-Length': str(len(blob)),
         'Content-Type': 'application/octet-stream',
         'X-Sha256': sha if sha is not None else hashlib.sha256(blob).hexdigest()}
    if orig:
        h['X-Orig-Name-B64'] = cw.b64_name(orig)
    return req('POST', '/api/push/' + cw.quote(name, safe=''), body=blob, headers=h)


stamp = time.strftime('%Y%m%d-%H%M%S')

# ---- 探针 / 鉴权 / 名字守卫
check('probe GET', req('GET', '/api/push', headers={'X-Bridge-Token': cw.TOKEN})[0], 200)
check('probe bad token', req('GET', '/api/push', headers={'X-Bridge-Token': 'x'})[0], 401)
check('post bad token', post('cb_%s-001_mac_text.txt' % stamp, b'x', token='bad')[0], 401)
check('dir traversal', post('../evil.txt', b'x')[0], 400)
check('unknown name', post('%s_mac.md' % stamp, b'# hi')[0], 404)
check('empty body', post('cb_%s-001_mac_text.txt' % stamp, b'')[0], 413)

# ---- 文本
txt = '直达推送自检 🚀 第二行'.encode('utf-8')
tname = 'cb_%s-101_mac_text.txt' % stamp
check('text 200', post(tname, txt)[0], 200)
time.sleep(0.1)
got = cw.read_clipboard_payload() or {}
check('text landed', (got.get('kind'), got.get('text')), ('text', txt.decode('utf-8')))
print('     echo set =', cw._PUSH_ECHO)
check('text echo fp', ('text:' + cw.sha1_bytes(txt)) in cw._PUSH_ECHO, True)
check('text dedup 200', post(tname, txt)[0], 200)
check('text dedup action', json.loads(post(tname, txt)[1].decode())['action'], 'duplicate')

# ---- 图片
from PIL import Image
buf = io.BytesIO()
Image.new('RGB', (32, 24), (10, 200, 40)).save(buf, 'PNG')
png = buf.getvalue()
iname = 'cb_%s-102_mac_image.png' % stamp
check('image 200', post(iname, png)[0], 200)
time.sleep(0.1)
got = cw.read_clipboard_payload() or {}
check('image landed', got.get('kind'), 'image')
a = Image.open(io.BytesIO(png)); a.load()
b = Image.open(io.BytesIO(got.get('png') or b'')); b.load()
check('image pixels', (a.size == b.size and a.convert('RGB').tobytes() == b.convert('RGB').tobytes()), True)
check('image echo fp', cw.payload_fp(got) in cw._PUSH_ECHO, True)

# ---- 文件（原名走 X-Orig-Name-B64）
blob = b'push test payload\n' * 100
fname = 'file_%s_pushselftest.bin' % stamp
orig = '中文名自检.bin'
check('file 200', post(fname, blob, orig=orig)[0], 200)
check('file pending', cw._PUSH_FILES[:1] and os.path.basename(cw._PUSH_FILES[0]), orig)
dest = os.path.join(cw.DIR_INBOX, orig)
check('file on disk', os.path.exists(dest), True)
with open(dest, 'rb') as f:
    check('file bytes', f.read() == blob, True)
os.remove(dest)

# ---- 清单
mname = 'xfer_%s.json' % stamp
check('manifest 200', post(mname, json.dumps({'id': 'x'}).encode('utf-8'))[0], 200)
check('manifest action', json.loads(post(mname, json.dumps({'id': 'x'}).encode('utf-8'))[1].decode())['action'], 'duplicate')

# ---- 坏 sha / 超限
check('bad sha', post('cb_%s-103_mac_text.txt' % stamp, b'abc', sha='0' * 64)[0], 400)
check('text over limit', post('cb_%s-104_mac_text.txt' % stamp, b'a' * (cw.CLIP_TEXT_MAX + 1))[0], 413)
check('bad sha not remembered', ('cb_%s-103_mac_text.txt' % stamp) in cw._PUSH_SEEN, False)

# ---- keep-alive 复用：同一条连接连推 3 条，必须都是 200（对端会复用连接省掉建连开销）
conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
statuses = []
for i in range(3):
    b = ('ka-%d' % i).encode('utf-8')
    nm = 'cb_%s-20%d_mac_text.txt' % (stamp, i)
    conn.request('POST', '/api/push/' + cw.quote(nm, safe=''), body=b,
                 headers={'X-Bridge-Token': cw.TOKEN, 'Content-Length': str(len(b)),
                          'X-Sha256': hashlib.sha256(b).hexdigest()})
    r = conn.getresponse()
    r.read()
    statuses.append(r.status)
conn.close()
check('keep-alive x3', statuses, [200, 200, 200])

# ---- 一次拒收之后，新连接必须立刻可用（拒收要关连接，不能把接收器带坏）
check('after reject fresh conn', post('cb_%s-300_mac_text.txt' % stamp, b'ok')[0], 200)

print('\n_PUSH_SEEN =', sorted(cw._PUSH_SEEN))
print('FAIL =', FAIL if FAIL else 'none')
srv.shutdown()
sys.exit(1 if FAIL else 0)