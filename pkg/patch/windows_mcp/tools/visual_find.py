"""PATCH 0.5.6: FindElements —— 找出画面上「有东西」的位置，不认它是什么。

为什么要这个：自绘窗口里大量可点的目标**没有文字**（微信的相机/垃圾桶图标），
还有一些**有文字但 OCR 读不出来**（微信的绿底白字「发表」按钮，实测连旁边浅底黑字的
「取消」也读不到）。这两类目标此前只能靠人事先量好坐标写进文档——
而坐标会随窗口移动、改版、换分辨率失效，等于把当天的界面焊进文档里。

维护者 9-14 的原话：「你把可计算坐标做死不能解决通用能力，下次换个任务或者微信改版又不行了。」
对。所以这里换个思路：**不认图标长什么样，只认「这块和背景不一样」**。

    画面 → 灰度 → 出现次数最多的灰度算背景 → 差异超阈值的算前景
         → 按列投影成区间 → 间隔小于 gap 的并成一簇 → 每簇就是一个视觉元素

笨，但正因为笨才通用：它没有任何「知识」可以过期。

9-14 实测（三个应用、四类目标，全部准到 1 像素，无任何预设坐标）：

| 目标                | OCR 能读到 | 本算法      | 手工实测    |
|---------------------|-----------|-------------|-------------|
| 微信朋友圈相机图标   | 否        | (1997,463)  | (1998,464)  |
| 记事本菜单三项       | 只读到1个  | 107/191/275 | 108/192/276 |
| 微信「发表」按钮     | 否        | (967,1016)  | (968,1015)  |
| 微信「取消」按钮     | 否        | (1147,1016) | (1147,1015) |

配合 OCR 就有了语义：和 OCR 文字框重叠的簇是文字，不重叠的是图标。
于是「1分钟前 右边那个图标」变成可计算的，不需要谁事先量好告诉它。
"""

import numpy as np


def visual_elements(gray, tol=28, gap=8, min_w=5, min_h=5, axis="x"):
    """在一张灰度图里找视觉元素。纯函数，不碰桌面，方便用合成图做确定性测试。

    gray: 二维 numpy 数组（uint8/int16 均可）
    返回 [(x1, y1, x2, y2), ...]，坐标相对这张图。

    tol  与背景的灰度差多少算前景。太小会把抗锯齿边缘算进来，太大会漏掉浅色图标。
    gap  两个区间相隔多少像素以内算同一个元素。一个图标内部的笔画是断开的，
         必须合并；但间隔太大又会把相邻的两个图标粘成一个。
    axis "x" = 按列投影，找**并排**的东西（工具栏、按钮行）——最常用。
         "y" = 按行投影，找**上下叠**的东西（侧边栏、列表项）。

    **这个算法只对「一条」有效，别拿整窗口喂它。** 9-14 剪映实测：整窗口按列投影时
    几乎每一列都有前景，全部粘成 3 个巨块。调用方要先把范围收窄到一条横条或竖条——
    工具里的 near_text 就是这么做的（按锚点文字自动切出那一条）。
    """
    if gray is None or gray.size == 0:
        return []
    g = np.asarray(gray, dtype=np.int16)
    if g.ndim != 2:
        raise ValueError("visual_elements expects a 2-D grayscale array")
    if axis == "y":
        # 转置一下复用同一套逻辑，出来的框再转回去
        flipped = visual_elements(g.T, tol=tol, gap=gap, min_w=min_h, min_h=min_w, axis="x")
        return [(y1, x1, y2, x2) for (x1, y1, x2, y2) in flipped]

    vals, cnt = np.unique(g, return_counts=True)
    bg = int(vals[int(np.argmax(cnt))])
    fg = np.abs(g - bg) > tol
    if not fg.any():
        return []

    cols = fg.any(axis=0)
    runs, start = [], None
    for i, on in enumerate(cols):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(cols) - 1))

    merged = []
    for a, z in runs:
        if merged and a - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], z)
        else:
            merged.append((a, z))

    out = []
    for a, z in merged:
        rows = np.where(fg[:, a:z + 1].any(axis=1))[0]
        if rows.size == 0:
            continue
        y1, y2 = int(rows[0]), int(rows[-1])
        if (z - a + 1) < min_w or (y2 - y1 + 1) < min_h:
            continue
        out.append((int(a), y1, int(z), y2))
    return out


def classify(elements, ocr_boxes, max_side=64, long_side=40, elongation=3.0):
    """给每个元素贴个标签：text / icon / other。

    text  —— 和某个 OCR 文字框有重叠（说明这块是字，OCR 已经认得）
    icon  —— 不是文字的**小东西**，也就是通常可以点的那些
    other —— 大块（图片、色块）和又长又细的东西（滚动条、分隔线、窗口圆角边框）

    ocr_boxes 是同一坐标系下的 [(x1,y1,x2,y2), ...]。
    分「不是文字」这一类才是重点：**OCR 读不出的东西，正是此前完全抓不到的那些。**

    判据不用「像不像方块」——9-14 在剪映首页实测踩到的：
    「⋯」更多按钮是 18x5，扁得很，按方块判会被扔进 other，用默认 kind="icon" 就漏了。
    改成按**大小**分：小的就是图标；只有「够长 + 长宽悬殊」才算滚动条那一类。
    对照过的真实尺寸：相机 26x20、发表按钮 41x20、下拉箭头 12x7、「⋯」18x5 → 都该是 icon；
    滚动条 17x55、对话框圆角边 40x90 → 都该是 other。
    """
    def overlaps(e, b):
        return not (e[2] < b[0] or e[0] > b[2] or e[3] < b[1] or e[1] > b[3])

    out = []
    for e in elements:
        w, h = e[2] - e[0] + 1, e[3] - e[1] + 1
        lo, hi = min(w, h), max(w, h)
        if any(overlaps(e, b) for b in (ocr_boxes or ())):
            kind = "text"
        elif hi > max_side:
            kind = "other"                      # 太大，是图片/色块/边框
        elif hi >= long_side and hi / max(1, lo) > elongation:
            kind = "other"                      # 又长又细，是滚动条/分隔线
        else:
            kind = "icon"
        out.append((e[0], e[1], e[2], e[3], kind))
    return out


def pick_near(elements, anchor, direction="right", kinds=("icon",)):
    """挑出锚点某一侧最近的元素。

    anchor 是 (x1,y1,x2,y2)，通常来自 OCR 的文字框。
    这是「1分钟前 右边那个图标」这类表达的实现：**按边缘算，不按中心算。**
    中心会随文字长短漂移——「1分钟前」和「23分钟前」宽度不同，中心差好几像素，
    而右边缘到图标的距离是稳定的。9-14 就是在这上面栽过一次。
    """
    ax1, ay1, ax2, ay2 = anchor
    acy = (ay1 + ay2) / 2.0
    cands = []
    for e in elements:
        kind = e[4] if len(e) > 4 else None
        if kinds and kind is not None and kind not in kinds:
            continue
        cy = (e[1] + e[3]) / 2.0
        cx = (e[0] + e[2]) / 2.0
        if direction == "right" and e[0] > ax2 and abs(cy - acy) <= max(14, (ay2 - ay1)):
            cands.append((e[0] - ax2, e))
        elif direction == "left" and e[2] < ax1 and abs(cy - acy) <= max(14, (ay2 - ay1)):
            cands.append((ax1 - e[2], e))
        elif direction == "below" and e[1] > ay2 and abs(cx - (ax1 + ax2) / 2.0) <= 200:
            cands.append((e[1] - ay2, e))
        elif direction == "above" and e[3] < ay1 and abs(cx - (ax1 + ax2) / 2.0) <= 200:
            cands.append((ay1 - e[3], e))
    if not cands:
        return None
    cands.sort(key=lambda p: p[0])
    return cands[0][1]


def register(mcp, *, get_desktop, get_analytics):
    from typing import Annotated

    from mcp.types import ToolAnnotations
    from pydantic import Field
    from fastmcp import Context
    from windows_mcp.infrastructure import with_analytics

    @mcp.tool(
        name="FindElements",
        description=(
            "Find the CLICKABLE THINGS on a window - including the ones that have no text at all "
            "(icons) and the ones OCR cannot read (white-on-green buttons). Use this when Find "
            "returns nothing because the window is self-drawn AND FindText returns nothing because "
            "the target is an icon or an unreadable button. It does not recognise what an icon is; "
            "it reports where something sits that differs from the background, which is exactly what "
            "you need in order to click it. Combine with near_text= to say 'the icon just right of "
            "this label' - that survives window moves, resizes and app redesigns, unlike a "
            "coordinate someone measured once. Centres come back as SCREEN pixels, ready for Click."
        ),
        annotations=ToolAnnotations(title="FindElements", readOnlyHint=True,
                                    destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    @with_analytics(get_analytics(), "FindElements-Tool")
    def find_elements_tool(
        window: Annotated[str | None, Field(description="Read THIS window's own content (substring of its title); works even when it is covered.")] = None,
        process: Annotated[str | None, Field(description="Same as window= but matches the executable name, e.g. 'Weixin'.")] = None,
        hwnd: Annotated[int | None, Field(description="Exact window handle, from Window(mode='list').")] = None,
        nth: Annotated[int, Field(description="Which window when several match (0-based).")] = 0,
        region: Annotated[list[int] | str | None, Field(description="Restrict to [left, top, right, bottom] in SCREEN pixels. Strongly recommended - a whole window returns a lot of noise.")] = None,
        kind: Annotated[str, Field(description="Filter: 'icon' (roughly square, no text - the usual target), 'text' (overlaps something OCR could read), 'other' (scrollbars, borders, big blocks), or 'all'.")] = "icon",
        near_text: Annotated[str | None, Field(description="Anchor on this text (located by OCR) and return only what sits beside it. Measured from the anchor's EDGE, not its centre, so it does not drift when the label's length changes.")] = None,
        direction: Annotated[str, Field(description="Which side of near_text: right | left | above | below.")] = "right",
        limit: Annotated[int, Field(description="Max elements to return.")] = 25,
        ctx: Context = None,
    ) -> str:
        import time

        import numpy as _np
        from windows_mcp.tools.ocr_find import _grab_for, _recognize, _as_box, _score_line

        t0 = time.time()
        region = _as_box(region)
        im, (ox, oy), target, note = _grab_for(window, process, hwnd, nth, region)
        if im is None:
            return "FindElements could not read anything: %s" % note

        gray = _np.asarray(im.convert("L"))
        ocr_boxes, anchor, lines_seen = [], None, 0
        try:
            res, scale = _recognize(im)
            needle = (near_text or "").strip().lower()
            needle_ns = needle.replace(" ", "")
            best = (0.0, None)
            for ln in res.lines:
                ws = list(ln.words)
                if not ws:
                    continue
                lines_seen += 1
                b = (min(w.bounding_rect.x for w in ws) / scale,
                     min(w.bounding_rect.y for w in ws) / scale,
                     max(w.bounding_rect.x + w.bounding_rect.width for w in ws) / scale,
                     max(w.bounding_rect.y + w.bounding_rect.height for w in ws) / scale)
                box = tuple(int(v) for v in b)
                # 只有认出来像样的文字才算"这块是文字"。
                # 9-14 剪映实测：OCR 会在图标上读出单个垃圾字符（微信左栏是 `0`/`囗`），
                # 照单全收的话，放大镜/列表/下拉箭头全被判成 text，kind="icon" 就一个都不剩。
                # 宁可把个别单字标签也当图标——多给一个候选无害，漏掉真图标才致命。
                if len((ln.text or "").replace(" ", "").strip()) >= 2:
                    ocr_boxes.append(box)
                if needle_ns:
                    s = _score_line(ln.text or "", needle, needle_ns)
                    if s > best[0]:
                        best = (s, box)
            if needle_ns:
                if best[0] < 0.6:
                    return ("FindElements: near_text %r not found (%d text lines read). "
                            "Drop near_text= to list everything in the region instead."
                            % (near_text, lines_seen))
                anchor = best[1]
        except Exception as e:
            if near_text:
                return "FindElements needs OCR for near_text= but it is unavailable: %s" % e

        # 按列投影只在「一条」上成立。给了 near_text 就按锚点自动切出那一条：
        # 找左右邻居切横条（按列投影），找上下邻居切竖条（按行投影）。
        # 9-14 剪映实测：不切条直接扫整窗，几乎每列都有前景，全部粘成 3 个巨块。
        if anchor is not None:
            ax1, ay1, ax2, ay2 = anchor
            pad = max(10, (ay2 - ay1))
            if direction in ("right", "left"):
                y0, y1 = max(0, ay1 - pad), min(gray.shape[0], ay2 + pad + 1)
                sub, dx, dy, ax = gray[y0:y1, :], 0, y0, "x"
            else:
                xpad = max(20, (ax2 - ax1))
                x0, x1 = max(0, ax1 - xpad), min(gray.shape[1], ax2 + xpad + 1)
                sub, dx, dy, ax = gray[:, x0:x1], x0, 0, "y"
            els = [(a + dx, b + dy, c + dx, d + dy)
                   for (a, b, c, d) in visual_elements(sub, axis=ax)]
        else:
            els = visual_elements(gray)

        tagged = classify(els, ocr_boxes)
        if anchor is not None:
            one = pick_near(tagged, anchor, direction,
                            kinds=None if kind == "all" else (kind,))
            tagged = [one] if one else []
        elif kind != "all":
            tagged = [e for e in tagged if e[4] == kind]

        dt = time.time() - t0
        if not tagged:
            return ("FindElements: nothing matched (kind=%s%s). %d visual element(s) were found in "
                    "total before filtering - try kind='all' to see them, or widen region=."
                    % (kind, ", near_text=%r" % near_text if near_text else "", len(els)))

        where = ("window '%s' own content" % target) if target else (
            "region %s" % (region,) if region else "full screen")
        out = ["FindElements: %d %s element(s) in %s (%.2fs). Centres are SCREEN pixels - pass "
               "straight to Click:" % (len(tagged), kind, where, dt)]
        for i, e in enumerate(tagged[:limit]):
            x1, y1, x2, y2 = e[0] + ox, e[1] + oy, e[2] + ox, e[3] + oy
            out.append("  nth:%d  centre=(%d,%d)  box=(%d,%d,%d,%d)  %dx%d  %s"
                       % (i, (x1 + x2) // 2, (y1 + y2) // 2, x1, y1, x2, y2,
                          x2 - x1 + 1, y2 - y1 + 1, e[4]))
        if len(tagged) > limit:
            out.append("  ... %d more" % (len(tagged) - limit))
        return "\n".join(out)
