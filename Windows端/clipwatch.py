#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clipwatch.py —— Windows 侧文件互传常驻服务（复用 Mac 的 AgentBridge 通道）

方向 1（Windows -> Mac）
  1. 剪贴板变化（GetClipboardSequenceNumber）-> 按 文件 > 图片 > 文字 取其一发到 win2mac：
     文件走 file_ 通道，图片/文字走 cb_<ts>_win_{image,text}.{png,txt} 通道
  2. outbox 目录投递 -> PUT 到 win2mac
  目录先打包成 <目录名>.zip（shutil，纯 Python，零外部依赖），两端都不自动解包。

剪贴板三类都靠本进程搬运，Deskflow 只做键鼠：它的剪贴板共享会 grab 所有权并 clearContents，
把 CF_HDROP 清掉（实测 1–4 s 内变成只剩 'Deskflow Ownership'），所以两端 clipboardSharing 必须
保持 false（Windows 侧生效键在 settings\\deskflow-server.conf 的 section: options）。

方向 2（Mac -> Windows）
  快路（协议 v2 直达推送）：本机监听 0.0.0.0:8900，Mac 有货直接 POST /api/push/<条目名>，
    延迟 ≈ 一次 RTT，没有轮询造成的时间差；落地成功才回 200，失败回 4xx/5xx 让对端重推或退回队列。
  兜底（协议 v1 队列）：每 2 秒【全量】列举 mac2win（刻意不用 ?since=：两套命名空间
     [0-9]*_mac.md 与 file_* 混排时字典序会永久漏掉普通消息），处理对端离线期间积压的条目。
  两条路共用同一套落地逻辑：file_ 校验 sha256 后原子改名进 inbox 并写 CF_HDROP +
    Preferred DropEffect=0x05 到剪贴板；cb_ 直接写剪贴板（文字 CF_UNICODETEXT / 图片
    CF_DIB + 注册格式 PNG）。同名条目靠「已处理集合」幂等去重，快路已落地的队列副本只 ack。

方向 1 的发送侧同样先走快路：POST 对端 /api/push，不可达才退回 PUT /api/win2mac。

进程形态：pythonw.exe 常驻，无窗口，零 UAC，登录自启。

用法：
  pythonw.clipwatch.py            # 常驻
  python.exe clipwatch.py --once           # 只跑一轮（调试）
  python.exe clipwatch.py --send <路径...> # 一次性发送指定路径（调试）
  python.exe clipwatch.py --status         # 打印连通性 + 状态
  python.exe clipwatch.py --selftest-clip # 剪贴板读写自检
  python.exe clipwatch.py --dragselftest  # 跨屏拖拽自检（自注入 LEFT-UP 走全链路）
"""

import base64
import draghook
import hashlib
import http.client
import json
import logging
import os
import queue
import re
import shutil
import struct
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, unquote, urlsplit

# ---------------------------------------------------------------- 常量

HOST = '192.168.10.153'
PORT = 8899
TOKEN = 'vuVLUCjC3uiNza4c'

# 协议 v2 反向推送：谁有货谁直接 POST 对端 /api/push，接收端不再被动轮询（延迟 ≈ RTT）。
# 直连不通（对端没起、防火墙挡、老版本）时自动退回协议 v1 队列，队列从此只做离线兜底与审计。
PEER_HOST = HOST                     # 对端（Mac）地址
PUSH_PORT = 8900                     # 本机监听端口：接收对端直达推送
PEER_PUSH_PORT = PORT                # 对端 /api/push 端口：Mac 把路由挂在现有 8899 上，没另起 8900
PUSH_PREFIXES = ('cb_', 'file_', 'xfer_', 'drag_')   # 只有载荷才值得直推；普通消息走队列（避免白吃 404）
STREAM_BLOCKSIZE = 1 << 18           # 传输分块 256 KB（实测把上行 12.3 拉到 33.9 MB/s）
PUSH_TIMEOUT = 60.0                  # 直连超时（长连接复用，覆盖单条 256 MB 的最坏耗时）
PUSH_SEEN_MAX = 500                  # 快路「已落地条目名」记忆上限（重启后据此继续去重）

QUEUE_OUT = 'win2mac'      # 我方写入（Windows -> Mac）
QUEUE_IN = 'mac2win'       # 我方读取（Mac -> Windows）

BASE = r'D:\KEEPPER\_filebridge'
DIR_INBOX = os.path.join(BASE, 'inbox')
DIR_OUTBOX = os.path.join(BASE, 'outbox')
DIR_STATE = os.path.join(BASE, 'state')
DIR_LOGS = os.path.join(BASE, 'logs')
DIR_TMP = os.path.join(BASE, 'tmp')
# 跨屏拖拽暂存区：载荷先落这里、松手才搬到目标目录，半包绝不进目标（与 Mac 约定一致）。
DIR_DRAGSTAGE = os.path.join(os.environ.get('LOCALAPPDATA') or DIR_TMP, 'KEEPPER', 'dragstage')
STATE_FILE = os.path.join(DIR_STATE, 'clipwatch.json')

MAX_BYTES = 256 * 1024 * 1024          # 单条上限，与冻结基线一致
PLAN_BYTES_PER_SEC = 5 * 1024 * 1024   # 规划速率 5 MB/s（实测 43.4 MB/s 的 8.7 倍余量）
TIMEOUT_BASE = 30.0                    # 单文件超时 = 30s + size / 5MB/s
LIGHT_TIMEOUT = 8.0                    # 轻量请求（列举/ack/health）超时
QUEUE_LIST_TIMEOUT = 2.5               # 队列列举单独设短超时：对端不可达时别把本地看门狗冻 8 s

POLL_SEC = 2.0                         # 队列兜底轮询间隔（快路是反向推送，这里只处理离线积压）
LOCAL_CLIP_SEC = 0.25                  # 本机剪贴板看门狗节奏：纯本地、无网络成本，从 2 s 收到 0.25 s
RETRY_SLEEPS = (1.0, 3.0, 9.0)         # 首次失败后的 3 次重试退避
DROPEFFECT_COPY = 0x05                 # Copy | Link，避免资源管理器粘贴变剪切

# 延迟写入 + 写后自愈：Deskflow 会回收剪贴板所有权（只留 'Deskflow Ownership'），
# 把我们刚写的 CF_HDROP 清掉，用户此时粘贴拿不到文件。
# 实测清空【不定时】：15:02:26 写入后约 1s 被清；15:12:13 写入后约 4s 被清；
# 而更早那次写入活了 70 s 以上。所以固定延迟复查不可靠，改为窗口内持续复查 + 被清则重写。
HEAL_WATCH_SEC = 12.0      # 写后复查窗口
HEAL_POLL_SEC = 1.0        # 复查采样间隔
HEAL_STABLE_HITS = 6       # 连续命中本批多少次才判定「保住了」（> 实测最长 4 s 清空延迟）
HEAL_MAX_REWRITES = 2      # 最多重写次数（不与 Deskflow 无限对打）

# 剪贴板内容同步（方向 1 的第 3 条来源）：Deskflow 只做键鼠，文字/图片/文件三类都由本进程搬运。
# 冲突根源：Deskflow 一旦 grab 剪贴板就会 clearContents，把非文字/图片格式清掉（实测 CF_HDROP 在
# 1–4 s 内被清成只剩 'Deskflow Ownership'），所以桌面端 clipboardSharing 必须保持 false。
CLIP_TEXT_MAX = 256 * 1024             # 单条文本上限；超限跳过，不把整篇日志当剪贴板搬走
CLIP_IMAGE_MAX = 32 * 1024 * 1024      # 单条图片 PNG 上限
CB_PREFIX = 'cb_'
RE_CB_NAME = re.compile(r'^cb_(\d{8}-\d{6}-\d{3})_(win|mac)_(text|image)\.(txt|png)$')

JUNK_PREFIX = ('~$',)
JUNK_SUFFIX = ('.tmp', '.crdownload', '.part', '.download')
JUNK_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store', 'icon\r'}

RE_FILE_NAME = re.compile(r'^file_\d{8}-\d{6}_(.*)$', re.S)

# inbox 超时自动清理（对齐 Mac 5 min TTL）：只删「落盘载荷」。
# agent 对话机制的文件由 _agentbridge 与桥接队列承载，从不落 inbox；下面这组模式
# 是防御性保留白名单——万一有对话/信令/隐藏文件出现在 inbox，一律不删。
INBOX_TTL = 300.0
RE_INBOX_SKIP = re.compile(r'(?:_win|_mac)\.(?:md|txt)$|^drag_|^msg|^xfer_|^cb_')

log = logging.getLogger('clipwatch')


# ---------------------------------------------------------------- 日志 / 目录 / 状态

def setup_logging(verbose_console=False):
    for d in (DIR_INBOX, DIR_OUTBOX, DIR_STATE, DIR_LOGS, DIR_TMP, DIR_DRAGSTAGE):
        os.makedirs(d, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fh = RotatingFileHandler(os.path.join(DIR_LOGS, 'clipwatch.log'),
                             maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
    log.addHandler(fh)

    if verbose_console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
        log.addHandler(sh)


def load_state():
    default = {
        'last_seq': -1,        # 上次见到的剪贴板序号
        'last_fp': '',         # 上次见到的剪贴板内容指纹（内容没变就不重发）
        'self_seq': -1,        # 我方自己写入剪贴板后的序号（防回环）
        'self_fp': '',         # 我方自己写入的内容指纹（与 self_seq 联合判定）
        'processed': [],       # 已下载并 ack 过的 mac2win 条目名
        'seen_msgs': [],       # 已见过的普通消息名（仅记日志，不 ack）
        'outbox_last': {},     # 发送箱上一轮指纹（稳定性判定）
        'outbox_done': {},     # 发送箱已发出的指纹
        'push_seen': [],       # 快路已落地的条目名（幂等去重，防重推/回环）
    }
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            st = json.load(f)
        for k, v in default.items():
            st.setdefault(k, v)
        return st
    except Exception:
        return default


def save_state(st):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- 工具

def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def clean_name(name):
    """清洗成可放进 URL 路径 / 文件系统的名字（保留 CJK，去掉路径分隔符与控制符）。"""
    bad = set('\\/:*?"<>|\r\n\t')
    name = ''.join('_' if (c in bad or ord(c) < 32) else c for c in name)
    name = name.strip().rstrip('. ').strip()
    if not name:
        name = 'unnamed'
    if len(name) > 120:
        stem, dot, ext = name.rpartition('.')
        if dot and len(ext) <= 12:
            name = stem[:120 - len(ext) - 1] + '.' + ext
        else:
            name = name[:120]
    return name


def unique_path(dirpath, filename):
    """同名冲突：序号插在扩展名前（报告(1).pdf）。"""
    p = os.path.join(dirpath, filename)
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        p = os.path.join(dirpath, '%s(%d)%s' % (stem, i, ext))
        if not os.path.exists(p):
            return p
        i += 1


def downloads_dir():
    """用户的「下载」目录（先查注册表 Shell Folders，兼容本地化名称；失败退回 ~\\Downloads）。"""
    try:
        import winreg
        key = r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            v, _ = winreg.QueryValueEx(k, '{374DE290-123F-4565-9164-39C4925E467B}')
            if v and os.path.isdir(v):
                return v
    except Exception:
        pass
    return os.path.join(os.path.expanduser('~'), 'Downloads')


def is_junk(path):
    base = os.path.basename(path)
    lower = base.lower()
    if lower in JUNK_NAMES:
        return True
    if any(base.startswith(p) for p in JUNK_PREFIX):
        return True
    if any(lower.endswith(s) for s in JUNK_SUFFIX):
        return True
    return False


def is_win_path(p):
    """是否为本机形式的 Windows 绝对路径：盘符 (C:\\ / C:/) 或 UNC (\\\\server\\share)。

    Deskflow 若真把对端剪贴板同步过来，CF_HDROP 里带的是 Mac 路径（/Users/...）；
    这类条目必须直接丢弃，否则构成 A -> B -> A 回环。
    """
    if p.startswith('\\\\'):
        return True
    return len(p) >= 3 and p[0].isalpha() and p[1] == ':' and p[2] in '\\/'


def timeout_for(size):
    return TIMEOUT_BASE + float(max(0, size)) / PLAN_BYTES_PER_SEC


def b64_name(name):
    return base64.b64encode(name.encode('utf-8')).decode('ascii')


def unb64_name(s):
    try:
        return base64.b64decode(s.encode('ascii')).decode('utf-8')
    except Exception:
        return None


def orig_name_from_qname(qname):
    m = RE_FILE_NAME.match(qname)
    return m.group(1) if m else qname


# ---------------------------------------------------------------- 剪贴板

def _open_clip():
    """占住剪贴板（最多重试 5 次）。返回 True 时必须配 CloseClipboard()。"""
    import win32clipboard
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def read_clipboard_files():
    """返回剪贴板里的文件/目录绝对路径列表；非 CF_HDROP 返回 []。"""
    import win32clipboard, win32con
    if not _open_clip():
        return []
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            return []
        data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
        # 兜底过滤空条目（Deskflow 空剪贴板回灌可能 marshall 出空 furl 列表）
        return [p for p in data if p] if data else []
    except Exception:
        return []
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def write_clipboard_files(paths):
    """写 CF_HDROP + Preferred DropEffect(Copy)。两者是两个独立格式，各 SetClipboardData 一次。"""
    import win32clipboard, win32con
    files = ''.join(p + '\0' for p in paths) + '\0'
    payload = struct.pack('<IiiII', 20, 0, 0, 0, 1) + files.encode('utf-16-le')
    cf_effect = win32clipboard.RegisterClipboardFormat('Preferred DropEffect')
    if not _open_clip():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, payload)
        win32clipboard.SetClipboardData(cf_effect, struct.pack('<I', DROPEFFECT_COPY))
        return True
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def clip_seq():
    import win32clipboard
    return win32clipboard.GetClipboardSequenceNumber()


def sha1_bytes(b):
    return hashlib.sha1(b).hexdigest()


def dib_to_png(dib):
    """CF_DIB（BITMAPINFOHEADER 起的裸 DIB）-> PNG 字节；失败返回 None。

    DIB 没有 BMP 的 14 字节文件头，PIL 不认，所以按 biSize / 调色板长度补一个 BM 头再交给 PIL。
    """
    import io
    from PIL import Image
    if not dib or len(dib) < 40:
        return None
    hdr_size = int.from_bytes(dib[0:4], 'little')
    if hdr_size not in (40, 52, 56, 108, 124):
        return None
    bit_count = int.from_bytes(dib[14:16], 'little')
    compression = int.from_bytes(dib[16:20], 'little')
    off = 14 + hdr_size
    if bit_count <= 8:
        clr_used = int.from_bytes(dib[32:36], 'little') or (1 << bit_count)
        off += clr_used * 4
    elif compression == 3 and hdr_size == 40:
        off += 12                       # BI_BITFIELDS：三个颜色掩码紧跟在头之后
    bmp = b'BM' + struct.pack('<IHHI', 14 + len(dib), 0, 0, off) + dib
    img = Image.open(io.BytesIO(bmp))
    img.load()
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def png_to_dib(png):
    """PNG 字节 -> CF_DIB（去掉 PIL 写出的 BMP 14 字节头）。带 alpha 的白底合成成 24bpp。"""
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    img.load()
    if img.mode == 'RGBA':
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'BMP')
    return buf.getvalue()[14:]


def read_clipboard_payload():
    """读剪贴板当前内容 -> {'kind':'files'|'image'|'text', ...}；空/不可识别返回 None。

    优先级 files > image > text（与 Mac 侧约定一致）：带文件的复制通常同时带一份文本表示，
    按文本发就丢掉了文件语义。
    """
    import win32clipboard, win32con
    if not _open_clip():
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            try:
                data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
            except Exception:
                data = None
            paths = [p for p in data if p] if data else []
            if paths:
                return {'kind': 'files', 'paths': paths}

        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
            try:
                dib = win32clipboard.GetClipboardData(win32con.CF_DIB)
            except Exception:
                dib = None
            png = dib_to_png(dib) if dib else None
            if png:
                return {'kind': 'image', 'png': png, 'fp': 'image:' + sha1_bytes(png)}

        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            try:
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            except Exception:
                text = None
            if text:
                return {'kind': 'text', 'text': text,
                        'fp': 'text:' + sha1_bytes(text.encode('utf-8'))}
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def payload_fp(payload):
    """内容指纹，用于「内容没变就不重发」（剪贴板序号会被无关进程扰动）。"""
    if payload is None:
        return ''
    if payload['kind'] == 'files':
        return 'files:' + sha1_bytes(
            '\0'.join(os.path.abspath(p) for p in payload['paths']).encode('utf-8'))
    return payload['fp']


def write_clipboard_text(text):
    import win32clipboard, win32con
    if not _open_clip():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def write_clipboard_image(png):
    """CF_DIB 为主（Word/画图认），再补一个注册格式 'PNG'（浏览器一类偏好它）。"""
    import win32clipboard, win32con
    dib = png_to_dib(png)
    if not dib:
        return False
    cf_png = win32clipboard.RegisterClipboardFormat('PNG')
    if not _open_clip():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
        win32clipboard.SetClipboardData(cf_png, png)
        return True
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def write_clipboard_files_heal(paths, bridge=None):
    """立即写一次 CF_HDROP，然后交给后台线程做「写后自愈」（不阻塞轮询）。

    返回 False 表示首次写入就没成功。
    """
    if not write_clipboard_files(paths):
        return False
    threading.Thread(target=_heal_watch, args=(paths, bridge), daemon=True).start()
    return True


def _heal_watch(paths, bridge):
    """写后持续复查：Deskflow 清掉本批文件就重写，最多 HEAL_MAX_REWRITES 次。

    只在本批文件被清空（剪贴板里再没有任何文件）时才重写；若剪贴板已被
    别的文件占据（用户自己复制了东西），立刻收手，不与用户抢。
    """
    want = set(os.path.abspath(p) for p in paths)
    rewrites = 0
    stable = 0
    deadline = time.time() + HEAL_WATCH_SEC
    while time.time() < deadline:
        time.sleep(HEAL_POLL_SEC)
        cur = read_clipboard_files()
        if cur:
            if set(os.path.abspath(p) for p in cur) == want:
                stable += 1
                if stable >= HEAL_STABLE_HITS:
                    log.info('剪贴板自愈复查通过：本批 %d 个文件稳定 %.0fs 未被清',
                             len(paths), stable * HEAL_POLL_SEC)
                    post_clip_ack(bridge, paths, True, rewrites)
                    return
            else:
                log.info('剪贴板已被其他文件占据（用户自己的复制），停止自愈')
                post_clip_ack(bridge, paths, False, rewrites)
                return
            continue
        if read_clipboard_payload() is not None:
            # 已被文字/图片占住 = 用户自己复制了别的东西，收手；Deskflow 清空时是彻底没内容
            log.info('剪贴板已被其他内容（文字/图片）占据，停止自愈')
            post_clip_ack(bridge, paths, False, rewrites)
            return
        stable = 0
        if rewrites >= HEAL_MAX_REWRITES:
            log.error('剪贴板自愈失败：Deskflow 反复清掉本批文件（已重写 %d 次），请从 inbox 手动复制',
                      rewrites)
            post_clip_ack(bridge, paths, False, rewrites)
            return
        rewrites += 1
        log.warning('剪贴板被清掉（Deskflow 回灌），第 %d 次重写', rewrites)
        write_clipboard_files(paths)
    log.warning('剪贴板自愈复查窗口结束（重写 %d 次），未再观察到清空', rewrites)
    post_clip_ack(bridge, paths, True, rewrites)


# ---------------------------------------------------------------- 通道客户端

class BridgeError(Exception):
    pass


class PushUnavailable(Exception):
    """对端直达接收器不可达/被拒 —— 调用方应退回协议 v1 队列。"""


class Bridge(object):
    """轻量请求走长连接（HTTP/1.1 keep-alive）；大文件传输用独立连接以单独设超时。"""

    def __init__(self):
        self._conn = None
        self._push = None
        self._qconn = None

    def _light_conn(self):
        if self._conn is None:
            self._conn = http.client.HTTPConnection(HOST, PORT, timeout=LIGHT_TIMEOUT)
        return self._conn

    def _q_conn(self):
        """队列列举专用连接：用短超时，避免对端不可达时阻塞本地看门狗。"""
        if self._qconn is None:
            self._qconn = http.client.HTTPConnection(HOST, PORT, timeout=QUEUE_LIST_TIMEOUT)
        return self._qconn

    def _push_conn(self):
        if self._push is None:
            self._push = http.client.HTTPConnection(PEER_HOST, PEER_PUSH_PORT, timeout=PUSH_TIMEOUT)
            self._push.blocksize = STREAM_BLOCKSIZE
        return self._push

    def _drop_push(self):
        try:
            if self._push is not None:
                self._push.close()
        except Exception:
            pass
        self._push = None

    def drop(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        try:
            if self._qconn is not None:
                self._qconn.close()
        except Exception:
            pass
        self._qconn = None
        self._drop_push()

    def push(self, name, local_path=None, blob=None, size=None, orig_name=None, sha256=None):
        """协议 v2：把一条条目直达推送到对端 /api/push。返回服务端 JSON（含 sha256）。

        对端未起 / 被防火墙挡 / 判 4xx-5xx 一律抛 PushUnavailable，由调用方退回协议 v1 队列。
        连接复用（keep-alive）以免每条小载荷都付一次新建连接的固定开销。
        """
        if blob is None:
            size = os.path.getsize(local_path) if size is None else size
            if sha256 is None:
                sha256 = sha256_file(local_path)
        else:
            size = len(blob)
            if sha256 is None:
                sha256 = hashlib.sha256(blob).hexdigest()
        headers = {'X-Bridge-Token': TOKEN, 'X-Sha256': sha256,
                   'Content-Length': str(size), 'Content-Type': 'application/octet-stream'}
        if orig_name:
            headers['X-Orig-Name-B64'] = b64_name(orig_name)
        path = '/api/push/' + quote(name, safe='')
        last = None
        for attempt in (1, 2):
            conn = self._push_conn()
            try:
                if blob is None:
                    with open(local_path, 'rb') as f:
                        conn.request('POST', path, body=f, headers=headers)
                        resp = conn.getresponse()
                        data = resp.read()
                else:
                    conn.request('POST', path, body=blob, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                if resp.version < 11 or resp.will_close:
                    self._drop_push()      # 对端是 HTTP/1.0，或判了拒收要关连接：不能复用
            except Exception as e:
                # 复用连接可能已被对端静默关闭：丢掉重开一次，仍失败才判不可达
                self._drop_push()
                last = repr(e)
                continue
            if resp.status != 200:
                raise PushUnavailable('HTTP %s %s' % (resp.status, data[:180]))
            try:
                return json.loads(data.decode('utf-8'))
            except Exception:
                return {}
        raise PushUnavailable(last)

    @staticmethod
    def _headers(extra=None):
        h = {'X-Bridge-Token': TOKEN}
        if extra:
            h.update(extra)
        return h

    def _light(self, method, path, body=None, extra=None):
        conn = self._light_conn()
        try:
            conn.request(method, path, body=body, headers=self._headers(extra))
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp, data
        except Exception:
            self.drop()
            raise

    def list_queue(self, queue):
        conn = self._q_conn()
        try:
            conn.request('GET', '/api/%s' % queue, headers=self._headers())
            resp = conn.getresponse()
            data = resp.read()
            if resp.version < 11 or resp.will_close:
                try:
                    conn.close()
                except Exception:
                    pass
                self._qconn = None
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            self._qconn = None
            raise
        if resp.status != 200:
            raise BridgeError('list %s -> HTTP %s' % (queue, resp.status))
        return json.loads(data.decode('utf-8')).get('messages', [])

    def health(self):
        st, resp, data = self._light('GET', '/api/health')
        if st != 200:
            raise BridgeError('health -> HTTP %s' % st)
        return json.loads(data.decode('utf-8'))

    def fetch_text(self, queue, name):
        path = '/api/%s/%s' % (queue, quote(name, safe=''))
        st, resp, data = self._light('GET', path)
        if st != 200:
            raise BridgeError('GET %s -> HTTP %s' % (name, st))
        return data

    def ack(self, queue, name):
        path = '/api/%s/%s/ack' % (queue, quote(name, safe=''))
        last = None
        for attempt in range(1 + len(RETRY_SLEEPS)):
            try:
                st, resp, data = self._light('POST', path)
                if st == 200:
                    return json.loads(data.decode('utf-8'))
                last = 'HTTP %s %s' % (st, data[:200])
            except Exception as e:
                last = repr(e)
            if attempt < len(RETRY_SLEEPS):
                time.sleep(RETRY_SLEEPS[attempt])
        raise BridgeError('ack %s failed: %s' % (name, last))

    # -------- 传输

    def put_bytes(self, queue, qname, blob, extra_headers=None):
        """小载荷（manifest / fail 回执）。"""
        headers = self._headers(extra_headers)
        headers['Content-Length'] = str(len(blob))
        headers['Content-Type'] = 'application/octet-stream'
        conn = http.client.HTTPConnection(HOST, PORT, timeout=LIGHT_TIMEOUT)
        conn.blocksize = STREAM_BLOCKSIZE
        try:
            conn.request('PUT', '/api/%s/%s' % (queue, quote(qname, safe='')),
                         body=blob, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise BridgeError('PUT %s -> HTTP %s %s' % (qname, resp.status, data[:200]))
        return json.loads(data.decode('utf-8'))

    def put_file(self, queue, qname, local_path, size, orig_name=None):
        """流式 PUT；返回服务端 sha256。"""
        headers = self._headers()
        headers['Content-Length'] = str(size)
        headers['Content-Type'] = 'application/octet-stream'
        if orig_name:
            headers['X-Orig-Name-B64'] = b64_name(orig_name)
        conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout_for(size))
        conn.blocksize = STREAM_BLOCKSIZE       # 默认 8192 太小：实测 8 KB 分块只跑到 12.3 MB/s
        try:
            with open(local_path, 'rb') as f:
                conn.request('PUT', '/api/%s/%s' % (queue, quote(qname, safe='')),
                             body=f, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise BridgeError('PUT %s -> HTTP %s %s' % (qname, resp.status, data[:200]))
        return json.loads(data.decode('utf-8'))

    def download_raw(self, queue, name, size, dest_part):
        """流式 GET ?raw=1 写到 dest_part，返回 (written, declared_sha, content_length, orig_name_b64)。"""
        path = '/api/%s/%s?raw=1' % (queue, quote(name, safe=''))
        conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout_for(size))
        conn.blocksize = STREAM_BLOCKSIZE
        try:
            conn.request('GET', path, headers=self._headers())
            resp = conn.getresponse()
            if resp.status != 200:
                raise BridgeError('GET %s -> HTTP %s' % (name, resp.status))
            declared = resp.getheader('X-Sha256')
            clen = resp.getheader('Content-Length')
            orig_hdr = resp.getheader('X-Orig-Name-B64')
            written = 0
            with open(dest_part, 'wb') as f:
                while True:
                    chunk = resp.read(STREAM_BLOCKSIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
        finally:
            conn.close()
        return written, declared, clen, orig_hdr


# ---------------------------------------------------------------- 直达推送接收器（协议 v2）

# 接收线程与主循环共享的三份小状态（都在 _PUSH_LOCK 下改）：
#   _PUSH_SEEN   已落地的条目名。做幂等去重：对端重推、或队列里还留着同一份副本时只处理一次。
#   _PUSH_ECHO   刚由快路写进本机剪贴板的内容指纹。主循环读到即吞掉，绝不回发（防两端互推成环）。
#   _PUSH_FILES  已落 inbox、待写剪贴板的路径。剪贴板与 st 记账只在主循环里做，避免跨线程改状态。
_PUSH_SEEN = set()
_PUSH_ECHO = set()
_PUSH_FILES = []
_PUSH_LOCK = threading.Lock()


def remember_pushed(name):
    """登记「这条已落地」。返回 False 表示之前就落地过（幂等跳过）。"""
    with _PUSH_LOCK:
        if name in _PUSH_SEEN:
            return False
        _PUSH_SEEN.add(name)
        return True


def forget_pushed(name):
    """落地失败：撤销登记，让对端可以重推。"""
    with _PUSH_LOCK:
        _PUSH_SEEN.discard(name)


def _entry_name_ok(name):
    """推送路径里的条目名必须是纯 basename，防目录穿越。"""
    if not name or len(name) > 200 or '\0' in name:
        return False
    if '/' in name or '\\' in name or name in ('.', '..'):
        return False
    return True


def apply_clip_blob(name, blob):
    """cb_ 内容 -> 本机剪贴板。返回 (ok, 描述)。"""
    m = RE_CB_NAME.match(name)
    if not m:
        return False, '命名不认识'
    kind = m.group(3)
    limit = CLIP_TEXT_MAX if kind == 'text' else CLIP_IMAGE_MAX
    if not blob or len(blob) > limit:
        return False, '长度越界 %d B（上限 %d B）' % (len(blob), limit)
    if kind == 'text':
        try:
            text = blob.decode('utf-8')
        except Exception:
            return False, '不是合法 UTF-8'
        if not write_clipboard_text(text):
            return False, '写本机剪贴板失败'
        return True, '文本 %d 字' % len(text)
    if not write_clipboard_image(blob):
        return False, '写本机剪贴板失败'
    return True, '图片 %d B' % len(blob)


def land_file(name, part, size, declared, orig_hdr):
    """校验字节数与 sha256 后原子落盘进 inbox。返回 (dest 或 None, 错误描述)。

    只负责落盘，不碰剪贴板（剪贴板统一交给主循环，见 _PUSH_FILES）。
    """
    true_name = unb64_name(orig_hdr) if orig_hdr else None
    if true_name:
        orig = clean_name(true_name)
    else:
        orig = clean_name(orig_name_from_qname(name))
        log.warning('%s 未回传 X-Orig-Name-B64，退化为剥前缀命名', name)
    got = os.path.getsize(part)
    if size and got != size:
        return None, '字节数不符：收到 %d / 声明 %d' % (got, size)
    sha = sha256_file(part)
    if declared:
        if declared.lower() != sha.lower():
            return None, 'sha256 不符：本地 %s / 声明 %s' % (sha, declared)
    else:
        log.warning('%s 未带 X-Sha256，仅按字节数校验', name)
    dest = unique_path(DIR_INBOX, orig)
    os.replace(part, dest)
    return dest, None


def drain_pushed_files(bridge, st):
    """把快路落地的文件写进剪贴板（在主循环的本地节奏里做）。"""
    with _PUSH_LOCK:
        paths = _PUSH_FILES[:]
        del _PUSH_FILES[:]
    if not paths:
        return
    try:
        if write_clipboard_files_heal(paths, bridge):
            st['self_seq'] = clip_seq()
            st['self_fp'] = payload_fp({'kind': 'files', 'paths': paths})
            st['last_seq'] = st['self_seq']
            st['last_fp'] = st['self_fp']
            log.info('直达推送：已把 %d 个文件写入剪贴板（自愈复查在后台）', len(paths))
        else:
            log.error('直达推送：写剪贴板失败（文件已在 inbox）')
            post_clip_ack(bridge, paths, False, 0)
    except Exception as e:
        log.error('直达推送：写剪贴板异常: %r', e)


# ---------------------------------------------------------------- 跨屏拖拽（目标端 = Windows）
#
# 会话语义（与 Mac 锁定，见 _agentbridge/msg-drag-locked.md）：
#   源端(Mac) 拖拽开始即发 drag_<id>_begin.json（带 items[].name = 随后直推的载荷条目名），
#   并后台把 file_*/xfer_* 预推到本机 8900（先落暂存区，不直接进 inbox）。
#   本机在 armed 窗口内观测到【注入的 LEFT-UP 且落点在 Windows 屏内】→ 暂存搬到「下载」，
#   回 drag_<id>_drop.json 给对端对账。cancel / armed TTL 清暂存。
# 载荷归属：优先按请求头 X-Drag-Id，其次按 begin.items 声明的条目名匹配（两端同源）。

DRAG_ARM_TTL = 30.0          # armed 会话存活上限（与 Mac 约定 30 s）
DRAG_STAGE_TTL = 24 * 3600   # 暂存目录最长保留，与 .part 的 24 h 一致

RE_DRAG_SIGNAL = re.compile(r'^drag_(?P<id>.+)_(?P<verb>begin|sync|drop|cancel)\.json$')

_DRAG_LOCK = threading.Lock()
_DRAG_SESSIONS = {}          # drag_id -> {'id','ts','dir','ptr','screen','by_qname':{qname:orig}}
_DRAG_QNAME = {}             # 推送条目名 -> drag_id（begin.items 声明，用于把 file_/xfer_ 归到暂存）
_DRAG_DROP_Q = queue.Queue()
_DRAG_TEST_TARGET = None     # 自检用：覆盖落点目录，避免污染「下载」
# 诊断：armed 期间每秒采样 (时间, 本机光标, 活动屏)，松手时一次性打印，用于判定跨屏时本机指针是否跟手。
_DRAG_TRACE = []
_DRAG_TRACE_LAST = [0.0]

# 目标端（Mac→Win）落点还原：armed 期间由鼠标钩子记录按住位移轨迹。
# 跨屏拖拽时本机指针被 Deskflow 停放（恒为屏心），松手坐标不可用 —— 只能取轨迹里
# 最后一次「非停放」位置作为真实落点。钩子线程读写（list.append 原子），不加锁。
_TGT_ARMED = [False]
_CUR_TRACE = []              # (时间, x, y, injected) 环形，最近 4000 条
_CUR_TRACE_CAP = 4000

# 指针屏蔽：指针在对端 Mac 屏时隐藏本机光标（Deskflow 会把它停放在屏心，很难看），
# 回到本机屏再恢复。用 ShowCursor 计数实现，进程退出自动恢复；另设超时兜底防卡死。
_CURSOR_MASKED = [False]
_CURSOR_MASK_SINCE = [0.0]
_CURSOR_MASK_BLOCK = [False]   # 超时强制恢复后置位：须再见到 'win' 才允许重新屏蔽
CURSOR_MASK_MAX_SEC = 120.0   # 连续屏蔽上限，超过强制恢复（防日志陈旧导致永久无光标）

# 源端（Win→Mac）：本机做源，Mac 做目标端。源判定见 draghook（GetGUIThreadInfo capture）。
_DRAG_SRC_Q = queue.Queue()
_DRAG_SRC = {'id': None, 'ts': 0.0, 'qnames': []}    # 当前源端会话（单会话）
_DRAG_SRC_ON = False                                 # 钩子线程只读该原子布尔，避免加锁
_DRAG_CROSSED = False                                # 本次拖拽是否触及过跨屏边界（钩子线程写、worker 读）
DRAG_SRC_COOLDOWN = 5.0                              # 会话结束后冷却，避免连点重复触发
DRAG_SRC_TTL = 30.0                                  # 源端会话最长存活

# 松手时"活动屏在哪"由 Deskflow 服务端日志判定：Deskflow 不把 Win 起手的拖拽按键状态转发给
# Mac（Mac 实测 btn 恒为 0，本地测不到 LEFT-UP），故落点触发必须由 Windows 侧发。
DESKFLOW_LOG = r'D:\KEEPPER\_deskflow\core-server.log'
MAC_SCREEN_NAME = 'MAC'                              # 与 deskflow-server.conf 的 screens\6\name 一致
RE_DF_SWITCH = re.compile(r'switch from "(?P<src>[^"]+)" to "(?P<dst>[^"]+)"')


def active_screen_from_log():
    """读 Deskflow 日志尾部推断当前活动屏：'mac'=客户端，'win'=本机服务端。

    依据最近一条 `switch from "A" to "B" at x,y` 的目标名。取不到返回 None（宁缺毋滥）。
    """
    try:
        size = os.path.getsize(DESKFLOW_LOG)
        with open(DESKFLOW_LOG, 'rb') as f:
            f.seek(max(0, size - 16384))
            tail = f.read().decode('utf-8', 'replace')
    except OSError:
        return None
    last = None
    for line in tail.splitlines():
        m = RE_DF_SWITCH.search(line)
        if m:
            last = 'mac' if m.group('dst') == MAC_SCREEN_NAME else 'win'
    return last


def cursor_parked(pt):
    """本机光标是否被 Deskflow 停放在虚拟屏正中（指针位于对端屏时的停放位，非真实落点）。

    实证：Mac→Win 拖拽松手瞬间 GetCursorPos 恒为 (1376,576) = 2752×1152 的正中心。
    """
    if not pt:
        return True
    x, y, w, h = draghook.virtual_screen_rect()
    return abs(pt[0] - (x + w // 2)) <= 2 and abs(pt[1] - (y + h // 2)) <= 2


def _resolve_drop_point(x, y):
    """把松手坐标还原为真实落点：被停放（屏心）时取按住轨迹里最后一次非停放位置。"""
    if not cursor_parked((x, y)):
        return x, y
    real = [t for t in _CUR_TRACE if not cursor_parked((t[1], t[2]))]
    if not real:
        return x, y
    _, rx, ry, _ = real[-1]
    log.info('拖拽：松手光标被停放 (%d,%d)，改用轨迹末端 (%d,%d)', x, y, rx, ry)
    return rx, ry


def mask_cursor_if_on_peer():
    """指针落在对端 Mac 屏时隐藏本机光标，回到本机屏再恢复。

    Deskflow 把「不在本机屏」的光标停放在虚拟屏正中（屏心很难看、也干扰落点观察），
    故按 Deskflow 日志推断的活动屏来屏蔽/恢复。另设 CURSOR_MASK_MAX_SEC 兜底：
    日志陈旧或读不到时，连续屏蔽超时即强制恢复，避免留下永久无光标。
    """
    active = active_screen_from_log()
    if active == 'win':
        _CURSOR_MASK_BLOCK[0] = False        # 见到本机屏，解除屏蔽禁令
    if active == 'mac' and not _CURSOR_MASK_BLOCK[0]:
        if not _CURSOR_MASKED[0]:
            if draghook.set_cursor_visible(False):
                _CURSOR_MASKED[0] = True
                _CURSOR_MASK_SINCE[0] = time.time()
                log.info('指针屏蔽：指针在对端 Mac 屏，已隐藏本机光标')
        elif time.time() - _CURSOR_MASK_SINCE[0] > CURSOR_MASK_MAX_SEC:
            draghook.set_cursor_visible(True)
            _CURSOR_MASKED[0] = False
            _CURSOR_MASK_BLOCK[0] = True     # 直到再见到 'win' 都不再屏蔽，防日志陈旧永久无光标
            log.warning('指针屏蔽：连续屏蔽超时 %.0fs，已强制恢复并暂停屏蔽', CURSOR_MASK_MAX_SEC)
    elif _CURSOR_MASKED[0]:
        draghook.set_cursor_visible(True)
        _CURSOR_MASKED[0] = False
        log.info('指针屏蔽：指针回到本机屏（active=%s），已恢复本机光标', active)


def _drag_stage_dir(drag_id):
    d = os.path.join(DIR_DRAGSTAGE, clean_name(drag_id))
    os.makedirs(d, exist_ok=True)
    return d


def drag_arm(doc):
    """收到 begin.json：登记 armed 会话、就绪暂存目录、记录 begin.items 声明的推送名。"""
    did = clean_name(str(doc.get('id') or ''))
    if not did:
        return None
    items = doc.get('items') or []
    by_qname = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        qn = it.get('name') or ''
        if not qn:
            continue
        orig = it.get('orig') or qn
        by_qname[qn] = clean_name(orig)
    sess = {'id': did, 'ts': time.time(), 'dir': _drag_stage_dir(did),
            'ptr': doc.get('ptr'), 'screen': doc.get('screen'),
            'by_qname': by_qname}
    with _DRAG_LOCK:
        _DRAG_SESSIONS[did] = sess
        for qn in by_qname:
            _DRAG_QNAME[qn] = did
    _TGT_ARMED[0] = True          # 让钩子线程开始记录落点轨迹
    del _CUR_TRACE[:]
    log.info('拖拽：armed 会话 %s，声明 %d 个载荷', did, len(by_qname))
    return sess


def drag_cancel(drag_id):
    with _DRAG_LOCK:
        sess = _DRAG_SESSIONS.pop(drag_id, None)
        for qn, d in list(_DRAG_QNAME.items()):
            if d == drag_id:
                _DRAG_QNAME.pop(qn, None)
    if sess:
        shutil.rmtree(sess['dir'], ignore_errors=True)
        log.info('拖拽：会话 %s 解除，清暂存', drag_id)
    if not _DRAG_SESSIONS:
        _TGT_ARMED[0] = False
        del _CUR_TRACE[:]


def _drag_sweep():
    """清超时 armed 会话；清超期暂存目录。"""
    now = time.time()
    with _DRAG_LOCK:
        dead = [did for did, s in _DRAG_SESSIONS.items() if now - s['ts'] > DRAG_ARM_TTL]
    for did in dead:
        drag_cancel(did)
        log.info('拖拽：会话 %s 超过 armed TTL(%.0fs)，自动解除', did, DRAG_ARM_TTL)
    try:
        for n in os.listdir(DIR_DRAGSTAGE):
            p = os.path.join(DIR_DRAGSTAGE, n)
            if os.path.isdir(p) and now - os.path.getmtime(p) > DRAG_STAGE_TTL:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def inbox_sweep():
    """清 inbox 里超过 INBOX_TTL 的落盘载荷（对齐 Mac 5 min TTL）。

    只删普通文件且 mtime 超时；目录、隐藏文件与 agent 对话相关文件（见 RE_INBOX_SKIP）
    一律保留，避免影响 agent 对话机制。每轮 full_beat 调一次，I/O 极轻。
    """
    now = time.time()
    try:
        names = os.listdir(DIR_INBOX)
    except OSError:
        return
    for n in names:
        if n.startswith('.') or RE_INBOX_SKIP.search(n):
            continue
        p = os.path.join(DIR_INBOX, n)
        try:
            if not os.path.isfile(p) or now - os.path.getmtime(p) <= INBOX_TTL:
                continue
            os.remove(p)
            log.info('inbox 超时清理: %s（>%.0fs）', n, INBOX_TTL)
        except OSError as e:
            log.warning('inbox 清理失败 %s: %r', n, e)


def drag_session_for_qname(qname, header_drag_id=None):
    """这条推送该归到哪个拖拽会话？优先 X-Drag-Id 头，其次 begin.items 声明的条目名。"""
    with _DRAG_LOCK:
        if header_drag_id and header_drag_id in _DRAG_SESSIONS:
            return _DRAG_SESSIONS[header_drag_id]
        did = _DRAG_QNAME.get(qname)
        return _DRAG_SESSIONS.get(did) if did else None


def land_drag_file(name, part, size, declared, orig_hdr, sess):
    """校验后原子落到该会话的暂存目录。返回 (dest 或 None, 错误描述)。"""
    true_name = unb64_name(orig_hdr) if orig_hdr else None
    if true_name:
        orig = clean_name(true_name)
    else:
        orig = clean_name(sess['by_qname'].get(name) or orig_name_from_qname(name))
    got = os.path.getsize(part)
    if size and got != size:
        return None, '字节数不符：收到 %d / 声明 %d' % (got, size)
    sha = sha256_file(part)
    if declared and declared.lower() != sha.lower():
        return None, 'sha256 不符：本地 %s / 声明 %s' % (sha, declared)
    dest = _move_into(part, sess['dir'], orig)   # 暂存可能在别的盘，必须跨卷安全搬
    return dest, None


def _move_into(src, target_dir, name, overwrite=False):
    """把暂存文件搬到目标目录并保证最终名下不出现半包。返回落地点。

    同卷：os.replace 原子改名。跨卷（暂存在 %LOCALAPPDATA%，下载可能在别的盘）：
    先拷到目标卷的 .part 临时名，再卷内原子改名，最后删源。
    overwrite=True 时同名直接覆盖（inbox 落地语义，与 Mac 一致），否则加序号。
    """
    dest = os.path.join(target_dir, name) if overwrite else unique_path(target_dir, name)
    try:
        os.replace(src, dest)
        return dest
    except OSError:
        pass
    tmp = dest + '.part'
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    os.remove(src)
    return dest


def handle_drag_signal(name, blob):
    """处理 drag_<id>_<verb>.json 信令，返回 (ok, detail, action)。"""
    m = RE_DRAG_SIGNAL.match(name)
    if not m:
        return False, '命名不认识', None
    did = clean_name(m.group('id'))
    verb = m.group('verb')
    try:
        doc = json.loads(blob.decode('utf-8')) if blob else {}
    except Exception as e:
        return False, 'JSON 解析失败: %r' % (e,), None
    if not isinstance(doc, dict):
        return False, 'JSON 非对象', None
    doc.setdefault('id', did)
    if verb == 'begin':
        drag_arm(doc)
        return True, 'armed', 'dragstage'
    if verb == 'cancel':
        drag_cancel(did)
        log.info('拖拽：对端 cancel %s（reason=%s）', did, doc.get('reason'))
        return True, 'cancelled', 'dragstage'
    if verb == 'sync':
        # 目标端回给源端的回执（Mac 落成后回我们）：结束本机源端会话并记日志对账
        return True, _drag_src_done(did), 'dragstage'
    if verb == 'drop':
        # 对端（源端）松手触发：本机作目标端。立即读本机光标定落点（Deskflow 跨屏后本机指针即落点），
        # 坐标完全取自本地，不用对端传来的 x/y（那是 Mac 屏坐标）；命中文件夹则直落，否则回退 inbox。
        pt = draghook.cursor_pos()
        log.info('拖拽：收到对端 drop %s，本机光标=%s，对端坐标=(%s,%s)，交投放线程',
                 did, pt, doc.get('x'), doc.get('y'))
        if pt:
            _DRAG_DROP_Q.put_nowait((int(pt[0]), int(pt[1]), time.time()))
        return True, 'drop-queued', 'dragstage'
    return True, 'ignored', 'dragstage'


def _drag_src_done(did):
    """源端收到目标端 sync 回执：清本机源端会话（若 id 匹配）。"""
    global _DRAG_SRC_ON
    with _DRAG_LOCK:
        cur = _DRAG_SRC['id']
        if cur and cur != did:
            log.info('拖拽源端：收到 drop 回执 %s，与本机会话 %s 不一致，忽略', did, cur)
            return 'drop-mismatch'
        _DRAG_SRC.update({'id': None, 'ts': time.time()})
    _DRAG_SRC_ON = False
    log.info('拖拽源端：会话 %s 收到目标端 drop 回执，已结束', did)
    return 'drop-done'


def explorer_selected_paths(hwnd):
    """取资源管理器窗口当前选中项路径（Win→Mac 拖拽载荷）。

    视图是 DirectUIHWND 子窗口，需上溯顶层再与 Shell.Application.Windows() 的 HWND 匹配；
    匹配不到用前台/唯一 Explorer 窗口兜底。取不到一律返回 []（宁缺毋滥，不发 begin）。
    """
    try:
        import win32com.client
    except Exception:
        log.error('拖拽源端：win32com 不可用，无法取选中项')
        return []
    root = draghook.root_hwnd(hwnd) or draghook.foreground_hwnd()
    try:
        shell = win32com.client.Dispatch('Shell.Application')
        windows = list(shell.Windows())
    except Exception as e:
        log.error('拖拽源端：Shell.Application 枚举失败: %r', e)
        return []
    fallback = None
    for win in windows:
        try:
            if 'explorer.exe' not in str(win.FullName).lower():
                continue
        except Exception:
            continue
        try:
            if int(win.HWND) == int(root):
                return [it.Path for it in win.Document.SelectedItems()]
        except Exception:
            continue
        if fallback is None:
            fallback = win
    if fallback is not None:
        try:
            return [it.Path for it in fallback.Document.SelectedItems()]
        except Exception:
            return []
    return []


def folder_path_at_point(x, y):
    """落点 (x,y) 下「可投入的文件夹」路径 —— Mac 侧 Finder 读 AXURL 直落盘的对应物。

    命中资源管理器文件夹窗口 → 其当前打开的文件夹；命中桌面 → 桌面目录；
    其它（普通应用窗口 / 无窗口）→ None，调用方退回「下载」。
    """
    root = draghook.root_hwnd(draghook.window_at_point((x, y)))
    if not root:
        return None
    cls = draghook.window_class(root)
    if cls in ('Progman', 'WorkerW'):
        desk = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
        return desk if os.path.isdir(desk) else None
    if cls not in ('CabinetWClass', 'ExploreWClass'):
        return None
    try:
        import win32com.client
        shell = win32com.client.Dispatch('Shell.Application')
        for win in shell.Windows():
            try:
                if int(win.HWND) != int(root):
                    continue
                p = str(win.Document.Folder.Self.Path)
                return p if os.path.isdir(p) else None
            except Exception:
                continue
    except Exception as e:
        log.error('拖拽投放：解析落点文件夹失败: %r', e)
    return None


def _drag_src_pretransfer(did, items):
    """源端预传：拖拽开始即后台把载荷直推到 Mac（落其暂存区），不等松手。"""
    b = Bridge()
    for it in items:
        try:
            b.push(it['name'], local_path=it['path'], size=it['size'],
                   orig_name=it['orig'], sha256=it['sha256'])
        except PushUnavailable:
            try:
                b.put_file(QUEUE_OUT, it['name'], it['path'], it['size'], orig_name=it['orig'])
            except Exception as e:
                log.error('拖拽源端：预传失败 %s: %r', it['name'], e)
        finally:
            if it.get('temp'):
                try:
                    os.remove(it['path'])
                except OSError:
                    pass
    log.info('拖拽源端：会话 %s 预传结束', did)


def _drag_src_start(bridge, cap):
    """源端拖拽开始：解析 Explorer 选中项 → 生成 drag_id/清单 → 发 drag_begin → 后台预传。"""
    global _DRAG_SRC_ON
    now = time.time()
    with _DRAG_LOCK:
        if _DRAG_SRC['id'] and now - _DRAG_SRC['ts'] <= DRAG_SRC_TTL:
            return                                   # 已有会话在跑
        if _DRAG_SRC['ts'] and now - _DRAG_SRC['ts'] < DRAG_SRC_COOLDOWN:
            return                                   # 冷却中
    staged = []
    for p in explorer_selected_paths(cap):
        if not is_win_path(p) or is_junk(p):
            continue
        try:
            if os.path.isdir(p):
                z = ensure_zip(p)                    # 目录打包，沿用既有约定（两端不自动解包）
                staged.append((z, os.path.basename(z), True))
            elif os.path.isfile(p):
                staged.append((p, os.path.basename(p), False))
        except Exception as e:
            log.error('拖拽源端：打包失败 %s: %r', p, e)
    staged = [(lp, on, t) for (lp, on, t) in staged if os.path.getsize(lp) <= MAX_BYTES]
    if not staged:
        log.info('拖拽源端：未取到可发送的选中项，放弃本次会话')
        return
    did = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    items = []
    for lp, on, t in staged:
        qn = 'file_%s_%s' % (time.strftime('%Y%m%d-%H%M%S'), clean_name(on))
        items.append({'name': qn, 'orig': on, 'kind': 'file',
                      'size': os.path.getsize(lp), 'sha256': sha256_file(lp),
                      'path': lp, 'temp': t})
    vx, vy, vw, vh = draghook.virtual_screen_rect()
    doc = {'id': did, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': 'win',
           'count': len(items),
           'ptr': {'x': 0, 'y': 0},
           'screen': {'x': vx, 'y': vy, 'w': vw, 'h': vh},
           'items': [{k: it[k] for k in ('name', 'orig', 'kind', 'size', 'sha256')}
                     for it in items]}
    with _DRAG_LOCK:
        _DRAG_SRC.update({'id': did, 'ts': time.time(), 'qnames': [it['name'] for it in items]})
    _DRAG_SRC_ON = True
    try:
        send_bytes(bridge, 'drag_%s_begin.json' % did,
                   json.dumps(doc, ensure_ascii=False).encode('utf-8'))
        log.info('拖拽源端：已发 drag_begin %s（%d 个载荷）', did, len(items))
    except Exception as e:
        log.error('拖拽源端：drag_begin 发送失败: %r', e)
        with _DRAG_LOCK:
            _DRAG_SRC.update({'id': None, 'ts': time.time()})
        _DRAG_SRC_ON = False
        return
    threading.Thread(target=_drag_src_pretransfer, args=(did, items), daemon=True).start()


def _drag_src_cancel(bridge, reason='back-to-source'):
    """源端取消（拖拽中断 / 松手回源屏）：发 drag_cancel，清本机会话。"""
    global _DRAG_SRC_ON
    with _DRAG_LOCK:
        did = _DRAG_SRC['id']
        _DRAG_SRC.update({'id': None, 'ts': time.time()})
    _DRAG_SRC_ON = False
    if not did:
        return
    vx, vy, vw, vh = draghook.virtual_screen_rect()
    doc = {'id': did, 'reason': reason,
           'screen': {'x': vx, 'y': vy, 'w': vw, 'h': vh},
           'ts': time.strftime('%Y-%m-%d %H:%M:%S')}
    try:
        send_bytes(bridge, 'drag_%s_cancel.json' % did,
                   json.dumps(doc, ensure_ascii=False).encode('utf-8'))
        log.info('拖拽源端：已发 drag_cancel %s（reason=%s）', did, reason)
    except Exception as e:
        log.error('拖拽源端：drag_cancel 发送失败: %r', e)


def _drag_src_release(bridge, x, y):
    """源端物理松手：按 Deskflow 当前活动屏决定去向。

    活动屏 == Mac → 发 drag_drop（落点交给 Mac：它收到后立刻读本地光标坐标做 AX 命中），
    并保留会话直到收到 sync 回执；否则说明拖回/未跨到 Mac → 发 cancel(dropped-on-source)。
    """
    with _DRAG_LOCK:
        did = _DRAG_SRC['id']
    if not did:
        return
    active = active_screen_from_log()
    if active == 'mac':
        doc = {'id': did, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': 'win'}
        try:
            send_bytes(bridge, 'drag_%s_drop.json' % did,
                       json.dumps(doc, ensure_ascii=False).encode('utf-8'))
            log.info('拖拽源端：松手于 Mac 屏，已发 drag_drop %s（落点由 Mac 本地决定）', did)
        except Exception as e:
            log.error('拖拽源端：drag_drop 发送失败: %r', e)
        return
    log.info('拖拽源端：松手时活动屏=%s，按回源处理', active or '未知')
    _drag_src_cancel(bridge, reason='dropped-on-source')


def _drag_src_worker():
    b = Bridge()
    import pythoncom
    pythoncom.CoInitialize()                 # 本线程要经 Shell.Application 取选中项，COM 必须按线程初始化
    try:
        while True:
            ev = _DRAG_SRC_Q.get()
            if ev is None:
                break
            try:
                if ev[0] == 'start':
                    _drag_src_start(b, ev[3])
                elif ev[0] == 'release':
                    _drag_src_release(b, ev[1], ev[2])
                elif ev[0] == 'cancel':
                    _drag_src_cancel(b)
            except Exception as e:
                log.error('源端拖拽处理异常: %r', e)
    finally:
        pythoncom.CoUninitialize()


def _drag_on_event(ev, x, y, injected, cap):
    """钩子回调（跑在钩子线程）：只判来源/落点，然后入队；绝不在此做 I/O 或加锁。"""
    global _DRAG_CROSSED
    try:
        if ev == 'down' and not injected:
            _DRAG_CROSSED = False                         # 每次按住重置
        elif ev == 'move':
            if _TGT_ARMED[0]:                            # armed 期间记录按住轨迹，供停放时还原落点
                _CUR_TRACE.append((time.time(), int(x), int(y), injected))
                if len(_CUR_TRACE) > _CUR_TRACE_CAP:
                    del _CUR_TRACE[:len(_CUR_TRACE) - _CUR_TRACE_CAP]
        elif ev == 'edge' and not injected:
            _DRAG_CROSSED = True                          # 触到跨屏边界，可能已到 Mac
        elif ev == 'up':
            if injected:
                if draghook.point_in_windows((x, y)):     # 目标端：注入 LEFT-UP 落本屏 → 投放
                    _DRAG_DROP_Q.put_nowait((int(x), int(y), time.time()))
            elif _DRAG_SRC_ON:
                if _DRAG_CROSSED:
                    # 按住期间触过跨屏边界：活动屏可能已是 Mac，但真值由 worker 读 Deskflow
                    # 日志判定（钩子线程不做 I/O）→ 是 Mac 则发 drop，否则发 cancel。
                    _DRAG_SRC_Q.put_nowait(('release', int(x), int(y), 0, time.time()))
                else:                                     # 松手在本屏且从未触边 → 确实未跨屏
                    _DRAG_SRC_Q.put_nowait(('cancel', int(x), int(y), 0, time.time()))
            elif _TGT_ARMED[0] and _DRAG_CROSSED:
                # 目标端（Mac→Win）：本次按住跨过屏且存在 armed 会话 → 物理松手处即落点。
                # 这条不依赖 Mac 的 drop 信令，且松手时本机指针就是真实位置（未被停放）。
                _DRAG_DROP_Q.put_nowait((int(x), int(y), time.time()))
        elif ev == 'dragstart' and not injected:          # 源端：物理拖拽且 OLE 源窗口 SetCapture
            _DRAG_SRC_Q.put_nowait(('start', int(x), int(y), cap, time.time()))
    except Exception:
        pass


def _drag_drop_worker():
    b = Bridge()
    import pythoncom
    pythoncom.CoInitialize()                 # 本线程要经 Shell.Application 解析落点文件夹，COM 必须按线程初始化
    try:
        while True:
            item = _DRAG_DROP_Q.get()
            if item is None:
                break
            try:
                _do_drag_drop(b, item[0], item[1], item[2])
            except Exception as e:
                log.error('拖拽投放异常: %r', e)
    finally:
        pythoncom.CoUninitialize()


def _do_drag_drop(bridge, x, y, ts, target_dir=None, send=True):
    """松手投放（本机作 Mac→Win 的目标端）：暂存搬到目标目录，回 sync 对账。"""
    _drag_sweep()
    with _DRAG_LOCK:
        live = [s for s in _DRAG_SESSIONS.values() if time.time() - s['ts'] <= DRAG_ARM_TTL]
    if not live:
        return
    sess = max(live, key=lambda s: s['ts'])
    did = sess['id']
    if len(live) > 1:
        log.warning('拖拽：%d 个会话同时 armed，取最新 %s', len(live), did)
    overwrite = False
    if target_dir:
        target, kind = target_dir, 'folder'
    elif _DRAG_TEST_TARGET:
        target, kind = _DRAG_TEST_TARGET, 'fallback'
    else:
        # mac2win：松手时本机指针若被 Deskflow 停放在屏心（指针此刻在对端 Mac 屏），该坐标
        # 不代表落点 —— 改用拖拽期间记录的按住轨迹里最后一次「非停放」位置还原真实落点。
        # 命中资源管理器/桌面就直落该目录，命不中才回退 inbox（同名覆盖）。
        x, y = _resolve_drop_point(x, y)
        folder = folder_path_at_point(x, y)
        if folder:
            target, kind = folder, 'folder'
        else:
            target, kind = DIR_INBOX, 'inbox'
            overwrite = True
        log.info('拖拽：mac2win 落点 (%d,%d) 命中 %s -> %s', x, y, kind, target)
    if _CUR_TRACE:
        log.info('拖拽诊断轨迹尾段: %s', ' | '.join(
            '%s(%d,%d)%s' % (time.strftime('%H:%M:%S', time.localtime(t)), cx, cy, 'inj' if i else '')
            for t, cx, cy, i in _CUR_TRACE[-8:]))
    moved = []
    try:
        names = sorted(os.listdir(sess['dir']))
    except OSError:
        names = []
    for n in names:
        src = os.path.join(sess['dir'], n)
        if os.path.isfile(src):
            moved.append(_move_into(src, target, n, overwrite=overwrite))
    if _DRAG_TRACE:
        log.info('拖拽诊断轨迹: %s', ' | '.join(
            '%s cur=%s act=%s' % (time.strftime('%H:%M:%S', time.localtime(t)), c, a)
            for t, c, a in _DRAG_TRACE))
        del _DRAG_TRACE[:]
    if send:
        vx, vy, vw, vh = draghook.virtual_screen_rect()
        doc = {'id': did, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
               'phase': 'done', 'landed': len(moved),
               'target': {'kind': kind, 'path': target},
               'x': x, 'y': y,
               'screen': {'x': vx, 'y': vy, 'w': vw, 'h': vh}}
        try:
            send_bytes(bridge, 'drag_%s_sync.json' % did,
                       json.dumps(doc, ensure_ascii=False).encode('utf-8'))
            log.info('拖拽：松手投放 %d 个文件 -> %s（端到端 %.3fs）',
                     len(moved), target, time.time() - ts)
        except Exception as e:
            log.error('拖拽：sync 回执发送失败: %r', e)
    drag_cancel(did)


class PushHandler(BaseHTTPRequestHandler):
    """POST /api/push/<条目名>：对端有货时直达本机，真正落地了才回 200。

    接收端语义 = 「这条条目刚刚出现在我的队列里」：file_ 落 inbox、cb_ 写剪贴板、
    xfer_ 记账；其余命名一律 404，让对端退回协议 v1 队列 —— Windows 侧没有本地队列，
    普通消息（agent 消息等）必须留在 Mac 的队列里，不能在这里吞掉。
    """

    server_version = 'ClipwatchPush/1.0'
    protocol_version = 'HTTP/1.1'
    timeout = 300                          # 只防「发一半就断」的挂死，复用连接不会被误杀

    def log_message(self, fmt, *args):
        pass                                   # 走自己的滚动日志，不打 stderr

    def _reply(self, code, doc):
        body = json.dumps(doc, ensure_ascii=False).encode('utf-8')
        try:
            if code != 200:
                # 拒收时 body 还没读完，必须关连接：否则残留字节会被当成下一个请求解析
                self.close_connection = True
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            if self.close_connection:
                self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        """连通性探针：GET /api/push → 200，不产生任何条目（给对端做握手自检用）。"""
        if urlsplit(self.path).path.rstrip('/') != '/api/push':
            return self._reply(404, {'ok': False, 'error': 'unknown route'})
        if self.headers.get('X-Bridge-Token') != TOKEN:
            return self._reply(401, {'ok': False, 'error': 'bad token'})
        return self._reply(200, {'ok': True, 'service': 'clipwatch-push', 'port': PUSH_PORT})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith('/api/push/'):
            return self._reply(404, {'ok': False, 'error': 'unknown route'})
        if self.headers.get('X-Bridge-Token') != TOKEN:
            return self._reply(401, {'ok': False, 'error': 'bad token'})
        name = unquote(path[len('/api/push/'):])
        if not _entry_name_ok(name):
            return self._reply(400, {'ok': False, 'error': 'bad name'})
        is_cb = bool(RE_CB_NAME.match(name))
        is_drag_sig = bool(RE_DRAG_SIGNAL.match(name))
        if is_cb:
            limit = CLIP_TEXT_MAX if name.endswith('.txt') else CLIP_IMAGE_MAX
        elif is_drag_sig or name.startswith('drag_'):
            limit = 1 << 20                        # drag_* 信令是 JSON，1 MB 富余
        elif name.startswith('file_') or name.startswith('xfer_'):
            limit = MAX_BYTES
        else:
            return self._reply(404, {'ok': False, 'error': 'unsupported name'})
        try:
            size = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self._reply(411, {'ok': False, 'error': 'bad content-length'})
        if size <= 0 or size > limit:
            return self._reply(413, {'ok': False,
                                     'error': 'size %d over limit %d' % (size, limit)})

        part = os.path.join(DIR_TMP, 'push_%s.part' % uuid.uuid4().hex[:8])
        t0 = time.time()
        try:
            written = 0
            with open(part, 'wb') as f:
                remain = size
                while remain > 0:
                    chunk = self.rfile.read(min(STREAM_BLOCKSIZE, remain))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    remain -= len(chunk)
            if written != size:
                return self._reply(400, {'ok': False,
                                         'error': 'short body %d/%d' % (written, size)})
            declared = self.headers.get('X-Sha256') or ''
            sha = sha256_file(part)
            if declared and declared.lower() != sha.lower():
                return self._reply(400, {'ok': False, 'error': 'sha256 mismatch'})

            if not remember_pushed(name):
                log.info('直达推送：%s 已落地过，幂等跳过', name)
                return self._reply(200, {'ok': True, 'action': 'duplicate', 'sha256': sha})

            if is_cb:
                with open(part, 'rb') as f:
                    blob = f.read()
                ok, detail = apply_clip_blob(name, blob)
                if not ok:
                    forget_pushed(name)                # 没落地就让对端重推
                    return self._reply(422, {'ok': False, 'error': detail})
                if name.endswith('.txt'):
                    fp = 'text:' + sha1_bytes(blob)
                else:
                    # 图片经 PNG<->DIB 往返字节会变，按真正落到剪贴板的内容取指纹
                    fp = payload_fp(read_clipboard_payload())
                if fp:
                    with _PUSH_LOCK:
                        _PUSH_ECHO.add(fp)
                action = 'clipboard'
                log.info('直达推送：已把对端剪贴板%s写入本机剪贴板（%s）', detail, name)
            elif is_drag_sig:
                with open(part, 'rb') as f:
                    blob = f.read()
                ok, detail, action = handle_drag_signal(name, blob)
                if not ok:
                    forget_pushed(name)
                    return self._reply(422, {'ok': False, 'error': detail})
                log.info('直达推送：拖拽信令 %s -> %s', name, detail)
            elif name.startswith('drag_'):
                # 未识别的 drag_* 变体：按兼容性承诺忽略并记日志，不报错
                action = 'dragnoop'
                log.warning('直达推送：未识别的 drag_ 条目 %s，已忽略', name)
            elif name.startswith('file_') or name.startswith('xfer_'):
                sess = drag_session_for_qname(name, self.headers.get('X-Drag-Id'))
                if sess:
                    dest, err = land_drag_file(name, part, size, declared,
                                               self.headers.get('X-Orig-Name-B64') or '', sess)
                    if not dest:
                        forget_pushed(name)
                        return self._reply(422, {'ok': False, 'error': err})
                    action = 'dragstage'          # 落暂存区，松手才搬到目标目录
                    log.info('直达推送：拖拽暂存 %s -> %s (%d B, %.3fs)',
                             name, dest, written, time.time() - t0)
                else:
                    dest, err = land_file(name, part, size, declared,
                                          self.headers.get('X-Orig-Name-B64') or '')
                    if not dest:
                        forget_pushed(name)
                        return self._reply(422, {'ok': False, 'error': err})
                    with _PUSH_LOCK:
                        _PUSH_FILES.append(dest)
                    action = 'inbox'
                    log.info('直达推送：已接收 %s -> %s (%d B, %.3fs)',
                             name, dest, written, time.time() - t0)
            else:
                action = 'manifest'
                log.info('直达推送：收到清单 %s (%d B)', name, written)
            return self._reply(200, {'ok': True, 'action': action, 'sha256': sha})
        except Exception as e:
            log.error('直达推送处理异常 %s: %r', name, e)
            return self._reply(500, {'ok': False, 'error': repr(e)})
        finally:
            try:
                os.remove(part)
            except OSError:
                pass


def start_push_server():
    """起反向推送接收器。起不来不影响主功能（自动退回纯队列模式）。"""
    try:
        srv = ThreadingHTTPServer(('0.0.0.0', PUSH_PORT), PushHandler)
    except Exception as e:
        log.error('直达推送接收器起不来（端口 %d 不可用），本次退回纯队列模式: %r', PUSH_PORT, e)
        return None
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.2},
                     daemon=True).start()
    log.info('直达推送接收器已监听 0.0.0.0:%d（POST /api/push/<条目名>）', PUSH_PORT)
    return srv


# ---------------------------------------------------------------- 发送（方向 1）

def ensure_zip(path):
    """目录 -> tmp 下的 <目录名>.zip，返回 zip 路径及是否临时生成。"""
    name = clean_name(os.path.basename(path.rstrip('\\/')))
    zip_path = os.path.join(DIR_TMP, name + '.zip')
    if os.path.exists(zip_path):
        os.remove(zip_path)
    shutil.make_archive(os.path.join(DIR_TMP, name), 'zip',
                        root_dir=os.path.dirname(os.path.abspath(path)),
                        base_dir=os.path.basename(os.path.abspath(path)))
    return zip_path


def is_push_payload(name):
    """只有载荷名（cb_/file_/xfer_）才对端认识并直达推送。

    普通消息（<ts>_win.md、<ts>_win-clipack.md）对端 /api/push 会判 404，直推只会白吃
    一次往返再降级；这类条目直接走队列，由对端 2 s 轮询取走即可。
    """
    return name.startswith(PUSH_PREFIXES)


def send_bytes(bridge, qname, blob):
    """小载荷发送：先走协议 v2 直达推送，不可达（或非载荷名）才退回协议 v1 队列 PUT。"""
    if is_push_payload(qname):
        try:
            res = bridge.push(qname, blob=blob)
            log.info('直达推送 %s (%d B) ok=%s', qname, len(blob), res.get('action', 'ok'))
            return res
        except PushUnavailable as e:
            log.info('直达推送不可用（%s），%s 退回队列', e, qname)
    return bridge.put_bytes(QUEUE_OUT, qname, blob)


def send_batch(bridge, raw_paths, origin, guard_win_path=True):
    """把一批路径发到 win2mac。返回 (ok_count, fail_list)。

    guard_win_path=True（默认，剪贴板路径）时丢弃非本机 Windows 路径：
    Deskflow 把对端剪贴板同步过来时 CF_HDROP 带的是 Mac 路径，必须丢弃防 A->B->A 回环。
    CLI --send 关闭该守卫（用户手动选中的一定是本机路径，且要能发带 . 的隐藏路径）。
    """
    staged = []          # (local_path, orig_name, temp?)
    for p in raw_paths:
        if guard_win_path and not is_win_path(p):
            log.warning('[%s] 非本机 Windows 路径（疑似 Deskflow 同步来的对端路径），丢弃: %s', origin, p)
            continue
        if is_junk(p):
            log.info('[%s] 跳过垃圾文件: %s', origin, p)
            continue
        try:
            if os.path.isdir(p):
                z = ensure_zip(p)
                staged.append((z, os.path.basename(z), True))
            elif os.path.isfile(p):
                staged.append((p, os.path.basename(p), False))
            else:
                log.warning('[%s] 不存在或不可读，跳过: %s', origin, p)
        except Exception as e:
            log.error('[%s] 打包失败 %s: %r', origin, p, e)

    if not staged:
        return 0, []

    xid = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    too_big = [(lp, on, t) for (lp, on, t) in staged if os.path.getsize(lp) > MAX_BYTES]
    for lp, on, t in too_big:
        log.error('[%s] 超限(%d B > %d B)，拒绝发送: %s', origin, os.path.getsize(lp), MAX_BYTES, on)
        if t:
            try:
                os.remove(lp)
            except Exception:
                pass
    staged = [(lp, on, t) for (lp, on, t) in staged if os.path.getsize(lp) <= MAX_BYTES]

    if not staged:
        return 0, []

    if len(staged) > 1:
        manifest = {
            'id': xid,
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'origin': origin,
            'count': len(staged),
            'total_bytes': sum(os.path.getsize(lp) for lp, _, _ in staged),
            'files': [{'name': on, 'bytes': os.path.getsize(lp),
                       'sha256': sha256_file(lp)} for lp, on, _ in staged],
        }
        try:
            send_bytes(bridge, 'xfer_%s.json' % xid,
                       json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
            log.info('[%s] 清单已发 xfer_%s.json（%d 个文件）', origin, xid, len(staged))
        except Exception as e:
            log.error('[%s] 清单发送失败，仍继续逐个发文件: %r', origin, e)

    ok, fail = 0, []
    for (lp, on, temp) in staged:            # 串行，便于对账、避免打死对端 IO
        size = os.path.getsize(lp)
        local_sha = sha256_file(lp)
        qname = 'file_%s_%s' % (time.strftime('%Y%m%d-%H%M%S'), clean_name(on))
        last = None
        sent = False
        for attempt in range(1 + len(RETRY_SLEEPS)):
            try:
                t0 = time.time()
                res = bridge.push(qname, local_path=lp, size=size, orig_name=on, sha256=local_sha)
                via = 'push'
            except PushUnavailable as pe:
                log.info('[%s] 直达推送不可用（%s），%s 退回队列', origin, pe, on)
                try:
                    t0 = time.time()
                    res = bridge.put_file(QUEUE_OUT, qname, lp, size, orig_name=on)
                    via = 'queue'
                except Exception as e:
                    last = repr(e)
                    log.warning('[%s] 发送失败(第 %d 次) %s: %s', origin, attempt + 1, on, last)
                    if attempt < len(RETRY_SLEEPS):
                        time.sleep(RETRY_SLEEPS[attempt])
                    continue
            try:
                if res.get('sha256') != local_sha:
                    raise BridgeError('sha256 不一致 本地=%s 对端=%s' % (local_sha, res.get('sha256')))
                dt = time.time() - t0
                log.info('[%s] 已发送 %s (%d B, %.3fs, %.1f MB/s, via=%s) -> %s',
                         origin, on, size, dt, size / dt / 1048576.0 if dt > 0 else 0.0, via, qname)
                sent = True
                break
            except Exception as e:
                last = repr(e)
                log.warning('[%s] 发送失败(第 %d 次) %s: %s', origin, attempt + 1, on, last)
                if attempt < len(RETRY_SLEEPS):
                    time.sleep(RETRY_SLEEPS[attempt])
        if sent:
            ok += 1
        else:
            fail.append({'name': on, 'bytes': size, 'sha256': local_sha, 'error': last})
        if temp:
            try:
                os.remove(lp)
            except Exception:
                pass

    if fail:
        receipt = {'id': xid, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'origin': origin, 'failed': fail}
        try:
            send_bytes(bridge, 'xfer_%s.fail.json' % xid,
                       json.dumps(receipt, ensure_ascii=False).encode('utf-8'))
        except Exception as e:
            log.error('失败回执发送失败: %r', e)
    log.info('[%s] 本轮完成：成功 %d，失败 %d', origin, ok, len(fail))
    return ok, fail


# ---------------------------------------------------------------- 接收（方向 2）

def send_clip_payload(bridge, payload, origin='clipboard'):
    """把一次剪贴板变化发到 win2mac。files 走既有 file_ 通道，text/image 走 cb_ 通道。"""
    kind = payload['kind']
    if kind == 'files':
        send_batch(bridge, payload['paths'], origin)
        return
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-%03d' % (int(time.time() * 1000) % 1000)
    if kind == 'text':
        blob = payload['text'].encode('utf-8')
        if len(blob) > CLIP_TEXT_MAX:
            log.warning('[%s] 文本 %d B 超过上限 %d B，跳过', origin, len(blob), CLIP_TEXT_MAX)
            return
        qname = '%s%s_win_text.txt' % (CB_PREFIX, stamp)
        send_bytes(bridge, qname, blob)
        log.info('[%s] 已发送剪贴板文本 %d 字 -> %s', origin, len(payload['text']), qname)
        return
    png = payload['png']
    if len(png) > CLIP_IMAGE_MAX:
        log.warning('[%s] 图片 %d B 超过上限 %d B，跳过', origin, len(png), CLIP_IMAGE_MAX)
        return
    qname = '%s%s_win_image.png' % (CB_PREFIX, stamp)
    send_bytes(bridge, qname, png)
    log.info('[%s] 已发送剪贴板图片 %d B -> %s', origin, len(png), qname)


# 出站剪贴板发送走独立线程：对端 /api/push 慢（实测一次小文本 6.7 s，日志里 12–17 s）时，
# 0.25 s 的本地看门狗不能被它堵住 —— 否则这十几秒内的新复制全被压住，表现为"剪贴板很久不更新"。
# 队列只留最新一条：对端慢时丢旧发新，保证最终落地的是用户最新复制的内容。
_SEND_Q = queue.Queue(maxsize=1)


def enqueue_clip_payload(payload):
    """把要发的内容交给发送线程；满则丢弃尚未发出的旧内容（最新优先）。"""
    try:
        _SEND_Q.put_nowait(payload)
    except queue.Full:
        try:
            _SEND_Q.get_nowait()
        except queue.Empty:
            pass
        try:
            _SEND_Q.put_nowait(payload)
        except queue.Full:
            pass


def _sender_loop():
    b = Bridge()                          # 独立 Bridge：不与主循环共享连接
    while True:
        payload = _SEND_Q.get()
        if payload is None:
            break
        try:
            send_clip_payload(b, payload, 'clipboard')
        except Exception as e:
            log.error('异步发送剪贴板失败: %r', e)


def check_clipboard(bridge, st):
    """剪贴板序号变了就读一次内容：文件/图片/文字三类取其一，全自动双向同步。"""
    seq = clip_seq()
    if seq == st['last_seq']:
        return
    payload = read_clipboard_payload()
    if payload is None:
        st['last_seq'] = seq
        return                                    # 空剪贴板，或只有不认识的格式（如 Deskflow Ownership）
    fp = payload_fp(payload)
    st['last_seq'] = seq

    # 快路（协议 v2）刚把对端内容写进本机剪贴板：吞掉，绝不回发（否则两端互推成环）
    with _PUSH_LOCK:
        echoed = fp in _PUSH_ECHO
        if echoed:
            _PUSH_ECHO.discard(fp)
    if echoed:
        st['last_fp'] = fp
        st['self_seq'] = seq
        st['self_fp'] = fp
        return

    if fp == st['last_fp']:
        return                                    # 内容没变（序号被无关进程扰动）
    st['last_fp'] = fp
    if seq == st['self_seq'] or fp == st['self_fp']:
        return                                    # 我方自己写入的，防回环
    if payload['kind'] == 'files':
        what = '%d 个文件/目录' % len(payload['paths'])
    elif payload['kind'] == 'image':
        what = '图片'
    else:
        what = '文本'
    if payload['kind'] == 'files' and _DRAG_SRC_ON:
        # 拖拽源端会话进行中：Explorer 拖拽会顺带把文件写进剪贴板，若照推会与 drag_ 协议重复，
        # 且可能抢在 Mac 落点前把文件送进 inbox。
        log.info('拖拽会话进行中，抑制剪贴板文件推送（避免与拖拽协议重复）')
        return
    log.info('剪贴板检测到 %s（交给发送线程）', what)
    enqueue_clip_payload(payload)


def scan_outbox(bridge, st):
    try:
        names = sorted(os.listdir(DIR_OUTBOX))
    except Exception as e:
        log.error('发送箱不可读: %r', e)
        return
    for n in names:
        p = os.path.join(DIR_OUTBOX, n)
        if n.startswith('.'):
            continue
        try:
            if os.path.isfile(p):
                fp = '%d:%d' % (os.path.getsize(p), int(os.path.getmtime(p)))
            elif os.path.isdir(p):
                cnt = tot = 0
                mx = 0
                for root, _, fns in os.walk(p):
                    for fn in fns:
                        try:
                            s = os.path.getsize(os.path.join(root, fn))
                        except OSError:
                            continue
                        cnt += 1
                        tot += s
                        mx = max(mx, int(os.path.getmtime(os.path.join(root, fn))))
                fp = 'd%d:%d:%d' % (cnt, tot, mx)
            else:
                continue
        except OSError:
            continue
        if st['outbox_done'].get(n) == fp:
            continue
        if st['outbox_last'].get(n) != fp:
            st['outbox_last'][n] = fp        # 等下一轮确认文件已写稳定
            continue
        log.info('发送箱投递: %s', n)
        ok, fail = send_batch(bridge, [p], 'outbox')
        if ok and not fail:
            st['outbox_done'][n] = fp


def poll_inbox(bridge, st):
    msgs = bridge.list_queue(QUEUE_IN)
    present = set()
    received = []          # 本轮收到的落地路径，收完统一写一次剪贴板
    for m in msgs:
        name = m.get('name', '')
        size = int(m.get('size') or 0)
        present.add(name)
        with _PUSH_LOCK:
            pushed = name in _PUSH_SEEN
        if pushed:
            # 快路已经落地过（对端推完又在队列里留了一份副本）：只 ack，不重复落地
            if name not in st['processed']:
                try:
                    bridge.ack(QUEUE_IN, name)
                    st['processed'].append(name)
                    log.info('快路已落地，队列副本仅 ack: %s', name)
                except Exception as e:
                    log.warning('队列副本 ack 失败 %s: %r', name, e)
            continue
        if name.startswith('xfer_') and name.endswith('.json'):
            if name not in st['processed']:
                handle_xfer(bridge, st, name)
            continue
        if name.startswith(CB_PREFIX):
            if name not in st['processed']:
                handle_clip_incoming(bridge, st, name, size)
            continue
        if not name.startswith('file_'):
            if name not in st['seen_msgs']:
                log.info('收到 Mac 普通消息（不 ack，留给人工/agent 取）: %s', name)
                st['seen_msgs'].append(name)
            continue
        if name.endswith('.part') or name in st['processed']:
            continue
        dest = handle_incoming(bridge, st, name, size)
        if dest:
            received.append(dest)
    # 已被 ack 的条目清出 processed（避免列表无限增长）
    st['processed'] = [n for n in st['processed'] if n in present]
    st['seen_msgs'] = [n for n in st['seen_msgs'] if n in present]

    if received:
        try:
            if write_clipboard_files_heal(received, bridge):
                st['self_seq'] = clip_seq()
                st['self_fp'] = payload_fp({'kind': 'files', 'paths': received})
                st['last_seq'] = st['self_seq']
                st['last_fp'] = st['self_fp']
                log.info('已把本轮 %d 个文件写入剪贴板（自愈复查在后台）', len(received))
            else:
                log.error('写剪贴板失败（文件已在 inbox）')
                post_clip_ack(bridge, received, False, 0)
        except Exception as e:
            log.error('写剪贴板异常（文件已在 inbox）: %r', e)


def post_clip_ack(bridge, paths, ok, rewrites):
    """回一条剪贴板落地回执给 Mac（约定：对端据此判定写入是否被 Deskflow 清掉）。"""
    if bridge is None:
        return
    name = time.strftime('%Y%m%d-%H%M%S') + '_win-clipack.md'
    lines = [
        '# 剪贴板落地回执',
        '',
        '- 结果: %s' % ('写入生效（复查窗口内未被清）' if ok else '写入被清掉且自愈未成功'),
        '- 自愈重写次数: %d' % rewrites,
        '- 落地文件数: %d' % len(paths),
        '- 文件: %s' % ', '.join(os.path.basename(p) for p in paths),
        '- 回执时刻: %s' % time.strftime('%Y-%m-%d %H:%M:%S'),
    ]
    try:
        send_bytes(bridge, name, '\n'.join(lines).encode('utf-8'))
        log.info('已回剪贴板落地回执 %s', name)
    except Exception as e:
        log.warning('剪贴板落地回执发送失败: %r', e)


def fetch_cb_blob(bridge, name, size):
    """取回 cb_ 条目的原始字节。

    必须走 ?raw=1：普通 GET 的响应被服务端按 UTF-8 解码（Content-Type: text/markdown），
    二进制图片会被替换字符毁掉（实测 260 B 的 PNG 变成 400 B 无效数据，PIL 报
    UnidentifiedImageError）。
    """
    part = os.path.join(DIR_TMP, name + '.part')
    written, declared, clen, _ = bridge.download_raw(QUEUE_IN, name, size, part)
    try:
        if clen is not None and written != int(clen):
            raise BridgeError('字节数不符：收到 %d / Content-Length %s' % (written, clen))
        if size and written != size:
            raise BridgeError('字节数不符：收到 %d / 列表 %s' % (written, size))
        if declared:
            sha = sha256_file(part)
            if sha.lower() != declared.lower():
                raise BridgeError('sha256 不符：本地 %s / 声明 %s' % (sha, declared))
        with open(part, 'rb') as f:
            return f.read()
    finally:
        try:
            os.remove(part)
        except OSError:
            pass


def handle_clip_incoming(bridge, st, name, size):
    """cb_<ts>_<win|mac>_{text,image}.{txt,png}：取回内容写进本机剪贴板，然后 ack。"""
    m = RE_CB_NAME.match(name)
    if not m:
        log.warning('cb 条目命名不认识，跳过（不 ack）: %s', name)
        return
    kind = m.group(3)
    limit = CLIP_TEXT_MAX if kind == 'text' else CLIP_IMAGE_MAX
    if size > limit:
        log.warning('cb 条目 %s 超限 (%d B > %d B)，跳过（不 ack）', name, size, limit)
        return
    try:
        blob = fetch_cb_blob(bridge, name, size)
    except Exception as e:
        log.error('cb 内容读取失败 %s: %r', name, e)
        return
    ok, what = apply_clip_blob(name, blob)
    if not ok:
        log.error('cb 落地失败 %s: %s（内容保留在队列里，下轮重试）', name, what)
        return
    if kind == 'text':
        fp = 'text:' + sha1_bytes(blob)
    else:
        # 图片经 PNG<->DIB 往返字节会变，按真正落到剪贴板的内容取指纹
        fp = payload_fp(read_clipboard_payload())
    st['self_seq'] = clip_seq()
    st['self_fp'] = fp
    st['last_seq'] = st['self_seq']
    st['last_fp'] = fp
    log.info('已把对端剪贴板%s写入本机剪贴板（%s）', what, name)
    try:
        res = bridge.ack(QUEUE_IN, name)
        log.info('已 ack %s -> %s', name, res.get('moved_to'))
        st['processed'].append(name)
    except Exception as e:
        log.error('ack 失败 %s（下轮会重写剪贴板）: %r', name, e)


def handle_xfer(bridge, st, name):
    """xfer_<id>.json 多文件清单 / .fail.json 失败回执：读出记账后 ack。"""
    try:
        doc = json.loads(bridge.fetch_text(QUEUE_IN, name).decode('utf-8'))
    except Exception as e:
        log.error('清单读取失败 %s: %r', name, e)
        return
    if name.endswith('.fail.json'):
        log.warning('对端失败回执 %s: %s', name,
                    json.dumps(doc.get('failed', doc), ensure_ascii=False))
    else:
        log.info('多文件清单 %s: id=%s count=%s total=%s bytes origin=%s',
                 name, doc.get('id'), doc.get('count'), doc.get('total_bytes'), doc.get('origin'))
        for f in doc.get('files', []):
            log.info('  清单项: %s (%s B)', f.get('name'), f.get('bytes'))
    try:
        res = bridge.ack(QUEUE_IN, name)
        log.info('已 ack 清单 %s -> %s', name, res.get('moved_to'))
        st['processed'].append(name)
    except Exception as e:
        log.error('ack 清单失败 %s: %r', name, e)


def handle_incoming(bridge, st, name, size):
    part = os.path.join(DIR_TMP, name + '.part')
    t0 = time.time()
    try:
        written, declared, clen, orig_hdr = bridge.download_raw(QUEUE_IN, name, size, part)
    except Exception as e:
        log.error('下载失败 %s: %r', name, e)
        try:
            os.remove(part)
        except OSError:
            pass
        return
    dt = time.time() - t0
    if clen is not None and written != int(clen):
        log.error('下载不完整 %s: 收到 %d / Content-Length %s', name, written, clen)
        try:
            os.remove(part)
        except OSError:
            pass
        return
    dest, err = land_file(name, part, size, declared, orig_hdr)
    if not dest:
        log.error('落地失败 %s: %s', name, err)
        try:
            os.remove(part)
        except OSError:
            pass
        return
    log.info('已接收 %s -> %s (%d B, %.2fs, %.1f MB/s)', name, dest, written, dt,
             written / dt / 1048576.0 if dt > 0 else 0.0)

    try:
        res = bridge.ack(QUEUE_IN, name)
        log.info('已 ack %s -> %s', name, res.get('moved_to'))
        st['processed'].append(name)
    except Exception as e:
        log.error('ack 失败 %s（下轮会重下）: %r', name, e)
    return dest


# ---------------------------------------------------------------- 主循环

def local_beat(bridge, st):
    """本地节奏（0.25 s）：只碰本机剪贴板，零网络成本。

    快路收到的东西由接收线程登记，这里消费掉 —— 保证剪贴板与 st 记账始终在单线程里发生。
    """
    check_clipboard(bridge, st)
    drain_pushed_files(bridge, st)
    mask_cursor_if_on_peer()
    if _DRAG_SESSIONS:                       # armed 期间每秒采样本机光标+活动屏（诊断跨屏跟手）
        now = time.time()
        if now - _DRAG_TRACE_LAST[0] >= 1.0:
            _DRAG_TRACE_LAST[0] = now
            _DRAG_TRACE.append((now, draghook.cursor_pos(), active_screen_from_log()))
            if len(_DRAG_TRACE) > 60:
                del _DRAG_TRACE[:-60]


def full_beat(bridge, st):
    """网络节奏（2 s）：发件箱扫描 + 队列兜底轮询 + 落盘。

    队列只兜底：对端离线期间积压的条目、快路不可用时的降级副本、以及审计流水。
    """
    try:
        scan_outbox(bridge, st)
    except Exception as e:
        log.error('发送箱扫描异常: %r', e)
    try:
        poll_inbox(bridge, st)
    except Exception as e:
        # 服务端重启窗口：连接失败即跳过本轮，下轮重试，不判致命
        log.warning('接收轮询失败（跳过本轮）: %r', e)
    with _PUSH_LOCK:
        st['push_seen'] = sorted(_PUSH_SEEN)[-PUSH_SEEN_MAX:]
    _drag_sweep()
    inbox_sweep()
    save_state(st)


def tick(bridge, st):
    try:
        local_beat(bridge, st)
    except Exception as e:
        log.error('剪贴板检查异常: %r', e)
    full_beat(bridge, st)


def run_forever():
    import win32event, win32api
    setup_logging()
    mutex = win32event.CreateMutex(None, False, 'KEEPPER_clipwatch_mutex')
    if win32api.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        log.info('已有 clipwatch 实例在运行，本进程退出')
        return 0

    st = load_state()
    with _PUSH_LOCK:
        _PUSH_SEEN.update(st.get('push_seen', []))
    bridge = Bridge()
    threading.Thread(target=_sender_loop, daemon=True).start()   # 出站发送与看门狗解耦
    srv = start_push_server()
    threading.Thread(target=_drag_drop_worker, daemon=True).start()   # 拖拽投放（松手才做 I/O）
    threading.Thread(target=_drag_src_worker, daemon=True).start()    # 拖拽源端（Win→Mac 预传/取消）
    _drag_hook = draghook.DragHook(_drag_on_event, log)              # 全局鼠标观测，永不拦截
    _drag_hook.start()
    log.info('clipwatch 启动：base=%s 本地剪贴板=%ss 队列兜底=%ss 快路=%s',
             BASE, LOCAL_CLIP_SEC, POLL_SEC,
             ('监听 0.0.0.0:%d + 直连 %s:%d/api/push' % (PUSH_PORT, PEER_HOST, PEER_PUSH_PORT))
             if srv else '未启用（只用队列）')
    last_full = 0.0
    while True:
        try:
            local_beat(bridge, st)
        except Exception as e:
            log.error('剪贴板检查异常: %r', e)
        now = time.time()
        if now - last_full >= POLL_SEC:
            last_full = now
            try:
                full_beat(bridge, st)
            except Exception as e:
                log.error('队列节奏异常: %r', e)
        time.sleep(LOCAL_CLIP_SEC)


def msgbox(title, text):
    """pythonw 下无控制台，右键「发送到对端」失败时弹窗告知；成功则静默。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x30)
    except Exception:
        pass


def main(argv):
    if sys.stdout is None:      # pythonw 启动时无控制台
        sys.stdout = open(os.devnull, 'w')
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == '--selftest-clip':
        setup_logging(verbose_console=True)
        p = os.path.join(DIR_TMP, '_selftest.txt')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('clipwatch selftest')
        ok = write_clipboard_files([p])
        back = read_clipboard_files()
        print('files  write ok =', ok)
        print('files  read back =', back)
        print('files  match =', [os.path.abspath(p)] == [os.path.abspath(x) for x in back])

        sample = '剪贴板文本回环自检 clipwatch %d' % int(time.time())
        print('text   write ok =', write_clipboard_text(sample))
        got = read_clipboard_payload()
        print('text   read back =', (got or {}).get('text'))
        print('text   match =', (got or {}).get('text') == sample)

        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (64, 48), (200, 30, 30)).save(buf, 'PNG')
        png = buf.getvalue()
        print('image  write ok =', write_clipboard_image(png))
        got = read_clipboard_payload()
        if got and got['kind'] == 'image':
            a = Image.open(io.BytesIO(png))
            a.load()
            b = Image.open(io.BytesIO(got['png']))
            b.load()
            same = a.size == b.size and a.convert('RGB').tobytes() == b.convert('RGB').tobytes()
            print('image  got %s, %d B, match = %s' % (b.size, len(got['png']), same))
        else:
            print('image  FAILED, read back =', (got or {}).get('kind'))
        print('seq =', clip_seq())
        print('注：自检会把测试图/文件留在剪贴板，常驻进程在跑的话会被同步到 Mac')
        return 0

    if cmd == '--status':
        setup_logging(verbose_console=True)
        st = load_state()
        b = Bridge()
        try:
            print('health =', json.dumps(b.health(), ensure_ascii=False))
            print('mac2win =', json.dumps(b.list_queue(QUEUE_IN), ensure_ascii=False, indent=2))
        except Exception as e:
            print('bridge unreachable: %r' % (e,))
        print('state =', json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if cmd == '--send':
        setup_logging(verbose_console=True)
        paths = argv[2:]
        if not paths:
            print('usage: --send <路径...>')
            return 2
        b = Bridge()
        ok, fail = send_batch(b, paths, 'cli', guard_win_path=False)
        print('ok = %d, fail = %d' % (ok, len(fail)))
        if fail:
            msgbox('Clipwatch 发送失败',
                   '以下文件未能送达 Mac：\n\n' +
                   '\n'.join('%s  <- %s' % (f['name'], f['error']) for f in fail))
        return 0 if not fail else 1

    if cmd == '--dragselftest':
        setup_logging(verbose_console=True)
        global _DRAG_TEST_TARGET
        import ctypes
        did = 'selftest-%s' % time.strftime('%H%M%S')
        qn = 'file_selftest_demo'
        sess = drag_arm({'id': did, 'items': [{'name': qn, 'orig': 'drag-selftest.txt'}]})
        if not sess:
            print('arm failed')
            return 1
        with open(os.path.join(sess['dir'], 'drag-selftest.txt'), 'w', encoding='utf-8') as f:
            f.write('drag selftest')
        _DRAG_TEST_TARGET = os.path.join(DIR_TMP, '_dragtest_out')
        os.makedirs(_DRAG_TEST_TARGET, exist_ok=True)
        for n in os.listdir(_DRAG_TEST_TARGET):
            os.remove(os.path.join(_DRAG_TEST_TARGET, n))
        threading.Thread(target=_drag_drop_worker, daemon=True).start()
        hk = draghook.DragHook(_drag_on_event, log)
        hk.start()
        time.sleep(0.5)                       # 等钩子线程装好
        u = ctypes.windll.user32
        sw = u.GetSystemMetrics(0) or 1920
        sh = u.GetSystemMetrics(1) or 1080
        u.mouse_event(0x8001, int(200 * 65535 / (sw - 1)), int(200 * 65535 / (sh - 1)), 0, 0)
        time.sleep(0.2)
        u.mouse_event(0x0004, 0, 0, 0, 0)     # 注入 LEFT-UP：钩子应捕获并触发投放
        time.sleep(1.5)
        got = sorted(os.listdir(_DRAG_TEST_TARGET))
        print('stage dir  =', sess['dir'])
        print('target dir =', _DRAG_TEST_TARGET)
        print('landed     =', got)
        print('ok         =', 'drag-selftest.txt' in got)
        hk.stop()
        return 0 if 'drag-selftest.txt' in got else 1

    if cmd == '--once':
        setup_logging(verbose_console=True)
        st = load_state()
        b = Bridge()
        tick(b, st)
        print('one tick done')
        return 0

    return run_forever()


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)