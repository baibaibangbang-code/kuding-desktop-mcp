"""PATCH 0.5.6: 操作提示层 —— 让坐在电脑前的人知道「现在是 AI 在动」。

维护者 9-14 看 Codex 操作时提的：它有顶部横幅、目标窗口外框、还自己画了个光标覆盖层，
「这样使用电脑的人也知道 AI 在操作」。千问办公也有横幅（「千问办公正在操控您的电脑」），
ZCode 有操控回合标识。**三家都有，我们是唯一完全静默的。**

静默的坏处很具体：机器前面有人时，鼠标突然自己动，没有任何东西告诉他这是工具在跑
还是中招了。测试机器、test client 那种别人也在用的机器尤其要紧。

三个部件，全部是独立的顶层窗口（Codex 也是这么做的，实测它那两个窗口类名都叫
CodexComputerUseCursorOverlay）：

  1. 顶部横幅   —— 说清是谁在操作
  2. 目标窗口外框 —— 四条细边，说清在操作哪个窗口
  3. 光标环     —— 跟着鼠标走，把「AI 的鼠标」和人自己的鼠标区分开

三条硬约束：

* **不能被我们自己的截图拍到**。否则 OCR 会把横幅文字当成目标窗口的内容，
  外框会被 FindElements 当成一个视觉元素。用 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
  ——这个 API 就是干这个的，被排除的窗口对所有截图路径都不可见。
* **不能抢焦点、不能挡点击**。`WS_EX_NOACTIVATE`（不抢）+ `WS_EX_TRANSPARENT`（鼠标穿透）。
* **不能把主流程搞崩**。整个模块所有入口都吞异常：提示层挂了是小事，
  把一次朋友圈发布搞失败是大事。

用法：动作工具在动手前调一次 `activity(hwnd)`，之后闲置几秒自动消失，不用显式关。
"""

import ctypes
import threading
import time
from ctypes import wintypes

# **必须用私有实例，不能用 ctypes.windll.user32。**
# windll.user32 是进程级共享单例；在这里给 GetWindowRect 绑上本模块的 RECT 类，
# 会连带改掉别的模块看到的同一个函数对象——9-15 实测 FindElements 当场报
# 「expected LP_RECT instead of pointer to RECT」，因为 frame_guard 传的是它自己的 RECT。
# WinDLL() 每次返回新对象，签名互不干扰。
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SWP_NOACTIVATE, SWP_NOZORDER, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x0010, 0x0004, 0x0040, 0x0080
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
HWND_TOPMOST = -1
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
WDA_EXCLUDEFROMCAPTURE = 0x00000011
WM_PAINT, WM_TIMER, WM_DESTROY = 0x000F, 0x0113, 0x0002
DT_CENTER, DT_VCENTER, DT_SINGLELINE = 0x1, 0x4, 0x20

# 外框平时细、动手那一下加粗。
# 9-15 维护者拍了张照片：横幅看见了，外框没看见——因为照片拍在两个动作之间，
# 而外框只活 4 秒。一个 4 秒的标记，人基本永远撞不上，那它就没起到作用。
# 现在外框跟着横幅一起是会话级的，含义也随之变准：
# **不是「刚刚点了这一下」，而是「AI 这一轮在操作哪个窗口」**——
# 对坐在电脑前的人，后者才是真正想知道的事。
BAR = 2                      # 平时粗细
BAR_ACTIVE = 6               # 动手那几秒的粗细
BANNER_W = 460          # 只当建窗时的初值，真实宽度按文字算
RING = 112                   # 光标光晕直径：照 Codex 那张照片量出来的，约光标高度的 2.5 倍
# 外框和光标环指的是「刚刚动的是这个窗口、这个位置」，动作过去了还挂着就是误导，所以短。
IDLE_HIDE = 4.0
# 横幅说的是另一件事：**这台电脑现在归 AI 在用**。那是会话级的状态，不是单个动作。
# 9-15 维护者盯着屏幕看，一次都没看见——因为原来横幅跟外框同寿命，
# 一分钟里闪 4 秒、其余全黑，等于没做。Codex 和千问的横幅是整个会话挂着的，
# 我抄了形没抄到这一点。动作之间隔一两分钟很正常，所以按分钟量级给。
BANNER_HOLD = 90.0
# Esc 认账的时间窗，**故意比 IDLE_HIDE 长得多**。
# 9-15 真机实测踩到的：动作之间模型要想、要看图，间隔轻易超过 4 秒；
# 原来只在横幅亮着时认 Esc，等于人在最想按的那个空档按下去**悄无声息地没了**。
# 叫停控件最糟的失败方式就是「看起来按了、其实没用」。90 秒之外就当是人自己的键。
CANCEL_GRACE = 90.0
# 横幅样式：solid（实心蓝）/ glass（深色玻璃）/ capsule（深底+蓝色端帽）
import os as _os
BANNER_STYLE = _os.getenv("UIA_DESKTOP_BANNER_STYLE", "glass")
BANNER_H = 46
ACCENT = 0x00F0A020          # COLORREF 是 BGR：这是橙蓝那一档的醒目色
KEY = 0x00010203             # 透明色键，随便挑一个画面里不会出现的

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
VK_ESCAPE = 0x1B
# 这一下按键是**程序合成的**还是**真人敲的**。
# 叫停的语义本来就是「坐在这台机器前的人要停」，合成的按键不是那个意思。
# 9-15 被这个咬了两次，都是真实场景：
#   ① 我跑 test_overlay_live.py，里面用 keybd_event 造了个 Esc 验护栏，
#      结果被**旁边 opencode 那个 MCP 服务**的钩子收走了——低级钩子是全系统的，
#      同一台机器上多个 agent 共用一副键盘。opencode 下一个动作被拒，停下来问人。
#   ② 更要命的是我们自己也发 Esc：微信文档里就写着「托盘弹框进去前先 {Esc} 归位」。
#      那等于 agent 自己把自己叫停，而且报出来的是「有人按了 Esc」，排查时会莫名其妙。
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02

_state = {"until": 0.0, "banner_until": 0.0, "hwnd": 0, "note": "", "cancel": False,
          "cancel_at": 0.0, "last": 0.0, "tail": True}
_lock = threading.Lock()
_thread = None
_failed = False
_error = ""    # 线程挂了的真实原因；只记布尔的话排查时等于没有


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """低级键盘钩子的参数。以前只按 c_ulong 数组读第 0 个（vkCode），
    现在要读 flags 才能判断是不是合成的。"""
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulonglong))]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
                             ctypes.c_ulonglong, ctypes.c_longlong)


def _decl():
    """显式声明签名。**别依赖 ctypes 默认签名**——HWND/HDC 会被按 c_int 截断，
    9-13、9-14 已经各栽过一次（GetDIBits 那次报的是「int too long to convert」）。"""
    u, g, k = user32, gdi32, kernel32
    u.CreateWindowExW.restype = wintypes.HWND
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HINSTANCE, ctypes.c_void_p]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                             ctypes.c_ubyte, wintypes.DWORD]
    u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    u.BeginPaint.restype = wintypes.HDC
    u.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
    u.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                            ctypes.POINTER(RECT), ctypes.c_uint]
    u.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    g.CreateSolidBrush.restype = wintypes.HBRUSH
    g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    g.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    u.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_uint]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
    u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    g.CreateEllipticRgn.restype = wintypes.HANDLE
    g.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
    # 这一条是 9-15 栽出来的：wndproc 里回落到 DefWindowProcW 时，
    # LPARAM 是 64 位值，按 ctypes 默认的 c_int 传就 OverflowError（「int too long to convert」）。
    # 而且它发生在 WNDPROC 回调里，被 ctypes 吞掉打印，_failed 一直是 False——
    # 症状是「线程说没事，但窗口就是不出来」。
    u.DefWindowProcW.restype = ctypes.c_longlong
    u.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                 ctypes.c_ulonglong, ctypes.c_longlong]
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    u.DestroyWindow.argtypes = [wintypes.HWND]
    # 句柄类返回值/参数一律显式声明。漏一个就是一次 OverflowError：
    # 9-15 这个模块里 DefWindowProcW 和 DeleteObject 各栽了一次，
    # 后者更阴——它在 WM_PAINT 里，症状只是「横幅一片空白」，不报到主线程。
    g.DeleteObject.argtypes = [wintypes.HANDLE]
    g.DeleteObject.restype = wintypes.BOOL
    k.GetModuleHandleW.restype = wintypes.HINSTANCE
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    u.GetSystemMetrics.restype = ctypes.c_int
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.TranslateMessage.argtypes = [ctypes.c_void_p]
    u.DispatchMessageW.argtypes = [ctypes.c_void_p]
    u.DispatchMessageW.restype = ctypes.c_longlong
    # _app_window 要用的三个。同样不许靠 ctypes 默认签名：
    # HWND 按默认的 c_int 传，句柄一大就 OverflowError。
    u.GetAncestor.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                           ctypes.POINTER(wintypes.DWORD)]
    u.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
    u.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.HDC, ctypes.c_void_p, wintypes.COLORREF, ctypes.c_void_p,
        wintypes.DWORD]
    u.GetDC.restype = wintypes.HDC
    u.GetDC.argtypes = [wintypes.HWND]
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateDIBSection.restype = wintypes.HANDLE
    g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD]
    g.SelectObject.restype = wintypes.HANDLE
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    # 这几个也必须声明。这个文件开头自己写过这条坑，我还是又踩了一次：
    # 句柄是指针宽的，ctypes 默认按 c_int 传，句柄一大就 OverflowError；
    # 而且它报在 DeleteDC 这种收尾调用上，看起来像「贴图成功了只是清理失败」，
    # 实际后果是整个提示层线程挂掉、一个像素都画不出来。
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.DeleteObject.argtypes = [wintypes.HANDLE]
    g.CreateSolidBrush.restype = wintypes.HANDLE
    u.GetWindow.restype = wintypes.HWND
    u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.MonitorFromWindow.restype = wintypes.HANDLE
    u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    u.GetMonitorInfoW.restype = wintypes.BOOL


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


from PIL import Image          # noqa: E402  画面一律 PIL 画
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01


def _blit(hwnd, im, x, y):
    """把一张 PIL RGBA 图贴到分层窗上（逐像素 alpha）。

    GDI 画不出抗锯齿，所以圆角、阴影、渐变一律先用 PIL 画好再整张贴上来。
    **必须预乘 alpha**：不预乘的话半透明像素会发白发灰，圆角边上尤其明显。
    """
    w, h = im.size
    # 预乘走 PIL 的 C 实现。纯 Python 逐像素在 2560x1600 的外框上要好几秒，
    # 而外框每次窗口移动都要重画一张。
    from PIL import ImageChops
    r, g_, b, a = im.split()
    im = Image.merge("RGBA", (ImageChops.multiply(r, a),
                              ImageChops.multiply(g_, a),
                              ImageChops.multiply(b, a), a))
    buf = im.tobytes("raw", "BGRA")

    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h          # 负高度 = 自上而下，跟 PIL 一致
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, bmp)
    ctypes.memmove(bits, bytes(buf), len(buf))

    blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    pt_dst, pt_src, size = POINT(x, y), POINT(0, 0), SIZE(w, h)
    user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst),
                               ctypes.byref(size), hdc, ctypes.byref(pt_src),
                               0, ctypes.byref(blend), ULW_ALPHA)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, hdc_screen)


def _make(cls, w, h, alpha, color, proc_win=False, ulw=False):
    """造一个点击穿透、不抢焦点、置顶、且截图拍不到的小窗。

    ulw=True 的窗口用 UpdateLayeredWindow 贴逐像素 alpha 的位图，
    **不能**再调 SetLayeredWindowAttributes——两者互斥，一起用的话
    后者会把逐像素 alpha 整个作废（画出来就是一块实心方块）。
    """
    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        cls, None, WS_POPUP, 0, 0, w, h, None, None, None, None)
    if not hwnd:
        return 0
    if ulw:
        pass                      # 画面由 _blit 推上去，这里什么都不设
    elif proc_win:
        user32.SetLayeredWindowAttributes(hwnd, KEY, 0, LWA_COLORKEY)
    else:
        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
    # 关键：把自己从所有截图里排除，否则 OCR 会读到横幅、FindElements 会把外框当元素。
    # 副作用是**任何**抓屏都看不到它，包括我自己调试时想截一张看看长什么样——
    # 所以留个开关，验证渲染效果时临时打开。
    import os
    if os.getenv("UIA_DESKTOP_OVERLAY_CAPTURABLE", "").strip().lower() not in ("1", "true", "yes"):
        try:
            user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass
    return hwnd


def _loop():
    global _failed
    try:
        _decl()
        hinst = kernel32.GetModuleHandleW(None)

        banner_text = [""]
        tail_text = [""]

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                r = RECT(0, 0, BANNER_W, BANNER_H)
                br = gdi32.CreateSolidBrush(ACCENT)
                user32.FillRect(hdc, ctypes.byref(r), br)
                gdi32.DeleteObject(br)
                gdi32.SetBkMode(hdc, 1)                 # TRANSPARENT
                gdi32.SetTextColor(hdc, 0x00FFFFFF)
                user32.DrawTextW(hdc, banner_text[0], -1, ctypes.byref(r),
                                 DT_CENTER | DT_VCENTER | DT_SINGLELINE)
                user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        proc = WNDPROC(wndproc)
        wc = WNDCLASS()
        wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "UiaDesktopOverlay"
        wc.hbrBackground = gdi32.CreateSolidBrush(KEY)
        if not user32.RegisterClassW(ctypes.byref(wc)):
            if kernel32.GetLastError() not in (1410,):      # 1410 = 已注册
                _failed = True
                return

        # 三块画面全部改成 PIL 画好再贴：GDI 没有抗锯齿，
        # 直角硬边和实心硬圆就是"丑"的来源（9-16 和 Codex 的提示层对比出来的）。
        frame_win = _make("UiaDesktopOverlay", 10, 10, 0, 0, ulw=True)
        screen_win = _make("UiaDesktopOverlay", 10, 10, 0, 0, ulw=True)
        ring = _make("UiaDesktopOverlay", RING, RING, 0, 0, ulw=True)
        banner = _make("UiaDesktopOverlay", BANNER_W, BANNER_H, 0, 0, ulw=True)
        if not banner or not ring or not frame_win or not screen_win:
            _failed = True
            return
        from windows_mcp.tools import overlay_art as _art
        _cache = {"banner": None, "glow": None, "frame": None}
        try:
            _cache["glow"] = _art.glow_image(RING)
            _blit(ring, _cache["glow"], 0, 0)
        except Exception:
            pass

        # —— Esc 叫停 ——（借自 Codex，它的提示层配置里有 "escToCancel": "Esc to cancel"）
        # 提示本身只是让人知道 AI 在动；**能叫停才是真给了人出口**。
        # 语义要说清楚：Esc 不会打断已经在飞的那一下，它让**下一个**动作拒绝执行。
        # 钩子只看不吞——照常 CallNextHookEx，Esc 对前台应用该怎样还怎样。
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                                      ctypes.c_ulonglong, ctypes.c_longlong)
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                             wintypes.HINSTANCE, wintypes.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_longlong
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_ulonglong, ctypes.c_longlong]

        def hook(code, wparam, lparam):
            try:
                if code >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    kb = ctypes.cast(lparam,
                                     ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    injected = bool(kb.flags & (LLKHF_INJECTED |
                                                LLKHF_LOWER_IL_INJECTED))
                    if kb.vkCode == VK_ESCAPE and not injected:
                        now = time.time()
                        with _lock:
                            if now - _state["last"] < CANCEL_GRACE:
                                _state["cancel"] = True
                                _state["cancel_at"] = now
                                # 横幅可能早就收起来了，闪一下告诉人「收到了」，
                                # 否则人按完什么都没发生，只能再按，或者以为没用。
                                _state["note"] = "已按 Esc · uia-desktop 下一个动作会停下"
                                _state["tail"] = False
                                # hwnd 不清：会话还在，外框继续标着"这一轮在操作哪个窗口"。
                                # 清掉会让外框收起再亮，白闪一下。
                                _state["until"] = 0.0      # 此刻没在动手，外框回到细的
                                _state["banner_until"] = max(
                                    _state["banner_until"], now + 6.0)
            except Exception:
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        hookproc = HOOKPROC(hook)          # 必须留引用，否则被回收后回调变野指针
        try:
            # hMod 必须传 NULL。**这里原来传的是 hinst（当前 exe 的模块句柄），
            # SetWindowsHookExW 一直返回 NULL、错误码 126 ERROR_MOD_NOT_FOUND——
            # 也就是说 Esc 叫停从上线起就没工作过**，而横幅上一直写着「按 Esc 停下」。
            # UI 上给出的承诺不兑现，比不给承诺更糟：人以为自己有个刹车。
            # 低层钩子的回调在本进程内（ctypes 生成的 thunk，不属于任何模块），
            # 这种情况文档要求 hMod 传 NULL。
            # 9-15 靠给 desktop_lock 的鼠标钩子加"回调被调用过几次"的计数
            # 才发现这件事——此前没有任何断言能照到它。
            _state["hook"] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hookproc, None, 0)
        except Exception:
            _state["hook"] = None

        # 横幅和「外框+光标环」寿命不同，所以分别记有没有显示着。
        # 早先合成一个 shown，横幅就只能跟着外框一起 4 秒消失。
        banner_on = bars_on = ring_on = screen_on = False
        _last_top = [0.0]
        while True:
            time.sleep(0.05)
            # 抽干消息，否则窗口不重绘
            msg = ctypes.create_string_buffer(64)
            while user32.PeekMessageW(msg, None, 0, 0, 1):
                user32.TranslateMessage(msg)
                user32.DispatchMessageW(msg)

            with _lock:
                until, b_until = _state["until"], _state["banner_until"]
                target, note, tail = _state["hwnd"], _state["note"], _state["tail"]
            now = time.time()
            live = now < until              # 外框 + 光标环：只跟着单个动作
            banner_live = now < b_until     # 横幅：会话级，长得多

            # —— 全屏外框：会话期间全程挂着 ——
            #
            # 和窗口外框回答的不是同一个问题：
            # 窗口外框说「AI 在动哪个窗口」，全屏外框说「这台电脑现在归不归我」。
            # 后者是坐在电脑前的人第一眼要的答案，所以它必须**全程**在——
            # 要人盯着才能确认的提示，等于没有。
            if banner_live and not screen_on:
                try:
                    sw = user32.GetSystemMetrics(0)
                    sh2 = user32.GetSystemMetrics(1)
                    if _cache.get("screen_key") != (sw, sh2):
                        _cache["screen"] = _art.screen_frame(sw, sh2, thick=5)
                        _cache["screen_key"] = (sw, sh2)
                    user32.SetWindowPos(screen_win, HWND_TOPMOST, 0, 0, sw, sh2,
                                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    _blit(screen_win, _cache["screen"], 0, 0)
                    screen_on = True
                except Exception as _e:
                    # **不许悄悄失败。** 全屏外框是给人看的唯一全局信号，
                    # 它画不出来而我们一声不吭，人就会以为"AI 没在动"——
                    # 这正是我一整天在别处修的那类错，不能自己又犯一次。
                    globals()["_error"] = "screen_frame: %s: %s" % (type(_e).__name__, _e)
            elif not banner_live and screen_on:
                user32.ShowWindow(screen_win, 0)
                screen_on = False
            elif screen_on:
                # **每秒把置顶重申一次。**
                #
                # 9-16 维护者要求全屏外框要包住底部快捷栏，我一看几何是对的
                # （整屏 2560x1600，任务栏那一条在矩形之内），可画面里就是被盖住——
                # 任务栏（Shell_TrayWnd）自己也是 topmost，**同一级里后放的在上面**。
                # 我们只在显示那一刻置顶了一次，之后任何一次任务栏被激活都会压过我们。
                # 所以"设一次 topmost"不等于"一直在最上面"，得反复重申。
                # 一秒一次：够压住它，又不会跟别的置顶窗抢得闪。
                if now - _last_top[0] > 1.0:
                    _last_top[0] = now
                    user32.SetWindowPos(screen_win, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
                    if banner_on:
                        user32.SetWindowPos(banner, HWND_TOPMOST, 0, 0, 0, 0,
                                            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)

            # —— 横幅 ——
            if banner_live:
                # **只在文字真的变了才重画。**
                # 第一版每 50ms 无脑 InvalidateRect(erase=True) + SWP_SHOWWINDOW，
                # 于是「擦成透明 → 画 → 再擦」不停循环，肉眼是闪的，抓图还会抓到透明那一帧
                # （9-15 排查这个花了不少时间：WM_PAINT 明明触发了 43 次，截出来却是空白）。
                # 横幅挂的时间比单个动作长得多，所以**文案要分清两件事**：
                # 「此刻正在动你的鼠标」和「会话还开着、随时可能再动」不是一回事。
                # 一直显示前者，人第一次发现横幅亮着而鼠标没动，就不会再信它了。
                if live:
                    want = note or "uia-desktop 正在操控这台电脑"
                else:
                    want = "uia-desktop 仍在这台电脑上工作"
                tail_text[0] = "按 Esc 停下" if tail else ""
                if want != banner_text[0] or not banner_on:
                    banner_text[0] = want
                    try:
                        # 宽度**按文字算**。写死 460px 是 9-16 抓到的真 bug：
                        # 文案一长两头各被切掉一截，提示把话说到一半比不提示更糟。
                        im = _art.banner_image(want, tail_text[0],
                                               style=BANNER_STYLE, h=BANNER_H)
                        _cache["banner"] = im
                        sw = user32.GetSystemMetrics(0)
                        user32.SetWindowPos(banner, HWND_TOPMOST,
                                            (sw - im.width) // 2, 2,
                                            im.width, im.height,
                                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
                        _blit(banner, im, (sw - im.width) // 2, 2)
                    except Exception:
                        pass
                banner_on = True
            elif banner_on:
                user32.ShowWindow(banner, 0)
                banner_on = False

            # —— 光标环：只在真的动了鼠标那几秒 ——
            # 必须短。一直留着，人会以为鼠标始终是 AI 在动，
            # 可绝大部分时间鼠标是他自己的。
            if live:
                p = POINT()
                user32.GetCursorPos(ctypes.byref(p))
                user32.SetWindowPos(ring, HWND_TOPMOST, p.x - RING // 2, p.y - RING // 2,
                                    RING, RING, SWP_NOACTIVATE | SWP_SHOWWINDOW)
                if _cache.get("glow") is not None:
                    _blit(ring, _cache["glow"], p.x - RING // 2, p.y - RING // 2)
                ring_on = True
            elif ring_on:
                user32.ShowWindow(ring, 0)
                ring_on = False

            # —— 外框：跟横幅一样是会话级，动手那几秒加粗 ——
            if banner_live and target and not user32.IsWindow(target):
                # 目标窗口关掉了（我们自己点关的也算）。外框不该就此消失——
                # 这一轮还在继续，只是换了个窗口。
                target = _app_window(user32.GetForegroundWindow())
                with _lock:
                    _state["hwnd"] = target
            if banner_live and target:
                r = _group_rect(target) or RECT()
                if r.right > r.left:
                    # 最大化的窗口矩形会超出屏幕，不夹一下四条边全画在可视区外
                    r = _clamp_to_screen(target, r)
                    b = BAR_ACTIVE if live else BAR
                    w, h = r.right - r.left, r.bottom - r.top
                    key = (w, h, b)
                    if _cache.get("frame_key") != key:
                        try:
                            # Win11 的窗口本身就是圆角的，套一个直角框上去
                            # 四个角会各露一块——这也是"丑"的一部分。
                            img, pad = _art.frame_edges(w, h, thick=b)
                            _cache["frame"] = img
                            _cache["frame_pad"] = pad
                            _cache["frame_key"] = key
                        except Exception:
                            _cache["frame"] = None
                    img = _cache.get("frame")
                    if img is not None:
                        pad = _cache.get("frame_pad", 0)
                        x, y = r.left - pad, r.top - pad
                        user32.SetWindowPos(frame_win, HWND_TOPMOST, x, y,
                                            img.width, img.height,
                                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
                        _blit(frame_win, img, x, y)
                    bars_on = True
                elif bars_on:
                    user32.ShowWindow(frame_win, 0)
                    bars_on = False
            elif bars_on:
                user32.ShowWindow(frame_win, 0)
                bars_on = False
    except Exception as e:
        import traceback
        globals()["_error"] = "%s: %s | %s" % (
            type(e).__name__, e, traceback.format_exc()[-500:].replace("\n", " ⏎ "))
        _failed = True


def _ensure():
    global _thread
    if _failed:
        return False
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="uia-overlay", daemon=True)
        _thread.start()
        time.sleep(0.25)          # 给它时间把窗口造出来
    return not _failed


GA_ROOTOWNER = 3
MIN_APP_W, MIN_APP_H = 400, 300     # 比这还小的多半是菜单/气泡，不是主窗


def _group_rect(target):
    """这个应用**这一组**窗口的并集矩形：主窗 + 它弹出来的菜单/对话框。

    9-16 维护者拍到的：我们在微信上发朋友圈，外框只框住主窗，
    而「选照片或视频 / 发表文字」那个弹出菜单**露在框外**。
    菜单是独立的顶层窗口，只框主窗就必然漏掉它——
    而且这不是微信独有的，任何有弹出菜单的应用都一样。

    外框说的是「AI 正在动这个应用」，那它就该把这个应用**此刻在屏幕上的全部**圈进去。
    只认两类：**属主是它的**窗口（GW_OWNER），以及**同进程、且贴着它**的弹出窗。
    同进程但离得远的不算——那多半是另一个文档窗，圈进来会让框大得没有意义。
    """
    r0 = RECT()
    if not user32.GetWindowRect(target, ctypes.byref(r0)):
        return None
    L, T, R, B = r0.left, r0.top, r0.right, r0.bottom
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(target, ctypes.byref(pid))
        mine = pid.value
        gap = 48                     # 菜单一般紧贴着窗口边；给一点余量

        def cb(h, _):
            if h == target or not user32.IsWindowVisible(h):
                return True
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(h, buf, 64)
            if buf.value == "UiaDesktopOverlay":      # 别把自己圈进去
                return True
            p2 = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p2))
            owner = user32.GetWindow(h, 4)            # GW_OWNER
            if p2.value != mine and owner != target:
                return True
            rr = RECT()
            if not user32.GetWindowRect(h, ctypes.byref(rr)):
                return True
            if rr.right - rr.left < 8 or rr.bottom - rr.top < 8:
                return True
            # 贴着才算：和主窗矩形有交集，或者间隙在 gap 之内
            if (rr.left > R + gap or rr.right < L - gap
                    or rr.top > B + gap or rr.bottom < T - gap):
                return True
            nonlocal_box[0] = min(nonlocal_box[0], rr.left)
            nonlocal_box[1] = min(nonlocal_box[1], rr.top)
            nonlocal_box[2] = max(nonlocal_box[2], rr.right)
            nonlocal_box[3] = max(nonlocal_box[3], rr.bottom)
            return True

        nonlocal_box = [L, T, R, B]
        user32.EnumWindows(ENUMPROC(cb), 0)
        L, T, R, B = nonlocal_box
    except Exception:
        pass
    out = RECT()
    out.left, out.top, out.right, out.bottom = L, T, R, B
    return out


def _clamp_to_screen(hwnd, r):
    """把窗口矩形夹到它所在显示器的范围内，外框才不会画到屏幕外去。

    维护者 9-15 的观察：「一开始没看到外框是因为剪映最大化了，外框在屏幕边缘」。
    查下去比"边缘"更严重——**最大化窗口在 Windows 里的矩形本来就超出屏幕**。
    本机实测 Claude 最大化时 rect 是 (-11,-11,2571,1539)，而屏幕是 2560x1600：
    左边框画在 x=-11、上边框在 y=-11、右边框在 2569，**四条边全在屏幕外**，一条都看不见。
    这不是观感问题，是实打实画丢了。

    夹完再让四条边贴着夹出来的矩形内侧，保证整条都在可见区域里。
    """
    try:
        mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not mon:
            return r
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return r
        m = mi.rcMonitor
        l = max(r.left, m.left)
        t = max(r.top, m.top)
        rr = min(r.right, m.right)
        b = min(r.bottom, m.bottom)
        if rr - l < 20 or b - t < 20:
            return r
        out = RECT()
        out.left, out.top, out.right, out.bottom = l, t, rr, b
        return out
    except Exception:
        return r


def _app_window(hwnd):
    """外框该框哪个窗口：框**应用主窗**，不框瞬间弹出的菜单。

    调用方给的句柄来自 `_owner_at`——那是「这个像素归哪个顶层窗口」，
    而右键菜单、下拉、气泡**在 Windows 眼里都是顶层窗口**。
    9-15 就栽在这儿：opencode 右键微信的相机图标，外框框在了那个右键菜单上，
    菜单一关窗口就没了，`GetWindowRect` 失败、外框隐藏，**此后整轮都是黑的**。
    维护者连着两轮反馈「没看到外框」，根因在这里，不在寿命。

    外框要说的是「AI 这一轮在操作哪个应用」，那就该框主窗；
    精确到哪个像素本来就由光标环负责，两者分工不重叠。

    做法：先问 Windows 这个窗口的根宿主（菜单的宿主就是主窗），
    还是太小就退一步——在同一个进程里挑最大的那个可见顶层窗口。
    全都不成就用原来那个，绝不因为找不到就什么都不画。
    """
    try:
        h = int(hwnd or 0)
        if not h:
            return 0
        root = user32.GetAncestor(h, GA_ROOTOWNER) or h
        r = RECT()
        if (user32.GetWindowRect(root, ctypes.byref(r))
                and r.right - r.left >= MIN_APP_W
                and r.bottom - r.top >= MIN_APP_H):
            return int(root)
        # 根宿主也小：同进程里找最大的可见顶层窗口
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        best, best_area = 0, 0
        cands = []

        def cb(w, _):
            q = wintypes.DWORD()
            user32.GetWindowThreadProcessId(w, ctypes.byref(q))
            if q.value == pid.value and user32.IsWindowVisible(w):
                rr = RECT()
                if user32.GetWindowRect(w, ctypes.byref(rr)):
                    cands.append((w, (rr.right - rr.left) * (rr.bottom - rr.top),
                                  rr.right - rr.left, rr.bottom - rr.top))
            return True

        user32.EnumWindows(ENUMPROC(cb), 0)
        for w, area, ww, hh in cands:
            if ww >= MIN_APP_W and hh >= MIN_APP_H and area > best_area:
                best, best_area = w, area
        return int(best or root or h)
    except Exception:
        return int(hwnd or 0)


def activity(hwnd=None, note="", seconds=IDLE_HIDE):
    """动手前调一次。闲置 `seconds` 秒后自动收起，不用显式关。

    整个函数吞掉所有异常：提示层是锦上添花，**绝不能因为它把正事搞失败**。
    """
    try:
        import os
        if os.getenv("UIA_DESKTOP_NO_OVERLAY", "").strip().lower() in ("1", "true", "yes"):
            return
        if not _ensure():
            return
        with _lock:
            now = time.time()
            _state["until"] = now + seconds
            _state["banner_until"] = max(_state["banner_until"], now + BANNER_HOLD)
            _state["last"] = now          # Esc 的认账窗口从这里算，见 CANCEL_GRACE
            # **不许把目标清成 0。**
            #
            # 9-16 维护者观察到「外框不是全程都有，横幅是」——就出在这一行。
            # 外框的条件是「横幅活着 **且** 有目标」，而这里无条件覆盖：
            # 任何一次不带 hwnd 的动作都会把目标抹掉，外框随之消失，
            # 于是一轮里外框断断续续地闪，而横幅一直在。
            # 拿不到新目标时**保留上一个**；连上一个都没有就用当前前台窗口——
            # 那本来就是我们正在动的那个。
            _t = _app_window(hwnd)
            if not _t:
                _t = _state.get("hwnd") or _app_window(user32.GetForegroundWindow())
            _state["hwnd"] = _t
            _state["tail"] = True
            if note:
                _state["note"] = note
    except Exception:
        pass


def watching(note=""):
    """**只**点亮横幅，不画外框也不画光标环。读屏类工具（截图 / OCR / 找元素）用这个。

    为什么读屏也要提示：动作之间隔一两分钟，那段时间我们并没有闲着——在看这台机器的屏幕。
    对坐在电脑前的人来说「AI 正在看我的屏幕」和「AI 正在动我的鼠标」都该知道，
    而且这样横幅才能在整个任务期间基本连续地挂着，而不是每个动作闪一下就没。

    不画外框是刻意的：外框的意思是「刚刚动了这个窗口」，读屏什么都没动，画了就是误导。
    """
    try:
        import os
        if os.getenv("UIA_DESKTOP_NO_OVERLAY", "").strip().lower() in ("1", "true", "yes"):
            return
        if not _ensure():
            return
        with _lock:
            now = time.time()
            _state["banner_until"] = max(_state["banner_until"], now + BANNER_HOLD)
            _state["last"] = now
            _state["tail"] = True
            # 读屏也要让外框继续挂着。外框现在的含义是
            # **「这一轮 AI 在哪个窗口上工作」**，不是「刚刚点了这一下」——
            # 后者由外框的**粗细**表示（动手那几秒加粗）。
            # 含义既然是会话级的，就不该因为这一步只是看而不是动就消失。
            if not _state.get("hwnd"):
                _state["hwnd"] = _app_window(user32.GetForegroundWindow())
            _state["note"] = note or "uia-desktop 正在读这台电脑的屏幕"
    except Exception:
        pass


def hide():
    """立刻收起（任务做完时调）。"""
    try:
        with _lock:
            _state["until"] = 0.0
            _state["banner_until"] = 0.0
    except Exception:
        pass


def take_cancel():
    """人有没有按过 Esc。返回按下距今多少秒（没按过返回 None）。取走即清零。

    只报一次：报过就把旗标清掉，不会把后续动作全卡死——人要真想停，再按一次就是了。

    语义：Esc **打断不了已经在飞的那一下**，它让下一个动作拒绝执行。
    这一点要如实告诉调用方，不能让人以为按下去就能立刻停住鼠标。
    """
    try:
        with _lock:
            if _state.get("cancel"):
                _state["cancel"] = False
                return max(0.0, time.time() - _state.get("cancel_at", 0.0))
    except Exception:
        pass
    return None
