"""PATCH: FindText — 屏幕文字识别（Windows 自带离线 OCR，不联网、不上传截图）。

为什么加：自绘界面（微信/Blender/剪映这类）UIA 读不到控件，只能靠人眼在截图上量坐标，
9-13 一天里本机和 test-client 各因坐标换算点偏一次（后者偏了 47px）。有了这个工具，
"找屏幕上的『发表』两个字"直接返回屏幕坐标，不经过人眼估算、不需要任何缩放换算。

三层定位策略，从稳到糙：
  1. Find      —— 能读到控件树时用语义选择器（最稳，不受窗口移动/缩放影响）
  2. FindText  —— 读不到控件但有文字时用本工具
  3. Click(loc)—— 纯图标无文字才退回坐标
"""

import ctypes

from PIL import Image
import time
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics

_ENGINE = None
_ENGINE_ERR = None


def _engine():
    """惰性创建 OCR 引擎；失败原因只算一次，避免每次调用都重试一遍慢路径。"""
    global _ENGINE, _ENGINE_ERR
    if _ENGINE is not None or _ENGINE_ERR is not None:
        return _ENGINE
    try:
        import winrt.windows.media.ocr as _ocr
        import winrt.windows.globalization as _glob
        eng = _ocr.OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            langs = list(_ocr.OcrEngine.available_recognizer_languages)
            if langs:
                eng = _ocr.OcrEngine.try_create_from_language(langs[0])
        if eng is None:
            _ENGINE_ERR = ("no OCR recognizer language is installed. Add one with: "
                           "Add-WindowsCapability -Online -Name Language.OCR~~~zh-CN~0.0.1.0 (run as admin)")
        _ENGINE = eng
    except ImportError as e:
        _ENGINE_ERR = ("WinRT bindings missing (%s). Install into the server venv: "
                       "uv pip install winrt-runtime winrt-Windows.Media.Ocr "
                       "winrt-Windows.Graphics.Imaging winrt-Windows.Globalization" % e)
    except Exception as e:
        _ENGINE_ERR = "%s: %s" % (type(e).__name__, e)
    return _ENGINE



def _await(op, timeout=30.0):
    """等 WinRT IAsyncOperation 完成再取结果。直接 get_results() 会报
    [WinError -2147483634] 在意外的时间调用了方法。"""
    import winrt.windows.foundation as _wf
    t0 = time.time()
    while op.status == _wf.AsyncStatus.STARTED:
        if time.time() - t0 > timeout:
            raise TimeoutError("WinRT async operation timed out after %.0fs" % timeout)
        time.sleep(0.004)
    if op.status != _wf.AsyncStatus.COMPLETED:
        raise RuntimeError("WinRT async op failed: status=%s" % (op.status,))
    return op.get_results()

def _grab(region):
    """抓屏为 PIL Image。region=(l,t,r,b) 屏幕坐标；None=全屏（含多显示器虚拟桌面）。"""
    from PIL import ImageGrab
    ctypes.windll.user32.SetProcessDPIAware()
    im = ImageGrab.grab(bbox=region, all_screens=True)
    return im.convert("RGB")


def _grab_for(window, process, hwnd, nth, region):
    """决定这次 OCR 抓哪儿。返回 (image, (ox,oy), target_hwnd, note)。

    给了 window/process/hwnd 就**只抓那个窗口自己的内容**（PrintWindow），被别的窗口
    盖住也读得到；否则退回抓屏。ox/oy 是把窗口内坐标换算成屏幕坐标的偏移。

    9-14 实测：钉钉被 Claude 窗口完全盖住时，抓屏裁剪拍到的是 Claude，
    PrintWindow 拿到了钉钉的完整界面。多开的四个微信从此可以各读各的，谁也不用挪谁。
    """
    if not (window or process or hwnd):
        box = tuple(int(v) for v in region) if region else None
        return _grab(box), ((box[0], box[1]) if box else (0, 0)), None, ""

    from windows_mcp.tools.window_ctl import capture_window, resolve_target, _state, _describe
    h, err = resolve_target(window, process, hwnd, nth)
    if err:
        return None, (0, 0), None, err
    if _state(h) == "minimized":
        return None, (0, 0), h, ("window '%s' is MINIMIZED - a minimized window has no real surface to draw "
                                 "(its rect is a tiny off-screen placeholder), so it cannot be read either. "
                                 "Restore it first: Window(mode='normalize', hwnd=%d)."
                                 % (_describe(h)["title"][:40], h))
    im, rect = capture_window(h)
    if im is None:
        return None, (0, 0), h, ("PrintWindow returned nothing for hwnd=%d - some windows refuse to render into a "
                                 "foreign DC. Bring it to the front (Window(mode='front')) and read the screen instead."
                                 % h)
    ox, oy = rect[0], rect[1]
    if region:                                  # region 是屏幕坐标，换算成窗口内坐标再裁
        l, t, r, b = (int(v) for v in region)
        im = im.crop((max(0, l - ox), max(0, t - oy),
                      min(im.width, r - ox), min(im.height, b - oy)))
        ox, oy = max(ox, l), max(oy, t)
    return im, (ox, oy), h, ""


def _as_box(v):
    """把 [l,t,r,b] 归一化：模型经常把列表序列化成字符串（'[1,2,3,4]'）。

    上游的 loc 早就有 _as_loc 做这件事（注释里写着"Claude Desktop strips anyOf schemas
    and the model serializes lists as strings"），我们的 region 一直漏了——
    9-14 让 opencode 跑真任务时连撞两次 pydantic 校验错，就是这个。
    """
    if v is None or isinstance(v, (list, tuple)):
        return list(v) if v is not None else None
    if isinstance(v, str):
        import json as _j
        try:
            return _j.loads(v)
        except Exception:
            return None
    return None


def cjk_warning(needle):
    """要找的词全是汉字时，提醒调用方 OCR 可能把字拆开。

    9-16 真实代价：我找「自测」三次都说"找不到"，于是三次判定
    "测试帖已经删干净了"——**而帖子一直在**。OCR 把「测」读成了「氵则」。
    换成同一行里的拉丁片段（`uia-desktop`）一次就中。

    这不是微信独有的：任何中文界面都可能这样，而失败的样子是**静默的**
    ——它不报错，只是"没找到"，看起来和"确实不存在"一模一样。
    所以提醒必须出现在"没找到"那条返回里，那是人唯一会读它的时刻。
    """
    t = (needle or "").strip()
    if not t:
        return ""
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    if cjk and cjk == len(t):
        return (" NOTE: %r is all Chinese, and OCR sometimes splits a character into its "
                "radicals (it read 测 as 氵则 on this machine), which makes an exact match fail "
                "silently - 'not found' then looks exactly like 'not there'. If the same line "
                "has Latin letters or digits, search for those instead and you will hit it." % t)
    return ""


def _score_line(raw, needle, needle_ns):
    """0 = 不匹配；1.0 = 精确包含；0<x<1 = 模糊相似度。

    抽到模块级是为了让 Click(text=...) 和 FindText 用同一套判据——
    两处各写一份迟早会不一致。
    """
    import difflib
    low_ns = (raw or "").lower().replace(" ", "")
    if needle in (raw or "").lower() or needle_ns in low_ns:
        return 1.0
    if not needle_ns or not low_ns:
        return 0.0
    best = difflib.SequenceMatcher(None, needle_ns, low_ns).ratio()
    # 目标短、行长时整行比对会被稀释，再按等长窗口滑一遍
    n = len(needle_ns)
    if len(low_ns) > n:
        for i in range(len(low_ns) - n + 1):
            best = max(best, difflib.SequenceMatcher(None, needle_ns, low_ns[i:i + n]).ratio())
    return best


def _words_for(line, needle_ns):
    """在匹配到的那一行里，framed 出**真正对上的那几个词**，而不是整行。

    9-14 真机验证抓到的 bug：记事本菜单栏 OCR 成一行「文件 编辑 查看 …」，
    按整行取中心点会落在「编辑」附近，点下去开的是错的菜单。
    命中的是行里的哪几个词，就只框那几个词。

    对不上（模糊匹配、或者词切分和字符位置对不齐）时退回整行——
    宁可回到老行为，也不要返回一个凭空算出来的框。
    """
    words = list(line.words)
    if not words or not needle_ns:
        return words
    spans, pos = [], 0
    for w in words:
        t = (w.text or "").replace(" ", "").lower()
        spans.append((pos, pos + len(t), w))
        pos += len(t)
    joined = "".join((w.text or "").replace(" ", "").lower() for w in words)
    at = joined.find(needle_ns)
    if at < 0:
        return words
    end = at + len(needle_ns)
    hit = [w for s, e, w in spans if s < end and e > at]
    return hit or words


def _screen_fallback_region(target, region):
    """退回屏幕读时，**把范围压回目标窗口在屏幕上的矩形**。

    9-15 自验时逮到的：整屏退回会读到别的应用里的同名文字——
    我拿微信窗口找「朋友圈」，退回整屏之后命中的坐标落在
    WorkBuddy 的报告正文里。那个坐标点下去就是点进了另一个程序。

    我们知道目标窗口在屏幕上占哪块，那就只读那块：
    既绕过了 PrintWindow 读不到 CEF 层的问题，
    又保住了"这个结果属于那个窗口"的含义。
    """
    if region:
        return list(region)
    if not target:
        return None
    try:
        from windows_mcp.tools.window_ctl import _rect
        l, t, r, b = _rect(target)
        if r > l and b > t:
            return [l, t, r, b]
    except Exception:
        pass
    return None


def preview_text(text, window=None, process=None, hwnd=None, nth=0, region=None,
                 fuzzy=0.6):
    """把**所有**命中都找出来并画框，不点任何东西。返回 (说明, 标注图, 命中列表)。

    WorkBuddy 9-15 排 P0-3 的那条，它的理由是决定性的：

    > 确认框标题是「删除该朋友圈?」，按钮文字是「删除」。OCR 是 substring 匹配，
    > `text="删除"` 会同时命中标题和按钮，`click_nth` 的顺序也没有保证。
    > ……**有了它我才敢在不可逆动作上用 click_text 锚点法**，否则永远要手算坐标。

    这正是问题所在：越是不可逆的按钮（删除、发表、确认），
    它的文案越容易和标题、提示语重复；而"点错一个不可逆按钮"的代价，
    远高于多花一次调用先看一眼。所以给一条**看得见落点、但什么都不做**的路。
    """
    from PIL import ImageDraw

    im, (ox, oy), target, note = _grab_for(window, process, hwnd, nth, region)
    if im is None:
        return ("preview: could not read the screen: %s" % note), None, []
    needle = (text or "").strip().lower()
    needle_ns = needle.replace(" ", "")
    try:
        res, scale = _recognize(im)
    except Exception as e:
        return ("preview: OCR unavailable: %s" % e), None, []

    hits = []
    for ln in res.lines:
        sc = _score_line(ln.text or "", needle, needle_ns)
        if sc <= 0 or (sc < 1.0 and sc < fuzzy):
            continue
        words = _words_for(ln, needle_ns)
        if not words:
            continue
        l = min(w.bounding_rect.x for w in words)
        t = min(w.bounding_rect.y for w in words)
        r = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        b = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        l, t, r, b = (v / scale for v in (l, t, r, b))
        hits.append({"score": sc, "line": (ln.text or "")[:60],
                     "box": (int(ox + l), int(oy + t), int(ox + r), int(oy + b)),
                     "centre": (int(ox + (l + r) / 2), int(oy + (t + b) / 2))})
    hits.sort(key=lambda h: (h["centre"][1], h["centre"][0]))

    shot = im.copy().convert("RGB")
    d = ImageDraw.Draw(shot)
    for i, h in enumerate(hits):
        bl, bt, br, bb = h["box"]
        d.rectangle([bl - ox - 3, bt - oy - 3, br - ox + 3, bb - oy + 3],
                    outline=(255, 40, 40), width=3)
        d.text((bl - ox, max(0, bt - oy - 14)), str(i), fill=(255, 40, 40))
    return "", shot, hits


def resolve_text(text, window=None, process=None, hwnd=None, nth=0, region=None,
                 fuzzy=0.6, occurrence=0):
    """现场 OCR 找这段文字，返回 ((x, y), 说明, 目标窗口句柄)；找不到时第一项是 None。

    第三项给自动置前用：知道"这个坐标本该属于谁"，才能在点下去之前验一句归属。

    给 Click(text=...) 用：**点击那一刻**才去定位，坐标不经过模型的手，
    所以不存在"图上量的坐标过期了"这回事。

    9-14 看千问办公发朋友圈时的真实失败催生的：它记着之前截图上「发表」按钮在 (481,538)，
    可文案一长，编辑框把按钮顶下去了，那一点打开的是「谁可以看」隐私选择器。
    窗口本身没动，所以 frame 守卫也拦不住——只有现场重新定位才躲得开这一类。
    """
    im, (ox, oy), target, note = _grab_for(window, process, hwnd, nth, region)
    if im is None:
        return None, "could not read the screen: %s" % note, None

    needle = (text or "").strip().lower()
    needle_ns = needle.replace(" ", "")

    def _pass(image, extra):
        """跑一遍 OCR 并打分。extra 是这一遍额外的放大倍数。"""
        res, scale = _recognize(image)
        scored = [(ln, _score_line(ln.text or "", needle, needle_ns)) for ln in res.lines]
        exact = [(ln, sc) for ln, sc in scored if sc >= 1.0]
        got, ap = exact, False
        if not got and fuzzy > 0:
            cand = sorted([(ln, sc) for ln, sc in scored if sc >= fuzzy], key=lambda x: -x[1])
            got, ap = cand, bool(cand)
        return got, ap, res, scale * extra

    # 作用域读不到时，自动退回整屏再读一次。
    #
    # WorkBuddy 9-15 在微信朋友圈上撞出来的 P0-2。它的原话：
    #   FindText '重新编辑' ... 窗口作用域只拿到左侧原生聊天列表 29 行，
    #   右侧朋友圈面板一个字都没有；换屏幕作用域就命中。
    #   「窗口作用域即便窗口被遮挡也能读」这个卖点，对朋友圈/公众号这类
    #   CEF 面板**不成立**。不修的话，使用者会据此做出错误的路径选择。
    #
    # 它还撞到更糟的一种：同一个调用直接抛 `WinRT async op failed: status=3`，
    # 而目标文字就在屏幕上。那条错误信息对使用者毫无指向性。
    #
    # 所以这里兜两种失败：抛异常、和读到了但一行都没匹配上。
    # 两种都退回整屏重来，并且**在结果里说清楚发生了什么**——
    # 悄悄退回去成功了，使用者会继续以为窗口作用域是可靠的，下次照样踩。
    fell_back = ""
    scoped = bool(window or process or hwnd)
    try:
        picked, approx, res, scale = _pass(im, 1)
    except Exception as e:
        if not scoped:
            return None, "OCR unavailable: %s" % e, None
        try:
            im2, (ox2, oy2), _t2, _n2 = _grab_for(
                None, None, None, 0, _screen_fallback_region(target, region))
            if im2 is None:
                return None, "OCR unavailable: %s" % e, None
            picked, approx, res, scale = _pass(im2, 1)
            im, ox, oy = im2, ox2, oy2
            fell_back = (" NOTE: reading that window on its own raised %r, so this fell back to "
                         "reading the SCREEN and that worked. Windows with an embedded web/CEF "
                         "layer (WeChat Moments, official-account pages, Electron panels) often "
                         "render nothing into a window-only capture. For those, the "
                         "'readable even when covered' property does NOT hold - bring the window "
                         "to the front and read the screen." % str(e)[:60])
        except Exception:
            return None, "OCR unavailable: %s" % e, None

    # 第一遍没找到就放大 2× 再试一次。
    # 9-15 在微软纸牌底栏上量出来的：1× 时「更多」**完全不在 OCR 输出里**，
    # 2× 就读出来了，而且不慢（0.07s -> 0.03s）。小字、深底浅字这类最吃这一下。
    # 只在已经失败时才付这个代价，成功路径一点不变。
    retried = False
    if not picked:
        try:
            # 放大的同时**把对比度也拉起来**。
            #
            # 只放大是不够的：9-16 在微信朋友圈编辑框上实测，
            # 「发表」「取消」是浅底浅灰字，单纯放大 3 倍只读出「取消」，
            # 放大 2 倍 + 自动对比度两个都读出来了。
            # 这条坑一天里撞了四次（test client 的微信右键菜单、记事本的 Markdown 框、
            # 千问办公读剪映按钮、这次的发表键），共同点从来不是字小，
            # 是**字和背景的差太小**——所以单纯放大救不回来。
            big = enhance(im, 2)
            p2, a2, r2, sc2 = _pass(big, 2)   # 图放大 2 倍，坐标就要除以 2 倍，不是乘
            if p2:
                picked, approx, res, scale, retried = p2, a2, r2, sc2, True
        except Exception:
            pass

    # 放大也没找到、而且这次是按窗口读的：退回整屏再来一遍。
    # CEF 面板在窗口自绘里是空的，放大一个空白没有任何意义。
    if not picked and scoped and not fell_back:
        try:
            im2, (ox2, oy2), _t2, _n2 = _grab_for(
                None, None, None, 0, _screen_fallback_region(target, region))
            if im2 is not None:
                p3, a3, r3, sc3 = _pass(im2, 1)
                if p3:
                    picked, approx, res, scale = p3, a3, r3, sc3
                    im, ox, oy = im2, ox2, oy2
                    fell_back = (" NOTE: that window's own content did not contain this text, but "
                                 "reading the SCREEN found it. That gap means the window has an "
                                 "embedded web/CEF layer (WeChat Moments, official-account pages, "
                                 "Electron panels) which renders nothing into a window-only "
                                 "capture. For windows like this the 'readable even when covered' "
                                 "property does NOT hold: the coordinates below were read off the "
                                 "SCREEN inside that window's rectangle, so if something is "
                                 "covering it right now, that is what a click would hit - check "
                                 "with Window(mode='at') or pass expected_hwnd.")
        except Exception:
            pass

    if not picked:
        sample = " | ".join((ln.text or "")[:20] for ln in list(res.lines)[:8])
        tip = ""
        if len(needle_ns) <= 1:
            # 这条是硬限制，不是我们能修的：放大到 4× 也读不出孤立的单字。
            tip = (" NOTE: %r is a single character. Windows OCR routinely omits an isolated "
                   "character entirely - it is not in the output at all, so no amount of fuzzy "
                   "matching will find it. Anchor on a longer neighbouring label and offset to "
                   "it with click_dx/click_dy instead." % text)
        if scoped:
            # 连整屏都没找到，但值得告诉调用方我们已经两条路都试过了，
            # 免得它下一步又去试"换个作用域"这条已经走过的路。
            tip += (" Both scopes were tried: that window's own content, and then the whole "
                    "screen. So this is not the window-only-capture problem that WeChat "
                    "Moments and other embedded-web panels have.")
        return None, (cjk_warning(text)
                      + "no line matched %r (%d lines read, tried again at 2x). Lines seen: %s%s"
                      % (text, len(list(res.lines)), sample or "(none)", tip)
                      + blank_read_note()), target
    if occurrence >= len(picked):
        return None, ("matched %d line(s) but occurrence:%d is out of range"
                      % (len(picked), occurrence)), target

    line = picked[occurrence][0]
    words = _words_for(line, needle_ns)
    l = min(w.bounding_rect.x for w in words)
    t = min(w.bounding_rect.y for w in words)
    r = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
    b = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
    l, t, r, b = (v / scale for v in (l, t, r, b))
    xy = (int(ox + (l + r) / 2), int(oy + (t + b) / 2))
    how = "fuzzy" if approx else "exact"
    hit = "".join(w.text or "" for w in words)
    whole = line.text or ""
    note = '%s match "%s" at %s' % (how, hit[:40], xy)
    if retried:
        note += " (only found after retrying the OCR at 2x)"
    if len(words) < len(list(line.words)):
        note += ' (narrowed from the whole line "%s")' % whole[:50]
    # 退回屏幕这件事必须说。悄悄退回去成功了，使用者会继续以为
    # 窗口作用域对这个应用是可靠的，下一次照样踩同一个坑。
    #
    # 而且退回屏幕之后，**读到的可能是盖在上面那个窗口的字**——
    # 窗口作用域本来是"不怕遮挡"的，退回屏幕就把这个性质弄丢了。
    # 所以命中点当场验一句归属：不归目标窗口就大声说，别让它闷头点下去。
    if fell_back:
        note += fell_back
        try:
            from windows_mcp.tools.window_ctl import _pid, _proc_name, _root_at
            at = _root_at(xy[0], xy[1])
            if target and at and int(at) != int(target):
                note += (" !! AND RIGHT NOW THAT POINT IS NOT YOURS: (%d,%d) currently belongs to "
                         "hwnd=%d (%s). The text was found on screen inside your window's "
                         "rectangle, but something is covering it there. Clicking would hit the "
                         "other window - raise yours first, or pass expected_hwnd=%d so the click "
                         "refuses instead."
                         % (xy[0], xy[1], at, _proc_name(_pid(at)) or "?", int(target)))
        except Exception:
            pass
    return xy, note, target


def read_text_lines(region=None, hwnd=None):
    """把一块区域里的文字读成一个行列表。给别的工具当零件用，不走 MCP。

    9-15 WorkBuddy 在微信上撞出来的需求：点完删除弹出一个 180x180 的确认框，
    `PrintWindow` 对它返回空（有些窗口拒绝渲染到外来 DC），
    于是我们只能报一句"新开了个窗口 hwnd=... 180x180"——它的原话是
    「等于把我指向死胡同」。
    但我们**知道那个窗口在屏幕上的矩形**，那就按屏幕坐标去读那一块：
    确认框会自报「删除该朋友圈?」，toast 会自报「已删除」。
    调用方要的从来不是句柄，是**那个窗口说了什么**。
    """
    try:
        im = _grab(region if region else None) if not hwnd else None
        if hwnd and im is None:
            from windows_mcp.tools.window_ctl import capture_window
            im, _box = capture_window(hwnd)
        if im is None:
            return []
        res, scale = _recognize(im)
        out = []
        for ln in res.lines:
            t = (ln.text or "").strip()
            if t:
                out.append(t)
        return out
    except Exception:
        return []


def targets_in(region=None, hwnd=None, limit=40):
    """把一块区域里**能当落点用的东西**列出来，坐标已经换算成屏幕坐标。

    Codex 和 WorkBuddy 9-15 各自独立提了同一条 P1。Codex 的原话：
    「`Screenshot(region=...)` 返回图之后，我还得自己在图上量像素、
    再加回 region 的左上角。这一步的算术应该由工具做。」
    WorkBuddy 的版本是「本区域可用目标屏幕坐标」。

    它们说的是同一件事，而且要的正是我们**已经算过一遍**的东西：
    `FindText` 内部就把 OCR 框换算成屏幕坐标了，只是从没把整份清单交出来过。
    调用方于是退回到"看图数像素"——那是这套工具里最容易出错的一步，
    而且错了不报错，只是点偏。

    只给 OCR 行，不给 UIA 控件树：要区域截图的场合，
    十有八九正是控件树读不到的那些（剪映的 Qt 自绘、微信的内嵌 CEF）——
    树读得到的时候调用方本来就该用 Find/Click 选择器，不该看图。
    """
    try:
        if hwnd:
            from windows_mcp.tools.window_ctl import capture_window
            im, box = capture_window(hwnd)
            ox, oy = (box[0], box[1]) if box else (0, 0)
        else:
            im = _grab(region if region else None)
            ox, oy = (int(region[0]), int(region[1])) if region else (0, 0)
        if im is None:
            return []
        res, scale = _recognize(im)
    except Exception:
        return []

    out = []
    for ln in res.lines:
        t = (ln.text or "").strip()
        if not t:
            continue
        try:
            words = list(ln.words)
            l = min(w.bounding_rect.x for w in words)
            tp = min(w.bounding_rect.y for w in words)
            r = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
            b = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        except Exception:
            continue
        l, tp, r, b = (v / scale for v in (l, tp, r, b))
        out.append({"text": t[:40],
                    "centre": (int(ox + (l + r) / 2), int(oy + (tp + b) / 2)),
                    "box": (int(ox + l), int(oy + tp), int(ox + r), int(oy + b))})
    # 按**阅读顺序**排，不是按纯 y 排。同一行的两个东西 y 差几个像素很正常，
    # 纯 y 排序会把右边那个排到左边那个前面——调用方照着序号找东西会找错。
    # 行高的一半当容差：比这更近的算同一行。
    if out:
        hs = [h["box"][3] - h["box"][1] for h in out] or [20]
        tol = max(8, sorted(hs)[len(hs) // 2] // 2)
        out.sort(key=lambda h: (h["centre"][1] // tol, h["centre"][0]))
    return out[:limit]


def icon_targets(region, want=3, tol=0.5, axis="auto"):
    """在一块区域里找**一排等距、大小相近的图标**，返回中心点（屏幕坐标）。

    axis: "h" 横排、"v" 竖排、"auto"（默认，按区域长宽比猜）。
    竖排导航栏（VS Code / Slack / Discord / Teams / 微信 左侧那条）
    和横排工具栏一样常见，判据完全相同，只是扫描方向换一下。

    为什么需要它：自绘应用的图标栏没有文字、UIA 树也是空的，
    我们原来只能说"这里没有可点的东西"，调用方只好截图数像素——
    而按窗口边缘算出来的偏移，换一个尺寸的窗口就落空（9-16 实测栽过）。

    判据是**和背景的差异**，不是"偏白"或"偏黑"。第一版只认接近纯白/纯黑的像素，
    结果在任务栏上一个图标都找不到——真实图标是**彩色**的，
    既不白也不黑，正好卡在两个阈值中间。
    先取这条带里出现最多的颜色当背景，再把离它远的像素当图案。

    找到之后还要过一道几何关：**一排大小相近、间距均匀**才算图标栏。
    正文、单个按钮、装饰图案都不满足，所以误报很少。
    """
    try:
        im = _grab(tuple(int(v) for v in region))
    except Exception:
        return []
    if im is None:
        return []
    im = im.convert("RGB")
    if axis == "auto":
        # 明显竖长的区域按竖排找，其余按横排。拿不准就按横排——
        # 横排是更常见的那一种，猜错了调用方还能显式指定 axis。
        axis = "v" if im.size[1] > im.size[0] * 1.6 else "h"
    if axis == "v":
        # 转置之后，原来的"竖"就成了"横"，下面那套判据一个字都不用改。
        pts = [(b, a) for (a, b) in _icon_run(im.transpose(Image.TRANSPOSE), want, tol)]
    else:
        pts = _icon_run(im, want, tol)
    return [(int(region[0]) + a, int(region[1]) + b) for a, b in pts]


def _icon_run(im, want, tol):
    """图内坐标下找一排图标。横着扫；竖排由调用方转置后走同一条路。"""
    w, h = im.size
    px = im.load()
    best = []
    for y0 in range(0, max(1, h - 24), 4):
        y1 = min(h, y0 + 34)
        # 这条带的背景色：取出现最多的那个（量化到 16 级，抗噪）
        from collections import Counter
        cnt = Counter()
        for x in range(0, w, 3):
            for y in range(y0, y1, 3):
                r_, g_, b_ = px[x, y]
                cnt[(r_ >> 4, g_ >> 4, b_ >> 4)] += 1
        if not cnt:
            continue
        bg = [(v << 4) + 8 for v in cnt.most_common(1)[0][0]]
        cols = {}
        for x in range(w):
            n = 0
            for y in range(y0, y1):
                r_, g_, b_ = px[x, y]
                if abs(r_ - bg[0]) + abs(g_ - bg[1]) + abs(b_ - bg[2]) > 150:
                    n += 1
            if n >= 3:
                cols[x] = n
        xs = sorted(cols)
        groups, cur = [], None
        for x in xs:
            if cur and x - cur[1] <= 8:
                cur[1] = x
            else:
                if cur:
                    groups.append(cur)
                cur = [x, x]
        if cur:
            groups.append(cur)
        groups = [g for g in groups if 8 <= g[1] - g[0] <= 56]
        if len(groups) < want:
            continue
        # 找**最长的一段等距连续图标**，而不是要求整条带都均匀。
        #
        # 9-16 在任务栏上验证时撞到的：那条带里除了图标还有个搜索框，
        # 头几个间距是 174/42/58，而后面 18 个稳定在 31~39。
        # "全部均匀"这条要求把一整排真图标全否掉了——
        # 判据写得太严和写得太松一样坏，只是失败的样子不同：它安静地一个都不给。
        if len(groups) < want:
            continue
        run_best = []
        i = 0
        while i < len(groups) - 1:
            j = i + 1
            gaps = [groups[i + 1][0] - groups[i][1]]
            while j < len(groups) - 1:
                nxt = groups[j + 1][0] - groups[j][1]
                gm = sum(gaps) / float(len(gaps))
                wa = groups[j + 1][1] - groups[j + 1][0]
                w0 = groups[i][1] - groups[i][0]
                if abs(nxt - gm) > max(10, gm * tol) or abs(wa - w0) > max(8, w0 * tol):
                    break
                gaps.append(nxt)
                j += 1
            if j - i + 1 > len(run_best):
                run_best = groups[i:j + 1]
            i = j if j > i else i + 1
        if len(run_best) >= want and len(run_best) > len(best):
            best = [((g[0] + g[1]) // 2, _cross_centre(px, h, g, bg, (y0 + y1) // 2))
                    for g in run_best]
    return best


def _cross_centre(px, h, group, bg, fallback):
    """量出这个图标在**另一根轴**上的中心。

    扫描带只是"大概在这一条"，带心不等于图标心：带宽 34px，图标可能只占一半，
    也可能比带还宽。所以在图标自己的跨度里，逐行数一下有多少像素和背景不同，
    取最实的那一段连续区间取中——这才是能点中的地方。
    """
    x0, x1 = group
    if x1 < x0:
        return fallback
    rows = []
    for y in range(h):
        n = 0
        for x in range(x0, x1 + 1):
            r_, g_, b_ = px[x, y]
            if abs(r_ - bg[0]) + abs(g_ - bg[1]) + abs(b_ - bg[2]) > 150:
                n += 1
        rows.append(n)
    thr = max(1, (x1 - x0 + 1) // 6)
    runs, cur = [], None
    for y in range(h):
        if rows[y] >= thr:
            cur = [y, y] if cur is None else [cur[0], y]
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    if not runs:
        return fallback
    # "最实"而不是"最长"：窗口边框那种一像素的长条总量很小，压不过图标。
    r = max(runs, key=lambda s: sum(rows[s[0]:s[1] + 1]))
    return (r[0] + r[1]) // 2


def graphic_beside(im, box, lines_px, side="right", span=80, pad=10, floor=25):
    """紧挨着一段文字的**没有文字的图标**在哪。返回 (x, y, 像素数) 或 None。

    这是 9-16 一天里我手写了三遍的那件事：微信朋友圈的垃圾桶、封面上的头像、
    名片卡里那排缩略图——全都是"图标本身没有字，但它旁边有字"。
    没有这个原语，调用方只能看图估一个偏移，而估偏五个像素就点不中：
    那天我用「时间戳右边 +24px」，放大四倍才看清它落在图标的右边缘上，
    点了十几次没反应，还以为是应用卡死了。

    判据和 icon_targets 一样是**和背景的差异**，不是"偏白"或"偏黑"——
    真实图标是彩色的，两头都不沾。背景取这条带里出现最多的颜色。

    lines_px 是 OCR 已经认出来的文字框（图内坐标）。压在文字上的团块直接跳过：
    那是字，不是图标，而字的坐标 FindText 本来就给了。
    """
    x0, y0, x1, y1 = box
    if side == "right":
        sx0, sx1 = int(x1) + 4, int(x1) + 4 + span
    else:
        sx0, sx1 = int(x0) - 4 - span, int(x0) - 4
    sy0, sy1 = int(y0) - pad, int(y1) + pad
    sx0, sy0 = max(0, sx0), max(0, sy0)
    sx1, sy1 = min(im.width, sx1), min(im.height, sy1)
    if sx1 - sx0 < 8 or sy1 - sy0 < 8:
        return None
    try:
        sub = im.crop((sx0, sy0, sx1, sy1)).convert("RGB")
    except Exception:
        return None
    px = sub.load()
    from collections import Counter
    cnt = Counter()
    for x in range(0, sub.width, 2):
        for y in range(0, sub.height, 2):
            c = px[x, y]
            cnt[(c[0] >> 4, c[1] >> 4, c[2] >> 4)] += 1
    if not cnt:
        return None
    bg = [(v << 4) + 8 for v in cnt.most_common(1)[0][0]]
    pts = []
    for x in range(sub.width):
        for y in range(sub.height):
            c = px[x, y]
            if abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > 150:
                pts.append((x + sx0, y + sy0))
    if len(pts) < floor:
        return None
    # 团块顶到了扫描带的外缘 = 它比这条带还宽，被切掉了一截，重心必然偏。
    # 这和 icon_targets 那次是同一个错：切了再取中，就会落在图标边缘上，
    # 而边缘差几个像素就点不中。加宽重来，最多加到 3 倍。
    edge = (sx1 - 1) if side == "right" else sx0
    if any(p[0] == edge for p in pts) and span < 240:
        wider = graphic_beside(im, box, lines_px, side=side, span=min(240, span * 3),
                               pad=pad, floor=floor)
        if wider:
            return wider
    cx = sum(p[0] for p in pts) / float(len(pts))
    cy = sum(p[1] for p in pts) / float(len(pts))
    for lb in lines_px or ():
        if lb[0] - 2 <= cx <= lb[2] + 2 and lb[1] - 2 <= cy <= lb[3] + 2:
            return None          # 这是字，不是图标
    return int(cx), int(cy), len(pts)


def targets_block(region=None, hwnd=None, limit=40):
    """targets_in 的文字版，直接拼进截图的说明里。读不到就返回空串——
    这块是**附赠**，它失败不该让截图本身失败。"""
    try:
        hits = targets_in(region=region, hwnd=hwnd, limit=limit)
    except Exception:
        return ""
    if not hits:
        return ("TARGETS: OCR found no text in this area, so there is nothing here I can hand you "
                "coordinates for. Measure off the picture yourself, and remember the offsets "
                "above.\n" + (blank_read_note() + "\n" if blank_read_note() else ""))
    lines = ["TARGETS in this area - these are SCREEN coordinates, ready to pass straight to "
             "Click / Move / Sequence. No arithmetic needed, and nothing here was clicked:"]
    for h in hits:
        lines.append("  (%d,%d)  %s" % (h["centre"][0], h["centre"][1], h["text"]))
    lines.append("  (%d line(s) of text. This is what OCR could read - an icon with no text will "
                 "not be in this list, and text being readable does not mean it is a button.)"
                 % len(hits))
    note = retry_note()
    if note:
        lines.append(note)
    # 补上没有文字的图标。上面那句"an icon with no text will not be in this list"
    # 是诚实的，但它描述的正是调用方最常卡住的地方，所以这里把它补上。
    if region:
        for ax, label, order in (("h", "ICONS (row)", "Left to right"),
                                 ("v", "ICONS (column)", "Top to bottom")):
            try:
                ic = icon_targets(region, axis=ax)
            except Exception:
                ic = []
            if not ic:
                continue
            lines.append("  %s - no text, found by shape: evenly spaced, similar-sized "
                         "blobs, which is what a toolbar or a side nav rail looks like:" % label)
            lines.append("    " + "  ".join("(%d,%d)" % p for p in ic))
            lines.append("    (%s as they appear. Shape cannot tell you what they DO - "
                         "hover one and read its tooltip if you need to know which is which.)"
                         % order)
    return "\n".join(lines) + "\n"


def _occlusion_note(target_hwnd, x, y):
    """能读不等于能点：坐标是屏幕坐标，窗口被盖住时点下去会打到上面那个窗口。"""
    if not target_hwnd:
        return ""
    try:
        # PATCH 0.5.5: 先给一个机器可读的字段，再给人话。
        # 原来只有下面那段散文，读漏了就等于没有；字段读不漏。
        from windows_mcp.tools.frame_guard import actionable
        ok, who = actionable(target_hwnd, x, y)
        if not ok:
            return ("  actionable=false (%s)  !! READ-ONLY: this point currently belongs to another "
                    "window, not your target - the text was read straight from the target even though "
                    "it is covered, but CLICKING here would hit the other window. Bring yours to the "
                    "front first, or move it somewhere clear." % who)
        return "  actionable=true"
    except Exception:
        pass
    return ""


def _to_softwarebitmap(im):
    """PIL Image -> WinRT SoftwareBitmap（走内存流 + BMP 解码，避免落盘）。"""
    import io
    import winrt.windows.graphics.imaging as _img
    import winrt.windows.storage.streams as _st

    buf = io.BytesIO()
    im.save(buf, format="BMP")
    data = buf.getvalue()

    stream = _st.InMemoryRandomAccessStream()
    writer = _st.DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(data)          # winrt 3.x 要 bytes，传 list 会报 "a bytes-like object is required"
    _await(writer.store_async())
    _await(writer.flush_async())
    writer.detach_stream()
    stream.seek(0)

    decoder = _await(_img.BitmapDecoder.create_async(stream))
    return _await(decoder.get_software_bitmap_async())


# 最近一次识别有没有走"放大重读"。只记最近一次，够用来在返回里说一句。
_LAST_RETRY = {"on": False, "k": 1, "gain": (0, 0)}
# 最近一次 OCR 读到了多少字。用来分清"读清楚了、上面没有"和"一个字都没读到"。
_LAST_READ = {"chars": 0, "lines": 0}


def blank_read_note():
    """上一次读几乎什么都没读到时，给一句**不许当成"没有"**的警告。

    9-16 的实账：凯琼的朋友圈窗 1342x971，页面上明明挂着一条测试帖，
    引擎只回一行 '0'（不是尺寸上限，本机 max_image_dimension=10000，是版面分析塌了）。
    于是"找不到测试帖"被判成"已经删干净"，而帖子是发给真实好友看的。

    读空和读到但没有，对下一步的指示正好相反。工具不说清楚，
    调用方就只能猜——而猜错的那一面代价极大。
    """
    c = _LAST_READ.get("chars", 0)
    if c >= 12:
        return ""
    return ("  !! READ NOTHING: this area returned %d character(s) of text in total, so a "
            "'not found' here is NOT evidence the thing is absent - it is evidence the area "
            "was not read. Large regions sometimes come back empty even when the page is "
            "plainly rendered. Read a smaller slice (half the height often works), or take a "
            "screenshot and look, before concluding anything." % c)


def retry_note():
    """把"这次读的时候放大了"写成一句话。没放大就返回空串。"""
    if not _LAST_RETRY.get("on"):
        return ""
    a, b = _LAST_RETRY.get("gain", (0, 0))
    return ("  (OCR read this area twice: the first pass got %d character(s), so it was "
            "re-read at %dx magnification, which got %d. Small panels need that; it is why "
            "this did not come back empty.)" % (a, _LAST_RETRY.get("k", 3), b))


def enhance(im, k=2):
    """放大 + 拉对比度。**两件事必须一起做**，只做一件都不够。

    9-16 现场实测（微信朋友圈编辑框，浅灰按钮「发表」「取消」）：
      原图                 -> 读不到
      只提对比度            -> 读不到
      只放大 3 倍           -> 只读到「取消」
      **放大 2 倍 + 自动对比度** -> 「发表」「取消」两个都读到

    这是那条反复出现的坑：test client 的微信右键菜单、我关记事本时的 Markdown 对话框、
    千问办公读剪映按钮——全是**浅底浅字**。它们的共同点不是字小，
    是**字和背景的差太小**，所以单纯放大救不回来。
    """
    from PIL import ImageOps
    if im is None:
        return None
    big = im.resize((max(1, im.width * k), max(1, im.height * k)), Image.LANCZOS)
    return ImageOps.autocontrast(big.convert("L"), cutoff=1).convert("RGB")


def _chars(res):
    """一次识别结果里一共多少个非空白字符。用来比较两遍读法谁读到的多。"""
    try:
        return sum(len((ln.text or "").replace(" ", "")) for ln in res.lines)
    except Exception:
        return 0


class _ShimRect(object):
    __slots__ = ("x", "y", "width", "height")

    def __init__(self, x, y, wd, ht):
        self.x, self.y, self.width, self.height = x, y, wd, ht


class _ShimWord(object):
    __slots__ = ("text", "bounding_rect")

    def __init__(self, text, rect):
        self.text, self.bounding_rect = text, rect


class _ShimLine(object):
    __slots__ = ("text", "words")

    def __init__(self, text, words):
        self.text, self.words = text, words


class _ShimResult(object):
    """只实现调用方真正用到的那几个属性：lines / text / words / bounding_rect。"""
    __slots__ = ("lines", "text")

    def __init__(self, lines):
        self.lines = lines
        self.text = "\n".join(l.text for l in lines)


def _tile_recognize(eng, im, band=420, overlap=90):
    """把图切成带重叠的横条逐条读，坐标按条偏移还原。

    为什么要重叠：切口正好落在一行字中间的话，那一行两边都读不全。
    重叠一条的高度足够覆盖一行，再按"行心落在本条的核心区"去重。
    """
    h = im.height
    if h <= band:
        return None
    ys = []
    y = 0
    while y < h:
        ys.append((y, min(h, y + band)))
        if y + band >= h:
            break
        y += band - overlap
    lines = []
    for i, (y0, y1) in enumerate(ys):
        try:
            sub = im.crop((0, y0, im.width, y1))
            r = _await(eng.recognize_async(_to_softwarebitmap(sub)))
        except Exception:
            continue
        for ln in r.lines:
            ws = list(ln.words)
            if not ws:
                continue
            # 全收下。宁可重复，绝不丢——重复能在下面去掉，丢了就再也找不回来。
            lines.append(_ShimLine(
                ln.text or "",
                [_ShimWord(w.text or "",
                           _ShimRect(w.bounding_rect.x, w.bounding_rect.y + y0,
                                     w.bounding_rect.width, w.bounding_rect.height))
                 for w in ws]))
    lines.sort(key=lambda l: (min(w.bounding_rect.y for w in l.words),
                              min(w.bounding_rect.x for w in l.words)))
    return _ShimResult(_dedupe_lines(lines)) if lines else None


def _dedupe_lines(lines):
    """同样的字、位置又挨着 = 同一行被两条各读了一遍。留框更完整的那个。

    不按"归哪一条管"去分，因为同一行在相邻两条里量出来的位置本就不一样
    （边上那条常被切掉一截），拿它和一个共用边界比，重复和丢失都会发生。
    """
    out = []
    for ln in lines:
        key = (ln.text or "").replace(" ", "")
        top = min(w.bounding_rect.y for w in ln.words)
        bot = max(w.bounding_rect.y + w.bounding_rect.height for w in ln.words)
        hi = max(8, bot - top)
        dup = None
        for j, old in enumerate(out):
            okey = (old.text or "").replace(" ", "")
            if not okey or not key:
                continue
            if okey != key and okey not in key and key not in okey:
                continue
            otop = min(w.bounding_rect.y for w in old.words)
            obot = max(w.bounding_rect.y + w.bounding_rect.height for w in old.words)
            # 比中心距，容差放到 1.2 个行高。
            # 边上那条会把字切掉一截，量出来的框往下挪半行——实测同一个 bravo
            # 两条里量到 top=369 和 top=395，差 26 而行高 25，卡在门槛外面就漏掉了。
            tol = 1.2 * max(hi, obot - otop, 8)
            if abs((otop + obot) / 2.0 - (top + bot) / 2.0) <= tol:
                dup = j
                break
        if dup is None:
            out.append(ln)
        elif len(ln.words) > len(out[dup].words):
            out[dup] = ln                  # 留读得更全的那个
    return out


def _recognize(im):
    _LAST_RETRY["on"] = False      # 先清掉：不清就会把上一次的放大记在这一次头上
    _LAST_READ["chars"] = 0
    _LAST_READ["lines"] = 0
    eng = _engine()
    if eng is None:
        raise RuntimeError(_ENGINE_ERR or "OCR engine unavailable")
    # 引擎有最大边长限制，超了先等比缩小，坐标再按比例还原
    import winrt.windows.media.ocr as _ocr
    limit = int(_ocr.OcrEngine.max_image_dimension)
    scale = 1.0
    if max(im.size) > limit:
        scale = limit / float(max(im.size))
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    bmp = _to_softwarebitmap(im)
    res = _await(eng.recognize_async(bmp))

    # 小图读不出来就放大再读一次。
    #
    # test client 9-15 报的（它自己没改，说不确定根因是分层窗口不进 BitBlt 还是 OCR 的尺度问题，
    # 交给上游判断——这个判断是对的）：微信右键菜单 160x116，
    # `Screenshot(region=)` 拍得清清楚楚、字全认得出，
    # 可 `ReadText(window=)` 只拿到被截断的第一行「选 照 片 或」，
    # 指定 region 直接 0 行。
    #
    # 「Screenshot 拍得到」本身就排除了第一个候选：像素抓得到，问题在读。
    # 合成同款图（160x116 深底浅字）实测：11~14px 原图读回 0/3 或一行残句
    # （14px 深底读回的正是 ['选 照 片 或']，和它在真机上看到的一模一样），
    # 放大 2~3 倍后 3/3 全认出来；放到 4 倍以上反而变差。
    # 深底浅底几乎没差别——纯粹是尺度问题。
    #
    # 不无条件放大：20px 以上的字放大了反而掉字。所以只在
    # **图本身就小、而且第一遍几乎什么都没读到**时重来一次，
    # 两个结果取字多的那个。失败路径多花约 30ms，成功路径一分钱不花。
    # 大图整块读空 -> 分带重读。
    # "读空"在这套工具里一直等价于"没有"，所以这条兜底不是锦上添花：
    # 少了它，一条挂在真实朋友圈上的测试帖会被报成"已经删干净"。
    if _chars(res) < 12 and im.height > 520:
        try:
            tiled = _tile_recognize(eng, im)
        except Exception:
            tiled = None
        if tiled is not None and _chars(tiled) > _chars(res):
            _LAST_RETRY["on"] = True
            _LAST_RETRY["k"] = 1
            _LAST_RETRY["gain"] = (_chars(res), _chars(tiled))
            _LAST_RETRY["tiled"] = True
            _LAST_READ["chars"] = _chars(tiled)
            _LAST_READ["lines"] = len(list(tiled.lines))
            return tiled, scale

    _LAST_READ["chars"] = _chars(res)
    _LAST_READ["lines"] = len(list(res.lines))
    if max(im.size) <= 700 and _chars(res) < 12:
        try:
            k = 3
            from PIL import Image as _PILImage
            # 重采样方式会改变识别结果：同一张 13px 的图，LANCZOS 能读出三行，
            # 默认的双三次只读出一行。放大是为了让笔画更清楚，就该用锐的那个。
            big = im.resize((im.width * k, im.height * k), _PILImage.LANCZOS)
            res2 = _await(eng.recognize_async(_to_softwarebitmap(big)))
            if _chars(res2) > _chars(res):
                _LAST_RETRY["on"] = True
                _LAST_RETRY["k"] = k
                _LAST_RETRY["gain"] = (_chars(res), _chars(res2))
                return res2, scale * k
        except Exception:
            pass
    return res, scale


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="FindText",
        description=(
            "Locate on-screen TEXT and get its SCREEN coordinates, using the offline Windows OCR engine "
            "(no network, nothing uploaded). Use this for self-drawn UIs where the accessibility tree is empty "
            "— WeChat, Blender, video editors, games, canvases — so you never have to eyeball a coordinate off a "
            "screenshot again (that is the single biggest source of misclicks: the screenshot you look at may be "
            "downscaled twice). Returns each match with its text, screen centre, box and confidence-ish word count, "
            "ready to pass straight to Click/PressMouse. Order of preference: Find (control tree) > FindText (this) "
            "> raw coordinates. Tip: to hit a small icon that sits next to a label, find the label and offset from it."
        ),
        annotations=ToolAnnotations(
            title="FindText", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "FindText-Tool")
    def find_text_tool(
        text: Annotated[str, Field(description="Text to look for. Case-insensitive substring match against each recognised line, and against the whole line with spaces removed (Chinese OCR often inserts spaces between characters).")],
        region: Annotated[list[int] | str | None, Field(description="Restrict the search to [left, top, right, bottom] in screen pixels. Much faster and more accurate than full screen; use the target window's rectangle when you know it.")] = None,
        fuzzy: Annotated[float, Field(description="Similarity threshold 0-1 used only when nothing matches exactly. Chinese OCR misreads characters often (it read 视频号 as 礻贝频号), so a fuzzy pass saves the call. 0 disables it.")] = 0.6,
        limit: Annotated[int, Field(description="Max matches to return.")] = 20,
        window: Annotated[str | None, Field(description="Read THIS window's own content instead of grabbing the screen - works even when the window is completely covered by another one, and never needs it brought to the front. Matches the window title (substring). Use it whenever the app is multi-instance (four WeChat windows) or something is on top of it.")] = None,
        process: Annotated[str | None, Field(description="Same as window= but matches the executable name, e.g. 'Weixin'.")] = None,
        hwnd: Annotated[int | None, Field(description="Exact window handle, from Window(mode='list').")] = None,
        nth: Annotated[int, Field(description="Which window to read when several match (0-based).")] = 0,
        ctx: Context = None,
    ) -> str:
        t0 = time.time()
        region = _as_box(region)
        im, (ox, oy), target, note = _grab_for(window, process, hwnd, nth, region)
        if im is None:
            return "FindText could not read anything: %s" % note
        try:
            res, scale = _recognize(im)
        except Exception as e:
            return "FindText unavailable: %s" % e
        needle = text.strip().lower()
        needle_ns = needle.replace(" ", "")
        lines = list(res.lines)
        # 判据和 resolve_text（Click(text=...) 走的那条）共用一份，免得两处慢慢长歪
        scored = [(ln, _score_line(ln.text or "", needle, needle_ns)) for ln in lines]
        exact = [(ln, s) for ln, s in scored if s >= 1.0]
        picked = exact
        approx = False
        if not picked and fuzzy > 0:
            cand = [(ln, s) for ln, s in scored if s >= fuzzy]
            cand.sort(key=lambda x: -x[1])
            picked = cand
            approx = bool(cand)

        hits = []
        for line, _sc in picked:
            raw = line.text or ""
            l = min(w.bounding_rect.x for w in line.words)
            t = min(w.bounding_rect.y for w in line.words)
            r = max(w.bounding_rect.x + w.bounding_rect.width for w in line.words)
            b = max(w.bounding_rect.y + w.bounding_rect.height for w in line.words)
            l, t, r, b = (v / scale for v in (l, t, r, b))
            hits.append({
                "text": raw,
                "cx": int(ox + (l + r) / 2), "cy": int(oy + (t + b) / 2),
                "box": (int(ox + l), int(oy + t), int(ox + r), int(oy + b)),
                "words": len(line.words),
            })
        dt = time.time() - t0
        if target:
            from windows_mcp.tools.window_ctl import _describe
            scope = "window '%s' own content %dx%d (drawn by the window itself, z-order irrelevant)" % (
                _describe(target)["title"][:30], im.width, im.height)
        else:
            scope = "region %s" % (list(region),) if region else "full screen %dx%d" % im.size
        if not hits:
            sample = " | ".join((ln.text or "")[:24] for ln in lines[:8])
            return ("FindText '%s': 0 matches in %s (%d lines read, %.2fs). "
                    "Lines seen: %s" % (text, scope, len(lines), dt, sample or "(none)"))
        kind = "fuzzy match(es) - VERIFY the text below before clicking" if approx else "match(es)"
        out = ["FindText '%s': %d %s in %s (%.2fs). Coordinates below are SCREEN pixels - pass them straight to Click/PressMouse:" % (text, len(hits), kind, scope, dt)]
        # 顺手把紧挨着文字的**无文字图标**量出来。
        # 删除的垃圾桶、头像、开关这类东西本身没有字，OCR 永远给不出它们，
        # 而调用方只能看图估偏移——估偏几个像素就点不中。
        # 在 OCR 用的同一张图上量，所以不受遮挡影响；量不到就什么都不说。
        lines_px = []
        try:
            for ln in lines:
                ws = list(ln.words)
                if ws:
                    lines_px.append((
                        min(w.bounding_rect.x for w in ws) / scale,
                        min(w.bounding_rect.y for w in ws) / scale,
                        max(w.bounding_rect.x + w.bounding_rect.width for w in ws) / scale,
                        max(w.bounding_rect.y + w.bounding_rect.height for w in ws) / scale))
        except Exception:
            lines_px = []
        said_why = False
        for i, h in enumerate(hits[:limit]):
            out.append('  nth:%d  centre=(%d,%d)  box=%s  "%s"%s'
                       % (i, h["cx"], h["cy"], h["box"], h["text"][:60],
                          _occlusion_note(target, h["cx"], h["cy"]) if i == 0 else ""))
            if i >= 3:
                continue
            bx = (h["box"][0] - ox, h["box"][1] - oy, h["box"][2] - ox, h["box"][3] - oy)
            for side in ("right", "left"):
                try:
                    got = graphic_beside(im, bx, lines_px, side=side)
                except Exception:
                    got = None
                if not got:
                    continue
                gx, gy, n = got
                out.append("      graphic %s of it at (%d,%d) - %d pixels that differ from the "
                           "background, and no OCR text there%s"
                           % (side, int(ox + gx), int(oy + gy), n,
                              ": an icon beside a label (delete bin, avatar, toggle). Its centre "
                              "is MEASURED, not estimated off a screenshot - use it rather than "
                              "adding an offset to the text box." if not said_why else ""))
                said_why = True
        if len(hits) > limit:
            out.append("  ... %d more" % (len(hits) - limit))
        return "\n".join(out)

    @mcp.tool(
        name="ReadText",
        description=(
            "Read ALL text currently visible on screen (or inside a region) with the offline Windows OCR engine. "
            "Use it to understand a self-drawn UI that the accessibility tree cannot describe, to verify a result "
            "after an action (e.g. a toast or a value in a canvas), or to find the right wording before calling "
            "FindText. Returns each line with its screen coordinates."
        ),
        annotations=ToolAnnotations(
            title="ReadText", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "ReadText-Tool")
    def read_text_tool(
        region: Annotated[list[int] | str | None, Field(description="[left, top, right, bottom] in screen pixels; omit for the whole screen.")] = None,
        with_coords: Annotated[bool, Field(description="Include each line's screen coordinates.")] = True,
        limit: Annotated[int, Field(description="Max lines to return.")] = 200,
        window: Annotated[str | None, Field(description="Read THIS window's own content instead of the screen - works even when it is completely covered, and never needs it brought to the front. Matches the window title (substring).")] = None,
        process: Annotated[str | None, Field(description="Same as window= but matches the executable name, e.g. 'Weixin'.")] = None,
        hwnd: Annotated[int | None, Field(description="Exact window handle, from Window(mode='list').")] = None,
        nth: Annotated[int, Field(description="Which window to read when several match (0-based).")] = 0,
        ctx: Context = None,
    ) -> str:
        t0 = time.time()
        region = _as_box(region)
        im, (ox, oy), target, note = _grab_for(window, process, hwnd, nth, region)
        if im is None:
            return "ReadText could not read anything: %s" % note
        try:
            res, scale = _recognize(im)
        except Exception as e:
            return "ReadText unavailable: %s" % e
        lines = list(res.lines)
        fell_back = ""
        if target and not lines:
            # 窗口自绘读回 0 行时，"0 line(s)" 本身是句没用的话：
            # 它分不清"这个窗口真的没字"和"它压根没渲染进外来 DC"。
            #
            # WorkBuddy 9-15 15:0x 实测：微信主窗（Qt 外壳 + CEF 聊天区）
            #   ReadText(hwnd=67306) -> "ReadText: 0 line(s), 0.03s from window '微信' own content
            #   820x700 (z-order irrelevant)  actionable=true"
            # 一句解释都没有，我据此以为"主窗里没字，读屏也没用"。
            # 可那个窗口的屏幕矩形里是有字的。
            # resolve_text 早就做了这个兜底（作用域读不到就退回整屏，并且明说原因），
            # ReadText 没有——于是"读不到"和"没东西读"长得一模一样。
            # 这里按同一套做法补上：拿该窗口在屏幕上的矩形重读一次，
            # 成功了就把结果当结果，同时把"发生了什么、坐标基准换成屏幕了"写进返回里，
            # 不悄悄换作用域（悄悄换，用的人会继续以为窗口作用域是可靠的，下次照样踩）。
            try:
                from windows_mcp.tools.window_ctl import _describe
                box = tuple(int(v) for v in _describe(target)["rect"])
                im2 = _grab(box)
                if im2 is not None:
                    res2, scale2 = _recognize(im2)
                    if res2.lines:
                        im, res, scale = im2, res2, scale2
                        ox, oy = box[0], box[1]
                        where = " in region %s" % (list(box),)
                        target = None
                        fell_back = (
                            "  NOTE: that window's OWN render came back empty (0 lines), which is what an "
                            "embedded web/CEF layer, a layered popup or a window that refuses PrintWindow "
                            "looks like - it does NOT mean the window has no text. This fell back to reading "
                            "the SCREEN at the window's rect %s, which worked; those are SCREEN coordinates. "
                            "Reported because a silent fallback would leave you trusting window-scoped reads "
                            "that will fail again next time." % (list(box),))
                    else:
                        # 两边都空，也要说话：不然 "0 line(s)" 还是分不清
                        # "这个窗口真的没字" 和 "它读不出来"。
                        fell_back = (
                            "  NOTE: the window's OWN render AND the screen at its rect %s both came back "
                            "with 0 lines, so the window really is blank right now (or covered by something "
                            "blank) - this is NOT the 'window refuses to be read' case, and it is not "
                            "evidence that OCR on that window is broken. If you expected text there, take a "
                            "Screenshot of the rect and look: WeChat windows do sit in a white-screen state "
                            "after being minimized and restored." % (list(box),))
            except Exception:
                pass
        if target:
            from windows_mcp.tools.window_ctl import _describe
            where = " from window '%s' own content %dx%d (z-order irrelevant)" % (
                _describe(target)["title"][:30], im.width, im.height)
            where += _occlusion_note(target, ox + im.width // 2, oy + im.height // 2)
        elif not where:
            where = (" in region %s" % (list(region),)) if region else ""
        out = ["ReadText: %d line(s), %.2fs%s" % (len(lines), time.time() - t0, where)]
        _rn = retry_note()
        if _rn:
            out.append(_rn)
        if fell_back:
            out.append(fell_back)
        for ln in lines[:limit]:
            if with_coords and ln.words:
                l = min(w.bounding_rect.x for w in ln.words) / scale
                t = min(w.bounding_rect.y for w in ln.words) / scale
                r = max(w.bounding_rect.x + w.bounding_rect.width for w in ln.words) / scale
                b = max(w.bounding_rect.y + w.bounding_rect.height for w in ln.words) / scale
                out.append("  (%d,%d)  %s" % (int(ox + (l + r) / 2), int(oy + (t + b) / 2), ln.text))
            else:
                out.append("  " + (ln.text or ""))
        if len(lines) > limit:
            out.append("  ... %d more lines" % (len(lines) - limit))
        return "\n".join(out)
