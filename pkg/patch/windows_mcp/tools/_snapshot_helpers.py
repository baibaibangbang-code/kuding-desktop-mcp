"""Snapshot / Screenshot shared helpers.

These functions were originally defined in ``__main__.py`` and are now shared
by the Snapshot and Screenshot tool modules.
"""

import io
import logging
import os
import time

from fastmcp.utilities.types import Image
from textwrap import dedent
from windows_mcp.desktop.service import Desktop, Size
from windows_mcp.desktop.utils import remove_private_use_chars


logger = logging.getLogger(__name__)

MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 1920, 1080


def _dpi_scale_text() -> str:
    """PATCH: 屏幕 DPI 缩放比，写进截图元信息里。

    UIA 和本工具报的都是物理像素，而非 DPI 感知进程的 GetWindowRect 报的是逻辑像素；
    150% 缩放下两者正好差 1.5 倍——算出来是个「看着像那么回事」的错坐标，不会报错，只会点偏。
    9-13、9-14 各栽过一次，所以直接把缩放比印在返回里。
    """
    import ctypes
    from ctypes import wintypes

    # GetDpiForSystem 不需要 DC，也就不会被句柄截断坑到。
    # （第一版用 GetDC + GetDeviceCaps，单独跑好好的，一被 windows_mcp 导入就失败——
    #   comtypes 改过 ctypes 的默认签名，HDC 按 c_int 截断成了负数。别再依赖默认签名。）
    try:
        fn = ctypes.windll.user32.GetDpiForSystem
        fn.restype = wintypes.UINT
        fn.argtypes = []
        dpi = int(fn())
        if dpi:
            return "%d%% (%d dpi)" % (round(dpi / 96.0 * 100), dpi)
    except Exception as e:
        first = "%s: %s" % (type(e).__name__, e)
    else:
        first = "GetDpiForSystem returned 0"

    try:
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        u.GetDC.restype, u.GetDC.argtypes = wintypes.HDC, [wintypes.HWND]
        g.GetDeviceCaps.restype, g.GetDeviceCaps.argtypes = ctypes.c_int, [wintypes.HDC, ctypes.c_int]
        u.ReleaseDC.restype, u.ReleaseDC.argtypes = ctypes.c_int, [wintypes.HWND, wintypes.HDC]
        dc = u.GetDC(None)
        try:
            dpi = int(g.GetDeviceCaps(dc, 88))  # LOGPIXELSX
        finally:
            u.ReleaseDC(None, dc)
        return "%d%% (%d dpi)" % (round(dpi / 96.0 * 100), dpi)
    except Exception as e:
        # 不吞：说不出来是多少，至少说清为什么，否则排查时又要从头猜
        return "unavailable (%s; %s)" % (first, type(e).__name__)


def _snap_scale(screen_w: int, screen_h: int, want: float) -> float:
    """PATCH 0.5.5: 把缩放比吸附到 1/N，让「图像坐标 → 屏幕坐标」变成整数倍。

    9-14 量出来的问题：本机屏幕 2560x1600，上限 1920x1080，等比缩到 1728x1080，
    于是换算系数是 **1.4815**。每一个坐标都要在脑子里乘一次 1.4815，乘完还要取整——
    这就是「点得不够准」的来源，不是鼠标不准。

    同日看千问办公：它的截图是 1280x800，正好是 2560x1600 的一半，换算系数是干净的 2，
    而且它对上层直接声称「图像坐标就是点击坐标」。系数干净 + 只有一个坐标系，
    是它明显更准的真实原因（它在微信上同样是纯 x/y 点，没有用控件树）。

    这里取折中：仍然缩（不缩的话 2560x1600 的图太费 token），但只允许缩成 1/2、1/3…
    系数从 1.4815 变成 2，乘法不会错，取整也不会漂。
    """
    if screen_w <= 0 or screen_h <= 0:
        return want
    fit = min(1.0, MAX_IMAGE_WIDTH / screen_w, MAX_IMAGE_HEIGHT / screen_h, want)
    if fit >= 1.0:
        return 1.0
    import math
    n = math.ceil(1.0 / fit)          # 1/1.4815 -> 需要 1/2，取更小的那一档，保证仍在上限内
    return 1.0 / max(2, n)


def _screenshot_scale() -> float:
    value = os.getenv("WINDOWS_MCP_SCREENSHOT_SCALE", "1.0")
    try:
        scale = float(value)
    except ValueError:
        logger.warning("Invalid WINDOWS_MCP_SCREENSHOT_SCALE value %r, using 1.0", value)
        scale = 1.0
    if not (0.1 <= scale <= 1.0):
        logger.warning("WINDOWS_MCP_SCREENSHOT_SCALE %r out of range [0.1, 1.0], clamping", scale)
        scale = max(0.1, min(1.0, scale))
    return scale


def _snapshot_profile_enabled() -> bool:
    value = os.getenv("WINDOWS_MCP_PROFILE_SNAPSHOT", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(value: bool | str) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def capture_desktop_state(
    desktop: Desktop,
    *,
    use_vision: bool,
    use_dom: bool,
    use_annotation: bool,
    use_ui_tree: bool,
    width_reference_line: int | None,
    height_reference_line: int | None,
    display: list[int] | None,
    tool_name: str,
    foreground_only: bool = False,
):
    profile_enabled = _snapshot_profile_enabled()
    profile_started_at = time.perf_counter()
    stage_started_at = profile_started_at
    desktop_state_ms = 0.0
    metadata_render_ms = 0.0
    screenshot_encode_ms = 0.0

    if use_dom and not use_ui_tree:
        raise ValueError("use_dom=True requires use_ui_tree=True")

    display_indices = Desktop.parse_display_selection(display)

    grid_lines = None
    if width_reference_line and height_reference_line:
        grid_lines = (int(width_reference_line), int(height_reference_line))

    # PATCH 0.5.5: 先把缩放吸附到 1/N 再交给上游，免得上游按"等比塞进上限"算出 0.675 这种系数。
    _scale = _screenshot_scale()
    try:
        import ctypes
        _u = ctypes.windll.user32
        _u.SetProcessDPIAware()
        _scale = _snap_scale(_u.GetSystemMetrics(0), _u.GetSystemMetrics(1), _scale)
    except Exception:
        pass

    desktop_state = desktop.get_state(
        use_vision=use_vision,
        use_dom=use_dom,
        use_annotation=use_annotation,
        use_ui_tree=use_ui_tree,
        foreground_only=foreground_only,
        as_bytes=False,
        scale=_scale,
        grid_lines=grid_lines,
        display_indices=display_indices,
        max_image_size=Size(width=MAX_IMAGE_WIDTH, height=MAX_IMAGE_HEIGHT),
    )
    if profile_enabled:
        desktop_state_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    interactive_elements = desktop_state.tree_state.interactive_elements_to_string()
    scrollable_elements = desktop_state.tree_state.scrollable_elements_to_string()
    semantic_tree = desktop_state.tree_state.semantic_tree_to_string()
    windows = desktop_state.windows_to_string()
    active_window = desktop_state.active_window_to_string()

    # Codex 9-15 在剪映上报的第 10 条：截图正确，却反复附带
    # "Focused Window: No active window found" 和 "Opened Windows: No windows found"，
    # 而同一时刻 Window(mode='list') 能好好地列出剪映。
    # **我们自己的两段输出互相打架**，调用方只能二选一信，很容易信错那个。
    #
    # 这两句来自 UIA 控件树那条路；剪映这类自绘程序在那条路上本来就是空的。
    # 改法照 Codex 的建议：说清楚是**哪条路**没检测到，并把另一条路的真实结果摆上，
    # 但**不替它解释原因**——它的原话：「不要直接补上『该应用自绘』，除非有证据；
    # 自绘只是可能原因，焦点、权限、时序或检测实现也可能影响结果。」
    if "No active window found" in active_window or "No windows found" in windows:
        try:
            from windows_mcp.tools.window_ctl import _enum, _pid, _proc_name, _text
            seen = [(h, _proc_name(_pid(h)), _text(h)) for h in _enum()]
        except Exception:
            seen = []
        if seen:
            note = ("  [the UI-tree path found nothing here; the Win32 window list still sees %d "
                    "window(s), e.g. %s. Only the UI-tree path is blind - the screenshot above is "
                    "real, and Window(mode='list') / FindText / coordinates all still work. This "
                    "says which path came up empty, not why.]"
                    % (len(seen), ", ".join("%s %r" % (p, (t or "")[:20])
                                            for _h, p, t in seen[:3])))
            if "No active window found" in active_window:
                active_window = active_window + note
            if "No windows found" in windows:
                windows = windows + note
    active_desktop = desktop_state.active_desktop_to_string()
    all_desktops = desktop_state.desktops_to_string()
    if profile_enabled:
        metadata_render_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    screenshot_bytes = None
    if use_vision and desktop_state.screenshot is not None:
        # PATCH 0.5.17: 编码前把调色板量化到 256 色。
        # 9-15 实测：一张 911x705 的截图原样是 924 KB，量化后 198 KB（剩 22%），
        # 而肉眼对照（纸牌的点数/花色/红黑/人物牌花纹）**看不出差别**——
        # 界面截图的颜色数本来就少，多出来的字节买不到任何信息。
        # 只影响递给模型的那张图：我们自己的 OCR 和 ReadColor 读的是原始截图。
        from windows_mcp.tools.snapshot import _encode
        screenshot_bytes = _encode(desktop_state.screenshot)
    if profile_enabled:
        screenshot_encode_ms = (time.perf_counter() - stage_started_at) * 1000
        logger.info(
            "%s profile: desktop_state_ms=%.1f metadata_render_ms=%.1f png_encode_ms=%.1f total_ms=%.1f use_vision=%s use_dom=%s use_ui_tree=%s use_annotation=%s display=%s",
            tool_name,
            desktop_state_ms,
            metadata_render_ms,
            screenshot_encode_ms,
            (time.perf_counter() - profile_started_at) * 1000,
            use_vision,
            use_dom,
            use_ui_tree,
            use_annotation,
            display,
        )

    return {
        "desktop_state": desktop_state,
        "interactive_elements": interactive_elements,
        "scrollable_elements": scrollable_elements,
        "semantic_tree": semantic_tree,
        "windows": windows,
        "active_window": active_window,
        "active_desktop": active_desktop,
        "all_desktops": all_desktops,
        "screenshot_bytes": screenshot_bytes,
    }


def _win32_focus_line():
    """不靠 UIA，直接问 Win32 前台窗口是谁。UIA 树空时这是唯一还答得上来的一层。"""
    try:
        import ctypes
        from ctypes import wintypes
        from windows_mcp.tools.window_ctl import _pid, _proc_name, _rect, _text
        h = ctypes.windll.user32.GetForegroundWindow()
        if not h:
            return ("Win32 Focused Window: nothing has the foreground right now (the desktop "
                    "itself, or a window that just closed).\n")
        title = _text(h) or ""
        proc = _proc_name(_pid(h)) or "?"
        l, t, r, b = _rect(h)
        return ("Win32 Focused Window: %s '%s' hwnd=%d %dx%d @(%d,%d)\n"
                "  (The UIA control tree came back empty, so the lines above cannot tell you what "
                "is in front. This line does not use UIA - it asks Win32 directly, and it is "
                "correct. An empty tree usually means the app draws its own widgets instead of "
                "publishing them: Qt apps like JianYing, and embedded web panels like WeChat "
                "Moments. For those, drive it by what you can SEE: FindText / Click(text=) to "
                "locate, Screenshot(region=[l,t,r,b]) to look closely - that one hands you the "
                "screen coordinates of every line of text in the area.)\n"
                % (proc.replace(".exe", ""), title[:50], h, r - l, b - t, l, t))
    except Exception as e:
        return "Win32 Focused Window: could not be read (%s)\n" % type(e).__name__


def build_snapshot_response(
    capture_result: dict[str, object],
    *,
    include_ui_details: bool,
    ui_detail_note: str | None = None,
):
    desktop_state = capture_result["desktop_state"]
    interactive_elements = capture_result["interactive_elements"]
    scrollable_elements = capture_result["scrollable_elements"]
    semantic_tree = capture_result["semantic_tree"]
    windows = capture_result["windows"]
    active_window = capture_result["active_window"]
    active_desktop = capture_result["active_desktop"]
    all_desktops = capture_result["all_desktops"]
    screenshot_bytes = capture_result["screenshot_bytes"]

    # Some applications (e.g. VS Code) embed Unicode Private Use Area characters in the
    # Automation Element Name property of certain UI elements (e.g. navigation bar items in VS Code).
    # These characters can cause display issues, so we strip them out before rendering.
    interactive_elements = remove_private_use_chars(interactive_elements)
    scrollable_elements = remove_private_use_chars(scrollable_elements)
    semantic_tree = remove_private_use_chars(semantic_tree)

    def display_to_string(display):
        primary = " primary" if display.primary else ""
        return (
            f"{display.index}:{display.device_name} "
            f"{display.bounding_box.xyxy_to_string()}{primary}"
        )

    metadata_text = ""
    # PATCH 0.5.5: 给这张画面发一个 frame id，登记当时的窗口布局。
    # 之后 Click/Move/Scroll 传 frame= 回来，就能在点下去之前发现「画面已经变了」。
    try:
        from windows_mcp.tools.frame_guard import new_frame
        _fid = new_frame()
        metadata_text += (
            "Frame: %s - coordinates read off THIS picture are only valid while the windows stay put. "
            "Pass frame=\"%s\" to Click / Move / Scroll and they will refuse to fire if the window that "
            "owned your target has since moved, closed, or been covered. Coordinates you got from "
            "Find / FindText / Window are taken live, so leave frame= off for those.\n" % (_fid, _fid)
        )
    except Exception as e:
        metadata_text += "Frame: unavailable (%s)\n" % type(e).__name__
    if desktop_state.screenshot_original_size:
        orig = desktop_state.screenshot_original_size
        scale = desktop_state.screenshot_scale or 1.0
        sw, sh = orig.width, orig.height
        iw, ih = round(sw * scale), round(sh * scale)
        coord_scale = round(1.0 / scale, 6) if scale else 1.0
        if scale < 1.0:
            how = "downscaled by this tool: multiply image pixels by %s to get screen pixels." % coord_scale
        else:
            how = "1:1 with the screen."
        metadata_text += (
            "!! COORDINATES: Click / Move / Scroll / loc= all take SCREEN pixels. "
            "This screen is %dx%d. The image returned here is %dx%d - %s "
            "WARNING: your own image viewer may downscale it AGAIN - if the picture you actually look at "
            "is W pixels wide, multiply whatever you read off it by %d/W before passing it in. "
            "Before clicking a small target, sanity-check against a landmark such as the taskbar clock. "
            "Find / FindText / Window all report coordinates in this SAME screen-pixel frame, so a coordinate "
            "from any of them goes straight into Click with no conversion. Display DPI scale is %s - at 150%% a "
            "window rect read from a non-DPI-aware tool is 1.5x smaller than these numbers, which looks like a "
            "plausible coordinate rather than an error, so never mix the two.\n"
            % (sw, sh, iw, ih, how, sw, _dpi_scale_text())
        )
    metadata_text += f"Cursor Position: {desktop_state.cursor_position}\n"
    if desktop_state.available_displays:
        metadata_text += "Visible Displays: "
        metadata_text += "; ".join(
            display_to_string(display) for display in desktop_state.available_displays
        )
        metadata_text += "\n"
    if desktop_state.screenshot_displays:
        metadata_text += f"Selected Displays: {','.join(str(index) for index in desktop_state.screenshot_displays)}\n"
    if desktop_state.screenshot_region:
        metadata_text += f"Screenshot Region: {desktop_state.screenshot_region.xyxy_to_string()}\n"
        metadata_text += "Coordinate Space: Virtual desktop coordinates\n"
    if desktop_state.screenshot_backend:
        metadata_text += f"Screenshot Backend: {desktop_state.screenshot_backend}\n"
    if ui_detail_note:
        metadata_text += f"{ui_detail_note}\n"

    # PATCH 0.5.26：UIA 树空的时候，至少把 Win32 眼里的前台窗口报出来。
    #
    # WorkBuddy 9-15 在剪映上的 P1-3：控件树返回空、`Focused Window` 也跟着空，
    # 结果它连"现在前面是哪个程序"都不知道。可这件事根本不需要 UIA——
    # `GetForegroundWindow` 一直答得上来。它的原话是「工具说不知道，
    # 但我从截图上明明看得见剪映在最前面」：**工具比调用方知道得还少**，
    # 那就是我们的问题，不是它的。
    #
    # Qt 自绘（剪映）和内嵌 CEF（微信朋友圈）都是这样：进程和窗口都在，
    # 只是不往 UIA 里报控件。所以除了报出是谁，还要说清"往哪走"。
    _tree_empty = not (semantic_tree or "").strip()
    _focus_empty = not str(active_window or "").strip() or _tree_empty
    if _focus_empty:
        metadata_text += _win32_focus_line()

    response_text = dedent(f'''
    {metadata_text}
    Active Desktop:
    {active_desktop}

    All Desktops:
    {all_desktops}

    Focused Window:
    {active_window}

    Opened Windows:
    {windows}
    ''')
    if include_ui_details:
        response_text += dedent(f'''

    UI Tree:
    {semantic_tree or "No elements found."}''')

    response = [response_text]
    if screenshot_bytes:
        response.append(Image(data=screenshot_bytes, format="png"))
    return response
