# -*- coding: utf-8 -*-
"""Deskflow 连接触启：登录自启的轻量守护，检测到客户端连上后拉起 clipwatch。

为什么需要它：剪贴板/文件双向传输（clipwatch）只在两端真的连上时才有意义，
故不再登录即常驻，而是由本守护盯 Deskflow 服务端日志 —— 一旦出现
`client "MAC" has connected`，就拉起 clipwatch；连接断开期间不做事。

判据与 clipwatch 一致，读同一个 `core-server.log` 尾部：最后一条
`has connected` / `has disconnected` 决定当前是否已连接。拉起用同一套
`run-clipwatch-hidden.vbs`（clipwatch 内建命名互斥体，重复拉起会自退，天然幂等）。

本脚本自身也用命名互斥体保证单例。日志见 logs/autostart.log。
"""

import ctypes
import ctypes.wintypes as w
import logging
import os
import re
import subprocess
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DESKFLOW_LOG = r'D:\KEEPPER\_deskflow\core-server.log'
LOG_FILE = os.path.join(BASE, 'logs', 'autostart.log')
LAUNCHER_VBS = os.path.join(BASE, 'run-clipwatch-hidden.vbs')   # 拉起 clipwatch（隐藏窗口）

CLIP_MUTEX = 'KEEPPER_clipwatch_mutex'      # 与 clipwatch.py 内建互斥体同名
SELF_MUTEX = 'KEEPPER_autostart_mutex'
POLL_SEC = 3.0
LAUNCH_COOLDOWN = 10.0                      # 拉起冷却，避免启动窗口内反复拉起
ERROR_ALREADY_EXISTS = 183

RE_MARK = re.compile(r'client "[^"]*" has (connected|disconnected)')

_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.CreateMutexW.restype = w.HANDLE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, ctypes.c_wchar_p]
_k32.CloseHandle.argtypes = [w.HANDLE]

log = logging.getLogger('autostart')


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    h = logging.FileHandler(LOG_FILE, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _mutex_exists(name):
    """命名互斥体是否已存在（存在 = 对应进程在跑）。取到句柄后立刻关闭，不留占用。"""
    h = _k32.CreateMutexW(None, True, name)
    err = ctypes.get_last_error()
    if h:
        _k32.CloseHandle(h)
    return err == ERROR_ALREADY_EXISTS


def clipwatch_running():
    return _mutex_exists(CLIP_MUTEX)


def client_connected():
    """Deskflow 当前是否有客户端连上：读日志尾部，最后一条 connect/disconnect 决定。"""
    try:
        size = os.path.getsize(DESKFLOW_LOG)
        with open(DESKFLOW_LOG, 'rb') as f:
            f.seek(max(0, size - 16384))
            tail = f.read().decode('utf-8', 'replace')
    except OSError:
        return False
    state = False
    for line in tail.splitlines():
        m = RE_MARK.search(line)
        if m:
            state = (m.group(1) == 'connected')
    return state


def launch_clipwatch():
    subprocess.Popen(['wscript.exe', LAUNCHER_VBS],
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def run_forever():
    setup_logging()
    if _mutex_exists(SELF_MUTEX):
        log.info('已有 autostart 实例在运行，本进程退出')
        return 0
    # 本实例持有自互斥体（创建新句柄后不关闭，进程存活期间一直持有）
    _k32.CreateMutexW(None, True, SELF_MUTEX)
    log.info('deskflow-autostart 启动：轮询 %s（%.1fs），连上即拉起 clipwatch',
             DESKFLOW_LOG, POLL_SEC)
    last_launch = 0.0
    while True:
        try:
            if client_connected() and not clipwatch_running():
                now = time.time()
                if now - last_launch >= LAUNCH_COOLDOWN:
                    last_launch = now
                    launch_clipwatch()
                    log.info('Deskflow 客户端已连上，拉起 clipwatch')
        except Exception as e:
            log.error('轮询异常: %r', e)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    raise SystemExit(run_forever())