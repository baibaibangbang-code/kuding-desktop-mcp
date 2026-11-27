"""PATCH 0.5.0: WaitUntil —— 等到某个条件成立再往下走（测试机器 9-14 提的第 4 条）。

在此之前，等弹窗、等页面出现、等导出完成，全靠外面手写 `sleep + 重试` 循环：
每一轮都是一次 MCP 往返，而且每次往返之间界面都可能变。
0.4.0 之后按 process/window 收窄的搜索只要 0.6 秒，完全够拿来做轮询谓词，于是把循环收进工具里。

三种谓词，一次调用替掉一整个循环：
  text=...   state=appear/disappear   区域 OCR 轮询（自绘窗口用这个）
  selector=... state=appear/disappear 控件树轮询（有控件树的用这个，更快更准）
"""

import time
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics


def _norm(s):
    """OCR 会在汉字之间塞空格（「获 取 数 据」），比对前统一去掉所有空白。"""
    return "".join((s or "").split())


def _line_centre(line, scale, ox, oy):
    xs, ys, xe, ye = None, None, None, None
    for w in line.words:
        r = w.bounding_rect
        l, t = r.x / scale + ox, r.y / scale + oy
        rr, bb = l + r.width / scale, t + r.height / scale
        xs = l if xs is None else min(xs, l)
        ys = t if ys is None else min(ys, t)
        xe = rr if xe is None else max(xe, rr)
        ye = bb if ye is None else max(ye, bb)
    return (int((xs + xe) / 2), int((ys + ye) / 2)) if xs is not None else None


def _ratio(needle, hay):
    """把 needle 在 hay 上滑窗取最高相似度。中文 OCR 常整字读错，逐字比对没用。"""
    import difflib
    if not needle or not hay:
        return 0.0
    best = difflib.SequenceMatcher(None, needle, hay).ratio()
    n = len(needle)
    if len(hay) > n:
        for i in range(len(hay) - n + 1):
            best = max(best, difflib.SequenceMatcher(None, needle, hay[i:i + n]).ratio())
    return best


def _ocr_hit(text, region, match, fuzzy):
    """返回 (命中?, 行文本, 中心坐标, 怎么匹配上的, 最像的那行和分数)。

    默认 contains（去空格后子串）。**默认不做模糊匹配**——模糊命中而不声张，
    等于工具替调用方猜，猜错了还看不出来。但超时的时候会把"最像的那一行"报出来，
    这样失败消息本身能告诉你是"根本没出现"还是"出现了但 OCR 读错了"。
    测试机器 9-14 实测：「正在删除」被读成「正在涂g87个项目」，contains 必然落空。
    """
    from windows_mcp.tools.ocr_find import _grab, _recognize
    im = _grab(tuple(region) if region else None)
    res, scale = _recognize(im)
    ox, oy = (region[0], region[1]) if region else (0, 0)
    want = _norm(text)
    best_line, best_score = None, 0.0
    for line in res.lines:
        low = _norm(line.text)
        if match == "exact":
            if want == low:
                return True, line.text, _line_centre(line, scale, ox, oy), "exact", None, 1.0
        elif want and want in low:
            return True, line.text, _line_centre(line, scale, ox, oy), "contains", None, 1.0
        r = _ratio(want, low)
        if r > best_score:
            best_line, best_score = line, r
    if match == "fuzzy" and best_line is not None and best_score >= fuzzy:
        return (True, best_line.text, _line_centre(best_line, scale, ox, oy),
                "FUZZY %.2f - VERIFY this is really your target" % best_score, None, best_score)
    return False, None, None, None, (best_line.text if best_line is not None else None), best_score


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="WaitUntil",
        description=(
            "Poll until a condition holds, instead of writing your own sleep-and-retry loop. Give EITHER text= "
            "(polls OCR over the screen or a region - use this for self-drawn apps like WeChat/JianyingPro) OR "
            "selector= (polls the control tree - faster and exact; always scope it with process: or window:). "
            "state='appear' waits for it to show up, state='disappear' waits for it to go away - use 'disappear' to "
            "wait out a progress dialog or a toast. Returns as soon as the condition holds, with how long it took "
            "and (for text) the screen coordinates you can pass straight to Click. On timeout it says what it saw "
            "last, so a failure tells you whether nothing appeared or something else did."
        ),
        annotations=ToolAnnotations(
            title="WaitUntil", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "WaitUntil-Tool")
    def wait_until_tool(
        text: Annotated[str | None, Field(description="Text to wait for on screen (OCR). Whitespace is ignored when matching.")] = None,
        selector: Annotated[str | None, Field(description="Selector to wait for, e.g. 'process:EasyVMS|role:button|name:导出'.")] = None,
        state: Annotated[Literal["appear", "disappear"], Field(description="Wait for the condition to become true, or to stop being true.")] = "appear",
        region: Annotated[list[int] | None, Field(description="OCR only: [left, top, right, bottom] screen pixels. Smaller region = faster and fewer false hits.")] = None,
        match: Annotated[
            Literal["contains", "fuzzy", "exact"],
            Field(description="OCR only. 'contains' (default): the line must contain your text once whitespace is stripped. 'fuzzy': accept the closest line above the threshold - Chinese OCR misreads whole characters (it read 正在删除 as 正在涂g87个项目), so use this when the target is small or low-contrast text; a fuzzy hit is labelled FUZZY so you can verify it. 'exact': the whole line must equal your text."),
        ] = "contains",
        fuzzy: Annotated[float, Field(description="Similarity threshold 0-1 for match='fuzzy'.")] = 0.6,
        timeout: Annotated[float, Field(description="How long to watch, in seconds. A single call returns after at most the client's own call deadline minus a safety margin (15s by default) even if you ask for more - it comes back saying STILL WAITING with what it last saw, so call it again to keep watching. That is deliberate: a call that runs past the deadline is killed by the client and you lose the whole observation.")] = 60.0,
        interval: Annotated[float, Field(description="Seconds between polls (0.2-30). The two paths cost wildly different amounts, measured on this machine: a text= poll is OCR at ~0.2-0.5s, but a selector= poll rebuilds the UI tree and takes ~6-7s, so within one call's budget you get roughly two selector polls no matter what interval you ask for - a small interval on a selector is not honoured because a single poll already exceeds it. Use text= when you need to catch something short-lived.")] = 1.0,
        ctx: Context = None,
    ) -> str:
        if bool(text) == bool(selector):
            return "Give exactly one of text= or selector=."
        want_timeout = max(1.0, min(float(timeout), 1800.0))
        # 客户端在 CLIENT_DEADLINE 秒上砍连接，收下一个注定砍不到头的 timeout
        # 等于把「我等了多久、看到了什么」整段丢掉——调用方只拿到 -32001，
        # 什么都不知道。9-15 opencode 的 WaitUntil(timeout=30) 就是这么没的。
        # 宁可早回、把看到的如实说清楚，让它自己决定接着等。
        from windows_mcp.tools.frame_wrap import SAFE_BUDGET
        timeout = min(want_timeout, SAFE_BUDGET)
        capped = timeout < want_timeout
        interval = max(0.2, min(float(interval), 30.0))
        desktop = get_desktop()
        t0 = time.time()
        polls = 0
        last = "nothing"

        while True:
            polls += 1
            try:
                if text:
                    hit, line, centre, how, near, score = _ocr_hit(text, region, match, fuzzy)
                    if hit:
                        last = "saw %r at %s" % (line, centre)
                        detail = "  [%s] matched line %r  centre=%s" % (how, line, centre)
                    else:
                        detail = ""
                        last = ("text not on screen; closest line was %r (similarity %.2f)" % (near, score)
                                if near else "nothing readable on screen")
                else:
                    from windows_mcp.tools.uia_actions import _find_hits
                    hits, _nth, how = _find_hits(desktop, selector)
                    hit = bool(hits)
                    last = ("%d match(es) [%s]" % (len(hits), how)) if hit else ("no match [%s]" % how)
                    detail = ("  first match at (%d,%d) %s" % (hits[0].center.x, hits[0].center.y,
                                                               getattr(hits[0], "control_type", "?"))) if hits else ""
            except Exception as e:
                hit, detail = False, ""
                last = "poll failed: %s: %s" % (type(e).__name__, str(e)[:120])

            done = hit if state == "appear" else (not hit)
            waited = time.time() - t0
            if done:
                return ("condition met after %.1fs (%d poll%s): %s %r %s.%s"
                        % (waited, polls, "" if polls == 1 else "s",
                           "text" if text else "selector", text or selector, state, detail))
            if waited + interval > timeout:
                tip = ("If this was a selector, check the window is not minimized (Window(mode='list')) and that the "
                       "scope is right; if it was text, try a smaller region or a shorter, more distinctive string.")
                if text and state == "appear" and match == "contains" and "similarity" in last:
                    try:
                        s = float(last.rsplit("similarity ", 1)[1].rstrip(")"))
                        if s >= 0.5:
                            tip = ("The closest line above scores %.2f - that is probably your target read wrongly by "
                                   "OCR (Chinese characters get misread whole). Retry with match='fuzzy'." % s)
                    except Exception:
                        pass
                if capped:
                    return ("STILL WAITING after %.1fs (%d polls): %s %r has not %s yet. "
                            "You asked for %.0fs but a single call is cut off at %.0fs by your "
                            "client, so this returned early WITH what it saw rather than dying "
                            "at the deadline and telling you nothing. Last poll saw: %s. "
                            "Call WaitUntil again to keep watching - nothing was lost. %s"
                            % (waited, polls, "text" if text else "selector", text or selector,
                               "appeared" if state == "appear" else "disappeared",
                               want_timeout, timeout, last, tip))
                return ("TIMEOUT after %.1fs (%d polls): %s %r never %s. Last poll saw: %s. %s"
                        % (waited, polls, "text" if text else "selector", text or selector,
                           "appeared" if state == "appear" else "disappeared", last, tip))
            time.sleep(interval)
