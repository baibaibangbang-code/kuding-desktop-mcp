"""PATCH 0.5.0: Window —— 窗口状态管理（测试机器 9-14 提的第 1 条 + 第 10 条，合成一个工具）。

为什么单独做这个：
- 现有的 `App(mode="switch")` 按应用名找不到窗口（9-14 实测 "Application ... not found"），
  而定时任务里天天要把窗口从最小化/托盘恢复出来再操作。
- `ShowWindow(SW_RESTORE)` 是「恢复到最小化之前的状态」——之前是最大化就还回最大化。
  要强制常规尺寸只能 `SetWindowPlacement` 把 showCmd 改成 SW_SHOWNORMAL（pywin32 返回的是不可写元组，得用 ctypes 结构体）。
- 把前台锁那套（敲一下 ALT + AttachThreadInput + BringWindowToTop + SetForegroundWindow）收进来，
  省得每台机器的外部脚本各抄一遍。
- 关键顺序依赖：`process:` 范围搜索**看不见最小化窗口**（元素全在屏幕外会被过滤），
  所以这个工具必须能脱离选择器独立定位窗口——只用 Win32 枚举，不依赖 UIA。
"""

import ctypes
from ctypes import wintypes
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED, SW_MINIMIZE, SW_RESTORE = 1, 2, 3, 6, 9
SW_SHOW = 5
SWP_NOZORDER, SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOMOVE = 0x0004, 0x0010, 0x0001, 0x0002
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                ("ptMinPosition", POINT), ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def _dpi_aware():
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def session_locked():
    """桌面是不是被锁屏挡住了。两条判据缺一不可，9-14 本机实测踩出来的：

    1) 输入桌面名不是 Default —— 输密码那一刻会切到安全桌面 Winlogon。
    2) 前台窗口属于 LockApp.exe / LogonUI.exe —— Win11 的锁屏界面平时**就跑在普通桌面上**，
       这时输入桌面仍然叫 Default，只看第 1 条会判成「没锁」（我第一版就是这么错的）。

    为什么值得单独报：锁屏时任何程序都抢不到前台，点击落空、截图拍到的是锁屏，
    而各个 API 只各自返回一个含糊的失败——SetForegroundWindow 返回 false、
    GetForegroundWindow 返回 0、截图一片白。无人值守的定时任务撞上这个就是静默失败。
    """
    try:
        h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
        if not h:
            return True
        try:
            b = ctypes.create_unicode_buffer(256)
            n = ctypes.c_int(0)
            if user32.GetUserObjectInformationW(h, 2, b, 256, ctypes.byref(n)):  # UOI_NAME
                if (b.value or "").lower() != "default":
                    return True
        finally:
            user32.CloseDesktop(h)
    except Exception:
        pass
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            return True
        return _proc_name(_pid(fg)).lower() in ("lockapp.exe", "logonui.exe")
    except Exception:
        return False


def _locked_note():
    return (" !! THE SESSION IS LOCKED (lock screen is in front): nothing can take the foreground, clicks go nowhere "
            "and screenshots capture the lock screen. Unattended jobs must run on an unlocked, logged-in desktop - "
            "unlock the machine (or stop it from locking) before retrying."
            if session_locked() else "")


def _text(h):
    b = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wintypes.HWND(h), b, 512)
    return b.value


def _cls(h):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(h), b, 256)
    return b.value


def _pid(h):
    p = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(h), ctypes.byref(p))
    return int(p.value)


def _proc_name(pid):
    h = kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        b = ctypes.create_unicode_buffer(512)
        n = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(n)):
            return b.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def _rect(h, strict=False):
    """窗口矩形。strict=True 时取不到就回 None，而不是一个假的 (0,0,0,0)。

    9-16 审代码时发现这个函数原来**根本不看 GetWindowRect 的返回值**：窗口已经关了
    也照样回一个全零矩形，看上去像"有个零尺寸的窗贴在屏幕左上角"。
    frame_guard 里另有一份查了返回值的同名函数——同一件事两种语义，迟早对不上。
    合成这一份：老调用方（12 处）行为不变，要分辨"取不到"的传 strict=True。
    """
    r = RECT()
    if not user32.GetWindowRect(wintypes.HWND(h), ctypes.byref(r)):
        return None if strict else (0, 0, 0, 0)
    return (r.left, r.top, r.right, r.bottom)


def _state(h):
    if user32.IsIconic(wintypes.HWND(h)):
        return "minimized"
    if user32.IsZoomed(wintypes.HWND(h)):
        return "maximized"
    return "normal"


def _enum(include_invisible=False):
    out = []

    def cb(h, _l):
        if not include_invisible and not user32.IsWindowVisible(wintypes.HWND(h)):
            return True
        t = _text(h)
        if not t:
            return True
        out.append(h)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def _describe(h):
    pid = _pid(h)
    l, t, r, b = _rect(h)
    return {"hwnd": int(h), "title": _text(h), "class": _cls(h), "pid": pid,
            "process": _proc_name(pid), "state": _state(h),
            "rect": (l, t, r, b), "size": (r - l, b - t)}


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002
DWMWA_CLOAKED = 14

_GWL = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_GWL.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
_GWL.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetLayeredWindowAttributes.restype = ctypes.c_bool
user32.GetLayeredWindowAttributes.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(ctypes.c_ubyte),
                                              ctypes.POINTER(wintypes.DWORD)]


def _cloaked(h):
    """DWM 说这个窗口「被遮蔽」——它在 EnumWindows 里可见，屏幕上却根本不存在。

    UWP/WinUI 应用退到后台或关掉以后，ApplicationFrameHost 会留下这种壳，标题还在。
    只看 IsWindowVisible 判不出来，这是窗口列表里噪音的一大来源。
    """
    try:
        v = ctypes.c_int(0)
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(h), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0:
            return v.value != 0
    except Exception:
        pass
    return False


def _helper_reason(h, w, ht):
    """这个窗口是不是「不是给人点的」。返回一个原因词，或 None 表示它是正经窗口。

    为什么要过滤：opencode 9-15 拿到 62 行，大半是 Notepad 的 PopupWindow 壳、
    辅助窗和透明宿主，真正的目标淹在里面——它每挑错一次就是点错一次窗口。

    为什么被过滤掉的也必须报出来：9-14 那次误判（开着三个微信只看到一个，
    以为窗口没了）的教训是**沉默的过滤比噪音更危险**。所以数量和原因一律回给
    调用方，并明说 all=true 能看到全部。
    """
    if _cloaked(h):
        return "cloaked"
    ex = int(_GWL(wintypes.HWND(h), GWL_EXSTYLE))
    if ex & WS_EX_TOOLWINDOW:
        return "tool window"
    if ex & WS_EX_LAYERED:
        a, key, fl = ctypes.c_ubyte(0), wintypes.DWORD(0), wintypes.DWORD(0)
        try:
            if user32.GetLayeredWindowAttributes(wintypes.HWND(h), ctypes.byref(key),
                                                 ctypes.byref(a), ctypes.byref(fl)):
                if (fl.value & LWA_ALPHA) and a.value == 0:
                    return "fully transparent"
        except Exception:
            pass
    if user32.IsIconic(wintypes.HWND(h)):
        # 最小化的窗口 rect 一律是 (-32000,-32000) 237x39——按尺寸判它是"太小"，
        # 就会把**这个工具存在的首要理由**（把窗口从最小化恢复出来再操作）过滤掉。
        # 9-15 第一版就这么干了，ChatGPT 和网易UU远程都被默默藏了。
        return None
    if w < 200 or ht < 120:
        return "too small"
    return None


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def capture_window(hwnd):
    """把**指定窗口自己的内容**画出来，被别的窗口盖住也照样拿得到。返回 (PIL.Image, (l,t,r,b))。

    抓屏是抓「屏幕这块区域现在长什么样」，窗口被盖住就只能拍到盖在上面的东西。
    `PrintWindow` 是让**窗口自己重绘一遍到我们给的 DC**，跟 z 序无关。
    `PW_RENDERFULLCONTENT`(=2) 那个标志是关键，少了它现代的分层/合成窗口会是黑的。

    9-14 本机对照实测：把钉钉放在 (60,60) 再用别的窗口盖住——
    抓屏裁剪拍到的是盖在上面那个窗口，PrintWindow 拿到了钉钉的完整界面，文字可读。
    钉钉是 Qt 自绘，正好是我们最难啃的那一类。

    意义不只是"能读"：多开的应用（四个微信）可以**各读各的，谁也不用挪谁**，
    只读操作从此不需要抢前台——而抢前台本身就是一大风险源。

    注意：能读**不等于**能点。坐标还是屏幕坐标，窗口被盖住时点下去会打到上面那个窗口，
    所以调用方拿到坐标后仍要用 _root_at() 验一下归属。
    """
    from PIL import Image
    _dpi_aware()
    l, t, r, b = _rect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None, (l, t, r, b)
    hdc = u32_GetWindowDC(wintypes.HWND(hwnd))
    if not hdc:
        return None, (l, t, r, b)
    mdc = g32_CreateCompatibleDC(hdc)
    bmp = g32_CreateCompatibleBitmap(hdc, w, h)
    g32_SelectObject(mdc, bmp)
    ok = u32_PrintWindow(wintypes.HWND(hwnd), mdc, 2)  # PW_RENDERFULLCONTENT
    buf = ctypes.create_string_buffer(w * h * 4)
    bi = _BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h          # 负高度 = 自上而下，省一次翻转
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    g32_GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    g32_DeleteObject(bmp)
    g32_DeleteDC(mdc)
    u32_ReleaseDC(wintypes.HWND(hwnd), hdc)
    if not ok:
        return None, (l, t, r, b)
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1), (l, t, r, b)


# 显式声明签名：comtypes 会改 ctypes 的默认值，句柄被按 c_int 截断过（9-14 的 DPI 探测就栽在这）
u32_GetWindowDC = user32.GetWindowDC
u32_GetWindowDC.restype, u32_GetWindowDC.argtypes = wintypes.HDC, [wintypes.HWND]
u32_ReleaseDC = user32.ReleaseDC
u32_ReleaseDC.restype, u32_ReleaseDC.argtypes = ctypes.c_int, [wintypes.HWND, wintypes.HDC]
u32_PrintWindow = user32.PrintWindow
u32_PrintWindow.restype, u32_PrintWindow.argtypes = wintypes.BOOL, [wintypes.HWND, wintypes.HDC, wintypes.UINT]
g32 = ctypes.windll.gdi32
g32_CreateCompatibleDC = g32.CreateCompatibleDC
g32_CreateCompatibleDC.restype, g32_CreateCompatibleDC.argtypes = wintypes.HDC, [wintypes.HDC]
g32_CreateCompatibleBitmap = g32.CreateCompatibleBitmap
g32_CreateCompatibleBitmap.restype, g32_CreateCompatibleBitmap.argtypes = wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]
g32_SelectObject = g32.SelectObject
g32_SelectObject.restype, g32_SelectObject.argtypes = wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]
g32_GetDIBits = g32.GetDIBits
g32_GetDIBits.restype = ctypes.c_int
g32_GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
g32_DeleteObject = g32.DeleteObject
g32_DeleteObject.restype, g32_DeleteObject.argtypes = wintypes.BOOL, [wintypes.HGDIOBJ]
g32_DeleteDC = g32.DeleteDC
g32_DeleteDC.restype, g32_DeleteDC.argtypes = wintypes.BOOL, [wintypes.HDC]


def resolve_target(title=None, process=None, hwnd=None, nth=None):
    """给 OCR 那边用：把 window/process/hwnd 解析成一个句柄，顺带回一句说明。

    `nth=None` 才是「没指定」；`nth=0` 是明明白白的「要第一个」。
    9-15 之前两者都是 0，于是调用方**根本没法表达「我知道有多个，就要第一个」**——
    传 nth=0 和不传一模一样，照样被拒。opencode 实跑时撞上了，只能改传 nth=1 绕过去。
    """
    if hwnd:
        return int(hwnd), None
    idx = 0 if nth is None else int(nth)
    cands = _pick(title, process, None, idx)
    if not cands:
        return None, ("no window matched (title=%r process=%r). Run Window(mode='list') to see what is open."
                      % (title, process))
    if len(cands) > 1 and nth is None:
        names = "; ".join("hwnd=%d '%s'" % (h, _text(h)[:28]) for h in cands[:5])
        return None, ("%d windows matched, refusing to guess which one to read: %s. Pass hwnd:, or "
                      "nth:0 if you really do want the first one."
                      % (len(cands), names))
    if idx >= len(cands):
        return None, "matched %d window(s) but nth:%d is out of range." % (len(cands), idx)
    return cands[idx], None


def _root_at(x, y):
    """屏幕坐标底下**实际**是哪个顶层窗口。

    测试机器 9-14 的险情催生的：那台机器上四个微信窗口叠在同一位置，右键"朋友圈窗"的坐标
    实际落在了下面主窗的聊天列表上，弹出的菜单里带「删除」——删的是整个聊天记录，不可逆。
    它认出来没点，但工具当时一声不吭。按坐标点自绘窗口时，先问一句这个点归谁。
    """
    pt = ctypes.c_longlong((int(y) << 32) | (int(x) & 0xFFFFFFFF))
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [ctypes.c_longlong]
    h = user32.WindowFromPoint(pt)
    if not h:
        return None
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    root = user32.GetAncestor(wintypes.HWND(h), 2)  # GA_ROOT
    return int(root or h)


def _pick(title, process, hwnd, nth, include_invisible=False):
    """只用 Win32 枚举定位，故意不依赖 UIA —— 最小化的窗口 UIA 侧几乎读不到。"""
    if hwnd:
        return [int(hwnd)]
    cands = []
    for h in _enum(include_invisible=include_invisible):
        if title and title.lower() not in _text(h).lower():
            continue
        if process and process.lower() not in _proc_name(_pid(h)).lower():
            continue
        cands.append(h)
    return cands


def _proc_name_running(name):
    """这个进程名还有活着的实例吗。True / False / None（查不出来）。

    用来分清"窗口关了"和"程序退了"——对微信、QQ 这类关闭即最小化到托盘的软件，
    这两件事天差地别。

    9-16 踩到的坑，值得写下来：原来这里走的是 `tasklist`，
    而 tasklist 按控制台代码页（中文机器上是 936）输出，
    `subprocess.run(text=True)` 在 PYTHONUTF8 下按 UTF-8 解码会抛 UnicodeDecodeError。
    关键是 **run() 不会把这个异常报给你**，它只是把 stdout 变成 None，
    于是 `name in (None or "")` 恒为 False——"查不到"被讲成了"进程没了"，
    工具因此当着用户的面宣布把他的微信关掉了，而微信好好地开着。

    所以改成 Toolhelp32 快照：不起子进程、没有代码页、没有解码。
    """
    import ctypes as _c
    from ctypes import wintypes as _w

    class _PE32(_c.Structure):
        _fields_ = [("dwSize", _w.DWORD), ("cntUsage", _w.DWORD),
                    ("th32ProcessID", _w.DWORD), ("th32DefaultHeapID", _c.POINTER(_c.c_ulong)),
                    ("th32ModuleID", _w.DWORD), ("cntThreads", _w.DWORD),
                    ("th32ParentProcessID", _w.DWORD), ("pcPriClassBase", _c.c_long),
                    ("dwFlags", _w.DWORD), ("szExeFile", _c.c_wchar * 260)]

    k = _c.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = _w.HANDLE
    k.CloseHandle.argtypes = [_w.HANDLE]
    want = (name or "").lower()
    if not want:
        return None
    snap = None
    try:
        snap = k.CreateToolhelp32Snapshot(0x00000002, 0)     # TH32CS_SNAPPROCESS
        if not snap or snap == _w.HANDLE(-1).value:
            return None
        e = _PE32()
        e.dwSize = _c.sizeof(_PE32)
        if not k.Process32FirstW(_w.HANDLE(snap), _c.byref(e)):
            return None
        while True:
            if (e.szExeFile or "").lower() == want:
                return True
            if not k.Process32NextW(_w.HANDLE(snap), _c.byref(e)):
                break
        return False
    except Exception:
        return None                 # 查不出来就说查不出来，别替它猜
    finally:
        if snap:
            try:
                k.CloseHandle(_w.HANDLE(snap))
            except Exception:
                pass


def _scan_tray_buttons(uia, want):
    """在托盘和溢出面板的 UIA 树里找名字完全等于 want 的按钮。"""
    out = []
    for cls in ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow",
                "Shell_TrayWnd"):
        h = _find_class(cls)
        if not h:
            continue
        try:
            root = uia.ControlFromHandle(int(h))
        except Exception:
            continue
        stack = [(root, 0)]
        while stack:
            node, d = stack.pop()
            if d > 6:
                continue
            try:
                kids = node.GetChildren()
            except Exception:
                continue
            for c in kids:
                try:
                    if c.ControlTypeName in ("ButtonControl", "MenuItemControl")                             and (c.Name or "").strip() == want:
                        out.append((c, cls))
                except Exception:
                    pass
                stack.append((c, d + 1))
    return out


def _tray_inventory(uia):
    """托盘里现在都有什么按钮。找不到名字时连这个一起说。

    测试机器 9-16 实测：四个微信号的托盘图标里，**有一个的 Name 是空字符串**，
    按名字过滤永远漏掉它，而我们当时只回一句"没有这个名字的按钮"——
    它无从知道那个号其实就在托盘里。名字为空不是稀奇事，得说出来。
    """
    named, blank = {}, 0
    # 只数**溢出面板**里的。Shell_TrayWnd 底下连任务栏上那排固定应用按钮一起挂着
    # （Chrome、WPS、"…已固定"），全列出来会把真正的托盘图标挤没（9-16 第一版就这样）。
    # _invoke_tray 调这个函数之前一定已经把面板弹开了，所以这里看得见。
    boxes = [c for c in ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow")
             if _find_class(c)]
    taskbar = not boxes
    for cls in (boxes or ["Shell_TrayWnd"]):
        h = _find_class(cls)
        if not h:
            continue
        try:
            root = uia.ControlFromHandle(int(h))
        except Exception:
            continue
        stack = [(root, 0)]
        while stack:
            node, d = stack.pop()
            if d > 6:
                continue
            try:
                kids = node.GetChildren()
            except Exception:
                continue
            for c in kids:
                try:
                    if c.ControlTypeName in ("ButtonControl", "MenuItemControl"):
                        nm = (c.Name or "").strip()
                        if nm:
                            named[nm] = named.get(nm, 0) + 1
                        else:
                            blank += 1
                except Exception:
                    pass
                stack.append((c, d + 1))
    if not named and not blank:
        return "The tray tree came back empty this time - try again in a second."
    shown = ", ".join("%r%s" % (k, " x%d" % v if v > 1 else "")
                      for k, v in sorted(named.items())[:14])
    out = "What IS in the tray right now: %s" % (shown or "nothing with a name")
    if taskbar:
        out += (" (the overflow panel is not open, so this is the taskbar row and may not "
                "include hidden tray icons)")
    if blank:
        out += ("; plus %d button(s) with NO NAME AT ALL. An unnamed one can still be the app you "
                "want - WeChat gives one icon per signed-in account and some of them come back "
                "nameless - so invoke= cannot reach it: use mode='tray' to list coordinates and "
                "click it, then check which window came up." % blank)
    return out


def _invoke_tray(name, which=0):
    """按名字调用托盘里的那个按钮。返回 (成功?, 说明)。

    托盘（含溢出面板）是标准 UIA 树，按钮名字就是「微信」这种人话。
    调用控件比点坐标稳，原因见 mode='tray' 的 invoke 说明。
    """
    want = (name or "").strip()
    if not want:
        return False, "no name given"
    try:
        import windows_mcp.uia as uia
    except Exception as e:
        return False, "UIA unavailable (%s)" % str(e)[:50]
    # 溢出面板得先弹出来才在树里，而且**弹开之后 UIA 树不是立刻就有内容**——
    # 刚被别的操作关掉时尤其明显：弹开、马上读，读到空的，于是报"没有这个名字的按钮"
    # （9-16 实测撞到）。所以要等面板真的可见，再给它一点时间，读不到就整轮重来。
    import time as _t3
    found = []
    for _round in range(3):
        tray_icons(open_overflow=True)
        for _ in range(10):
            ov = _find_class("TopLevelWindowForOverflowXamlIsland") or                 _find_class("NotifyIconOverflowWindow")
            if ov and user32.IsWindowVisible(wintypes.HWND(ov)):
                break
            _t3.sleep(0.2)
        _t3.sleep(0.35)
        found = _scan_tray_buttons(uia, want)
        if found:
            break
    if not found:
        return False, "no button with that exact name in the tray or its overflow panel. " + _tray_inventory(uia)
    if which >= len(found):
        return False, ("only %d icon(s) named that, so nth:%d is out of range (they are numbered "
                       "from 0)" % (len(found), which))
    c, cls = found[which]
    extra = ""
    if len(found) > 1:
        # 同名多个 = 通常是同一个应用登录了多个账号。默认调第一个，但必须能指定——
        # 9-16 实测：两个微信号都卡住时，不带 nth 只会反复解开同一个，
        # 另一个永远轮不到，而返回里看不出这回事。
        extra = (" (%d icons share this name - this invoked nth:%d; two of the same app usually "
                 "means two accounts are signed in, so pass nth: to pick, and check which one "
                 "came up before doing anything irreversible)" % (len(found), which))
    for pat in ("GetInvokePattern", "GetLegacyIAccessiblePattern"):
        try:
            p = getattr(c, pat)()
            if p is None:
                continue
            if pat == "GetInvokePattern":
                p.Invoke()
            else:
                p.DoDefaultAction()
            return True, "in %s via %s%s" % (cls, pat[3:-7], extra)
        except Exception:
            continue
    try:
        c.Click(simulateMove=False)
        return True, "in %s via a control click%s" % (cls, extra)
    except Exception as e:
        return False, "found it in %s but could not invoke it: %s" % (cls, str(e)[:60])


def tray_icons(open_overflow=True):
    """任务栏通知区里有哪些图标。返回 [(名字, (cx, cy), 在哪)]。

    9-16 现场撞到的：本机两个微信号都缩在托盘里，主窗是 **hidden**（不是最小化），
    `Window(mode='list')` 一个也看不见，调用方会以为微信没开。
    千问办公把这条排在它给我们的建议第一位，理由就是它自己能做到——
    它的 `get_window_state` 把托盘溢出区读成一排具名 Button，照着名字点就行。
    维护者也亲眼见过它这么点开微信。

    我们走同一条路：托盘是标准 UIA 树（`Shell_TrayWnd` 是个 PaneControl），
    按钮名字就是「微信」「千问办公」这种人话，读出来直接能点。

    溢出区（「显示隐藏的图标」那个小面板）平时是隐藏窗口，读不到内容，
    **必须先点一下把它弹出来**——这一步是真动作，所以默认会做但会在返回里说清。
    """
    import time as _t
    out = []
    try:
        import windows_mcp.uia as uia
    except Exception:
        return out

    def _buttons(hwnd, where):
        try:
            root = uia.ControlFromHandle(int(hwnd))
        except Exception:
            return
        stack = [(root, 0)]
        while stack:
            node, d = stack.pop()
            if d > 6:
                continue
            try:
                kids = node.GetChildren()
            except Exception:
                continue
            for c in kids:
                try:
                    if c.ControlTypeName in ("ButtonControl", "MenuItemControl") and c.Name:
                        r = c.BoundingRectangle
                        if r.right > r.left:
                            out.append((c.Name.strip(),
                                        ((r.left + r.right) // 2, (r.top + r.bottom) // 2),
                                        where))
                except Exception:
                    pass
                stack.append((c, d + 1))

    tray = _find_class("Shell_TrayWnd")
    if tray:
        _buttons(tray, "taskbar")

    if open_overflow:
        chevron = None
        for name, xy, _w in out:
            if "隐藏的图标" in name or "Show hidden icons" in name:
                chevron = xy
                break
        ov = (_find_class("TopLevelWindowForOverflowXamlIsland")
              or _find_class("NotifyIconOverflowWindow"))
        already = bool(ov and user32.IsWindowVisible(wintypes.HWND(ov)))
        # **「点一下打开」和「确保是打开的」不是一回事。**
        # 第一版无条件点那个 V 形按钮：面板要是本来就开着，这一下就把它**关掉**了，
        # 于是读回 0 个图标。9-16 现场撞到——上一次调用把面板留开了，
        # 下一次调用就什么也读不到，看起来像功能时灵时不灵。
        if chevron and not already:
            try:
                user32.SetCursorPos(chevron[0], chevron[1])
                _t.sleep(0.08)
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
                _t.sleep(0.8)
                ov = (_find_class("TopLevelWindowForOverflowXamlIsland")
                      or _find_class("NotifyIconOverflowWindow"))
            except Exception:
                pass
        if ov:
            _buttons(ov, "overflow")
    return out


def _find_class(cls):
    """按窗口类找一个顶层窗（含隐藏的）。托盘和溢出区都得这么找。"""
    hit = []

    def cb(h, _l):
        b = ctypes.create_unicode_buffer(96)
        user32.GetClassNameW(h, b, 96)
        if b.value == cls:
            hit.append(h)
            return False
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(cb), 0)
    except Exception:
        pass
    return hit[0] if hit else None


def _force_front(h):
    """前台锁：直接 SetForegroundWindow 常被系统拒绝。敲一下 ALT 让系统认为有用户输入，
    再把自己的线程输入队列挂到当前前台线程上，然后置顶。"""
    import time as _t
    hw = wintypes.HWND(h)
    if user32.IsIconic(hw):
        user32.ShowWindow(hw, SW_RESTORE)
        _t.sleep(0.25)
    elif not user32.IsWindowVisible(hw):
        # "缩进托盘"不是最小化：窗口被 ShowWindow(SW_HIDE) 藏起来了，
        # IsIconic 是 False，所以上面那条判断抓不到它。
        # 微信、QQ 这类常驻托盘的应用都是这么做的。
        # 9-16 实测：本机两个微信号的主窗都在，`Window(list)` 一个也看不见，
        # 千问办公把这条排在它建议的第一位——它只能去数任务栏图标。
        user32.ShowWindow(hw, SW_SHOW)
        _t.sleep(0.35)
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(wintypes.HWND(fg), None) if fg else 0
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
    user32.BringWindowToTop(hw)
    ok = bool(user32.SetForegroundWindow(hw))
    if attached:
        user32.AttachThreadInput(tid_me, tid_fg, False)
    _t.sleep(0.25)
    return ok or user32.GetForegroundWindow() == h


def _normalize(h, x=None, y=None, w=None, hgt=None):
    """恢复成常规尺寸。SW_RESTORE 会还原成最小化之前的状态（可能是最大化），
    所以走 SetWindowPlacement 直接把 showCmd 写成 SW_SHOWNORMAL。"""
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(wintypes.HWND(h), ctypes.byref(wp)):
        raise RuntimeError("GetWindowPlacement failed")
    wp.showCmd = SW_SHOWNORMAL
    if x is not None and y is not None:
        cw = wp.rcNormalPosition.right - wp.rcNormalPosition.left
        ch = wp.rcNormalPosition.bottom - wp.rcNormalPosition.top
        wp.rcNormalPosition.left, wp.rcNormalPosition.top = int(x), int(y)
        wp.rcNormalPosition.right, wp.rcNormalPosition.bottom = int(x) + cw, int(y) + ch
    if w is not None and hgt is not None:
        wp.rcNormalPosition.right = wp.rcNormalPosition.left + int(w)
        wp.rcNormalPosition.bottom = wp.rcNormalPosition.top + int(hgt)
    if not user32.SetWindowPlacement(wintypes.HWND(h), ctypes.byref(wp)):
        raise RuntimeError("SetWindowPlacement failed")


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Window",
        description=(
            "Manage top-level windows by Win32 handle - list them, bring one to the front, restore it from "
            "minimized/tray, maximize, minimize, move or resize. Use this BEFORE Find/Snapshot on a scheduled or "
            "unattended run: a minimized window has all of its elements off-screen, so selector searches return "
            "nothing and screenshots show the wrong thing. mode='list' also reports each window's CLASS NAME, which "
            "tells you what automation will work: a Qt* class with no child HWNDs is self-drawn (use FindText + "
            "coordinates), and an 'Internet Explorer_Hidden' window in the list means legacy IE-hosted web content "
            "whose UIA tree is empty. mode='normalize' is the one you usually want after 'front': plain restore puts "
            "a window back to whatever it was before (often maximized), normalize forces a regular-sized window."
        ),
        annotations=ToolAnnotations(
            title="Window", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "Window-Tool")
    def window_tool(
        mode: Annotated[
            Literal["list", "tray", "close", "at", "front", "normalize", "maximize", "minimize", "move", "resize", "info"],
            Field(description="What to do. 'list'/'info'/'at' are read-only."),
        ] = "list",
        title: Annotated[str | None, Field(description="Match on the window title (substring, case-insensitive).")] = None,
        process: Annotated[str | None, Field(description="Match on the executable name, e.g. 'PBIDesktop' or 'Weixin'.")] = None,
        hwnd: Annotated[int | None, Field(description="Exact window handle, from a previous list.")] = None,
        nth: Annotated[int | None, Field(description="Which match to act on when several windows match (0-based). Leave it out and an ambiguous match is refused; pass nth:0 to say you really do want the first one.")] = None,
        invoke: Annotated[str | None, Field(description="mode='tray' only: CLICK the tray icon with this name by invoking the control itself instead of returning coordinates (exact name, e.g. 'WeChat'; several of that name -> pass nth:). Prefer it over coordinates: the overflow panel sometimes paints nothing while its buttons are real, so a coordinate click lands on blank desktop and still reports success. NOTE mode='tray' OPENS the '^' overflow panel - it changes the screen, which matters when nobody is watching.")] = None,
        all: Annotated[bool, Field(description="mode='list' only: include helper windows too. By default the list skips untitled, tiny and tool windows, which are usually popup hosts and transparent overlays rather than anything you can act on.")] = False,
        hidden: Annotated[bool, Field(description="mode='list' only: also list HIDDEN windows - that is what 'sitting in the tray' really is (apps like WeChat hide the main window instead of minimising, so IsIconic is false and they show up nowhere). Off by default (most hidden windows are empty shells); turn it on when an app is clearly running but the list is empty.")] = False,
        detail: Annotated[bool, Field(description="mode='list' only: add each window's class name and full rect. Off by default because the compact form costs about half the tokens and a listing is as expensive as a screenshot - turn it on when you actually need the window class.")] = False,
        x: Annotated[int | None, Field(description="move/normalize: new left edge, screen pixels.")] = None,
        y: Annotated[int | None, Field(description="move/normalize: new top edge, screen pixels.")] = None,
        width: Annotated[int | None, Field(description="resize/normalize: new width in pixels.")] = None,
        height: Annotated[int | None, Field(description="resize/normalize: new height in pixels.")] = None,
        ctx: Context = None,
    ) -> str:
        _dpi_aware()
        if mode == "at":
            if x is None or y is None:
                return "mode='at' needs x and y."
            h = _root_at(x, y)
            if not h:
                return "nothing at (%d,%d) - off-screen?" % (x, y)
            d = _describe(h)
            line = ("(%d,%d) belongs to hwnd=%d  %s  class=%s  state=%s  title='%s'"
                    % (x, y, d["hwnd"], d["process"], d["class"], d["state"], d["title"][:50]))
            if title or process:
                want = (title or process).lower()
                ok = want in d["title"].lower() or want in d["process"].lower()
                line += ("\nMATCHES what you expected." if ok else
                         "\n!! DOES NOT MATCH %r. Clicking here would hit the WRONG window - it is stacked on top "
                         "of the one you meant, or yours is behind. Bring yours to the front (Window mode='front') "
                         "or move it somewhere clear (mode='move'), then check this point again."
                         % (title or process))
            return line + _locked_note()

        # opencode 9-15：「Window(list) 返回了 62 个窗口，大量是 Notepad PopupWindow、
        # 辅助窗和透明宿主，真正目标被淹没」。
        # 一次返回 60 多行、其中大半是噪音，等于把挑选的负担全推给调用方——
        # 而它每挑错一次就是一次点错。默认只列**看得出是给人用的**窗口：
        # 有标题、够大、不是工具窗。要全部就显式 all=True。
        if mode == "close":
            # 我们一直有 minimize/maximize，**却没有 close**。
            # 9-16 维护者点破的：要测我们自己的软件，关窗口这一步就该走我们的工具，
            # 而我当时用的是裸 Win32 PostMessage——绕过了被测对象。
            #
            # 对常驻托盘的应用（微信、QQ），关闭 = 收回托盘，不是退出进程。
            # 所以这里要如实区分"窗口没了"和"程序退了"，别让调用方以为把它关掉了。
            h2 = int(hwnd) if hwnd else (_pick(title, process, None, 0) or [0])[0]
            if not h2:
                return "close: no window matched (title=%r process=%r hwnd=%r)." % (
                    title, process, hwnd)
            before = _describe(h2)
            alive_proc = _proc_name(_pid(h2))
            user32.PostMessageW(wintypes.HWND(h2), 0x0010, 0, 0)   # WM_CLOSE
            gone = False
            import time as _t2      # 这个模块顶层没有 import time——我刚写 close 时想当然了
            for _ in range(20):
                _t2.sleep(0.25)
                if not user32.IsWindow(wintypes.HWND(h2)) or not user32.IsWindowVisible(
                        wintypes.HWND(h2)):
                    gone = True
                    break
            still = _proc_name_running(alive_proc)
            if gone:
                if still is True:
                    tail = ("The process %s is STILL RUNNING - apps like WeChat and QQ treat "
                            "close as 'hide to the tray', so the window is gone but the program "
                            "is not. Re-open it with Window(mode='tray')." % alive_proc)
                elif still is False:
                    tail = "No %s process is left, so the program exited too." % alive_proc
                else:
                    # 查不出来就别断言。说"进程也没了"而它其实活着，
                    # 会让调用方以为自己把用户的软件关掉了。
                    tail = ("Could not tell whether %s is still running, so do not assume either "
                            "way - Window(mode='list', hidden=true) shows its windows if it is "
                            "alive but hidden in the tray." % alive_proc)
                return ("Closed %s '%s' (hwnd=%d). %s"
                        % (before["process"], before["title"][:40], h2, tail))
            # WM_CLOSE 被无视时，点**真正的关闭按钮**——人就是这么关的。
            #
            # 9-16 实测：本机两个微信号，一个正常收回托盘，另一个完全不理 WM_CLOSE。
            # 同一个程序的两个窗口待遇都能不一样，所以"发了消息"永远不能当成"关掉了"。
            # 自绘标题栏（微信是 Qt）的关闭按钮在右上角，位置按窗口矩形算——
            # 这个偏移和应用无关，是 Windows 窗口的通用约定，不是猜某个 app 的布局。
            try:
                r2 = _rect(h2)
                bx, by = r2[2] - 28, r2[1] + 20
                # 点之前先问一句这个点现在归谁。
                #
                # 右上角正是**每个**窗口放关闭按钮的地方，所以一旦别的窗口盖在上面，
                # 这一下就不是"没点到"，而是"关掉了用户正在用的另一个程序"。
                # 不可逆动作上的盲点击，宁可不点也不能乱点。
                _own = _root_at(bx, by)
                if _own and int(_own) != int(h2):
                    _d3 = _describe(_own)
                    return ("close: hwnd=%d (%s '%s') ignored WM_CLOSE, and I did NOT click its "
                            "close button because (%d,%d) belongs to %s ('%s') right now - that "
                            "is where every window keeps its close button, so clicking blind "
                            "there could have closed THAT program instead. Nothing was clicked. "
                            "Bring the window you mean to the front first "
                            "(Window(mode='front', hwnd=%d)) and try again."
                            % (h2, before["process"], before["title"][:40], bx, by,
                               _d3["process"], _d3["title"][:30], h2))
                user32.SetCursorPos(bx, by)
                _t2.sleep(0.08)
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
                for _ in range(16):
                    _t2.sleep(0.25)
                    if not user32.IsWindow(wintypes.HWND(h2)) or not user32.IsWindowVisible(
                            wintypes.HWND(h2)):
                        return ("Closed %s '%s' (hwnd=%d) by CLICKING its close button - it "
                                "ignored WM_CLOSE. Some windows do; a posted message is never "
                                "proof that a window closed."
                                % (before["process"], before["title"][:40], h2))
            except Exception:
                pass
            return ("close: hwnd=%d (%s '%s') is STILL OPEN. Both a posted WM_CLOSE and a click "
                    "on its close button were tried and it stayed. An unsaved-changes prompt is "
                    "the usual reason - look at the window before trying again. Do NOT assume it "
                    "closed. A window that ignores BOTH is usually wedged rather than busy - "
                    "it can still be pumping messages and be enabled, so nothing looks wrong. "
                    "For a tray-resident app the fix is to click its TRAY icon WITH THE WINDOW "
                    "STILL OPEN (Window(mode='tray') lists the coordinates); that unwedges it, "
                    "and closing works again straight after. Do that instead of retrying close."
                    % (h2, before["process"], before["title"][:40]))

        if mode == "tray":
            # 缩在托盘里的应用，主窗是 hidden（不是最小化），mode='list' 一个也看不见，
            # 调用方会以为程序没开、然后去重新启动它。
            # 千问办公把这条排在它给我们的建议第一位，理由是它自己做得到——
            # 托盘是标准 UIA 树，图标名字就是「微信」这种人话，读出来直接能点。
            if invoke:
                hit, why = _invoke_tray(invoke, int(nth or 0))
                if hit:
                    return ("Invoked the tray icon named %r (%s). This calls the control itself, "
                            "so it works even when the overflow panel paints nothing - which it "
                            "sometimes does, and then clicking its coordinates hits bare desktop "
                            "and still looks like it worked. Check what appeared with "
                            "Window(mode='list'); a tray click usually SHOWS a hidden window, and "
                            "it also un-wedges an app that had stopped responding."
                            % (invoke, why))
                return ("Could not invoke a tray icon named %r: %s. Window(mode='tray') without "
                        "invoke lists every name it can see - use one of those, spelled exactly."
                        % (invoke, why))
            icons = tray_icons(open_overflow=True)
            if not icons:
                return ("Tray could not be read (no UIA tree under Shell_TrayWnd). "
                        "Fall back to Window(mode='list', hidden=true), which finds the "
                        "hidden main windows themselves.")
            ov = [i for i in icons if i[2] == "overflow"]
            lines = ["%d icon(s) in the notification area; %d were in the OVERFLOW panel, "
                     "which had to be OPENED to read them - so that panel is on screen right "
                     "now. SCREEN coordinates; click them like any button."
                     % (len(icons), len(ov))]
            for name, xy, where in icons:
                chev = any(k in name for k in ("\u9690\u85cf\u7684\u56fe\u6807", "Show hidden"))
                if where != "overflow" and not chev:
                    continue
                flat = " ".join(name.split())[:46]
                lines.append("  (%d,%d)  %s%s"
                             % (xy[0], xy[1], flat,
                                "   <- this one opens the overflow" if chev else ""))
            lines.append("  (The same app listed twice usually means two accounts are signed "
                         "in. Check which one you are on before doing anything irreversible.)")
            lines.append("  !! These coordinates are PERISHABLE: the overflow panel closes as "
                         "soon as anything else takes focus, so click within a second of this "
                         "call. If your click seems to do nothing, the panel had already shut "
                         "and you clicked bare desktop - call this again and click straight away.")
            lines.append("  A tray icon is also the CURE for a wedged window, and you click it "
                         "with the window still on screen. If an app has gone blank, or ignores "
                         "clicks, or refuses to close - click its tray icon and it comes back to "
                         "life; then close and reopen it normally if you still want a fresh "
                         "window. (Measured 9-16 on WeChat: both windows were pumping messages "
                         "and enabled, yet ignored WM_CLOSE and every click; minimise-restore and "
                         "resizing did nothing; one tray click fixed both.) A synthetic click "
                         "works here - you do not need a human for it. Better still, skip the "
                         "coordinates: Window(mode='tray', invoke='NAME') calls the button "
                         "itself, which is immune to both the perishable coordinates and the "
                         "panel painting nothing.")
            lines.append("  If a tray icon stops responding to clicks altogether (it happens "
                         "after an app has been hidden and shown many times), do not conclude "
                         "the app is unusable: its hidden window is still there. "
                         "Window(mode='list', hidden=true) finds it and Window(mode='front') "
                         "brings it back - a different mechanism that keeps working when the "
                         "tray does not.")
            return "\n".join(lines)

        if mode == "list":
            # list 永远列出**全部**匹配，不管有没有过滤条件。
            # 9-14 测试机器报的：带 process 过滤时只回了第一个窗口，它开着 3 个微信却只看到 1 个，
            # 一度误判「窗口没了」，最后自己写 EnumWindows 才拿到真值。
            # "列出" 和 "挑一个来操作" 是两件事，之前被我合到一条路上了。
            if hidden:
                # 把藏起来的顶层窗也列出来。默认不列：绝大多数是常驻后台的空壳，
                # 列出来只会把真正能操作的那几个淹掉。
                # 但"应用明明开着、列表里一个窗口都没有"是**更糟的**一种失败——
                # 调用方会以为程序没运行，然后去重新启动它。
                _all = _enum(include_invisible=True)
                if title or process or hwnd:
                    _want = set(_pick(title, process, hwnd, 0, include_invisible=True))
                    hs = [h for h in _all if h in _want]
                else:
                    hs = _all
            else:
                hs = _enum() if not (title or process or hwnd) else _pick(title, process, hwnd, 0)
            marked = []
            for h in hs:
                d = _describe(h)
                marked.append((d, _helper_reason(h, d["size"][0], d["size"][1])))
            skipped = [m for m in marked if m[1]]
            rows = marked if all else [m for m in marked if not m[1]]
            # PATCH 0.5.26：按 **z 序**（前到后）列，不再按进程名字母序。
            #
            # WorkBuddy 9-15 的 P2-1：「一屏几十行按字母排，我要找的那个
            # （刚才还在最前面的剪映）排在中间，得一行行扫。」
            # 字母序只在"按名字查"时有用，而那种场合它本来就该传 process=。
            # 不传过滤条件时它要问的其实是"现在都开着什么、哪个在最前面"——
            # 这个问题的答案就是 z 序。EnumWindows 本来就按 z 序给，
            # 是我在这里排序把它扔掉了。
            # 表头不能把隐藏窗说成 visible。列了一堆"缩在托盘里"的窗口
            # 却在上面写着 visible，跟这一天里我修的那些"把没做到说成做到了"是同一类错。
            what = ("top-level window(s) INCLUDING HIDDEN ones (those are the tray-resident "
                    "ones: the app hid its window instead of minimising it)"
                    if hidden else "visible top-level window(s)")
            if title or process or hwnd:
                what += " matching %s" % ("hwnd:%s" % hwnd if hwnd else ("process:%s" % process if process else "title:%s" % title))
            # 太长就截断。整份列表和一张整窗截图一样贵（约 1084 token），
            # 而后半截几乎全是几个月没碰过的后台窗口。前 24 个按 z 序取，
            # 要的那个不在里面时，传 process= 比翻全表便宜得多。
            CAP = 24
            cut = 0
            total = len(rows)
            if total > CAP and not (title or process or hwnd) and not all:
                cut = total - CAP
                rows = rows[:CAP]
            # 表头报的数必须是**下面真列出来的行数**。
            # 先写 32 再只列 24，等于工具自己撒了个小谎——调用方数不对就会怀疑
            # 别的地方也在骗它，而这套工具全靠"它说的能信"立着。
            lines = ["%s%s, FRONT-MOST FIRST (that is the z-order, so the top line is the "
                     "window in front). SCREEN pixels, same frame as Click. Size then top-left.%s"
                     % (("%d of %d " % (len(rows), total)) if cut else ("%d " % total),
                        what, _locked_note())]
            if len(rows) > 8 and not (title or process or hwnd):
                lines.append("  (You listed EVERYTHING. If you already know which app you want, "
                             "pass process= or title= and this comes back as one or two lines "
                             "instead of %d - a full listing costs about as much as a screenshot, "
                             "and it adds up fast if you do it between every action.)" % len(rows))
            # 行要短。9-15 量出来的：默认这张表 1084 token，**和一张整窗截图一样贵**，
            # 而 opencode 那次 227 分钟的会话调了 77 次 Window——光列窗口就烧掉约 8 万 token，
            # 它的上下文中位数 10.8 万正是这么堆起来的，而上下文越大每轮推理越慢。
            #
            # 砍掉的是重复和用不上的：rect 和 size 只留一个（另一个能算出来）、
            # 类名默认不出（按坐标动作的调用方几乎不看它）、去掉定宽填充的空格。
            # 要完整信息传 detail=true。
            for d, why in rows:
                st = {"maximized": "max", "minimized": "min", "normal": "norm"}.get(
                    d["state"], d["state"])
                if not user32.IsWindowVisible(wintypes.HWND(d["hwnd"])):
                    st = "HIDDEN"       # 藏起来的，不是最小化——两者完全不同
                if detail:
                    lines.append("  hwnd=%-10d %-9s %-22s %-28s %s  rect=%s size=%dx%d%s"
                                 % (d["hwnd"], d["state"], d["process"][:22], d["class"][:28],
                                    (d["title"][:40] or "-"), d["rect"],
                                    d["size"][0], d["size"][1],
                                    ("   [%s]" % why) if why else ""))
                else:
                    lines.append("  hwnd=%d %s %s %r %dx%d @(%d,%d)%s"
                                 % (d["hwnd"], st, d["process"].replace(".exe", "")[:20],
                                    (d["title"][:36] or "-"),
                                    d["size"][0], d["size"][1], d["rect"][0], d["rect"][1],
                                    (" [%s]" % why) if why else ""))
            if cut:
                lines.append("  (%d more window(s) further back in the z-order are not shown - they "
                             "are the ones you have not touched in a while. If what you want is not "
                             "above, pass process= or title= and you get it wherever it sits.)" % cut)
            if skipped and not all:
                kinds = sorted(set(w for _d, w in skipped))
                lines.append("  (%d more window(s) not listed: %s. Those are popup hosts, transparent "
                             "overlays, tool windows and leftover shells - not things you can act on. "
                             "Pass all=true to see them, and do look there if the window you expected "
                             "is missing from this list.)" % (len(skipped), ", ".join(kinds)))
            return "\n".join(lines)

        idx = 0 if nth is None else int(nth)
        cands = _pick(title, process, hwnd, idx)
        if not cands:
            return ("no window matched (title=%r process=%r hwnd=%r). Run Window(mode='list') to see what is open; "
                    "note that windows with an empty title are skipped." % (title, process, hwnd))
        if idx >= len(cands):
            return "matched %d window(s) but nth:%d is out of range." % (len(cands), idx)
        if len(cands) > 1 and mode not in ("list", "info"):
            preview = "; ".join("hwnd=%d %s '%s'" % (d["hwnd"], d["state"], d["title"][:30])
                                for d in (_describe(h) for h in cands[:5]))
            if nth is None:
                return ("%d windows matched, refusing to guess: %s. Re-run with hwnd:, or nth:0 if "
                        "you really do want the first one."
                        % (len(cands), preview))
        h = cands[idx]
        before = _describe(h)

        if mode == "info":
            return "\n".join("%s: %s" % (k, v) for k, v in before.items())

        import time as _t
        if mode == "front":
            # 这个窗口本来就在最前面吗？在的话，这次调用什么也没改变，
            # 而调用方并不知道——它只看到一句"front: ... normal -> normal"。
            #
            # 9-15 实测 opencode 打纸牌：每次 ClickAndWatch 之前都先来一发
            # Window(mode='front')，全是空操作。这不是它笨——是我们没说。
            # 而同一次会话量下来，工具执行只占全程 4.0%，**96% 是它两次调用
            # 之间的思考时间（中位 14.9 秒）**。省掉一次没用的调用，
            # 省的不是那 0.1 秒工具时间，是一整轮推理。
            was_front = (int(user32.GetForegroundWindow() or 0) == int(h))
            ok = _force_front(h)
            after = _describe(h)
            if ok:
                note = ""
            else:
                # 被拒的时候，真正要回答的问题不是"被拒了没"，而是"**现在前台是谁**"。
                #
                # WorkBuddy 9-15 12:4x 实测：本机四个微信全是最小化，Window(mode='front') 连着被拒，
                # 报的就是这句 "SetForegroundWindow was refused"。我把微信的窗口、状态、
                # 前台锁都查了一遍都没问题（甚至改用 AttachThreadInput 也一样）。
                # 根因最后是打印 GetForegroundWindow 才找到的：Windows 的「搜索」浮层
                # （SearchHost.exe，Windows.UI.Core.CoreWindow，标题就叫「搜索」）占着前台。
                # 它不在任何按进程筛窗口的列表里——按 process='Weixin' 列窗口根本看不见它——
                # 而它一边吃着 SetForegroundWindow，一边把落在它身上的合成点击也吃掉，
                # 于是"点了一下没反应"看着像工具的毛病。查到它花了好几轮调用。
                #
                # 拒绝的理由本来就摆在 GetForegroundWindow 里，直接报出来，别让下一个人再查一遍。
                fg = int(user32.GetForegroundWindow() or 0)
                who = ""
                if fg and fg != int(h):
                    try:
                        who = ("  The foreground belongs to hwnd=%d (%s '%s') - that window holds the "
                               "foreground lock, and it also swallows synthetic clicks that land on it. "
                               "It is often something you never listed: a system flyout (Windows search, "
                               "the IME candidate panel) or another app's dialog. Window(mode='list') "
                               "filtered to your own process will NOT show it. Press Esc to dismiss a "
                               "flyout, or click your window once, then retry."
                               % (fg, _proc_name(_pid(fg)) or "?", (_text(fg) or "")[:30]))
                    except Exception:
                        who = "  The foreground belongs to hwnd=%d." % fg
                note = ("  (SetForegroundWindow was refused; the window was raised but may not have focus - "
                        "if it is an elevated window, a medium-integrity service cannot drive it at all)"
                        + who + _locked_note())
            if was_front and before["state"] == after["state"]:
                note += ("  NOTE: it was ALREADY the foreground window, so this call changed nothing. "
                         "You do not need to call this before clicking - Click, ClickAndWatch and Type "
                         "act on screen coordinates and do not require the window to be in front, and "
                         "they refuse on their own if something else owns the point you aimed at. "
                         "Dropping this call saves you a whole round trip per action.")
            return "front: '%s' state %s -> %s rect=%s%s" % (before["title"][:40], before["state"], after["state"], after["rect"], note)
        if mode == "normalize":
            _normalize(h, x, y, width, height)
            _t.sleep(0.2)
            after = _describe(h)
            return "normalize: '%s' %s%s -> %s rect=%s size=%dx%d" % (
                before["title"][:40], before["state"], before["rect"], after["state"], after["rect"], *after["size"])
        if mode == "maximize":
            user32.ShowWindow(wintypes.HWND(h), SW_SHOWMAXIMIZED)
        elif mode == "minimize":
            user32.ShowWindow(wintypes.HWND(h), SW_MINIMIZE)
        elif mode == "move":
            if x is None or y is None:
                return "move needs x and y."
            if user32.IsIconic(wintypes.HWND(h)) or user32.IsZoomed(wintypes.HWND(h)):
                _normalize(h)
            user32.SetWindowPos(wintypes.HWND(h), None, int(x), int(y), 0, 0, SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE)
        elif mode == "resize":
            if width is None or height is None:
                return "resize needs width and height."
            if user32.IsIconic(wintypes.HWND(h)) or user32.IsZoomed(wintypes.HWND(h)):
                _normalize(h)
            user32.SetWindowPos(wintypes.HWND(h), None, 0, 0, int(width), int(height), SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOMOVE)
        _t.sleep(0.2)
        after = _describe(h)
        return "%s: '%s' %s%s -> %s rect=%s size=%dx%d" % (
            mode, before["title"][:40], before["state"], before["rect"], after["state"], after["rect"], *after["size"])
