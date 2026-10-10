# -*- coding: utf-8 -*-
"""KEEPPER 看门狗：登录自启的常驻守护，保证 Windows 侧服务与 Trae 无关地一直在线。

它做两件事，每 POLL 秒一轮：
  1) 保活 Deskflow 服务端（deskflow-core.exe）——进程不在就拉起 run-server-hidden.vbs。
     服务端一挂 Mac 就连不上，故必须自愈，不能依赖任何 IDE/终端会话。
  2) 客户端连上后拉起 clipwatch（剪贴板/文件双向传输）——同样自愈。

为什么不靠 Trae：本脚本由「计划任务」在登录时以 wscript 启动，父进程是系统，
与 Trae 的作业对象无关；Trae 关掉不会带走它。脚本自身用命名互斥体保证单例。
日志见 logs/autostart.log。
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
LAUNCH_SERVER_VBS = r'D:\KEEPPER\_deskflow\run-server-hidden.vbs'   # 拉起 Deskflow 服务端
LAUNCH_CLIP_VBS = os.path.join(BASE, 'run-clipwatch-hidden.vbs')    # 拉起 clipwatch
LOG_FILE = os.path.join(BASE, 'logs', 'autostart.log')

CLIP_MUTEX = 'KEEPPER_clipwatch_mutex'      # 与 clipwatch.py 内建互斥体同名
SELF_MUTEX = 'KEEPPER_autostart_mutex'
SERVER_IMAGE = 'deskflow-core.exe'
POLL_SEC = 5.0
RELAUNCH_COOLDOWN = 15.0                    # 同一目标两次拉起的最小间隔，避免启动窗口内反复拉起
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


def server_running():
    """deskflow-core.exe 是否在跑（tasklist 查询，轻量）。"""
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq %s' % SERVER_IMAGE, '/NH'],
                             capture_output=True, text=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return SERVER_IMAGE.lower() in (out.stdout or '').lower()
    except OSError:
        return False


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


def _run_hidden(vbs):
    subprocess.Popen(['wscript.exe', vbs],
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def run_forever():
    setup_logging()
    if _mutex_exists(SELF_MUTEX):
        log.info('已有看门狗实例在运行，本进程退出')
        return 0
    _k32.CreateMutexW(None, True, SELF_MUTEX)   # 本实例持有自互斥体（句柄不关，进程存活期间一直持有）
    log.info('KEEPPER 看门狗启动：%s 保活 + 客户端连上即拉起 clipwatch（%.0fs 一轮）',
             SERVER_IMAGE, POLL_SEC)
    last = {}
    while True:
        try:
            now = time.time()
            if not server_running():
                if now - last.get('server', 0) >= RELAUNCH_COOLDOWN:
                    last['server'] = now
                    _run_hidden(LAUNCH_SERVER_VBS)
                    log.warning('Deskflow 服务端未在运行，已拉起')
            elif client_connected() and not clipwatch_running():
                if now - last.get('clip', 0) >= RELAUNCH_COOLDOWN:
                    last['clip'] = now
                    _run_hidden(LAUNCH_CLIP_VBS)
                    log.info('Deskflow 客户端已连上，拉起 clipwatch')
        except Exception as e:
            log.error('看门狗轮询异常: %r', e)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    raise SystemExit(run_forever())
