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
