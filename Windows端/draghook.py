# -*- coding: utf-8 -*-
"""跨屏拖拽 —— Windows 侧全局低层鼠标钩子（WH_MOUSE_LL）。

只观测、永不拦截：钩子一律 `CallNextHookEx`，绝不 `return 1`（否则会截断 Deskflow
的键鼠转发，Mac↔Windows 的鼠标就废了）。与 Mac 侧 `CGEventTap(kCGHIDEventTap,
listen-only)` 对称。

线程模型：本模块在**独立线程**里装钩子并跑 `GetMessage` 循环。回调只做极轻量的判定
（是否注入、落点、以及按住期间 `GetGUIThreadInfo(0).hwndCapture` 是否出现），然后回调
上层；任何 I/O 都交给上层消费线程，避免拖慢低层钩子链（Windows 对低层钩子回调有
~300ms 超时，超时会静默摘钩）。

事件契约（回调 `on_event(ev, x, y, injected, cap)`）：
  'down'      —— 物理/注入的 LEFT-DOWN（起点）
  'move'      —— 按住期间的位移（位置变化才报，用于还原跨屏时被停放的落点轨迹）
  'dragstart' —— 按住期间首次出现 capture（OLE 拖拽源窗口已 SetCapture）→ 拖拽开始
  'edge'      —— 按住期间触及虚拟屏下边缘（Mac 挂下方）→ 可能已跨屏，每按一次只报一次
  'up'        —— LEFT-UP（终点）
cap 仅在 'dragstart' 时非 0（拖拽源窗口 hwnd）。

判据实证（probe_dragsource.log）：真实拖拽 LDOWN→MOVE 时 capture=explorer.exe 的
DirectUIHWND；单纯点击（无 MOVE）不出现 capture；injected 可区分 Deskflow 注入。

契约见 D:\\KEEPPER\\_agentbridge\\msg-drag-locked.md。
"""

import ctypes
import ctypes.wintypes as w
import threading

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_QUIT = 0x0012
LLMHF_INJECTED = 0x00000001

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', w.POINT), ('mouseData', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('flags', w.DWORD), ('hwndActive', w.HWND),
                ('hwndFocus', w.HWND), ('hwndCapture', w.HWND), ('hwndMenuOwner', w.HWND),
                ('hwndMoveSize', w.HWND), ('hwndCaret', w.HWND), ('rcCaret', w.RECT)]


_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)

_user32 = ctypes.WinDLL('user32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_user32.SetWindowsHookExA.restype = w.HHOOK
_user32.SetWindowsHookExA.argtypes = [ctypes.c_int, _HOOKPROC, w.HINSTANCE, w.DWORD]
_user32.CallNextHookEx.restype = _LRESULT
_user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
_user32.GetMessageA.argtypes = [ctypes.POINTER(w.MSG), w.HWND, ctypes.c_uint, ctypes.c_uint]
_user32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
_user32.PostThreadMessageA.argtypes = [w.DWORD, ctypes.c_uint, w.WPARAM, w.LPARAM]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetGUIThreadInfo.argtypes = [w.DWORD, ctypes.POINTER(GUITHREADINFO)]
_user32.GetAncestor.restype = w.HWND
_user32.GetAncestor.argtypes = [w.HWND, ctypes.c_uint]
_user32.GetForegroundWindow.restype = w.HWND
_user32.WindowFromPoint.restype = w.HWND
_user32.WindowFromPoint.argtypes = [w.POINT]
_user32.GetClassNameW.argtypes = [w.HWND, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetCursorPos.restype = w.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
_kernel32.GetCurrentThreadId.restype = w.DWORD

GA_ROOT = 2


def virtual_screen_rect():
    """整个虚拟桌面（含多显示器）的 (x, y, w, h)。"""
    return (_user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def point_in_windows(pt):
    """点是否落在 Windows 任一物理屏内（Deskflow 把 Mac 也算虚拟屏，但注入坐标仍是本机像素）。"""
    x, y, cx, cy = virtual_screen_rect()
    return cx > 0 and cy > 0 and x <= pt[0] < x + cx and y <= pt[1] < y + cy


def capture_hwnd():
    """前台线程当前 SetCapture 的窗口（0 = 无）。OLE DoDragDrop 会对拖拽源窗口 SetCapture。"""
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if _user32.GetGUIThreadInfo(0, ctypes.byref(gti)):
        return gti.hwndCapture
    return 0


def root_hwnd(hwnd):
    """取顶层窗口（资源管理器视图是 DirectUIHWND 子窗口，需上溯到顶层才能匹配 Shell 窗口）。"""
    return _user32.GetAncestor(hwnd, GA_ROOT) if hwnd else 0


def foreground_hwnd():
    return _user32.GetForegroundWindow()


def window_at_point(pt):
    """屏幕坐标处的窗口句柄（Mac 侧 AXUIElementCopyElementAtPosition 的对应物）。"""
    return _user32.WindowFromPoint(w.POINT(int(pt[0]), int(pt[1])))


def window_class(hwnd):
    """窗口类名（'CabinetWClass' = 资源管理器文件夹窗口，'Progman' = 桌面）。"""
    if not hwnd:
        return ''
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def cursor_pos():
    """当前光标屏幕坐标（Mac 侧 NSEvent.mouseLocation 的对应物）。取不到返回 None。"""
    pt = w.POINT()
    return (pt.x, pt.y) if _user32.GetCursorPos(ctypes.byref(pt)) else None


# ---- 光标可见性：跨屏时把本机指针「屏蔽」（隐藏）/「恢复」----
_user32.ShowCursor.argtypes = [w.BOOL]
_user32.ShowCursor.restype = ctypes.c_int

_CUR_VISIBLE = [True]               # 本进程侧记录的当前可见性（初始按可见计）


def set_cursor_visible(visible):
    """把本机光标的显示计数推到「可见 / 隐藏」，返回是否达成。

    ShowCursor 返回**新的显示计数**：可见 >=0、隐藏 <0。据此自校正，避免计数错位。
    计数只在本进程存活期间有效 —— 进程退出后系统自动恢复光标可见，即使被强杀
    也不会留下「永久无光标」（实证：计数 -1..-4 期间光标被隐藏）。
    """
    if _CUR_VISIBLE[0] == visible:
        return True
    want = 1 if visible else 0
    for _ in range(64):
        c = _user32.ShowCursor(want)
        if (c >= 0) == visible:
            _CUR_VISIBLE[0] = visible
            return True
    return False


class DragHook(object):
    """装 WH_MOUSE_LL 并回调 on_event(ev, x, y, injected, cap)。"""

    def __init__(self, on_event, log=None):
        self._cb = on_event
        self._log = log
        self._hook = None
        self._tid = None
        self._thread = None
        self._down = False
        self._started = False
        self._at_edge = False                  # 本次按住期间是否已触到跨屏边界（每按一次重置）
        self._edge_y = 0
        self._last_pt = None                   # 上一次上报过的鼠标位置（用于按位移节流 'move'）
        self._proc = _HOOKPROC(self._handle)   # 保住引用：回调对象被 GC 会让钩子崩

    def start(self):
        self._thread = threading.Thread(target=self._run, name='draghook', daemon=True)
        self._thread.start()

    def stop(self):
        if self._tid:
            _user32.PostThreadMessageA(self._tid, WM_QUIT, 0, 0)

    def _run(self):
        self._tid = _kernel32.GetCurrentThreadId()
        self._hook = _user32.SetWindowsHookExA(WH_MOUSE_LL, self._proc, None, 0)
        if not self._hook:
            if self._log:
                self._log.error('draghook: SetWindowsHookExA 失败 err=%d', ctypes.get_last_error())
            return
        if self._log:
            self._log.info('draghook: WH_MOUSE_LL 已装（tid=%d），只观测不拦截', self._tid)
        msg = w.MSG()
        while _user32.GetMessageA(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        _user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        if self._log:
            self._log.info('draghook: 已卸载')

    def _emit(self, ev, x, y, injected, cap=0):
        try:
            self._cb(ev, x, y, injected, cap)
        except Exception:
            pass

    def _handle(self, code, wparam, lparam):
        if code >= 0:
            try:
                info = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                x, y = info.pt.x, info.pt.y
                inj = bool(info.flags & LLMHF_INJECTED)
                if wparam == WM_LBUTTONDOWN:
                    self._down = True
                    self._started = False
                    self._at_edge = False
                    self._last_pt = (x, y)
                    _, vy, _, vh = virtual_screen_rect()
                    self._edge_y = vy + vh - 1     # 虚拟屏下边缘（Mac 挂在下方）
                    self._emit('down', x, y, inj)
                elif wparam == WM_MOUSEMOVE and self._down:
                    # 按住期间的位移轨迹（按位置变化节流）：跨屏时本机指针会被停放，
                    # 真实落点只能靠这条轨迹还原 —— 上层用它推算松手位置。
                    if (x, y) != self._last_pt:
                        self._last_pt = (x, y)
                        self._emit('move', x, y, inj)
                    if not self._started:
                        cap = capture_hwnd()
                        if cap:
                            self._started = True
                            self._emit('dragstart', x, y, inj, cap)
                    # 按住期间一旦触及虚拟屏下边缘，就认为可能已跨到 Mac（每按一次只报一次）
                    if not self._at_edge and y >= self._edge_y - 2:
                        self._at_edge = True
                        self._emit('edge', x, y, inj)
                elif wparam == WM_LBUTTONUP:
                    self._down = False
                    self._emit('up', x, y, inj)
            except Exception:
                pass
        return _user32.CallNextHookEx(None, code, wparam, lparam)