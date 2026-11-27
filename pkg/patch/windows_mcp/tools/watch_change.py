"""PATCH 0.5.18：点完等画面变化，并直接告诉你**哪儿变了**。

opencode 9-15 打完纸牌后的原话，是这一版最值钱的一句反馈：

> **最费时间的是读盘面和验证动作结果，不是决定走法，也不是点击本身。**
> 从 0 分推进到 85 分，主要时间都花在"截图、识牌、核对"，而非思考合法性。

它排第 2、3 的需求正对着这句：

> 2. **点击后等待区域变化** —— 类似 `ClickAndWaitChange(loc, expected_hwnd, region, timeout)`，
>    返回变化区域和前后截图。本局每走一步都要手工 `Click → Screenshot`；
>    合并后既快又能确定点击生效。
> 3. **截图差异工具** —— 比较两帧并返回变化框。例如移动 J♣ 后直接报告
>    "C4 明牌消失、C7 增加牌、C4 翻出 2♦"，至少能快速定位需要重读的区域，
>    而不用重新审视整盘。

两件事合成一个工具，因为它们本来就是一件事的两半：**做了动作，然后只看变化的那一块**。

省下的是什么，说清楚：
- 少一次工具往返 = 少一轮模型推理（实测 4-5 秒，是每步 6-7 秒里的大头）
- **只回变化框，不回整张图** = 模型不用重读整个界面。这才是 opencode 说的那个大头。
- 顺带解决另一个真问题：`Click` 的返回只说"鼠标动作发出去了"，不说有没有生效。
  它上一轮**因此把两次无效的牌库点击误认为牌库没有变化**。这里直接回答"变没变"。

**变化 = 生效，但没变化 ≠ 没生效。** 有些操作本来就不改变画面（点一个已经选中的项）。
这一点必须写在返回里，否则就是把一个假信号塞给调用方。
"""

import ctypes
import time
from typing import Optional

user32 = ctypes.windll.user32

from mcp.types import ToolAnnotations
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics

GRID = 16          # 变化检测的格子大小（像素）。太小噪声多，太大定位糊
MIN_DIFF = 12      # 单格平均亮度差超过这个才算变了，压掉抗锯齿和光标的抖动


def _grab(hwnd, region):
    """取一帧。**窗口没了就返回 None，不要抛异常。**

    Codex 9-15 在剪映上撞到的第 9 条：点"选择文件夹"，点击成功、对话框关掉了，
    工具却回了一句 `Error calling tool 'ClickAndWatch':
    'NoneType' object has no attribute 'size'`。
    它的原话：「实际点击成功且对话框已关闭……应区分操作失败和操作后截图失败。」

    这是最坏的一种错法：**动作做成了，工具却报失败**。调用方要么白重试一次
    （对不可逆的动作就是做了两遍），要么以为没成、走上完全错误的分支。
    窗口在点击后消失恰恰是"点成了"的强证据，应该这么说出来，而不是崩。
    """
    from PIL import ImageGrab
    from windows_mcp.tools.window_ctl import capture_window, _rect
    if hwnd:
        im, box = capture_window(hwnd)
        if im is None:
            # PrintWindow 抓不到 != 窗口没了。Qt 的弹出菜单、CEF 面板都拒绝 PrintWindow，
            # 可它们就在屏幕上——退回按它的屏幕矩形抓屏。
            # 9-16 实测：微信朋友圈的右键菜单（Qt*ToolSaveBits）就是这样，
            # 菜单明明弹着、字都读得出，ClickAndWatch 却因为"抓不到"拒绝点击，
            # 纯文字发布这条路整个死在这一步。只有窗口真的不在了才返回 None。
            try:
                u32 = __import__("ctypes").windll.user32
                if u32.IsWindow(hwnd) and u32.IsWindowVisible(hwnd):
                    l, t, r, b = _rect(hwnd)
                    if r - l > 0 and b - t > 0:
                        im = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
                        box = (l, t, r, b)
            except Exception:
                im = None
        if im is None:
            return None, (box[0], box[1])
        if region:
            l, t, r, b = [int(v) for v in region]
            im = im.crop((max(0, l - box[0]), max(0, t - box[1]),
                          max(1, r - box[0]), max(1, b - box[1])))
            return im, (l, t)
        return im, (box[0], box[1])
    if region:
        l, t, r, b = [int(v) for v in region]
        return ImageGrab.grab(bbox=(l, t, r, b), all_screens=True), (l, t)
    im = ImageGrab.grab(all_screens=True)
    return im, (0, 0)


def changed_boxes(a, b, grid=GRID, min_diff=MIN_DIFF):
    """两张同尺寸的图，返回变化区域的包围框列表（图内坐标）。

    做法是按格子比平均亮度，不是逐像素比：逐像素会被抗锯齿、光标、
    渐变动画淹没，最后每次都"整屏都变了"，等于没说。
    """
    if a.size != b.size:
        return [(0, 0, b.width, b.height)]
    ga, gb = a.convert("L"), b.convert("L")
    pa, pb = ga.load(), gb.load()
    w, h = ga.size
    hits = set()
    for gy in range(0, h, grid):
        for gx in range(0, w, grid):
            x2, y2 = min(gx + grid, w), min(gy + grid, h)
            n = (x2 - gx) * (y2 - gy)
            if n <= 0:
                continue
            sa = sb = 0
            for y in range(gy, y2, 2):        # 隔行采样，够用且快一倍
                for x in range(gx, x2, 2):
                    sa += pa[x, y]
                    sb += pb[x, y]
            cnt = max(1, ((y2 - gy + 1) // 2) * ((x2 - gx + 1) // 2))
            if abs(sa - sb) / cnt >= min_diff:
                hits.add((gx // grid, gy // grid))
    if not hits:
        return []
    # 把相邻的格子合成矩形（四连通洪泛）
    boxes = []
    seen = set()
    for cell in hits:
        if cell in seen:
            continue
        stack, comp = [cell], []
        seen.add(cell)
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (cx + dx, cy + dy)
                if nb in hits and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        xs = [c[0] for c in comp]
        ys = [c[1] for c in comp]
        boxes.append((min(xs) * grid, min(ys) * grid,
                      min((max(xs) + 1) * grid, b.width),
                      min((max(ys) + 1) * grid, b.height)))
    boxes.sort(key=lambda r: -((r[2] - r[0]) * (r[3] - r[1])))
    return boxes


# 刚给过谁一张图。给 Screenshot 用来提醒调用方"这张你手上已经有了"。
#
# 9-15 量出来的：opencode 那次 227 分钟会话调了 77 次 Window、93 次 Screenshot，
# 而每张整窗截图约 1074 token、每次列窗口约 1084 token——上下文中位数
# 10.8 万就是这么堆起来的，**而上下文越大，它每一轮想得越久**。
# 它并不知道自己在浪费，因为没人告诉它。工具知道，那就该由工具说。
LAST_PICTURE = {"hwnd": None, "at": 0.0, "what": ""}


def top_level_windows():
    """当前所有可见、有标题的顶层窗口 {hwnd: (title, rect)}。用来发现"点出了新窗口"。"""
    from windows_mcp.tools.window_ctl import _enum, _rect, _text
    out = {}
    for h in _enum():
        try:
            out[int(h)] = (_text(h), _rect(h))
        except Exception:
            pass
    return out


def new_windows(before_map, after_map):
    """点击之后多出来的顶层窗口，按面积从大到小。"""
    out = []
    for h, (title, rect) in after_map.items():
        if h in before_map:
            continue
        w, ht = rect[2] - rect[0], rect[3] - rect[1]
        if w < 120 or ht < 80:      # 太小的多半是宿主窗、气泡，不是对话框
            continue
        out.append((h, title, rect, w * ht))
    out.sort(key=lambda t: -t[3])
    return out


def cluster_boxes(boxes, gap=120):
    """把挨得近的变化框并成一组，返回每组的包围框，按组内变化面积从大到小。

    为什么需要这个：opencode 9-15 第三轮报的——我上一版为了不让
    「时钟 + 底栏」这种两头的小框撑出一张大而空的图，改成了"只取最大的那一个框"。
    结果撞上另一种情况就坏了：**一张牌的变化常常被拆成好几个碎框**
    （发光的牌边、左上角的点数、花色小块），最大的那个可能只是
    16x240 的一道边框，截出来根本看不出是什么牌。

    它的原话：「应该先把距离较近、属于同一对象的碎框聚成一组，
    再对聚类后的包围框做取景；尤其当所有框都很小但集中在一张牌的矩形范围内时，
    应返回整张牌，而不是把点数拆片误判成时钟或光标噪声。」

    所以判据不能是"单框面积"，而是"**这一堆东西加起来有多少变化**"：
    一张牌的碎片挨在一起，合起来面积可观；时钟和光标各自孤零零，合起来还是很小。

    用单链聚类：两个框的矩形间距小于 gap 就算同一组，再传递合并。

    gap 取 120 是照着"一个界面对象有多大"定的：本机分辨率下一张牌大约
    180x260，它的碎片（牌边、左上角点数、右下角点数）纵向可以隔到 200 像素，
    gap=60 会把同一张牌拆成两三组——第一版就是这么漏的。
    而真正该分开的东西（时钟 vs 牌桌、底栏 vs 牌堆）隔着上千像素，
    120 离它们还差得远。
    """
    items = list(boxes)
    if not items:
        return []

    def near(a, b):
        dx = max(0, max(a[0] - b[2], b[0] - a[2]))
        dy = max(0, max(a[1] - b[3], b[1] - a[3]))
        return dx <= gap and dy <= gap

    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if near(items[i], items[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups = {}
    for i, bx in enumerate(items):
        groups.setdefault(find(i), []).append(bx)

    out = []
    for g in groups.values():
        box = (min(b[0] for b in g), min(b[1] for b in g),
               max(b[2] for b in g), max(b[3] for b in g))
        area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in g)
        out.append((box, area, len(g)))
    out.sort(key=lambda t: -t[1])
    return out


_DEAD_CLICKS = {}


def _match_owner_note(x, y):
    """按文字点、又没指定窗口时，报出这个点到底落在谁身上。

    千问办公 9-16 第 7 轮的假成功：它要点微信右键菜单里的「发表文字」，
    调的是 ClickAndWatch(text="发表文字")，没带 hwnd。工具在**整个屏幕**上找，
    结果匹配到了千问自己聊天窗里它刚写的那句叙述（"Click 发表文字"），
    点完照样回一句语气正常的「Clicked left at (1011,1202). A WINDOW CLOSED」——
    它是靠下一张截图里没有发表框才识破的。

    我们在那一刻明明知道这个点归谁，却一个字没说。说出来就够了：
    模型看见「这个点在千问办公的窗口里」，一眼就知道匹配跑偏了。
    不拒绝、不猜意图——它可能真的就是要点那个窗。
    """
    try:
        # 用 window_ctl 现成的 _describe，别再拼一遍——frame_guard.actionable 也是用它，
        # 三处各拼各的迟早说法不一致（9-16 我自己就差点又写一份）。
        from windows_mcp.tools.window_ctl import _root_at, _describe
        h = _root_at(x, y)
        if not h:
            return ""
        d = _describe(h)
        proc, title = d.get("process") or "?", d.get("title") or ""
        return (" That point is inside hwnd=%d %s %r - with text= and no hwnd=, the WHOLE SCREEN is "
                "searched, so the same words in a background window (a chat window showing these very "
                "words, your own notes, another app) can win the match. If that is not the window you "
                "meant, pass hwnd= or region= and try again." % (int(h), proc, title[:40]))
    except Exception:
        return ""


def _wedge_note(hwnd, changed):
    """同一个窗连着几次点了都没反应，就把「它可能卡了」说出来。

    单次没变化是常态（点已选中的项、效果在别处），所以前两次不说话。
    连续三次就不是巧合：9-16 微信进入过一种**消息泵活着、窗口 enabled、
    没有模态框、中完整性，却忽略一切合成输入**的状态，WM_CLOSE 和点击全都没反应，
    最小化还原、改窗口大小、拉长按压、直接投 WM_LBUTTONDOWN 都救不回来，
    而点一下它的托盘图标就全好了。

    工具当时把这十几次各自报成「这个窗没变化」，于是同一个持续状态被拆成
    十几条互不相干的回复，看的人只会以为「这一下没点中」，
    然后一遍遍去调坐标——方向整个是错的。
    """
    key = int(hwnd or 0)
    if not key:
        return ""
    if changed:
        _DEAD_CLICKS.pop(key, None)
        return ""
    n = _DEAD_CLICKS.get(key, 0) + 1
    _DEAD_CLICKS[key] = n
    if n < 3:
        return ""
    return ("\nTHAT IS %d CLICKS IN A ROW ON hwnd=%d WITH NO CHANGE. One is ordinary; three is a "
            "state, not a coincidence - the window has most likely stopped accepting input. It "
            "can look perfectly healthy while in this state (pumping messages, enabled, no modal "
            "dialog, same integrity level), and retrying, holding the button down longer or "
            "posting mouse messages will all keep failing. For a tray-resident app the cure is "
            "its TRAY icon, invoked WITH THE WINDOW STILL OPEN: "
            "Window(mode='tray', invoke='NAME'). Otherwise look at the window before clicking "
            "a %dth time." % (n, key, n + 1))


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="ClickAndWatch",
        description=(
            "Clicks, then waits until the picture STOPS changing, and reports WHERE it changed "
            "- one call instead of Click + Screenshot. A plain Click only tells you the event was "
            "sent; a click on a dead spot reads exactly like one that worked. "
            "Takes loc=[x,y] or text=\"发表\" (located by OCR at click time), hwnd= to watch one "
            "window (read through PrintWindow, so it works while covered), region=[l,t,r,b] in "
            "SCREEN coordinates, and timeout (default 3s, an OBSERVATION BUDGET checked between "
            "samples - one last `settle` can run past it, so timeout=4 settle=1.2 may report at "
            "5.4s). Returns changed rectangles in SCREEN coordinates, biggest first, and ATTACHES "
            "a picture of the changed area; pass show=false for rectangles only. If it timed out "
            "mid-animation it says so rather than passing off a half-drawn frame. "
            "preview=true with text= CLICKS NOTHING: it numbers every match and boxes them in a "
            "picture. Use it before anything irreversible - a confirm dialog repeats the same word "
            "in its title and on its button, so text=\"删除\" matches both and the order is not "
            "guaranteed. "
            "If the click OPENS A NEW WINDOW it says so and shows you THAT window. "
            "expected_process=/expected_hwnd= refuse the click if the pixel belongs to someone "
            "else at click time; activate=true (with hwnd=) raises the window inside this call, "
            "closing the gap a separate Window(front) leaves open, at the cost of stealing focus. "
            "READ THE RESULT CAREFULLY: neither direction proves anything. A change does NOT mean "
            "your click caused it - a clock or score keeps ticking whatever you do. NO change does "
            "NOT mean nothing happened - an already-selected item, or an effect off-screen, "
            "changes no pixels. It reports what it saw, not what happened."
        ),
        annotations=ToolAnnotations(
            title="ClickAndWatch",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "ClickAndWatch-Tool")
    def click_and_watch_tool(
        loc: Optional[list] = None,
        text: Optional[str] = None,
        hwnd: Optional[int] = None,
        region: Optional[list] = None,
        timeout: float = 3.0,
        settle: float = 0.35,
        button: str = "left",
        clicks: int = 1,
        expected_process: Optional[str] = None,
        expected_hwnd: Optional[int] = None,
        show: bool = True,
        activate: bool = False,
        preview: bool = False,
        nth: int = 0,
        ctx: Context = None,
    ):
        from windows_mcp.tools.frame_wrap import _check_owner
        from windows_mcp.tools.ocr_find import _as_box, resolve_text
        from windows_mcp.tools.overlay import activity, take_cancel

        desktop = get_desktop()
        region = _as_box(region)
        timeout = max(0.3, min(float(timeout or 3), 30))

        ago = take_cancel()
        if ago is not None:
            raise ValueError(
                "STOPPED: somebody pressed Esc %.0f seconds ago while uia-desktop was operating "
                "this machine, so nothing was clicked." % ago)

        if preview:
            # WorkBuddy 9-15 排 P0-3 的那条。它的理由是决定性的：
            # 「确认框标题是『删除该朋友圈?』，按钮文字是『删除』，substring 匹配
            #   会同时命中标题和按钮，click_nth 的顺序也没有保证……
            #   有了它我才敢在不可逆动作上用 click_text 锚点法。」
            # 越是不可逆的按钮，文案越容易和标题重复；而点错一个不可逆按钮的代价，
            # 远高于多花一次调用先看一眼。所以给一条**看得见落点、但什么都不做**的路。
            from fastmcp.utilities.types import Image
            from windows_mcp.tools.ocr_find import preview_text
            from windows_mcp.tools.snapshot import _encode
            if not text:
                return ("ClickAndWatch: preview=true is for text= targets - it shows every place "
                        "that text matches so you can pick. With loc= there is nothing to "
                        "disambiguate; use Window(mode='at', x=, y=) to check who owns that point.")
            err, shot, hits = preview_text(text, hwnd=hwnd, region=region)
            if err:
                return "ClickAndWatch preview: %s" % err
            if not hits:
                return ("ClickAndWatch preview: %r matched nothing. NOTHING WAS CLICKED." % text)
            ls = ["PREVIEW ONLY - NOTHING WAS CLICKED. %r matches %d place(s) "
                  "(SCREEN coordinates, top to bottom):" % (text, len(hits))]
            for i, h in enumerate(hits):
                ls.append("  [%d] centre=(%d,%d) box=%s  %s  in line %r"
                          % (i, h["centre"][0], h["centre"][1], h["box"],
                             "exact" if h["score"] >= 1.0 else "fuzzy %.2f" % h["score"],
                             h["line"]))
            ls.append("A plain call would take nth:%d, i.e. [%d] at (%d,%d)."
                      % (nth or 0, min(nth or 0, len(hits) - 1),
                         hits[min(nth or 0, len(hits) - 1)]["centre"][0],
                         hits[min(nth or 0, len(hits) - 1)]["centre"][1]))
            if len(hits) > 1:
                ls.append("MORE THAN ONE MATCH. This is exactly the case that bites on "
                          "destructive buttons: a confirm dialog often repeats the word in its "
                          "title ('Delete this post?') and on its button ('Delete'). Pick with "
                          "nth=, or anchor on neighbouring text and offset with click_dx/click_dy.")
            ls.append("The picture below is what was read, with every match boxed and numbered.")
            return ["\n".join(ls), Image(data=_encode(shot), format="png")]

        chose = ""
        if text:
            # nth 以前只活在 preview 的提示文案里，真正点击时被丢掉了。
            #
            # WorkBuddy 9-15 15:1x 实测（微信朋友圈，删测试帖）：
            #   preview 列出两处命中并写着「A plain call would take nth:0」，
            #   我照抄成 ClickAndWatch(text="删除", nth=1)——
            #   返回 "Clicked left at (920,485)"，而 (920,485) 正是 preview 标为 [0] 的**标题**，
            #   红按钮在 (900,544)。确认框没关，只回一句 "NOTHING changed"。
            #   照着自己的提示做，却点到提示里叫你别点的那个——这比不给提示更糟。
            #
            # Click(click_text=..., click_nth=N) 早就把 N 接成 resolve_text(occurrence=) 了，
            # 这里照同一套接上，preview 的承诺才算数；越界时 resolve_text 会回
            # "matched 2 line(s) but occurrence:5 is out of range"，原样上报，不替它猜。
            occ = int(nth or 0)
            xy, why, tgt = resolve_text(text, hwnd=hwnd, region=region, occurrence=occ)
            if xy is None:
                return "ClickAndWatch could not locate %r: %s" % (text, why)
            loc = list(xy)
            # 挑了哪一行，必须报出来。
            #
            # WorkBuddy 9-15 第二轮的卡点 5：确认框里「删除该朋友圈?」是标题、
            # 「删除」是按钮，它写 text="删除"，工具挑中了**标题**，
            # 点了个死标签，然后只回一句 "NOTHING changed"。
            # 它的原话最扎心：「这不是『读不到』，是**读到了、选错了、且不上报**」。
            #
            # 同一次调用里我们明明知道挑的是哪一行、框在哪，却一个字都没说。
            # 报出来，它就能一眼看出"我要的是按钮，你给我的是标题"。
            chose = " Matched by text: %s." % why
            if not hwnd:
                chose += _match_owner_note(loc[0], loc[1])
            if occ:
                chose += (" That is match #%d, and this call CLICKED it - with text= the nth: you "
                          "pass is the one that gets clicked (it used to be ignored: nth:1 still "
                          "clicked match #0)." % occ)
            if len(text or "") <= 4:
                chose += (" NOTE: %r is short, and OCR matches substrings - a confirm dialog "
                          "usually repeats the same word in its title and on its button, so this "
                          "may not be the one you meant. ClickAndWatch(text=..., preview=true) "
                          "lists every match with a picture and clicks nothing." % text)
        if not loc or len(loc) != 2:
            return "ClickAndWatch: pass loc=[x, y] or text=\"...\"."

        raised = ""
        if text:
            raised = chose.strip() + " "
        if activate:
            # opencode 9-15 报的本轮最大摩擦，原话：
            # 「expected_process 能防误点，却无法让流程继续。」
            # 先 Window(front) 再 ClickAndWatch 是两次调用，**中间那段空档**
            # 恰恰就是别的窗口抢前台的窗口期，而它每次都得赌一次。
            # 放进同一次调用，抢占和补救之间就没有往返了。
            # 注意这会抢焦点（人正在打字就被打断），所以默认关着，要显式开。
            if not hwnd:
                return ("ClickAndWatch: activate=true needs hwnd= so it knows which window to "
                        "raise.")
            # opencode 9-15 第二轮报的：activate=true 偶尔第一次没置前成功，
            # **原样重试就好了**。既然重试有用，那这一次重试就该由工具自己做——
            # 让调用方重试等于白费它一整轮推理（实测中位 14.9 秒），
            # 而这里重试一次只要 0.3 秒。
            #
            # 会失败是正常的：Windows 有前台锁，SetForegroundWindow 经常被拒，
            # 尤其在别的窗口刚获得焦点、或者有动画正在跑的时候。
            # 所以不但要重试，还要把**试了几次、API 怎么说、最后前台是谁**
            # 一起报出来——它上一轮的原话是"需要更清楚地报告是否尝试激活"。
            from windows_mcp.tools.window_ctl import (_force_front, _pid,
                                                      _proc_name, _root_at, _text)

            def _fg():
                h = user32.GetForegroundWindow()
                return int(h or 0)

            tries, okf = 0, False
            for tries in range(1, 4):
                okf = bool(_force_front(int(hwnd)))
                time.sleep(0.25)
                if _fg() == int(hwnd):
                    break
            fg = _fg()
            landed = _root_at(int(loc[0]), int(loc[1])) if loc else None
            if fg == int(hwnd):
                raised = ("Raised hwnd=%s to the front%s. "
                          % (hwnd, "" if tries == 1 else " (took %d attempts)" % tries))
            else:
                raised = ("TRIED to raise hwnd=%s %d time(s); SetForegroundWindow %s, and the "
                          "foreground is now hwnd=%d (%s '%s'). The click below went ahead "
                          "anyway because a window does not have to be in front to receive a "
                          "click at its coordinates - but if the result looks wrong, that is "
                          "the first thing to suspect. "
                          % (hwnd, tries, "said it succeeded" if okf else "refused",
                             fg, _proc_name(_pid(fg)) if fg else "?",
                             (_text(fg) or "")[:28] if fg else ""))
            if landed is not None and int(landed) != int(hwnd):
                raised += ("Note: (%d,%d) currently belongs to hwnd=%d (%s), not the window you "
                           "named. " % (loc[0], loc[1], landed,
                                        _proc_name(_pid(landed)) or "?"))

        if hwnd and not activate:
            # 你说了点哪个窗，那就在点之前看一眼这个坐标现在归谁。
            #
            # 不拦住这次点击——窗口不必在前台也能收到坐标点击，拦了会误伤正常用法。
            # 但**必须说**：点完之后"这个窗没变化"有两种完全不同的原因，
            # 一种是点了没效果，另一种是根本没点到它。分不清这两种，
            # 排查方向就会整个跑偏（9-16 我就跑偏了半小时）。
            from windows_mcp.tools.window_ctl import _pid as _p2
            from windows_mcp.tools.window_ctl import _proc_name as _pn2
            from windows_mcp.tools.window_ctl import _root_at as _ra2
            from windows_mcp.tools.window_ctl import _text as _tx2
            try:
                _land = _ra2(int(loc[0]), int(loc[1]))
            except Exception:
                _land = None
            if _land and int(_land) != int(hwnd):
                raised += ("HEADS UP - (%d,%d) does not belong to hwnd=%s right now: hwnd=%d "
                           "(%s '%s') is on top there, and that is what this click will hit. "
                           "The click still goes ahead, because a window does not have to be in "
                           "front to be clicked at its own coordinates - but if the watched "
                           "window then shows no change, THIS is the reason, not a dead button. "
                           "Pass activate=true to raise your window first. "
                           % (loc[0], loc[1], hwnd, int(_land),
                              _pn2(_p2(_land)) or "?", (_tx2(_land) or "")[:28]))

        if expected_process or expected_hwnd:
            _check_owner({"loc": loc, "expected_process": expected_process,
                          "expected_hwnd": expected_hwnd}, "ClickAndWatch")

        before, origin = _grab(hwnd, region)
        if before is None:
            return ("ClickAndWatch REFUSED - could not capture hwnd=%s before clicking "
                    "(the window is gone, or it refuses PrintWindow). Nothing was clicked. "
                    "Run Window(mode='list') to see what is actually there." % hwnd)
        wins_before = top_level_windows()
        activity(int(hwnd or 0), "uia-desktop 正在操控这台电脑 · ClickAndWatch")
        desktop.click(loc=list(loc), button=button, clicks=clicks)

        # opencode 9-15 第二轮报的：发牌动画里 settle=0.8 仍然抓到中间帧。
        # 它说得对——原来的语义是"看到第一处变化就返回"，
        # 而动画的第一帧恰恰是最没用的那一帧：牌还在飞。
        # 现在改成**等画面不动了再返回**：第一次发现变化之后继续看，
        # 直到连续两次采样完全一致（或者撞上 timeout）。
        # 返回里会说清楚是"稳定了"还是"还在动"，别让调用方拿一张飞行中的图当真。
        t0 = time.time()
        boxes, after = [], before
        settled = None
        first_boxes = []
        vanished = False
        ticker = None       # 外层先定义：没进内层循环时下面照样会读它
        while time.time() - t0 < timeout:
            time.sleep(settle)
            after, _o = _grab(hwnd, region)
            if after is None:
                # 点完之后窗口抓不到了——多半就是这一下把它关掉了。
                # 这是"点成了"的强证据，不是错误。
                vanished = True
                break
            boxes = changed_boxes(before, after)
            if boxes:
                # 看到变化了，接着看它停没停
                first_boxes = boxes
                prev = after
                settled = False
                ticks = 0
                ticker = None
                while time.time() - t0 < timeout:
                    time.sleep(settle)
                    cur, _o2 = _grab(hwnd, region)
                    if cur is None:
                        vanished = True
                        break
                    d = changed_boxes(prev, cur)
                    if not d:
                        settled = True
                        after = cur
                        break
                    # 只有"一个小东西在原地跳数字"时，也该算停稳了。
                    #
                    # opencode 9-15 第八轮的 P3：蜘蛛纸牌两次发牌分别到
                    # 4.8 秒和 6.8 秒仍被判"还在动"。它说警告本身是对的，
                    # 但怀疑是游戏的计时器在挡着——「可考虑识别周期性计时器变化，
                    # 避免它阻止稳定判定」。
                    #
                    # 它猜对了机制：原来的条件是**一个像素都不许变**，
                    # 而带计时器的窗口永远满足不了这个条件，于是每一次
                    # ClickAndWatch 都以"还在动"收场，调用方被迫再等再截一次图。
                    # 判据是"画面停了"，不是"每个像素都冻住了"。
                    # 连着两轮只剩小块在动，就当停稳，并把那块**报出来**——
                    # 不报出来就成了偷偷放宽标准。
                    if all((r - l) * (b - t) < 2500 for l, t, r, b in d):
                        ticks += 1
                        ticker = max(d, key=lambda x: (x[2] - x[0]) * (x[3] - x[1]))
                        if ticks >= 2:
                            settled = "ticking"
                            after = cur
                            break
                    else:
                        ticks = 0
                        ticker = None
                    prev = cur
                    after = cur
                # 用**最后一帧**跟起始帧比，拿到的是"这一下最终改了什么"。
                # 但动画有可能绕回起点（闪烁、循环、发牌发完又盖上），
                # 那样末帧和首帧一模一样，这里会算出"什么都没变"——
                # 而我们明明亲眼看着它在动。这种时候退回第一次检测到的变化框，
                # 否则就会对调用方说"没变化"，那是睁眼说瞎话。
                boxes = changed_boxes(before, after) or first_boxes
                break
        waited = time.time() - t0

        # 这一下有没有**开出一个新窗口**？
        #
        # Codex 9-15 在剪映上逮到的，是纸牌永远撞不出来的一类：
        # 「ClickAndWatch(hwnd=主窗口) 打开导入框后只返回变暗的主窗口图，
        #   没有带回新弹出的文件对话框，必须额外列窗口和截图才能继续。」
        # 它说得对，而且这在真实业务里是常态——导入、保存、确认，
        # 全是点一下弹出一个**新的顶层窗口**。只盯着原窗口看，
        # 看到的永远只是"它变暗了"，真正要读的东西在另一个窗口上。
        #
        # 发现新窗口就直接说，并且把镜头换过去：既然是这一下点出来的，
        # 那它才是"结果"。原窗口只是变暗，没什么可看的。
        if vanished:
            from windows_mcp.tools.window_ctl import _pid, _proc_name, _text
            still = bool(user32.IsWindow(int(hwnd)))
            gone_new = new_windows(wins_before, top_level_windows())
            # Codex 9-15 提醒过的一条，照做：**不能把所有抓不到图都归因成"窗口关掉了"**。
            # 句柄真的没了，和句柄还在但这一帧没抓到，是两件事，
            # 前者是"点成了"的证据，后者只是取图失败，什么也证明不了。
            # 猜错的代价是反方向的：把取图失败说成"点成了"，调用方就不会重试；
            # 把窗口关闭说成失败，调用方会把不可逆的动作再做一遍。
            if not still:
                msg = (raised + "Clicked %s at (%d,%d). The click was performed, and hwnd=%s is "
                       "no longer a window at all - it was closed. A window closing right after "
                       "you click in it is evidence the click WORKED (a dialog dismissed, a "
                       "window closed), so do NOT repeat it: repeating is how a one-way action "
                       "gets done twice. Note this says the window closed, NOT that whatever it "
                       "was for succeeded - check that separately."
                       % (button, loc[0], loc[1], hwnd))
            else:
                msg = (raised + "Clicked %s at (%d,%d). The click was performed, but the "
                       "screenshot afterwards failed: hwnd=%s still EXISTS yet could not be "
                       "captured (minimized, hidden, or refusing PrintWindow). This says nothing "
                       "about whether the click had any effect - it is a capture failure, not a "
                       "click failure. Look another way before deciding, and be careful about "
                       "repeating the click."
                       % (button, loc[0], loc[1], hwnd))
            if gone_new:
                h2, t2, r2, _a2 = gone_new[0]
                msg += (" A new window also appeared: hwnd=%d %r at %s - that is probably where "
                        "things went next." % (h2, (t2 or "")[:40], r2))
            return msg

        wins_after = top_level_windows()
        opened = new_windows(wins_before, wins_after)
        # 反过来也算一遍：**点完之后消失了哪些窗口**。
        #
        # WorkBuddy 9-15 第二轮的卡点 3，它的判断很准：
        # 「『新窗口』检测不带 hwnd 也能出来；但『窗口关闭』检测只在传了 hwnd= 时才有。
        #   这个不对称很致命——『发表』这种动作恰恰是靠『编辑窗消失』来确认的。」
        #
        # 而我们手上本来就有前后两份顶层窗口清单，反着比一次就有了，
        # 不需要调用方传任何东西。这条尤其重要，因为它服务的是**不可逆动作**：
        # 发表、删除、提交完成的标志往往就是那个窗口没了。
        gone = new_windows(wins_after, wins_before)
        switched = None
        if opened and hwnd:
            nh, ntitle, nrect, _a = opened[0]
            try:
                shot, norigin = _grab(nh, None)
                if shot is not None:
                    after, origin = shot, norigin
                    boxes = [(0, 0, after.width, after.height)]
                    switched = (nh, ntitle, nrect)
            except Exception:
                switched = None

        scope = ("window hwnd=%s" % hwnd) if hwnd else "the screen"
        if gone and not opened:
            gh, gt, gr, _ga = gone[0]
            lines_gone = (raised + "Clicked %s at (%d,%d). A WINDOW CLOSED as a result: "
                          "hwnd=%d %r %dx%d at %s."
                          % (button, loc[0], loc[1], gh, (gt or "")[:40],
                             gr[2] - gr[0], gr[3] - gr[1], gr))
            lines_gone += (" A window disappearing right after you click in it is usually how a "
                           "post / save / delete confirms itself, so do NOT repeat the click - "
                           "repeating is how a one-way action gets done twice. Note this says the "
                           "window closed, NOT that whatever it was for succeeded: check that "
                           "separately, somewhere the result would actually show.")
            if len(gone) > 1:
                lines_gone += (" (%d windows closed in total; the largest is named above.)"
                               % len(gone))
            return lines_gone

        if opened and not switched:
            # WorkBuddy 9-15 报的 P0-1，而且是我今天加的这个功能造出来的：
            # 我们认出了新窗口、还加了一句"结果在那个窗口里，用 hwnd= 去读"，
            # 然后那个 180x180 的 toast `PrintWindow returned nothing`——
            # 它的原话：「等于把我指向死胡同」。它最后花了 3 次调用 + 1 张整屏图
            # 才确认"删没删掉"，而这件事本来一次调用就能闭环。
            #
            # 修法两层：
            # 一、既然抓不到窗口自身的画面，**还知道它在屏幕上的矩形**，
            #    那就按屏幕坐标 OCR 那一块——确认框会自报「删除该朋友圈?」，
            #    toast 会自报「已删除」。这正是调用方真正想要的东西。
            # 二、读不出来时说清是哪一种：窗口还在但拒绝渲染，还是它已经消失了。
            #    丢一句 180x180 等于什么也没说。
            nh, ntitle, nrect, _a0 = opened[0]
            said = []
            try:
                from windows_mcp.tools.ocr_find import read_text_lines
                said = read_text_lines(region=list(nrect)) or []
            except Exception:
                said = []
            alive = bool(user32.IsWindow(int(nh)))

            head = raised + ("Clicked %s at (%d,%d). A NEW WINDOW OPENED as a result: "
                             % (button, loc[0], loc[1]))
            head += ", ".join("hwnd=%d %r %dx%d" % (h, (t or "")[:40], r[2] - r[0], r[3] - r[1])
                              for h, t, r, _a in opened[:3])
            if said:
                head += ". IT SAYS: %s" % " | ".join(x[:40] for x in said[:6])
            else:
                head += (". Its own content could not be captured (%s), and OCR of the screen "
                         "where it sits found no text either"
                         % ("it still exists but refuses to render into a foreign DC - bring it "
                            "to the front and read the screen" if alive else
                            "it has already closed, which is what a toast does"))
            return (head + ". That window is where the result of your click is - read it with "
                    "hwnd= on that handle. Watching the window you named only shows it dimming "
                    "behind the dialog.")

        if not boxes:
            # "没变化"的时候，**主动去看一眼现在最前面是谁**。
            #
            # 千问办公 9-15 在剪映上撞到的（它排的第 4 条）：它点「新建草稿」，
            # 弹出的「是否确认新建草稿?」是一个**独立窗口**，它监视的编辑器窗口
            # 只是变暗——像素差算下来就是"没变"。工具回了 NOTHING changed，
            # 它据此判断"没切成新草稿"，**又多点了两次菜单**，
            # 而实际上第一次就已经成功了。它的原话：
            # 「modal 是最常见的『点了没反应』原因，值得内置检查。」
            #
            # 它说得对，而且这比"叫调用方 check another way"强得多：
            # 我们**当场就能查**，GetForegroundWindow 一句话的事。
            # 让调用方去查一件我们随手能查的事，是把成本转嫁出去，
            # 而这次转嫁的代价是它多点了两次——在不可逆动作上，那就是做了两遍。
            extra = ""
            try:
                fg = int(user32.GetForegroundWindow() or 0)
                if fg and (not hwnd or fg != int(hwnd)):
                    from windows_mcp.tools.window_ctl import _describe
                    d = _describe(fg)
                    said = []
                    try:
                        from windows_mcp.tools.ocr_find import read_text_lines
                        said = (read_text_lines(region=list(d["rect"])) or [])[:5]
                    except Exception:
                        said = []
                    extra = ("\nBUT THE FOREGROUND IS SOMEONE ELSE: hwnd=%d %s %r %dx%d. A modal "
                             "dialog is the most common reason a click looks like it did nothing "
                             "- the window you watched only dims behind it, and dimming is zero "
                             "difference in its own render.%s Read that window before clicking "
                             "again; clicking again is how an irreversible action gets done twice."
                             % (fg, d.get("process"), (d.get("title") or "")[:40],
                                d["size"][0], d["size"][1],
                                (" IT SAYS: %s" % " | ".join(x[:40] for x in said)) if said else ""))
            except Exception:
                extra = ""
            extra += _wedge_note(hwnd, changed=False)
            return (raised + "Clicked %s at (%d,%d). Watched %s for %.1fs and NOTHING changed "
                    "THERE.%s\n"
                    "That does not prove the click did nothing: clicking an already-selected "
                    "item, or an action whose effect is elsewhere, changes no pixels here."
                    % (button, loc[0], loc[1], scope, waited, extra))

        _wedge_note(hwnd, changed=True)     # 有反应了，计数清零

        # 第一次真机测试就撞到的：点空白桌布也报「变化了」——那一小块其实是**秒表在跳**。
        # 报「变了」而不说「变了多少」，等于递一个假信号：调用方会以为这一下点出了效果。
        # 所以面积必须一起说，而且小到一定程度就要明说「这多半不是你点出来的」。
        watched = float(before.width * before.height) or 1.0
        big = boxes[0]
        big_area = (big[2] - big[0]) * (big[3] - big[1])
        trivial = big_area < 2500 or (big_area / watched) < 0.0005

        if switched:
            nh, ntitle, nrect = switched
            lines = [raised + ("Clicked %s at (%d,%d) and a NEW WINDOW OPENED %.1fs later: "
                               "hwnd=%d %r at %s. The picture below is THAT window, not the one "
                               "you named - it is where the result of your click is. The window "
                               "you named is only dimmed behind it, which is why watching it "
                               "would have shown you nothing useful."
                               % (button, loc[0], loc[1], waited, nh, (ntitle or "")[:40], nrect))]
        else:
            lines = [raised + "Clicked %s at (%d,%d); %s changed %.1fs later, in %d place(s) "
                     "(SCREEN coordinates, biggest first):"
                     % (button, loc[0], loc[1], scope, waited, len(boxes))]
        if settled == "ticking" and ticker:
            # 光报一个坐标框，调用方还是不知道那块是什么。
            # Codex 9-15 的 P2：「打开选项时点名 [939,1312,1003,1344]，
            # 仅凭文字不容易确认动的是什么。」它建议附一张小图——
            # 但那块东西上八成**写着字**（时钟、比分、步数），
            # 读出来比给图便宜得多，也更直接：「它显示的是 3:10」一句话就完了。
            says = ""
            try:
                from windows_mcp.tools.ocr_find import read_text_lines
                box = (origin[0] + ticker[0], origin[1] + ticker[1],
                       origin[0] + ticker[2], origin[1] + ticker[3])
                txt_lines = [t for t in read_text_lines(region=box) if t]
                if txt_lines:
                    says = " It reads %r right now, which tells you what it is." % (
                        " ".join(txt_lines)[:40],)
            except Exception:
                pass
            lines.append("NOTE: the picture stopped changing except for one small area "
                         "(%dx%d at [%d,%d,%d,%d]) that kept changing every sample - a clock, a "
                         "score or a move counter does that.%s This is reported as settled: "
                         "waiting for THAT to stop would mean waiting forever. If you were "
                         "expecting something at that spot, it is still moving."
                         % (ticker[2] - ticker[0], ticker[3] - ticker[1],
                            origin[0] + ticker[0], origin[1] + ticker[1],
                            origin[0] + ticker[2], origin[1] + ticker[3], says))
        if settled is False:
            lines.append("!! THE SCREEN WAS STILL MOVING when the timeout arrived, so this is a "
                         "frame from the middle of an animation, not the finished state. Card "
                         "deals and slide transitions look like this. Look again before reading "
                         "anything off it.")
        for l, t, r, b in ([] if switched else boxes[:8]):
            a = (r - l) * (b - t)
            mark = ("   <- tiny; a clock digit, a caret or a cursor looks like this"
                    if a < 2500 else "")
            lines.append("  [%d,%d,%d,%d]  %dx%d%s"
                         % (origin[0] + l, origin[1] + t, origin[0] + r, origin[1] + b,
                            r - l, b - t, mark))
        if len(boxes) > 8:
            lines.append("  ... and %d more" % (len(boxes) - 8))
        # 有没有哪一块变化，是落在**你点的那个地方**的？
        #
        # WorkBuddy 9-15 第二轮报的 P0-A（本轮最贵的一条）：它点垃圾桶图标，
        # 我们回了 12 处变化，**没有一处包含点击点**——全是窗口被激活时
        # 边缘重绘的 16px 竖条和底部 48px 横条。它据此以为点生效了，
        # 又点了一次才拿到 "NOTHING changed"，白点两次。
        #
        # 它说得准：同样是"这多半不是你点出来的"，我们只在"变化都很小"时才说，
        # 而"变化都离你很远"是同样强、甚至更强的信号——
        # 一次点击的效果几乎总会在落点附近留下痕迹。
        near_click = None
        try:
            cx, cy = int(loc[0]), int(loc[1])
            pad = 24      # 按钮的点击反馈常常只覆盖按钮本身，给一点余量
            near_click = any((origin[0] + l - pad) <= cx <= (origin[0] + r + pad)
                             and (origin[1] + t - pad) <= cy <= (origin[1] + b + pad)
                             for l, t, r, b in boxes)
        except Exception:
            near_click = None

        # 每次都报一句"最近的变化离点击点多远"，不管走的是哪条分支。
        #
        # opencode 9-15 第八轮（蜘蛛纸牌）的 P2：两次发牌都**没有**触发
        # "变化都在别处"那条提醒，可前八个变化框全在牌列里。它的原话：
        # 「看不出为何没有触发……输出『有/无变化框覆盖点击点、最近距离、面积』
        #   会更容易判断。」
        #
        # 它说的是对的，而且戳中了一个更一般的毛病：我把一个连续量
        # （离点击点多远）压成了一个开关（触发/不触发），然后只在开关翻了的时候说话。
        # 调用方拿不到那个量，就既没法判断，也没法预期下一次会不会触发。
        # 把量本身交出去，开关翻不翻都不重要了。
        try:
            if boxes:
                def _dist(bx):
                    l, t, r, b = bx
                    sl, st = origin[0] + l, origin[1] + t
                    sr, sb = origin[0] + r, origin[1] + b
                    dx = 0 if sl <= cx <= sr else min(abs(cx - sl), abs(cx - sr))
                    dy = 0 if st <= cy <= sb else min(abs(cy - st), abs(cy - sb))
                    return (dx * dx + dy * dy) ** 0.5
                nearest = min(boxes, key=_dist)
                nd = _dist(nearest)
                nl, nt, nr, nb = nearest
                # 距离说清两件事，否则同一份报告里会出现两个不同的数字。
                # Codex 9-15 打 FreeCell 时点空自由格，一份报告里同时出现
                # 1452 px 和 1484 px——一个是**离最近的框**（这一行），
                # 另一个是**离最大的那个框**（下面"变化都很小"那条）。
                # 两个都对，但不说清各自是谁，看着就像自相矛盾。
                # 它还提了一条更细的：这是到**框**的距离，不是到变化像素的距离。
                # 框按检测网格取整，所以它是上界不是精确值——这话得自己说出来。
                lines.append("  distance from your click: the NEAREST change box is %.0f px away "
                             "(%dx%d at [%d,%d,%d,%d]); %s covers (%d,%d) itself. (Distance is "
                             "measured to the box, and boxes are snapped to the detection grid, "
                             "so treat it as approximate.)"
                             % (nd, nr - nl, nb - nt,
                                origin[0] + nl, origin[1] + nt, origin[0] + nr, origin[1] + nb,
                                "one of the boxes" if near_click else "nothing",
                                cx, cy))
        except Exception:
            pass
        if near_click is False:
            # 措辞从"下结论"退回"报事实"。
            #
            # opencode 9-15 第七轮当场打脸：它点库存牌堆 (290,330)，
            # 废牌区正常翻出 7♥——**动作成功了**，而废牌区离点击点天然隔着 136 像素，
            # 于是我这条新警告每抽一张牌就误报一次。它的原话：
            # 「附图证明动作成功，但文字结论与图片矛盾。这是本版最高优先级问题。」
            #
            # 它说得对，而且指出了我犯错的地方：按钮、库存、菜单**本来就常在别处产生结果**。
            # "变化不在点击点上"是一个**事实**，不是"没生效"的**结论**。
            # 我拿一个事实去推一个结论，就把 WorkBuddy 那边的假阴性
            # 换成了这边的假阳性——两边都是在替调用方下它自己该下的判断。
            lines.append("NOTE: none of these changes is at (%d,%d), the place you clicked - they "
                         "are all somewhere else. That is NORMAL for plenty of things: a stock "
                         "pile deals to a waste pile beside it, a button updates a panel across "
                         "the window, a menu opens below. It is also what a window repainting "
                         "after being activated looks like, which would mean your click did "
                         "nothing. Both look identical from here - decide from the picture below "
                         "whether this is the result you expected."
                         % (loc[0], loc[1]))
        if trivial:
            # 这条和上面那条是同一个毛病，我只改了一半。
            #
            # opencode 9-15 第八轮的 P0：「点击 Q 和一次点错牌面时仍出现
            # 『probably NOT your click / Treat this as nothing happened』。
            # 这重新引入了你刚从 P0 移除的推断。建议只陈述『最大变化为 16x16，
            # 且位于计时器区域』。」
            #
            # 它抓得准：上一版我把"变化都在别处"那条从结论退回事实，
            # 却把隔壁这条"变化都很小"原样留着——而两条犯的是同一个错，
            # 都是拿一个观测量去替调用方下判断。**改一半等于没改**：
            # 调用方还是会在同一个地方被同一句话误导，只是换了个分支。
            lines.append("FACT: every change is tiny - the largest is %dx%d, %.3f%% of what was "
                         "watched%s. A clock digit, a blinking caret and an idle animation all "
                         "look exactly like this, and so does a click that landed on something "
                         "with almost no visual feedback. Read the boxes above and say which one "
                         "it is; do not assume either way."
                         % (big[2] - big[0], big[3] - big[1], big_area * 100 / watched,
                            (", and that LARGEST box is %.0f px from where you clicked"
                             % (((max(0, max(origin[0] + big[0] - loc[0],
                                             loc[0] - origin[0] - big[2]))) ** 2
                                 + (max(0, max(origin[1] + big[1] - loc[1],
                                               loc[1] - origin[1] - big[3]))) ** 2) ** 0.5))))
        else:
            lines.append("Look at just those with Screenshot(region=...) instead of re-reading "
                         "the whole window - that is usually where the time goes.")

        if show:
            # 把变化的那一块直接画出来一起递回去。
            #
            # 9-15 量 opencode 那次会话：390 次调用里 Screenshot 93 + Click 83 +
            # Window 77，占了 65%，而每走一步的固定套路是
            # Window(front) -> ClickAndWatch -> Screenshot(变化区)。
            # 工具执行只占全程 4.0%，**96% 是它两次调用之间的推理，中位 14.9 秒**——
            # 所以"少一次往返"省的不是那 0.1 秒工具时间，是一整轮思考。
            # 既然这一步我们已经知道哪儿变了、手里也已经有变化之后的那张图，
            # 就没有理由让调用方再回来问一遍。
            try:
                from fastmcp.utilities.types import Image
                from windows_mcp.tools.snapshot import _encode

                # opencode 9-15 第二轮报的：「多个相距很远的小变化会取巨大并集，
                # 例如时钟和底栏同时变化时，返回一张大面积但有效内容很少的缩略图」。
                # 取全部框的并集，等于让**最没信息量的那两个小框**决定整张图的大小。
                #
                # 所以先把明显是噪声的小框剔掉（时钟、光标、进度点），
                # 再看剩下的并集划不划算：并集面积比这些框自身面积之和大太多，
                # 说明它们散在两头，中间全是空的——那还不如只给最大的那一个框。
                # 先按"挨得近"聚类，再挑**变化面积最大的那一组**，
                # 而不是挑最大的单个框——一张牌常被拆成牌边 + 点数好几个碎框，
                # 挑单框会只截到一道 16x240 的发光边。
                groups = cluster_boxes(boxes)
                # 一次动作常常在**两个地方**同时留下结果：牌从这一列移到那一列，
                # 源和目标各变一块。Codex 9-15 第二轮报的 P1 就是这个——
                # 「跨列移牌需要同时看到来源翻出的牌」，只给最热闹的一组不够用。
                #
                # 所以第二组够大时也一起给（各自独立成图，各自带各自的换算公式）。
                # 不做拼图：拼起来只需解释一次布局，但坐标会变成第三套坐标系，
                # 而混着的坐标系正是 opencode 点名过的毛病。
                picks = [groups[0]]
                if len(groups) > 1 and groups[1][1] >= groups[0][1] * 0.25:
                    picks.append(groups[1])
                shown_n = sum(g[2] for g in picks)
                dropped = len(boxes) - shown_n

                pad = 40          # 上下文留宽一点，贴边裁常常缺一个角
                shots = []
                for ul, _area, _n in picks:
                    cl = max(0, ul[0] - pad)
                    ct = max(0, ul[1] - pad)
                    cr = min(after.width, ul[2] + pad)
                    cb = min(after.height, ul[3] + pad)
                    crop = after.crop((cl, ct, cr, cb))
                    # 变化铺满整窗时（整页重绘）就别递原尺寸了，等比缩一下；
                    # 缩了必须把换算公式一起给，否则坐标对不上。
                    scale = 1
                    if max(crop.size) > 1000:
                        scale = 2 if max(crop.size) <= 2000 else 3
                        crop = crop.resize((max(1, crop.width // scale),
                                            max(1, crop.height // scale)))
                    shots.append((crop, cl, ct, scale))
                data = _encode(shots[0][0])
                cl, ct, scale = shots[0][1], shots[0][2], shots[0][3]
                LAST_PICTURE.update({"hwnd": int(hwnd) if hwnd else None,
                                     "at": time.time(),
                                     "what": "the area that changed"})
                lines.append("")
                if dropped:
                    # 「挨得近就几乎肯定同属一物」也是个下得太满的判断。
                    # Codex 9-15 打 FreeCell 时逮到：7♦ 移到 8♠ 那一次，
                    # 这一组里**混进了远处的计时器**。聚类只知道像素挨得近，
                    # 不知道它们是不是一回事——这又是拿几何当语义。
                    lines.append("The picture below covers the BUSIEST cluster of changes - %d "
                                 "box(es) that sit close together. Boxes near each other are "
                                 "often one object (a card, a dialog, a row), but not always: "
                                 "a clock or a counter that happens to sit nearby gets swept in "
                                 "too. %d other box(es) are somewhere else on screen and are NOT "
                                 "in this picture; use Screenshot(region=...) on them if you need "
                                 "to see them." % (shown_n, dropped))
                if len(shots) > 1:
                    # 「两处变化 = 一次移动」是个**过强的语义判断**。
                    # opencode 9-15 第七轮：它双击 A♠ 没能入基础堆，
                    # 只产生了两处细小的发光边缘，而我照样说了"这就是移动的样子"。
                    # 它的原话：「不是单纯多给一张图，而是『移动』这个语义判断过强。」
                    # 两处都够大才配这么说；都是细边光晕时，只陈述"两处变了"。
                    both_big = all(g[1] >= 2500 for g in picks)
                    if both_big:
                        # 上一版我把"这就是移动的样子"限制到"两组都够大"，以为够了。
                        # 不够。opencode 9-15 第八轮：它点 (300,300) 只是**选中一张牌
                        # 并高亮候选落点**，牌一张没动，两组变化照样都够大，
                        # 于是这句话又说出来了。它的原话：
                        # 「两组变化虽够大，但『选中源牌 + 高亮目标』同样满足这个条件。」
                        #
                        # 面积根本区分不出"移动"和"选中联动"——我拿尺寸当语义用了。
                        # 那就别用：只说"两个较大的区域变了"，把它可能是什么一并列出来。
                        # Codex 的 P3：每次正常移牌都重复同一长段免责声明，读多了有负担。
                        # 它说得对——一句话讲得完的事不该占五行。
                        # 该保的是"面积分不出语义"这个要点，不是把三种可能逐一铺开。
                        lines.append("TWO pictures follow: two larger areas changed. That is what "
                                     "a move looks like - and also what selecting something and "
                                     "lighting up its destinations looks like. Size cannot tell "
                                     "them apart; the pictures can. Each picture has its OWN "
                                     "coordinate formula.")
                    else:
                        lines.append("TWO pictures follow: two separate places changed. They are "
                                     "SMALL, though - thin edges and glows look like this, and so "
                                     "does a highlight that appeared without anything actually "
                                     "moving. Do not read this as a completed move unless the "
                                     "pictures show it. Each has its OWN coordinate formula.")
                else:
                    lines.append("The picture below is that changed area as it looks NOW, cropped "
                                 "from the very frame this report was computed on - you do not "
                                 "need a follow-up Screenshot for it.")
                for n, (crop, scl, sct, sscale) in enumerate(shots, start=1):
                    tag = ("picture %d" % n) if len(shots) > 1 else "that picture"
                    lines.append("!! COORDINATES in %s (%dx%d): screen_x = %d + image_x * %d,  "
                                 "screen_y = %d + image_y * %d."
                                 % (tag, crop.width, crop.height,
                                    origin[0] + scl, sscale, origin[1] + sct, sscale))
                    if sscale > 1:
                        lines.append("   (Shrunk to 1/%d because that change covered most of the "
                                     "window.)" % sscale)
                # 和 Screenshot 一样：直接给字符串 + Image 对象，让 FastMCP 自己包。
                # 手动拼 {"type":"text"} 字典会被当成普通数据序列化成 JSON 字符串，
                # 调用方看到的就是一坨转义后的 JSON 而不是正文。
                out = ["\n".join(lines)]
                for crop, _a, _b, _c in shots:
                    out.append(Image(data=_encode(crop), format="png"))
                return out
            except Exception as e:
                lines.append("(could not attach a picture of the changed area: %s - use "
                             "Screenshot(region=...) on the boxes above)" % e)
        return "\n".join(lines)
