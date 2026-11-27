"""Snapshot and Screenshot tools — desktop state capture."""

import logging

from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context

from windows_mcp.tools._snapshot_helpers import (
    _as_bool,
    capture_desktop_state,
    build_snapshot_response,
)

logger = logging.getLogger(__name__)

# Populated by register(); exposed for backward-compatible test imports.
state_tool = None
screenshot_tool = None


def register(mcp, *, get_desktop, get_analytics):
    global state_tool, screenshot_tool
    @mcp.tool(
        name='Snapshot',
        description="Take a screenshot and inspect the screen. Keywords: screenshot, screen capture, see screen, observe, look, inspect, UI elements, what's on screen. Captures complete desktop state including: system language, focused/opened windows, interactive elements (buttons, text fields, links, menus with coordinates), and scrollable areas. Set use_vision=True to include screenshot with cursor highlight. Set use_annotation=False to get a clean screenshot without bounding box overlays on UI elements (default: True, draws colored rectangles around detected elements). Set use_ui_tree=False for a faster screenshot-only snapshot when you do not need interactive or scrollable element extraction. Set width_reference_lines/height_reference_lines to overlay a grid for better spatial reasoning (make sure vision is enabled to use it). Set use_dom=True for browser content to get web page elements instead of browser UI. Set display=[0] or display=[0,1] using zero-based active Windows display indices; omit it to keep the default full-desktop behavior. Always call this first to understand the current desktop state before taking actions. foreground_only DEFAULTS TO TRUE: the UI tree is walked for the focused window only, which is what you almost always want - the opened-windows list is returned either way. Pass foreground_only=False to walk every window, but expect it to be slow and to sometimes fail outright on a busy desktop (a deep UI tree can exhaust Python's recursion limit); scope with Find('process:...') instead whenever you can.",
        annotations=ToolAnnotations(
            title="Snapshot",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "State-Tool")
    def _state_tool(
        use_vision: bool | str = False,
        use_dom: bool | str = False,
        use_annotation: bool | str = True,
        use_ui_tree: bool | str = True,
        width_reference_line: int | None = None,
        height_reference_line: int | None = None,
        display: list[int] | None = None,
        foreground_only: bool | str = True,
        ctx: Context = None,
    ):
        # PATCH 0.5.6: foreground_only 默认从 False 改成 True。
        #
        # 9-15 opencode 跑剪映时报「整屏 Snapshot 返回无窗口」。复现下来不是返回空，
        # 是**整个调用崩了**：全桌面遍历先对某个窗口连报 4 次
        # UIA_E_ELEMENTNOTAVAILABLE，接着 tree_traversal 抛
        # RecursionError（Python 默认递归上限 1000，UIA 树比这深），
        # 错误刷屏刷出 64MB+，客户端那头只看到超时/空结果。
        #
        # 而我们 SKILL 的第 0 条本来就写着「先限定范围，全桌面扫描又慢又漏」——
        # 工具的默认值却正好相反，等于给所有人埋了个坑。现在让默认值和规矩一致。
        # 要旧行为就显式写 foreground_only=False（窗口列表两种模式下都照常返回）。
        try:
            capture_result = capture_desktop_state(
                get_desktop(),
                use_vision=_as_bool(use_vision),
                use_dom=_as_bool(use_dom),
                use_annotation=_as_bool(use_annotation),
                use_ui_tree=_as_bool(use_ui_tree),
                foreground_only=_as_bool(foreground_only),
                width_reference_line=width_reference_line,
                height_reference_line=height_reference_line,
                display=display,
                tool_name="Snapshot tool",
            )
        except Exception as e:
            logger.warning(
                "Snapshot failed with display=%s use_vision=%s use_dom=%s",
                display,
                use_vision if 'use_vision' in locals() else None,
                use_dom if 'use_dom' in locals() else None,
                exc_info=True,
            )
            return [f'Error capturing desktop state: {str(e)}. Please try again.']

        return build_snapshot_response(capture_result, include_ui_details=True)

    @mcp.tool(
        name='Screenshot',
        description="Captures a fast screenshot-first desktop snapshot with cursor position, desktop/window summaries, and an image. This path skips UI tree extraction for speed. Use Snapshot when you need interactive element ids, scrollable regions, or browser DOM extraction. Note: the returned image may be downscaled for efficiency; when it is, multiply image coordinates by the ratio of original size to displayed size to get the actual screen coordinates for mouse actions (Click, Move, etc.). Pass region=[left,top,right,bottom] in SCREEN pixels to look closely at one area - and a region shot also comes back with TARGETS: every line of text OCR can read in that area, with its SCREEN coordinates already worked out, so you can click one without measuring pixels off the picture. Pass targets=True to get that list for a whole window too, or targets=False to suppress it.",
        annotations=ToolAnnotations(
            title="Screenshot",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Screenshot-Tool")
    def _screenshot_tool(
        use_annotation: bool | str = False,
        width_reference_line: int | None = None,
        height_reference_line: int | None = None,
        display: list[int] | None = None,
        window: str | None = None,
        process: str | None = None,
        hwnd: int | None = None,
        nth: int | None = None,
        region: list | str | None = None,
        targets: bool | None = None,
        ctx: Context = None,
    ):
        # PATCH 0.5.6: 给 Screenshot 加按窗口拍的能力。
        #
        # 之前只能拍整个桌面。可我们**内部早就有** capture_window()（PrintWindow，
        # 被别的窗口盖住也拍得到），FindText / ReadText / FindElements 一直在用它，
        # 却从来没把那张图递给模型——于是有视觉的客户端只能看整屏，
        # 看不到被遮挡或在后台的窗口，等于白白浪费了已有的能力。
        #
        # 维护者 9-14 点破的：我们不做模型，是组合现成的模型和工具；
        # 客户端的模型有没有视觉是它的事，我们该做的是**把能看的东西递到它面前**。
        #
        # 按窗口拍还顺带两个好处：图小很多（省 token、单位像素更清楚），
        # 而且不用把窗口提到前面来——不打扰人正在做的事。
        # PATCH 0.5.16: 加按区域截。
        # 9-15 玩纸牌时撞到的：想看清一个角落（一张牌的花色），
        # 只能整窗来一张图——费 token，而且缩放后那个角落反而更糊。
        # region 一律是**屏幕坐标**，和 FindText / Window 返回的是同一套，
        # 调用方不用做任何换算。
        from windows_mcp.tools.ocr_find import _as_box
        region = _as_box(region)
        # PATCH 0.5.26：区域截图默认附一份"这块里有什么、屏幕坐标是多少"。
        #
        # Codex 和 WorkBuddy 9-15 各自独立提的同一条 P1：拿到区域图之后，
        # 还得自己在图上量像素、再加回左上角偏移。那是这套工具里最容易错的一步——
        # 而且错了不报错，只是点偏。这份换算我们内部本来就做（FindText 一直在做），
        # 只是从没交出来过。
        #
        # 只在**区域截图**上默认开：要区域图就是要在那块里动手，而且区域小、
        # OCR 快、行数少。整屏/整窗默认不开——几十上百行文字，比图还贵。
        want = targets if targets is not None else bool(region)
        if window or process or hwnd:
            return _window_shot(window, process, hwnd, nth, region, want)
        if region:
            return _region_shot(region, want)
        try:
            capture_result = capture_desktop_state(
                get_desktop(),
                use_vision=True,
                use_dom=False,
                use_annotation=_as_bool(use_annotation),
                use_ui_tree=False,
                width_reference_line=width_reference_line,
                height_reference_line=height_reference_line,
                display=display,
                tool_name="Screenshot tool",
            )
        except Exception as e:
            logger.warning(
                "Screenshot failed with display=%s",
                display,
                exc_info=True,
            )
            return [f'Error capturing screenshot: {str(e)}. Please try again.']

        return build_snapshot_response(
            capture_result,
            include_ui_details=False,
            ui_detail_note="UI Tree: Skipped for fast screenshot-only capture. Call Snapshot when you need interactive or scrollable elements.",
        )

    state_tool = _state_tool
    screenshot_tool = _screenshot_tool


def _already_have(hwnd, region):
    """这张图调用方刚拿到过吗？拿到过就直说，别让它闷头再花一次。

    9-15 量的账：一张整窗截图约 1074 token，一次 Window(list) 约 1084 token。
    opencode 那次 227 分钟的会话截了 93 次图、列了 77 次窗口，
    上下文中位数因此堆到 10.8 万——**而上下文越大，它每一轮想得越久**，
    最后 96% 的时间花在推理上，工具只占 4%。

    它的固定套路是 ClickAndWatch 之后再补一张 Screenshot，
    可 ClickAndWatch 已经把变化那块的图附回去了。
    它不是笨，是**没人告诉它手上已经有了**。工具知道，就该由工具说。

    只在很窄的条件下提醒：同一个窗口、25 秒内、这次还没指定 region。
    条件放宽就会变成聒噪，而聒噪的提示会被整体忽略——连真有用的那次也一起丢掉。
    """
    import time as _t
    try:
        from windows_mcp.tools.watch_change import LAST_PICTURE
    except Exception:
        return ""
    if region or not hwnd or not LAST_PICTURE.get("hwnd"):
        return ""
    try:
        if int(LAST_PICTURE["hwnd"]) != int(hwnd):
            return ""
    except Exception:
        return ""
    ago = _t.time() - float(LAST_PICTURE.get("at") or 0)
    if ago > 25:
        return ""
    return ("NOTE: ClickAndWatch already handed you a picture of %s in this window %.0fs ago, "
            "and a full-window screenshot costs roughly five times what that crop did. If you "
            "only needed to see the result of that click, you already had it.\n"
            % (LAST_PICTURE.get("what") or "the change", ago))


def _encode(im, full_color=False):
    """把图编码成 PNG。默认先把调色板量化到 256 色。

    **为什么**：9-15 量 Codex 的节奏时顺手量了我们自己递出去的图——
    一张 911x705 的窗口截图要 **924 KB**，base64 之后 1.2 MB 进对话。
    界面截图的颜色数本来就少，量化到 256 色之后 **199 KB，剩 22%**，
    而肉眼对照（纸牌的点数、花色、红黑、人物牌花纹）**看不出差别**，
    只有纯色背景上多了点抖动纹理，那块本来就不携带信息。

    **只影响递给模型的那张图**：我们自己的 OCR（FindText/ReadText）和 ReadColor
    读的都是原始截图，不经过这一步，所以识别精度一点不受影响。

    留了 `full_color=True` 的出口：真要看渐变、看照片、看细微色差时用得上。
    默认压缩、按需还原，比反过来强——**绝大多数截图是界面，不是照片**。
    """
    import io as _io
    buf = _io.BytesIO()
    if full_color:
        im.save(buf, format="PNG")
    else:
        try:
            # FASTOCTREE：快，而且对 RGB 界面图的表现比中位切分好
            im.convert("RGB").quantize(colors=256, method=2).save(
                buf, format="PNG", optimize=True)
        except Exception:
            buf = _io.BytesIO()
            im.save(buf, format="PNG")
    data = buf.getvalue()
    buf.close()
    return data


def _region_shot(region, want_targets=False):
    """整屏模式下只截指定的一块。"""
    import io as _io

    from PIL import ImageGrab
    from fastmcp.utilities.types import Image

    try:
        l, t, r, b = [int(v) for v in region]
    except Exception:
        return ["Screenshot: region must be [left, top, right, bottom] in screen pixels."]
    if r - l < 2 or b - t < 2:
        return ["Screenshot: region [%d,%d,%d,%d] is too small." % (l, t, r, b)]
    im = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    data = _encode(im)
    tb = ""
    if want_targets:
        from windows_mcp.tools.ocr_find import targets_block
        tb = targets_block(region=(l, t, r, b))
    return ["Screenshot of region [%d,%d,%d,%d] (%dx%d px, 1:1 with the screen). "
            "Coordinates you read off this image must have (%d,%d) added back to become "
            "screen coordinates.\n%s" % (l, t, r, b, im.width, im.height, l, t, tb),
            Image(data=data, format="png")]


def _crop_note(im, region, ox, oy):
    """把屏幕坐标的 region 换算成图内坐标并裁掉。返回 (裁好的图, 说明)。

    调用方永远只用**屏幕坐标**——它手上的坐标都是 FindText / Window 给的屏幕坐标，
    让它再去换算一次窗口内坐标，就是凭空多一个出错的地方。
    """
    try:
        l, t, r, b = [int(v) for v in region]
    except Exception:
        return im, "  (region was not [left, top, right, bottom]; ignored)"
    cl, ct = max(0, l - ox), max(0, t - oy)
    cr, cb = min(im.width, r - ox), min(im.height, b - oy)
    if cr - cl < 2 or cb - ct < 2:
        return im, ("  (region [%d,%d,%d,%d] does not overlap what was captured; "
                    "showing the whole thing instead)" % (l, t, r, b))
    return im.crop((cl, ct, cr, cb)), ("  Cropped to [%d,%d,%d,%d] in SCREEN coordinates."
                                       % (l, t, r, b))


def _window_shot(window, process, hwnd, nth, region=None, want_targets=False):
    """只拍一个窗口自己的画面，返回 [说明文字, 图]。"""
    import io as _io

    from fastmcp.utilities.types import Image
    from windows_mcp.tools.ocr_find import _grab_for
    from windows_mcp.tools.window_ctl import _describe
    from windows_mcp.tools._snapshot_helpers import _dpi_scale_text

    im, (ox, oy), target, note = _grab_for(window, process, hwnd, nth, None)
    if im is None:
        return ["Screenshot could not read that window: %s" % note]

    crop_note = ""
    if region:
        im, crop_note = _crop_note(im, region, ox, oy)
        ox, oy = max(ox, int(region[0])), max(oy, int(region[1]))

    d = _describe(target) if target else {"title": "?", "process": "?"}

    # 大窗口原尺寸发出去太费 token。用和整屏截图同一条规则缩：**只允许 1/N**，
    # 这样换算系数一定是整数，不会出现 1.4815 那种要乘完再取整的数（9-14 那一刀的由头）。
    from windows_mcp.tools._snapshot_helpers import _snap_scale
    # 明确要了 region 就**不缩**——除非裁出来的那块本身还超上限。
    #
    # 千问办公 9-15 排的第 2 条：同样是一块 ~1000x1024 的区域，
    # `region=[59,96,980,1120]`（不带 hwnd，走整屏那条路）是 1:1，
    # 而 `region=[900,96,1881,1120]`（带 hwnd，走这条路）被缩了，
    # 于是它把 Q♦ 的 y 算错约 160px，第一次拖拽弹回原位。
    # 同一个参数、相近的尺寸、两种待遇——这是我们自己制造的坐标陷阱。
    #
    # 而且"要一小块"这个动作本身就在说：我要看清楚这里。
    # 把它缩小既违背这个意图，又凭空多出第二个坐标系。
    scale = _snap_scale(im.width, im.height, 1.0)
    if region and scale < 1.0 and im.width <= 2600 and im.height <= 2600:
        scale = 1.0
    if scale < 1.0:
        im = im.resize((round(im.width * scale), round(im.height * scale)))

    data = _encode(im)

    # 9-15 opencode 跑朋友圈时提的：整屏截图有 Frame: 行，按窗口截图没有，
    # 于是 frame= 这条防过期坐标的路在按窗口截图上用不了。补上，两条路一致。
    try:
        from windows_mcp.tools.frame_guard import new_frame
        fid = new_frame(target)
        frame_line = ("Frame: %s - pass frame=\"%s\" to Click / Type / Move / Scroll and they refuse "
                      "to fire if the window that owned your target has moved, closed or been covered "
                      "since this picture.\n" % (fid, fid))
    except Exception as e:
        frame_line = "Frame: unavailable (%s)\n" % type(e).__name__

    shrunk_warning = ""
    if scale < 1.0:
        # 藏在 COORDINATES 那行里不够——千问办公看漏了一次，代价是一次错点。
        # 缩放是这份返回里**最容易让人算错坐标**的一件事，就该排在最前面。
        shrunk_warning = ("!! THIS PICTURE IS SHRUNK TO 1/%d. Every coordinate you read off it "
                          "must be multiplied by %d before it means anything on screen.\n"
                          % (round(1 / scale), round(1 / scale)))
    txt = (
        shrunk_warning +
        frame_line +
        _already_have(hwnd, region) +
        # 裁过就必须说。不说的话调用方会以为拿到的是整窗，
        # 而坐标换算的基准已经跟着变了——量出来的坐标会全错。
        ((crop_note.strip() + "\n") if crop_note else "") +
        "Screenshot of ONE window: %s '%s'  hwnd=%s\n"
        "Image is %dx%d and it is the window's OWN content, rendered by the window itself - "
        "so this works even while another window covers it, and nothing was brought to the front.\n"
        "!! COORDINATES: screen_x = %d + image_x * %d,  screen_y = %d + image_y * %d.\n"
        "   (%s The offset is where this window sits on screen.)\n"
        "!! Readable does not mean clickable: if another window is on top, a click at those screen "
        "coordinates hits that other window. Check with Window(mode='at', x=, y=) first, or bring "
        "this one to the front.\n"
        "Display DPI scale is %s.\n"
        % (d.get("process"), (d.get("title") or "")[:40], target, im.width, im.height,
           ox, int(round(1 / scale)), oy, int(round(1 / scale)),
           "The image is 1:1 with screen pixels." if scale >= 1.0 else
           "The image was shrunk to 1/%d, so multiply by %d." % (round(1 / scale), round(1 / scale)),
           _dpi_scale_text())
    )
    if want_targets:
        from windows_mcp.tools.ocr_find import targets_block
        # 裁过就按裁出来的那块屏幕矩形读；没裁就按整个窗口自绘的画面读，
        # 后者能读到被遮挡的内容——这正是按窗口截图的意义所在。
        if region:
            txt += targets_block(region=tuple(int(v) for v in region))
        else:
            txt += targets_block(hwnd=target)
    return [txt, Image(data=data, format="png")]
