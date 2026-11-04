# -*- coding: utf-8 -*-
"""
Lattice.exe  —— 一键把窗口摆成预设布局（纯 ctypes，零第三方依赖）

用法：
  Lattice.exe                应用第 1 套方案，并常驻监听热键
  Lattice.exe -p 2           应用第 2 套方案
  Lattice.exe --once         只排一次，不常驻
  Lattice.exe --list         列出当前可见窗口 + 所有方案
  Lattice.exe --capture      把"现在的窗口排布"算成百分比，打印出来
  Lattice.exe --capture --save   把当前排布存成新方案（追加到 lattice.json）

热键（常驻时生效）：Ctrl+Alt+1..9 切换方案，Ctrl+Alt+Q 退出常驻

原理：读系统工作区（自动避开任务栏/顶栏/dock），按百分比算目标矩形，
      还原最大化窗口后用 SetWindowPos 摆位。全程单进程 DPI 感知，多屏按监视器算。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except Exception:
    dwmapi = None

# ---------- 常量 ----------
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_MAXIMIZE = 0x01000000
WS_EX_TOOLWINDOW = 0x00000080
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_HIDE = 0
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SPI_GETWORKAREA = 0x0030
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
VK_CONTROL, VK_MENU, VK_Q = 0x11, 0x12, 0x51
WM_HOTKEY = 0x0312
HK_QUIT = 99          # RegisterHotKey 的 id，用来区分"退出"和"切方案"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000

# 依次尝试：越靠前越不容易和系统/驱动/输入法撞车。
# 用 RegisterHotKey 独占注册 —— 注册成功后别的程序就收不到这组键了。
HOTKEY_CANDIDATES = [
    ("Ctrl+Alt+Shift+", MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT),
    ("Win+Alt+", MOD_WIN | MOD_ALT | MOD_NOREPEAT),
    ("Ctrl+Alt+", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT),
]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(RECT), wt.LPARAM)


def set_dpi_aware():
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------- 工作区 ----------
def primary_work():
    r = RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def all_works():
    out = []

    def _cb(hmon, hdc, lprc, lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            out.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def pick_work(prof):
    m = prof.get("monitor", "primary")
    if isinstance(m, int):
        ws = all_works()
        if 1 <= m <= len(ws):
            return ws[m - 1]
        return primary_work()
    return primary_work()


# ---------- 窗口枚举 ----------
_pname_cache = {}


def _exe_of(pid):
    if pid in _pname_cache:
        return _pname_cache[pid]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    name = ""
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value)
        kernel32.CloseHandle(h)
    _pname_cache[pid] = name
    return name


def _cls_of(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def _title_of(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    b = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, b, n + 2)
    return b.value


def list_windows():
    """可见、有标题、非工具窗口的顶层窗口，按 z 序（最上层在前）。"""
    rows = []
    self_pid = os.getpid()

    def _cb(hwnd, lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _title_of(hwnd)
        if not title:
            return True
        cls = _cls_of(hwnd)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                   "Windows.UI.Core.CoreWindow", "MultitaskingViewFrame"):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == self_pid:
            return True
        exe = _exe_of(pid.value)
        if exe.lower() in ("lattice.exe",):   # 常驻进程 / 主进程不同 pid，只比 pid 拦不住自己
            return True
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w_, h_ = r.right - r.left, r.bottom - r.top
        if w_ < 160 or h_ < 120:  # 过滤掉条状 dock / 浮窗
            return True
        rows.append({
            "hwnd": hwnd, "exe": exe, "title": title, "cls": cls,
            "rect": (r.left, r.top, w_, h_),
            "maximized": bool(user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_MAXIMIZE),
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return rows


def work_of_window(hwnd):
    hmon = user32.MonitorFromWindow(hwnd, 2)  # NEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return primary_work()


# ---------- 轻量提示条（纯 ctypes，1.4s 自动消失） ----------
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_TOAST_CLASS = "LatticeToastV2"
_toast_proc = None
_toast_state = {}
_API = {}


def _can(name):
    """GDI 部分函数（FillRect/DrawText/SetBkMode/SetTextColor）由 user32 转发，两边都找。"""
    for dll in (kernel32, gdi32, user32):
        try:
            return getattr(dll, name)
        except AttributeError:
            continue
    raise AttributeError("not found: " + name)


def _proto(name, args, ret=None):
    fn = _can(name)
    fn.argtypes = args
    if ret is not None:
        fn.restype = ret
    return fn


def _bind():
    """显式声明参数类型——64 位下句柄按 int 传会被截断成野指针。"""
    if _API:
        return _API
    P = ctypes.POINTER
    _API["CreateSolidBrush"] = _proto("CreateSolidBrush", [wt.COLORREF], wt.HBRUSH)
    _API["CreateFontW"] = _proto("CreateFontW",
                                [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR], wt.HANDLE)
    _API["DeleteObject"] = _proto("DeleteObject", [wt.HANDLE], wt.BOOL)
    _API["FillRect"] = _proto("FillRect", [wt.HDC, P(RECT), wt.HBRUSH], ctypes.c_int)
    _API["SelectObject"] = _proto("SelectObject", [wt.HDC, wt.HANDLE], wt.HANDLE)
    _API["SetBkMode"] = _proto("SetBkMode", [wt.HDC, ctypes.c_int], ctypes.c_int)
    _API["SetTextColor"] = _proto("SetTextColor", [wt.HDC, wt.COLORREF], wt.DWORD)
    _API["DrawTextW"] = _proto("DrawTextW",
                               [wt.HDC, wt.LPCWSTR, ctypes.c_int, P(RECT), wt.UINT], ctypes.c_int)
    _API["CreateRoundRectRgn"] = _proto("CreateRoundRectRgn", [ctypes.c_int] * 6, wt.HANDLE)
    user32.BeginPaint.argtypes = [wt.HWND, P(PAINTSTRUCT)]
    user32.BeginPaint.restype = wt.HDC
    user32.EndPaint.argtypes = [wt.HWND, P(PAINTSTRUCT)]
    user32.RegisterClassW.argtypes = [P(WNDCLASSW)]
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, ctypes.c_ubyte, wt.DWORD]
    user32.SetWindowRgn.argtypes = [wt.HWND, wt.HANDLE, wt.BOOL]
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetMessageW.argtypes = [P(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.DispatchMessageW.argtypes = [P(wt.MSG)]
    user32.TranslateMessage.argtypes = [P(wt.MSG)]
    _API["GetModuleHandleW"] = _proto("GetModuleHandleW", [wt.LPCWSTR], wt.HMODULE)
    return _API


def toast(text, ms=1500):
    """屏幕中下方弹一条深色提示，ms 毫秒后自动消失。失败静默跳过，绝不影响分屏本身。"""
    global _toast_proc
    try:
        api = _bind()

        def _proc(hwnd, msg, wp, lp):
            if msg == 0x000F:  # WM_PAINT
                ps = PAINTSTRUCT()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                st = _toast_state.get(hwnd)
                if st and hdc:
                    br = api["CreateSolidBrush"](0x00221E1A)
                    rc = RECT(0, 0, st["w"], st["h"])
                    api["FillRect"](hdc, ctypes.byref(rc), br)
                    api["DeleteObject"](br)
                    api["SetBkMode"](hdc, 1)  # TRANSPARENT
                    api["SetTextColor"](hdc, 0x00F5F5F5)
                    api["SelectObject"](hdc, st["font"])
                    api["DrawTextW"](hdc, st["text"], -1, ctypes.byref(rc),
                                     0x00000001 | 0x00000004 | 0x00000020)  # CENTER|VCENTER|SINGLELINE
                user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == 0x0113:  # WM_TIMER
                user32.KillTimer(hwnd, 1)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:  # WM_DESTROY
                _toast_state.pop(hwnd, None)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        _toast_proc = WNDPROC(_proc)
        hinst = api["GetModuleHandleW"](None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(_toast_proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = _TOAST_CLASS
        user32.RegisterClassW(ctypes.byref(wc))  # 已注册返回 0，无所谓

        try:
            dpi = user32.GetDpiForSystem()
        except Exception:
            dpi = 96
        sc = (dpi or 96) / 96.0
        font = api["CreateFontW"](int(-16 * sc), 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0,
                                  "Microsoft YaHei UI")
        w = int(min(760 * sc, 34 * sc + len(text) * 9.8 * sc))
        h = int(42 * sc)
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        x, y = (sw - w) // 2, int(sh * 0.855)
        hwnd = user32.CreateWindowExW(
            0x00000008 | 0x00080000 | 0x00000080 | 0x00000020,  # TOPMOST|LAYERED|TOOLWINDOW|NOACTIVATE
            _TOAST_CLASS, "", 0x80000000, x, y, w, h,
            None, None, hinst, None)
        if not hwnd:
            return
        rgn = api["CreateRoundRectRgn"](0, 0, w + 1, h + 1, int(20 * sc), int(20 * sc))
        if rgn:
            user32.SetWindowRgn(hwnd, rgn, True)
        user32.SetLayeredWindowAttributes(hwnd, 0, 242, 2)  # LWA_ALPHA
        _toast_state[hwnd] = {"text": text, "w": w, "h": h, "font": font}
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        user32.SetTimer(hwnd, 1, int(ms), None)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        api["DeleteObject"](font)
    except Exception:
        if "--console" in sys.argv:
            import traceback
            traceback.print_exc()


# ---------- 可见边框补偿 ----------
def frame_pad(hwnd):
    """窗口矩形比"肉眼可见边框"大出去多少（左, 上, 右, 下）。

    Chrome/微信 这类程序有 7px 左右的不可见拖拽边框，不补偿的话
    设成 2px 的缝隙，视觉上会变成 9px。取不到就返回全 0，退回几何对齐。
    """
    try:
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return 0, 0, 0, 0
        if not dwmapi:
            return 0, 0, 0, 0
        e = RECT()
        dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        hr = dwmapi.DwmGetWindowAttribute(hwnd, 9,  # DWMWA_EXTENDED_FRAME_BOUNDS
                                          ctypes.byref(e), ctypes.sizeof(e))
        if hr != 0 or e.right <= e.left or e.bottom <= e.top:
            return 0, 0, 0, 0
        # 可见边相对窗口矩形的内缩量，正值 = 可见边在窗口矩形里面
        pad = (e.left - r.left, e.top - r.top, r.right - e.right, r.bottom - e.bottom)
        if max(abs(v) for v in pad) > 32:   # 离谱的值不要，退回几何对齐
            return 0, 0, 0, 0
        return pad
    except Exception:
        return 0, 0, 0, 0


# ---------- 可见内容边实测 ----------
# DWM 的 EXTENDED_FRAME_BOUNDS 只知道"系统给的隐形拖拽边框"，不知道应用自己画的
# 透明边/阴影边。实测：Chrome 真实内缩 8px（DWM 只报 7），WorkBuddy 这种 Electron
# 无边框窗口真实内缩约 10px 而 DWM 报 0 —— 两个窗口挨在一起时，肉眼看到的缝
# 就会一个大一个小。
#
# 这里的办法：把「屏幕上真实像素」和「窗口自己渲染出来的像素」逐列比对。
#   PrintWindow 渲染出的是应用自己画的东西，既不含 DWM 叠上去的阴影，
#   也不含透过透明边看到的桌面背景。
#   所以只有窗口真正画了实心内容的位置，两边像素才会对得上。
# 这样量出来的"实心内容边"对任何程序都成立，不用维护一张手写补偿表。

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
_VIS_TOL = 3          # 屏幕像素 vs 自渲染像素的允许色差
_VIS_SCAN = 48        # 每条边最多往里扫多少像素
_VIS_SAMPLES = 21     # 每条边取多少个采样点
_VIS_MAX = 20         # 单边内缩超过这么多就当测错了，退回 DWM 补偿
_SHADOW_MARGIN = 16   # DWM 阴影最多向外溢出这么多像素（Chrome 5、MuMu 12）


class _BIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                ("biHeight", ctypes.c_int), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_int),
                ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint),
                ("biClrImportant", ctypes.c_uint)]


class _BI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BIH), ("bmiColors", wt.DWORD * 3)]


def _gdi_init():
    """补齐 GDI 声明——句柄不声明会被当成 int 传，64 位下直接 OverflowError。"""
    try:
        user32.GetDC.restype = wt.HDC
        user32.GetDC.argtypes = [wt.HWND]
        user32.ReleaseDC.argtypes = [wt.HDC, wt.HWND]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.PrintWindow.restype = wt.BOOL
        user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
        gdi32.CreateCompatibleDC.restype = wt.HDC
        gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
        gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.restype = wt.HGDIOBJ
        gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        gdi32.BitBlt.restype = wt.BOOL
        gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
        gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wt.HDC]
        return True
    except Exception:
        return False


def _dib(w, h):
    bmi = _BI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    return bmi, ctypes.create_string_buffer(w * h * 4)


def _grab_screen():
    """整个虚拟屏幕的像素（BGRA）。拿不到就返回 None。"""
    if not _gdi_init():
        return None
    x0 = user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
    y0 = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        return None
    try:
        hdc = user32.GetDC(None)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x0, y0, SRCCOPY)
        bmi, buf = _dib(w, h)
        ok = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, hdc)
    except Exception:
        return None
    if not ok:
        return None
    return x0, y0, w, h, buf.raw


def _render_self(hwnd):
    """窗口自己渲染出来的像素（应用画的，不含 DWM 阴影、不含桌面背景）。"""
    if not _gdi_init():
        return None
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0 or w > 6000 or h > 6000:
        return None
    try:
        hdc = user32.GetDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        bmi, buf = _dib(w, h)
        ok = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    except Exception:
        return None
    if not ok:
        return None
    return (r.left, r.top, r.right, r.bottom), buf.raw, w, h


def _bgr(buf, w, x, y):
    o = (y * w + x) * 4
    return buf[o], buf[o + 1], buf[o + 2]


def _same(a, b, tol):
    return (abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
            and abs(a[2] - b[2]) <= tol)


def _dimmed(s, o, tol=6):
    """屏幕上的这个像素，是不是"自渲染的颜色被阴影压暗了"的结果。

    邻居窗口外溢过来的阴影会把这一侧压暗，但压暗的仍然是自己画的内容。
    找同一个比例 k 让 s ≈ k*o，k 在 0.15~1.05 之间就算命中。
    自渲染本身是纯黑时不能用这个判据（压暗前后都是黑，分不出是谁的）。
    """
    if o[0] <= 8 and o[1] <= 8 and o[2] <= 8:
        return False
    ks = []
    for i in range(3):
        if o[i] > 8:
            ks.append(s[i] / float(o[i]))
    if not ks:
        return False
    k = sum(ks) / len(ks)
    if k < 0.15 or k > 1.05:
        return False
    for i in range(3):
        if abs(s[i] - o[i] * k) > tol:
            return False
    return True


def _hit(s, o):
    """这个屏幕像素算不算"这个窗口画的内容"。"""
    return _same(s, o, _VIS_TOL) or _dimmed(s, o)


def _ink_info(hwnd):
    """窗口自渲染 + 每一列/行有没有"非黑"像素的掩码。

    PrintWindow 渲染出来的是应用自己画的东西：应用没画的地方（DWM 阴影带、
    系统隐形边框）是纯黑。掩码用来区分"这一列是应用画的"还是"这一列空着"。
    """
    got = _render_self(hwnd)
    if not got:
        return None
    (rl, rt, rr, rb), own, ow, oh = got

    def spread(n):
        out = []
        for i in range(_VIS_SAMPLES):
            t = 0.15 + 0.70 * i / float(max(1, _VIS_SAMPLES - 1))
            v = int(n * t)
            out.append(0 if v < 0 else (n - 1 if v > n - 1 else v))
        return out

    ys = spread(oh)
    xs = spread(ow)

    def black(p):
        return p[0] <= 2 and p[1] <= 2 and p[2] <= 2

    col_nb = []
    for x in range(ow):
        nb = False
        for yy in ys:
            if not black(_bgr(own, ow, x, yy)):
                nb = True
                break
        col_nb.append(nb)
    row_nb = []
    for y in range(oh):
        nb = False
        for xx in xs:
            if not black(_bgr(own, ow, xx, y)):
                nb = True
                break
        row_nb.append(nb)
    return {"rect": (rl, rt, rr, rb), "own": own, "ow": ow, "oh": oh,
            "col_nb": col_nb, "row_nb": row_nb}


def visible_pad(hwnd, screen, others=(), ink=None, floor=None):
    """量出窗口矩形比"肉眼看到的实心内容"大出去多少（左, 上, 右, 下）。

    屏幕像素与窗口自渲染像素对得上的位置，才是这个窗口真正画了东西的地方；
    对不上的地方，要么是透明边透着背景，要么是 DWM 画的阴影。

    others —— 邻居窗口的自渲染信息。落在邻居"画过东西"的区域里的采样点一律
    不用：那些像素被邻居外溢过来的阴影压暗了，拿来比对只会得出假的结论。

    某条边量不出来（窗口超出屏幕、被别的东西盖住）就是 None，
    由调用方拿 frame_pad 的 DWM 值顶上。
    """
    if ink is None:
        ink = _ink_info(hwnd)
    if not ink:
        return None, None, None, None
    if not floor:
        floor = (0, 0, 0, 0)
    fl, ft, fr, fb = [max(0, int(v or 0)) for v in floor]
    (rl, rt, rr, rb), own, ow, oh = ink["rect"], ink["own"], ink["ow"], ink["oh"]
    sx, sy, sw, sh, scr = screen
    rx, ry = rl - sx, rt - sy              # 窗口左上角在屏幕位图里的位置
    if ow < 8 or oh < 8:
        return None, None, None, None

    def spread(n):
        out = []
        for i in range(_VIS_SAMPLES):
            t = 0.15 + 0.70 * i / float(max(1, _VIS_SAMPLES - 1))
            v = int(n * t)
            out.append(0 if v < 0 else (n - 1 if v > n - 1 else v))
        return out

    ys = spread(oh)
    xs = spread(ow)

    def black(p):
        return p[0] <= 2 and p[1] <= 2 and p[2] <= 2

    def scr_black(p):
        return p[0] <= 8 and p[1] <= 8 and p[2] <= 8

    # 自己"没画东西"的列/行。
    #
    # 光看"自渲染是不是纯黑"不够：MuMu 这类模拟器在窄窗口里会给画面留黑边，
    # 那块黑是它自己画的实心黑，屏幕上也是黑；而 DWM 阴影带同样是自渲染黑，
    # 但屏幕上透出来的是桌面/被压暗的邻居。两者必须分开，否则实心黑边会被
    # 当成透明边白白让位，窗口矩形就压到邻居身上、缝隙变 0。
    #
    # 判据：自渲染黑 **且** 屏幕那一列也不是黑 → 才是"没画"（透底）。
    # 自渲染黑 + 屏幕黑 → 实心黑，算内容。
    def dead_col(x):
        if not all(black(_bgr(own, ow, x, yy)) for yy in ys):
            return False
        for yy in ys:
            sxp, syp = rx + x, ry + yy
            if 0 <= sxp < sw and 0 <= syp < sh:
                if scr_black(_bgr(scr, sw, sxp, syp)):
                    return False          # 屏幕也是黑 → 实心黑，是内容
        return True

    def dead_row(y):
        if not all(black(_bgr(own, ow, xx, y)) for xx in xs):
            return False
        for xx in xs:
            sxp, syp = rx + xx, ry + y
            if 0 <= sxp < sw and 0 <= syp < sh:
                if scr_black(_bgr(scr, sw, sxp, syp)):
                    return False
        return True

    col_dead = [dead_col(x) for x in range(ow)]
    row_dead = [dead_row(y) for y in range(oh)]

    def blocked(px, py, axis):
        """这个屏幕点是不是落在邻居"画过东西"的区域里（被它的阴影压暗了）。"""
        for o in others:
            if not o:
                continue
            (ol, ot, orr, ob), oow, ooh = o["rect"], o["ow"], o["oh"]
            if not (ol <= px < orr and ot <= py < ob):
                continue
            if axis == 'x':
                k = px - ol
                if 0 <= k < oow and o["col_nb"][k]:
                    return True
            else:
                k = py - ot
                if 0 <= k < ooh and o["row_nb"][k]:
                    return True
        return False

    def col_content(x):
        """屏幕第 x 列是不是这个窗口画出来的实心内容。"""
        hit = 0
        tot = 0
        for yy in ys:
            sxp, syp = rx + x, ry + yy
            if not (0 <= sxp < sw and 0 <= syp < sh):
                continue
            if blocked(sxp, syp, 'x'):
                continue
            tot += 1
            if _hit(_bgr(scr, sw, sxp, syp), _bgr(own, ow, x, yy)):
                hit += 1
        return tot >= 4 and hit * 2 >= tot

    def row_content(y):
        """屏幕第 y 行是不是这个窗口画出来的实心内容。"""
        hit = 0
        tot = 0
        for xx in xs:
            sxp, syp = rx + xx, ry + y
            if not (0 <= sxp < sw and 0 <= syp < sh):
                continue
            if blocked(sxp, syp, 'y'):
                continue
            tot += 1
            if _hit(_bgr(scr, sw, sxp, syp), _bgr(own, ow, xx, y)):
                hit += 1
        return tot >= 4 and hit * 2 >= tot

    # floor —— DWM 声明的可见边界。DWM 已经说"这几像素不是可见边框"的地方，
    # 屏幕上往往只是它的阴影带（阴影盖在深色壁纸上时和"实心黑"长得一模一样），
    # 所以从这里往里扫，不再把阴影带当内容。
    def scan_left():
        for i in range(fl, min(_VIS_SCAN, ow)):
            if col_dead[i]:
                continue          # 应用没画的地方，一定不是内容
            if col_content(i):
                return i
        return None

    def scan_right():
        stop = max(fr - 1, ow - 1 - _VIS_SCAN)
        for i in range(ow - 1, stop, -1):
            if col_dead[i]:
                continue
            if col_content(i):
                return ow - 1 - i
        return None

    def scan_top():
        for i in range(ft, min(_VIS_SCAN, oh)):
            if row_dead[i]:
                continue
            if row_content(i):
                return i
        return None

    def scan_bottom():
        stop = max(fb - 1, oh - 1 - _VIS_SCAN)
        for i in range(oh - 1, stop, -1):
            if row_dead[i]:
                continue
            if row_content(i):
                return oh - 1 - i
        return None

    out = []
    for val, low in zip((scan_left(), scan_top(), scan_right(), scan_bottom()),
                        (fl, ft, fr, fb)):
        if val is None or val > _VIS_MAX:
            out.append(None)          # 量不出来 / 离谱，交给 DWM 值兜底
        else:
            out.append(max(val, low))  # 不可能比 DWM 声明的可见边界还靠外
    # 每条边独立：某条边量不出来（窗口超出屏幕、被别的东西盖住）就是 None，
    # 由调用方拿 frame_pad 的 DWM 值顶上。
    return tuple(out)


# ---------- 摆位 ----------
def zone_rects(zones, wa, margin=2, gap=2):
    """按百分比算出像素矩形，并留出两种缝：
         margin —— 窗口与屏幕（工作区）边缘的距离
         gap    —— 相邻窗口之间的缝隙宽度（两侧各让一半，缝宽正好 = gap）
       注意：不是每个窗口各缩 gap —— 那样两窗之间会变成 2*gap。
    """
    wx, wy, ww, wh = wa
    L, T, R, B = wx, wy, wx + ww, wy + wh
    g2 = gap // 2          # 内部边界两侧各让这么多 → 合计正好 gap
    out = []
    for z in zones:
        rx, ry, rw, rh = z["rect"]
        l = wx + int(round(ww * rx / 100.0))
        t = wy + int(round(wh * ry / 100.0))
        r = l + int(round(ww * rw / 100.0))
        b = t + int(round(wh * rh / 100.0))
        # 贴着工作区外沿的边用 margin，落在内部的边用 g2；都按原始坐标判定
        l2 = l + (margin if l <= L else g2)
        t2 = t + (margin if t <= T else g2)
        r2 = r - (margin if r >= R else g2)
        b2 = b - (margin if b >= B else g2)
        out.append((l2, t2, max(120, r2 - l2), max(80, b2 - t2)))
    return out


def _split_process(rp):
    """拆 "chrome|msedge|*" -> ("chrome|msedge", True)。

    末尾的 * 表示这个格子接受"任意窗口"：明确写出来的程序一个都没开时，
    用当前桌面上剩下的窗口顶上，不至于空着一个格子。
    正则本身可以含 |（如 Weixin|WeChat），也可以含 .* 这类量词，只把单独的 * 当通配符。
    """
    rp = (rp or "").strip()
    if not rp:
        return "", True
    parts = [x.strip() for x in rp.split("|")]
    any_ok = any(x == "*" for x in parts)
    parts = [x for x in parts if x and x != "*"]
    return "|".join(parts), any_ok


def _pickzone(zones, wins):
    """给每个格子挑窗口，返回与 zones 等长的列表（可能是 None）。

    两轮，顺序很关键：
      第一轮 —— 写明程序的格子先挑（不然"任意窗口"的格子会把指定窗口抢走）；
      第二轮 —— 剩下的格子（process 为空 / 含 * / 指定程序没开）从剩余窗口里补。
    候选窗口按 z 序，也就是"最近用过的优先"，所以主位往往落在当前在用的程序上。
    """
    used = set()
    hits = [None] * len(zones)

    def _find(rp, rt, pool):
        for win in pool:
            if win["hwnd"] in used:
                continue
            if rp and not re.search(rp, win["exe"], re.I):
                continue
            if rt and not re.search(rt, win["title"], re.I):
                continue
            return win
        return None

    # 第一轮：精确匹配
    for i, z in enumerate(zones):
        rx, _any = _split_process(z.get("process"))
        rt = (z.get("title") or "").strip()
        if rt == "*":
            rt = ""
        if not rx:
            continue
        hit = _find(rx, rt, wins)
        if hit:
            hits[i] = hit
            used.add(hit["hwnd"])

    # 第二轮：任意窗口兜底
    for i, z in enumerate(zones):
        if hits[i]:
            continue
        _rx, any_ok = _split_process(z.get("process"))
        if not any_ok:
            continue
        for win in wins:
            if win["hwnd"] in used:
                continue
            hits[i] = win
            used.add(win["hwnd"])
            break

    return hits


def _place(hits, rects, padf):
    """把窗口摆到目标矩形。padf(hwnd) 返回可见边补偿 (左, 上, 右, 下)。

    摆位顺序按目标矩形的横坐标从左到右，每个窗口都提到前面（HWND_TOP），
    于是**左边的窗口沉到最底**。这一条很关键：DWM 画在窗口矩形内侧的阴影带
    （Chrome 有 7px、MuMu 有 3px）会外溢到右邻身上，把那条缝撑宽；把左边的
    窗口压到底层，阴影被右邻盖住，两侧的缝宽才是同一个数。
    """
    order = sorted(range(len(hits)), key=lambda i: (rects[i][0] if i < len(rects) else 0))
    res = [None] * len(hits)
    for i in order:
        hit = hits[i]
        if not hit:
            continue
        hwnd = hit["hwnd"]
        x, y, cw, ch = rects[i]
        # 最大化 / 最小化的窗口必须先还原，否则 SetWindowPos 只改"还原尺寸"，画面不动
        if hit.get("maximized") or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.12)
        # 不用 SWP_NOZORDER + 0(HWND_TOP)：把窗口提到最前。
        # 否则被 WPS/视频全屏之类的窗口盖着，用户看到的就是"点了没反应"。
        pl, pt, pr, pb = padf(hwnd)
        ok = user32.SetWindowPos(hwnd, 0, x - pl, y - pt,
                                 cw + pl + pr, ch + pt + pb, SWP_SHOWWINDOW)
        res[i] = ok
    return res


def _merge_pad(hwnd, vp):
    """实测出来的可见边和 DWM 补偿合起来：实测优先，量不出来的边用 DWM 值顶上。"""
    dp = frame_pad(hwnd)
    if not vp:
        return dp
    return tuple(d if v is None else v for v, d in zip(vp, dp))


def _restack(hits, rects):
    """整理 z 序：按布局层次排 —— 靠左的沉到最底，同一列里靠下的也沉到下面。

    DWM 画在窗口矩形**外侧**的阴影会外溢到邻居身上（Chrome 外扩 5px、
    MuMu 外扩 12px），把那条缝撑宽。让压在上层的那一侧去盖住它：
      · 水平相邻 → 右边那个在上面，左边的沉底，它的阴影就被盖住
      · 垂直相邻 → 上面那个在上面，下面的沉底，它的阴影同样被盖住

    单独再跑一遍、而且用相对定位（放到某个窗口的下方），是因为 Chrome 收到
    移动消息后自己会跳到前面抢焦点，摆位时的顺序会被它打乱。
    """
    # 从底到顶：x 小的先（靠左的沉底），同一列里 y 大的先（靠下的沉底）
    order = sorted(range(len(hits)), key=lambda i: (rects[i][0], -rects[i][1]))
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    top = None
    for i in reversed(order):            # 从顶往底摆
        hit = hits[i]
        if not hit:
            continue
        hwnd = hit["hwnd"]
        # hWndInsertAfter 给一个窗口句柄 = 放到那个窗口的**下方**
        user32.SetWindowPos(hwnd, top if top else 0, 0, 0, 0, 0, flags)
        top = hwnd


def _measure_pads(hits):
    """实测每个窗口"肉眼看到的实心内容边"（左, 上, 右, 下）。

    屏幕上有两种东西会骗过测量：
      1) 邻居窗口外溢过来的阴影（Chrome 外扩 5px、MuMu 外扩 12px）会把邻居的
         边缘压暗，看着像"内容画到那儿去了"；
      2) 窗口自己的 DWM 阴影带（在矩形内侧）本来就不是内容。
    第一种靠"避开邻居画过东西的区域"排除，第二种靠屏幕像素与窗口自渲染像素
    比对来区分 —— 自渲染里不含 DWM 叠上去的阴影。
    """
    pads = {}
    screen = _grab_screen()
    if not screen:
        return pads
    inks = [_ink_info(h["hwnd"]) if h else None for h in hits]
    for i, hit in enumerate(hits):
        if not hit:
            continue
        others = [inks[j] for j in range(len(hits)) if j != i]
        vp = visible_pad(hit["hwnd"], screen, others, ink=inks[i],
                         floor=frame_pad(hit["hwnd"]))
        if vp and any(v is not None for v in vp):
            pads[hit["hwnd"]] = _merge_pad(hit["hwnd"], vp)
    return pads


def apply_profile(prof, verbose=True, dry=False):
    zones = prof.get("zones", [])
    margin = int(prof.get("margin", 2) or 0)
    gap = int(prof.get("gap", 2) or 0)
    wa = pick_work(prof)
    wins = list_windows()
    rects = zone_rects(zones, wa, margin, gap)
    hits = _pickzone(zones, wins)
    done = []

    if dry:      # --dry：只报"谁会去哪个格子"，不动任何窗口
        for idx, (z, hit) in enumerate(zip(zones, hits), 1):
            if not hit:
                done.append((idx, "跳过", f"没找到匹配窗口 process={z.get('process')}", None))
            else:
                x, y, cw, ch = rects[idx - 1]
                done.append((idx, "预演", f'{hit["exe"]}  （{hit["title"][:28]}）', (x, y, cw, ch)))
        if verbose:
            for idx, st, msg, rect in done:
                print(f"  区块{idx} [{st}] {msg}" + (f"  -> {rect[2]}x{rect[3]} @ {rect[0]},{rect[1]}" if rect else ""))
        return done

    # 第一步：先把窗口矩形直接摆到目标位置（不补偿）。
    # 目标矩形之间本来就不重叠，这样量出来的"实心内容边"不会被邻居的阴影带污染。
    oks = _place(hits, rects, lambda hw: (0, 0, 0, 0))
    time.sleep(0.3)          # 等窗口重绘完

    # 第二步：实测每个窗口"肉眼看到的实心内容边"。
    # Chrome 的 8px（隐形边框+阴影）、MuMu 的 3px、WorkBuddy 的 0px 全都会被算进去 ——
    # 任意两个软件之间的可见缝隙都一样，跟用的是哪个程序无关。
    pads = _measure_pads(hits)

    # 第三步：按实测到的可见边再摆一次，把"看得见的实心内容"精确对到目标位置。
    if pads:
        oks = _place(hits, rects, lambda hw: pads.get(hw) or frame_pad(hw))
        log("可见边精修 " + " | ".join(
            "%s%s" % (h["exe"], pads.get(h["hwnd"], ())) for h in hits if h))
    time.sleep(0.2)
    # 最后整理 z 序：左边的窗口沉到最底，阴影被右邻盖住，缝宽才一致
    _restack(hits, rects)
    time.sleep(0.3)
    _restack(hits, rects)   # Chrome 收到移动消息后异步跳到前面，得再压一次

    for idx, (z, hit) in enumerate(zip(zones, hits), 1):
        if not hit:
            done.append((idx, "跳过", f"没找到匹配窗口 process={z.get('process')}", None))
            continue
        x, y, cw, ch = rects[idx - 1]
        ok = oks[idx - 1] if idx - 1 < len(oks) else False
        done.append((idx, "OK" if ok else "失败",
                     f'{hit["exe"]}  （{hit["title"][:28]}）', (x, y, cw, ch)))
    if verbose:
        for idx, st, msg, rect in done:
            print(f"  区块{idx} [{st}] {msg}" + (f"  -> {rect[2]}x{rect[3]} @ {rect[0]},{rect[1]}" if rect else ""))
    return done


# ---------- 捕获当前布局 ----------
def capture():
    wins = list_windows()
    zones = []
    for win in wins:
        wa = work_of_window(win["hwnd"])
        x, y, w_, h_ = win["rect"]
        # 窗口外框（含不可见边）向内收一点，避免百分比漂移
        pct = [round((x - wa[0]) * 100.0 / wa[2], 2),
               round((y - wa[1]) * 100.0 / wa[3], 2),
               round(w_ * 100.0 / wa[2], 2),
               round(h_ * 100.0 / wa[3], 2)]
        zones.append({
            "rect": pct,
            "process": re.escape(win["exe"]) + "|*",
            "title": "",
            "_win": f'{win["exe"]} （{win["title"][:30]}）',
        })
    zones.sort(key=lambda z: (z["rect"][0] > 1, z["rect"][1]))
    return zones


# ---------- 常驻热键 ----------
def _down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


WM_TRAY = 0x8001

# 托盘专用图标（16/20/24/32/48 逐尺寸手绘 + 大尺寸圆角版），内嵌以免依赖 exe 资源索引。
_TRAY_ICO_B64 = (
    "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"
    "UE5HDQoaCgAAAA1JSERSAAABAAAAAQAIBgAAAFxyqGYAAARZSURBVHic7dq9UZtBFEBR4XER1EDkmJCAWlwDQ8hQg2txQEhMRA3u"
    "AoeOzNig/ZHuOQVY+K3e1UrzHQ4AAAAAAAAAAAAAAAAAAAAAAAAAAMBUF4cNXD+8vq3+G/jj+f5q6fvi149vmffD5feXpbNe9uKW"
    "/jTMikFp6XeKwfQXtPinaVQILP7aEHw5TGT5T9eIs7P86+cyLQCW//Qd8wwt/x7zmRIAy38+jnGWln+fOQ0PgOU/P585U8u/17yG"
    "BsDyn6+PnK3l/5iRc5v6IyCwl2EB8Ol//v7njH36f86o+bkBQJgAQNiQALj+d/zLWbv+H8eIOboBQJgAQJgAQJgAQJgAQJgAQJgA"
    "QJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgA"
    "QJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgA"
    "QJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgA"
    "QJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAQJgAMNzl95cLY95zjgIAYQIAYQLAFL4G"
    "7Dk/AYAwAWAat4D95iYATCUCe81LAJhOBPaZkwCwhAjsMR8BYBkRWD+Xr7NeCN57s//68e2tPqHLBU9MCgDbvflLMbhc/Ji0ALCd"
    "1UtR4jcACBMACBMACBMACBMACBMACBMACPMcANu5+XmXeRDo6fbRg0BQWvq//b9XxMANgKWqi//eLGaGwG8ALGP5189FAFjC8u8x"
    "HwFgOsu/z5wEgKks/17zEgCmsfz7zU0AIEwAmMKn/57zEwAIEwAIEwCGc/3fd44CAGECAGECAGECAGECAGECAGECAGECAGECAGEC"
    "AGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGEC"
    "AGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGECAGFDAvB8"
    "f3Ux4t9lP/9y1k+3j94PRzBijm4AECYAEDYsAL4GnL//OWNfAz5n1PzcACBsaADcAs7XR87WLeBjRs5t+A1ABM7PZ85UBPaa15Sv"
    "ACJwPo5xliKwz5ym/QYgAqfvmGcoAnvMZ+qPgCJwukacnQisn8uyJ7SuH17fVr02+0X75udd/v3wtOCJyS0e0RSDvay+qZVi8OQx"
    "aQAAAAAAAAAAAAAAAAAAAAAAAAA4FP0GNqTuk5E3gskAAAAASUVORK5CYII="
)

_TRAY = {"cur": 0, "hwnd": None, "nid": None, "shell32": None, "icon": None,
         "added": False}


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


def _tray_bind():
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wt.BOOL
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                                  ctypes.c_int, wt.UINT]
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
    user32.LoadIconW.restype = wt.HANDLE
    user32.CreatePopupMenu.restype = wt.HMENU
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wt.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [wt.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    user32.PeekMessageW.restype = wt.BOOL
    return shell32


def _tray_tip():
    """悬停提示：带上实际生效的热键组合。"""
    lab = _TRAY.get("hk_label") or ""
    n = len(_TRAY.get("profiles") or []) or 3
    if lab:
        return "Lattice ｜ %s1~%d 切方案，右键更多" % (lab, min(9, n))
    return "Lattice ｜ 右键切方案 / 退出"


def _tray_hicon():
    """优先用内嵌的托盘专用图标，按系统小图标尺寸取；失败再退 exe 资源、系统默认。"""
    if _TRAY.get("icon"):
        return _TRAY["icon"]
    icon = 0
    path = ""
    try:
        import base64
        import tempfile
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        side = user32.GetSystemMetrics(49) or 16          # SM_CXSMICON
        data = base64.b64decode(_TRAY_ICO_B64)
        path = os.path.join(tempfile.gettempdir(), "lattice-tray.ico")
        try:
            same = os.path.getsize(path) == len(data)
        except OSError:
            same = False
        if not same:
            with open(path, "wb") as f:
                f.write(data)
        icon = user32.LoadImageW(None, path, 1, side, side, 0x10)   # IMAGE_ICON | LR_LOADFROMFILE
        if not icon:
            icon = user32.LoadImageW(None, path, 1, 0, 0, 0x10)     # 退回 ico 自带尺寸
    except Exception:
        icon = 0
    if not icon:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            icon = user32.LoadImageW(hinst, ctypes.cast(1, wt.LPCWSTR), 1, 0, 0, 0x40)
        except Exception:
            icon = 0
    if not icon:
        icon = user32.LoadIconW(None, ctypes.cast(32512, wt.LPCWSTR))
    _TRAY["icon"] = icon
    log("托盘图标加载完成：句柄=%s 路径=%s" % (bool(icon), path or "exe资源"))
    return icon


def _tray_icon_add(hwnd):
    """NIM_ADD / NIM_MODIFY —— explorer 重启后要重新加一次。"""
    shell32 = _TRAY["shell32"]
    nid = _TRAY["nid"]
    if nid is None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4          # NIF_MESSAGE|NIF_ICON|NIF_TIP
        nid.uCallbackMessage = WM_TRAY
    icon = _tray_hicon()
    nid.hIcon = icon
    nid.szTip = _tray_tip()[:127]
    _TRAY["nid"] = nid
    # 第一次 NIM_ADD；之后（explorer 重启、热键变更刷提示）必须用 NIM_MODIFY，
    # 否则重复 ADD 会返回 False 而且图标不会更新。
    op = 1 if _TRAY.get("added") else 0        # 1=NIM_MODIFY, 0=NIM_ADD
    ok = shell32.Shell_NotifyIconW(op, ctypes.byref(nid))
    if ok:
        _TRAY["added"] = True
    log("通知图标 op=%s 返回=%s 图标句柄=%s 提示=%s"
        % ("MODIFY" if op else "ADD", bool(ok), bool(icon), nid.szTip))


def _tray_menu(hwnd, profiles):
    m = user32.CreatePopupMenu()
    for i, p in enumerate(profiles[:9], 1):
        flags = 0x00000800 if (i - 1) == _TRAY["cur"] else 0   # MF_CHECKED
        user32.AppendMenuW(m, flags, i, "方案 %d  %s" % (i, p.get("name", "")))
    user32.AppendMenuW(m, 0x00000800, 0, None)                 # MF_SEPARATOR
    user32.AppendMenuW(m, 0, 100, "打开配置文件夹")
    user32.AppendMenuW(m, 0, 101, "退出 Lattice")
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)      # 不设前台，菜单会点不掉
    cmd = user32.TrackPopupMenu(m, 0x0002 | 0x0100, pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, 0, 0, 0)
    user32.DestroyMenu(m)
    return cmd


def _tray_quit(hwnd):
    try:
        if _TRAY["nid"] is not None:
            _TRAY["shell32"].Shell_NotifyIconW(2, ctypes.byref(_TRAY["nid"]))  # NIM_DELETE
    except Exception:
        pass
    user32.DestroyWindow(hwnd)


def _register_hotkeys(hwnd, profiles):
    """挑一组能独占注册的修饰键，返回 (说明, 是否成功)。主键盘和小键盘都试。"""
    n = min(9, len(profiles))
    user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
    user32.RegisterHotKey.restype = wt.BOOL
    user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
    for label, mod in HOTKEY_CANDIDATES:
        got = []
        ok = True
        for i in range(1, n + 1):
            if not user32.RegisterHotKey(hwnd, i, mod, 0x30 + i):   # 主键盘 1..9
                ok = False
                break
            got.append(i)
        if not ok:
            for i in got:
                user32.UnregisterHotKey(hwnd, i)
            log("热键组合 %s1..%d 被占用，换下一组" % (label, n))
            continue
        for i in range(1, n + 1):        # 小键盘也注册，失败无所谓
            user32.RegisterHotKey(hwnd, 10 + i, mod, 0x60 + i)
        # 同一组合 + Q = 退出常驻（原来只有轮询模式才有退出键，独占模式只能靠托盘）
        user32.RegisterHotKey(hwnd, HK_QUIT, mod, 0x51)
        return label, True
    return "", False


def _unregister_hotkeys(hwnd, n=9):
    for i in list(range(1, n + 1)) + [10 + k for k in range(1, n + 1)] + [HK_QUIT]:
        try:
            user32.UnregisterHotKey(hwnd, i)
        except Exception:
            pass


def _hotkey_fire(idx, profiles):
    if not (1 <= idx <= len(profiles)):
        return
    prof = profiles[idx - 1]
    _TRAY["cur"] = idx - 1
    log("热键命中 %s%d -> %s" % (_TRAY.get("hk_label", ""), idx, prof.get("name", "")))
    apply_profile(prof)
    toast("方案 %d　%s" % (idx, prof.get("name", "")), 1100)


def hotkey_loop(profiles, cfg=None):
    """常驻：托盘图标 + 独占热键。

    热键走 RegisterHotKey（系统级独占，注册成功后别的程序收不到同一组合，
    不会再出现"按 Ctrl+Alt+1 顺手把浏览器最大化"这种事）。
    候选组合按 HOTKEY_CANDIDATES 顺序试，全被占用才退回轮询。
    """
    global _tray_proc
    _TRAY["profiles"] = profiles
    log("常驻开始（托盘 + 独占热键），方案数=%d" % len(profiles))
    cls = "LatticeTrayV1"
    hwnd = None
    use_poll = True

    try:
        shell32 = _tray_bind()
        _TRAY["shell32"] = shell32
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        def _proc(h, msg, wp, lp):
            if msg == WM_HOTKEY:
                if wp == HK_QUIT:
                    log("收到退出热键，常驻结束")
                    toast("Lattice 已退出常驻", 1200)
                    _tray_quit(h)
                    return 0
                _hotkey_fire(wp - 10 if wp >= 10 else wp, profiles)
                return 0
            if msg == WM_TRAY:
                ev = lp & 0xFFFF
                if ev in (0x0205, 0x0204):          # 右键
                    cmd = _tray_menu(h, profiles)
                    if 1 <= cmd <= 9:
                        _hotkey_fire(cmd, profiles)
                    elif cmd == 100:
                        try:
                            os.startfile(app_dir())
                        except Exception:
                            pass
                    elif cmd == 101:
                        log("托盘退出")
                        _tray_quit(h)
                    return 0
                if ev == 0x0202:                    # 左键 = 方案1
                    _hotkey_fire(1, profiles)
                    return 0
                return 0
            if msg == taskbar_created:
                _tray_icon_add(h)                   # explorer 重启后补图标
                return 0
            if msg == 0x0002:                       # WM_DESTROY
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(h, msg, wp, lp)

        _tray_proc = WNDPROC(_proc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(_tray_proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = cls
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0x00000080, cls, "Lattice", 0x80000000,
                                      0, 0, 0, 0, None, None, hinst, None)
        if hwnd:
            _tray_icon_add(hwnd)
            _TRAY["hwnd"] = hwnd
            log("托盘图标已注册")
    except Exception:
        import traceback
        log("托盘创建失败：" + traceback.format_exc()[-400:])
        hwnd = None

    # 独占热键注册
    if hwnd:
        label, ok = _register_hotkeys(hwnd, profiles)
        if ok:
            use_poll = False
            _TRAY["hk_label"] = label
            log("热键已独占注册：%s1..%d" % (label, min(9, len(profiles))))
            _tray_icon_add(hwnd)          # NIM_MODIFY：把实际键位写进悬停提示
            toast("热键：%s1/2/3（已独占，不再和系统冲突）" % label, 2400)
        else:
            log("候选组合全部被占用，退回轮询模式（可能重复触发系统热键）")
            toast("热键组合全被占用，退回轮询：可能和系统热键重复触发", 2600)
    else:
        toast("托盘没建起来，热键用轮询模式", 2000)

    latched = set()
    beat = time.time()
    msg = wt.MSG()
    while True:
        try:
            if hwnd:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:      # WM_QUIT
                        log("收到 WM_QUIT，常驻结束")
                        _unregister_hotkeys(hwnd)
                        return True
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

            if use_poll:      # 只有独占注册失败时，才用轮询兜底
                if _down(VK_CONTROL) and _down(VK_MENU):
                    hit = None
                    for i in range(1, min(9, len(profiles)) + 1):
                        if _down(0x30 + i) or _down(0x60 + i):
                            hit = i
                            break
                    if hit is not None:
                        if hit not in latched:
                            latched.add(hit)
                            _hotkey_fire(hit, profiles)
                    elif _down(VK_Q):
                        if "q" not in latched:
                            latched.add("q")
                            log("收到退出热键，常驻结束")
                            _unregister_hotkeys(hwnd)
                            if hwnd:
                                _tray_quit(hwnd)
                            return True
                    else:
                        latched.clear()
                else:
                    latched.clear()

            if time.time() - beat > 300:
                beat = time.time()
                log("心跳：常驻存活")
            time.sleep(0.04)
        except Exception:
            import traceback
            log("常驻循环异常：" + traceback.format_exc()[-500:])
            time.sleep(1)


# ---------- 日志 / 拉起常驻 ----------
def log(msg):
    """写一行到 exe 同目录 lattice.log。--noconsole 下没控制台，出问题只能靠它。"""
    try:
        p = os.path.join(app_dir(), "lattice.log")
        if os.path.exists(p) and os.path.getsize(p) > 200 * 1024:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                keep = f.read()[-40000:]
            with open(p, "w", encoding="utf-8") as f:
                f.write("...(日志已截断)...\n" + keep)
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------- 配置 ----------
DEFAULT_CONFIG = {
    "_说明": "rect = [左边%, 上边%, 宽%, 高%]，相对工作区（自动避开任务栏/顶栏）。process 是 exe 名正则。margin=窗口与屏幕边距离(px)，gap=窗口之间缝隙(px)。",
    "profiles": [
        {
            "name": "工作三分屏（大左 + 右上 + 右下）",
            "hotkey": "1",
            "monitor": "primary",
            "margin": 2,
            "gap": 2,
            "zones": [
                {"rect": [0, 0, 62.3, 100], "process": r"chrome|msedge|firefox|brave|360se|QQBrowser"},
                {"rect": [62.3, 0, 37.7, 63], "process": r"WorkBuddy"},
                {"rect": [62.3, 63, 37.7, 37], "process": r"Weixin|WeChat"},
            ],
        },
        {
            "name": "左右对半",
            "hotkey": "2",
            "monitor": "primary",
            "margin": 2,
            "gap": 2,
            "zones": [
                {"rect": [0, 0, 50, 100], "process": r"chrome|msedge|firefox|brave"},
                {"rect": [50, 0, 50, 100], "process": r"WorkBuddy|Weixin|WeChat|Code|explorer"},
            ],
        },
        {
            "name": "三列等宽",
            "hotkey": "3",
            "monitor": "primary",
            "margin": 2,
            "gap": 2,
            "zones": [
                {"rect": [0, 0, 33.34, 100], "process": r"chrome|msedge|firefox|brave"},
                {"rect": [33.34, 0, 33.33, 100], "process": r"WorkBuddy|Code|notepad"},
                {"rect": [66.67, 0, 33.33, 100], "process": r"Weixin|WeChat"},
            ],
        },
    ],
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), "lattice.json")


def load_config():
    p = config_path()
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return DEFAULT_CONFIG
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- 入口 ----------
def msgbox(text, title="Lattice", flags=0x40):
    try:
        user32.MessageBoxW(None, text, title, flags)
    except Exception:
        print(text)


class _Null:
    def write(self, *a):
        pass

    def flush(self):
        pass


def _hide_own_console():
    """双击启动时，把自己独占的控制台窗口藏起来。

    这个 exe 是控制台子系统程序（没加 --windowed），双击时 Windows 会新建一个
    控制台。启动完它转成常驻、一直不退出，那个黑窗口就赖在屏幕上不走。

    只有在"这个控制台是本次启动新建的"时候才藏：
    GetConsoleProcessList 只数到 1 个进程（就是自己）= 控制台是双击新建的；
    从 cmd 里跑会是 2 个（cmd + 自己），那就别动，--list / --capture 的输出还得看。
    """
    try:
        kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD]
        kernel32.GetConsoleProcessList.restype = wt.DWORD
        kernel32.GetConsoleWindow.restype = wt.HWND
        kernel32.GetConsoleWindow.argtypes = []
        buf = (wt.DWORD * 8)()
        n = kernel32.GetConsoleProcessList(buf, 8)
        if n != 1:
            return
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def main():
    if sys.stdout is None:   # --noconsole 打包后 stdout 为 None，print 会炸
        # 若从 cmd/bat 里启动，尝试挂到父控制台，好让 --list / --capture 的输出看得见
        attached = False
        try:
            if kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                f = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                sys.stdout = sys.stderr = f
                attached = True
        except Exception:
            attached = False
        if not attached:
            sys.stdout = _Null()
            sys.stderr = _Null()
    else:                    # 控制台可能是 GBK，遇到特殊字符别崩
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(errors="replace")
            except Exception:
                pass
    set_dpi_aware()
    argv = sys.argv[1:]
    if "--console" not in argv:
        # 双击拉起时藏掉自己新建的控制台（从 cmd 里跑不受影响，--list 照样能看）
        _hide_own_console()
    cfg = load_config()
    profiles = cfg.get("profiles", [])
    if not profiles:
        msgbox("lattice.json 里没有任何方案。", "Lattice", 0x30)
        return 1

    if "--list" in argv:
        print("== 可见窗口 ==")
        for w in list_windows():
            print(f'  {w["exe"]:<22} {w["rect"][2]}x{w["rect"][3]} @ {w["rect"][0]},{w["rect"][1]}  （{w["title"][:30]}）')
        print("\n== 方案 ==")
        for i, p in enumerate(profiles, 1):
            print(f"  {i}. {p.get('name', '')}  ({len(p.get('zones', []))} 区块, gap={p.get('gap', 0)})")
            for j, z in enumerate(p.get("zones", []), 1):
                rx, any_ok = _split_process(z.get("process"))
                desc = rx or "任意窗口"
                if rx and any_ok:
                    desc += "  (+ 任意窗口兜底)"
                elif not rx:
                    desc += "  (不挑程序，按最近使用顺序)"
                print(f"       区块{j}: {desc}")
        return 0

    if "--capture" in argv:
        zones = capture()
        for z in zones:
            print(f'  {z["rect"]}  {z["_win"]}')
        if "--save" in argv:
            newp = {
                "name": time.strftime("捕获-%m%d-%H%M"),
                "hotkey": str(len(profiles) + 1),
                "monitor": "primary",
                "margin": 2,
            "gap": 2,
                "zones": [{"rect": z["rect"], "process": z["process"], "title": ""} for z in zones],
            }
            cfg["profiles"].append(newp)
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"\n已存为方案 {len(cfg['profiles'])}：{newp['name']}")
        return 0

    # 选方案
    idx = 0
    for i, a in enumerate(argv):
        if a in ("-p", "--profile") and i + 1 < len(argv):
            try:
                idx = int(argv[i + 1]) - 1
            except ValueError:
                pass
    idx = max(0, min(idx, len(profiles) - 1))
    prof = profiles[idx]

    # 单实例判定：已在常驻 → 本次只排一次
    kernel32.CreateMutexW.restype = wt.HANDLE
    ctypes.set_last_error(0)
    kernel32.CreateMutexW(None, False, "Local\\Lattice.Resident.v1")
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    if "--quiet" in argv:
        # 被主实例拉起来的常驻进程：不重复摆位、不弹提示条，静默待命
        log("常驻进程启动(quiet)，已有实例=%s" % already)
        hotkey_loop(profiles, cfg)
        return 0

    print(f"方案 {idx + 1}：{prof.get('name', '')}")
    dry = "--dry" in argv or "--dry-run" in argv
    done = apply_profile(prof, dry=dry)
    if dry:
        print("（预演，未移动任何窗口。去掉 --dry 才会真的摆）")
        return 0
    okc = sum(1 for d in done if d[1] == "OK")
    miss = [d for d in done if d[1] == "跳过"]
    fail = [d for d in done if d[1] == "失败"]
    log("应用方案 %d「%s」：成功 %d/%d" % (idx + 1, prof.get("name", ""), okc, len(done)))

    if okc == 0:
        # 一个都没匹配上 —— 必须说话，否则用户看到的就是"点了没反应"
        msgbox(
            f"方案「{prof.get('name', '')}」一个窗口都没匹配到。\n\n"
            + "\n".join(f"区块{d[0]}：{d[2]}" for d in done)
            + "\n\n命令行跑 Lattice.exe --list 可看当前窗口真实 exe 名，"
              "再改 lattice.json 的 process。",
            "Lattice：没找到目标窗口", 0x30)
    elif miss or fail:
        names = "、".join(f"区块{d[0]}" for d in miss + fail)
        toast(f"排好 {okc}/{len(done)}，{names} 没找到窗口", 1800)
    else:
        toast(f"已排好 {okc}/{len(done)} 个窗口　{prof.get('name', '')}", 1400)

    if "--once" in argv:
        return 0

    if already:
        # 已经有常驻实例在跑：本次只负责把窗口排好，排完就退，不再起第二个进程。
        # 原来这里会 spawn 一个子进程，那是 "Failed to remove temporary directory" 的来源：
        # 子进程从本进程的 _MEI 解压目录里启动，本进程退出时删不掉自己被占用的临时目录。
        return 0

    if "--daemon" in argv or "--console" in argv or cfg.get("auto_hotkeys", True):
        log("本进程直接转入常驻（托盘 + 热键），exe=%s" % sys.executable)
        toast("已排好 %d/%d 个窗口　热键：Ctrl+Alt+Shift+1/2/3" % (okc, len(done)), 2000)
        hotkey_loop(profiles, cfg)
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        msgbox(tb[-800:], "Lattice：出错", 0x10)
        sys.exit(1)
