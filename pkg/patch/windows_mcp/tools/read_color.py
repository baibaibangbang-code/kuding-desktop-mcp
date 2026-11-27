"""PATCH 0.5.16：读屏幕上某处的颜色。

9-15 用我们自己这套工具玩微软纸牌时撞出来的缺口：
**牌的红黑我只能靠模型自己看图判断，工具一点忙帮不上。**

这不是游戏专属需求。界面里有大量状态是**只用颜色编码**的：
按钮启用还是灰掉、开关开还是关、选中态、必填项标红、进度条、
状态点（绿=在线/红=断开）、剪映时间轴上被选中的片段、微信的未读红点。
`FindText` 读不到（没有文字），`Find` 读不到（自绘界面没控件树），
`FindElements` 只能在一维条带上找边界。**颜色这一维我们整个是瞎的。**

几个刻意的设计：

- **不做「单个像素」**。一个像素是噪声：抗锯齿的边缘、渐变、半透明叠加，
  随便挪一格就是另一个值。默认取一小块的**主色**，并告诉调用方这个主色占多少——
  占比低就说明那块地方本来就不是纯色，**这个数字比颜色本身更能说明问题**。
- **能按窗口读**（`hwnd=`）。走 `capture_window` 的 `PrintWindow`，
  所以**被别的窗口盖住、或者在后台的窗口照样读得到**，和 `FindText` 那条路一致。
- **一次可以问多个点**。判断「这两个按钮是不是同一个状态」要比较，
  分两次调用中间界面可能已经变了。
- **返回颜色名**。模型拿到 `#D32F2F` 不如拿到 `red` 直接；但十六进制也一并给，
  精确比较还得靠它。
"""

from typing import Optional

from mcp.types import ToolAnnotations
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics

# 常见界面色的参考点。只用来起个名字，不用来做判断——
# 判断该由调用方拿十六进制自己比。
_NAMES = (
    ("black", (0, 0, 0)), ("white", (255, 255, 255)),
    ("gray", (128, 128, 128)), ("silver", (200, 200, 200)),
    ("dark gray", (64, 64, 64)),
    ("red", (220, 40, 40)), ("dark red", (140, 20, 20)),
    ("orange", (245, 150, 30)), ("yellow", (245, 220, 60)),
    ("green", (60, 175, 75)), ("dark green", (25, 100, 40)),
    ("cyan", (60, 200, 210)), ("blue", (40, 110, 220)),
    ("dark blue", (20, 50, 130)), ("purple", (140, 70, 190)),
    ("pink", (240, 140, 180)), ("brown", (130, 85, 45)),
    ("beige", (235, 225, 200)),
)


def name_of(rgb):
    r, g, b = rgb
    best, bd = "?", None
    for nm, (cr, cg, cb) in _NAMES:
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if bd is None or d < bd:
            best, bd = nm, d
    return best


def hex_of(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


def centroid_of(img, target, quant=24):
    """这个颜色出现在这一小块的什么位置。返回 (包围框, 质心)，都是图内坐标。

    opencode 提的：只知道"有 9% 的红"不够，**要知道红在哪**——
    牌角标的红和背景高亮的红、人物图案的红，判断完全不同。
    """
    px = img.convert("RGB").load()
    w, h = img.size
    tk = (target[0] // quant, target[1] // quant, target[2] // quant)
    xs, ys = [], []
    for y in range(0, h, max(1, h // 80) if h > 80 else 1):
        for x in range(0, w, max(1, w // 80) if w > 80 else 1):
            r, g, b = px[x, y]
            if (r // quant, g // quant, b // quant) == tk:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None, None
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1), \
           (sum(xs) // len(xs), sum(ys) // len(ys))


def dominant(img, quant=24):
    """一小块里的主色。

    先把每个通道量化到 `quant` 的格子里再统计——不量化的话，
    渐变和抗锯齿会让几百个几乎一样的颜色各算各的，谁也占不到多数，
    结果就是「主色占 2%」这种没意义的答案。
    """
    px = list(img.convert("RGB").getdata())
    if not px:
        return (0, 0, 0), 0.0, []
    buckets = {}
    for r, g, b in px:
        k = (r // quant, g // quant, b // quant)
        s = buckets.get(k)
        if s is None:
            buckets[k] = [1, r, g, b]
        else:
            s[0] += 1
            s[1] += r
            s[2] += g
            s[3] += b
    ranked = sorted(buckets.values(), key=lambda s: -s[0])
    total = float(len(px))
    out = []
    for s in ranked[:3]:
        n = s[0]
        out.append((((s[1] // n), (s[2] // n), (s[3] // n)), n / total))
    return out[0][0], out[0][1], out


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="ReadColor",
        description=(
            "Reads the colour of one or more spots on screen - the one dimension the other "
            "tools are blind to. Use it for state that an interface encodes ONLY as colour: a "
            "button greyed out vs enabled, a toggle on vs off, a selected row, a field marked "
            "red for invalid, a status dot (green=online, red=down), an unread badge, a "
            "progress bar, a red vs black playing card. FindText needs text, Find needs a UI "
            "tree, FindElements only finds edges - none of them can tell you a colour. "
            "Pass loc=[x,y] for one spot, or points=[[x1,y1],[x2,y2]] to sample several in one "
            "call (compare two controls without the screen changing between calls), or "
            "region=[l,t,r,b] for an area. Add hwnd=/window=/process= to read a specific "
            "window - that path uses PrintWindow, so it works even when the window is covered "
            "or in the background. It reports the DOMINANT colour of a small patch, never a "
            "single pixel (one pixel is noise from anti-aliasing and gradients), together with "
            "the share that colour holds: a low share means the spot is not a flat colour, and "
            "that number tells you more than the colour does. Both a hex value and a rough "
            "name are returned; compare hex values, read the name."
        ),
        annotations=ToolAnnotations(
            title="ReadColor",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "ReadColor-Tool")
    def read_color_tool(
        loc: Optional[list] = None,
        points: Optional[list] = None,
        region: Optional[list] = None,
        radius: int = 4,
        hwnd: Optional[int] = None,
        window: Optional[str] = None,
        process: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        from PIL import ImageGrab
        from windows_mcp.tools.window_ctl import capture_window, resolve_target

        radius = max(1, min(int(radius or 4), 60))
        target = None
        origin = (0, 0)
        img = None

        if hwnd or window or process:
            target, why = resolve_target(title=window, process=process, hwnd=hwnd)
            if not target:
                return "ReadColor: %s" % why
            img, box = capture_window(target)
            origin = (box[0], box[1])

        def patch(x, y):
            """取以 (x,y) 为中心的一小块。坐标一律是**屏幕坐标**，
            按窗口读时在这里换算成窗口内坐标——让调用方永远只用屏幕坐标，
            少一次换算就少一个出错的地方。"""
            if img is not None:
                cx, cy = x - origin[0], y - origin[1]
                l, t = max(0, cx - radius), max(0, cy - radius)
                r, b = min(img.width, cx + radius + 1), min(img.height, cy + radius + 1)
                if r <= l or b <= t:
                    return None
                return img.crop((l, t, r, b))
            return ImageGrab.grab(bbox=(x - radius, y - radius,
                                        x + radius + 1, y + radius + 1),
                                  all_screens=True)

        spots = []
        if region:
            try:
                l, t, r, b = [int(v) for v in region]
            except Exception:
                return "ReadColor: region must be [left, top, right, bottom]."
            if img is not None:
                im = img.crop((max(0, l - origin[0]), max(0, t - origin[1]),
                               max(1, r - origin[0]), max(1, b - origin[1])))
            else:
                im = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
            rgb, share, top = dominant(im)
            lines = ["ReadColor region [%d,%d,%d,%d] (%dx%d px):" % (l, t, r, b,
                                                                     r - l, b - t)]
            ox2 = l if img is None else l
            oy2 = t if img is None else t
            for c, sh in top:
                box, cen = centroid_of(im, c)
                where = ""
                if box and cen:
                    # 位置一律换算回**屏幕坐标**，和调用方手上的其它坐标同一套
                    where = ("  at [%d,%d,%d,%d] centre (%d,%d)"
                             % (ox2 + box[0], oy2 + box[1], ox2 + box[2], oy2 + box[3],
                                ox2 + cen[0], oy2 + cen[1]))
                lines.append("  %s  %s  %.0f%% of the area%s"
                             % (hex_of(c), name_of(c), sh * 100, where))
            lines.append("The box and centre tell you WHERE that colour is - a red that sits in "
                         "the top-left corner is a card's rank marker; a red spread across the "
                         "whole box is a background or a highlight. The share alone cannot tell "
                         "those apart.")
            return "\n".join(lines)

        if loc:
            spots.append(loc)
        if points:
            spots.extend(points)
        if not spots:
            return "ReadColor: pass loc=[x,y], points=[[x,y],...] or region=[l,t,r,b]."

        scope = ("window hwnd=%s" % target) if target else "the screen"
        out = ["ReadColor on %s, %dx%d patch around each point:"
               % (scope, radius * 2 + 1, radius * 2 + 1)]
        for sp in spots:
            try:
                x, y = int(sp[0]), int(sp[1])
            except Exception:
                out.append("  %r is not [x, y]" % (sp,))
                continue
            im = patch(x, y)
            if im is None:
                out.append("  (%d,%d) is outside that window" % (x, y))
                continue
            rgb, share, _top = dominant(im)
            flat = "flat" if share >= 0.7 else ("mixed" if share >= 0.35 else "very mixed")
            out.append("  (%d,%d)  %s  %s  dominant %.0f%% (%s)"
                       % (x, y, hex_of(rgb), name_of(rgb), share * 100, flat))
        if len(spots) > 1:
            out.append("Compare the hex values, not the names - two different states can land "
                       "on the same rough name.")
        return "\n".join(out)
