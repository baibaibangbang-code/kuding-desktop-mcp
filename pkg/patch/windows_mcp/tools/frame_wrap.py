"""PATCH 0.5.5: 给上游的 Click / Type / Move / Scroll 加一个可选的 frame= 参数。

为什么不直接改 input.py：那是上游文件（windows-mcp==0.8.5），整份接管等于自己维护一个
500 行的分叉，只为加一个参数。这里改成在注册的那一刻把工具函数包一层——
上游函数原样调用，签名用 inspect 动态插一个参数进去，所以上游以后改了自己的签名也不会被我们吞掉。

拦的是什么：坐标是从某张截图上量的，量完到点下去之间画面变了。
判据在 frame_guard.check() 里，只看「这个点的归属窗口变没变」。

Type 也包进来了，而且它才是最该包的那个：往错的窗口里打字、再带一个回车，
在微信上就是把内容发给了错的人——比点错一下更不可逆。
"""

import functools
import inspect
import os
import time
from typing import Optional

_TARGETS = ("Click", "Type", "Move", "Scroll", "Shortcut")
# Shortcut 没有坐标，所以 frame= 对它没有意义；它要绑的是**这一下按键会进哪个窗口**。
_FOCUS_TARGET_TOOLS = ("Shortcut",)

# 哪些参数装的是坐标。Move 拖拽时起点终点都要验。
_COORD_ARGS = ("loc", "from_loc")


def _as_xy(v):
    """把上游那几种坐标写法归一成 (x, y)，认不出来就返回 None（认不出就不拦，别误伤）。"""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            import json
            v = json.loads(v)
        except Exception:
            return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return int(v[0]), int(v[1])
        except (TypeError, ValueError):
            return None
    return None


# 不带前缀的别名。9-14 让 opencode 跑真任务时它在 Click 上写了 process="Weixin"——
# 因为我们**其它每个工具**都叫 process=（FindText/ReadText/Window 全是），
# 只有 Click 为了躲开 Type 的 text= 加了 click_ 前缀。它猜对了语义、猜错了名字，被 pydantic 挡回去。
# Click 本身没有这几个参数，不存在冲突，所以两种写法都收。
_ALIASES = {"text": "click_text", "window": "click_window", "process": "click_process",
            "hwnd": "click_hwnd", "region": "click_region", "nth": "click_nth",
            "dx": "click_dx", "dy": "click_dy"}


def _apply_aliases(kwargs):
    for short, full in _ALIASES.items():
        v = kwargs.pop(short, None)
        if v is not None and kwargs.get(full) is None:
            kwargs[full] = v


def _ensure_on_top(target, xy, why, text_target, spec, tool_name):
    """点下去之前确认这个坐标真的落在目标窗口上；被盖住就把目标提到前面再重新定位。

    0.5.5 追加。千问办公的做法是每轮开工先把**它自己的窗口**挪到屏幕边上，从根上不挡住目标。
    那一招我们抄不了：我们是 MCP 服务，寄生在调用方进程里，自己没有窗口——
    9-14 实测顺着父进程链往上找，claude.exe 一个可见窗口都没有，再往上就是 explorer.exe，
    而它拥有 Progman（桌面本身）和任务栏。照那个思路做，第一次跑就会去挪桌面。

    我们不需要挪任何人的窗口：要的只是"目标别被挡住"，把目标提前就够了，
    而且提完还能再验一次——它挪完是直接假定不挡了，我们验。

    只在能力范围内动手：知道目标是谁（调用方给了 window=/process=/hwnd=）才置前；
    没指定目标就什么都不做，绝不乱动别人的窗口。
    """
    if not target:
        return xy, why
    try:
        from windows_mcp.tools.frame_guard import actionable
        from windows_mcp.tools.window_ctl import _force_front
    except Exception:
        return xy, why

    ok, who = actionable(target, xy[0], xy[1])
    if ok:
        return xy, why

    try:
        _force_front(target)
    except Exception as e:
        raise ValueError(
            "%s: %r sits under another window (%s) and bringing the target to the front failed (%s). "
            "Move the window out of the way, then retry."
            % (tool_name, text_target, who, type(e).__name__))

    # 置前会改变布局（窗口可能被还原、滚动条位置变），所以重新定位而不是沿用旧坐标
    import time as _t
    _t.sleep(0.25)
    from windows_mcp.tools.ocr_find import resolve_text
    xy2, why2, target2 = resolve_text(text_target, **spec)
    if xy2 is None:
        raise ValueError("%s: brought the target to the front but then could not locate %r: %s"
                         % (tool_name, text_target, why2))
    ok2, who2 = actionable(target2 or target, xy2[0], xy2[1])
    if not ok2:
        raise ValueError(
            "%s REFUSED: %r is still covered by %s after bringing the target to the front. "
            "Clicking would hit the other window. Move one of them (Window(mode='move')) and retry."
            % (tool_name, text_target, who2))
    return xy2, why2 + " (target was covered by %s, brought it to the front and re-located)" % who


def _helper_hint(hwnd):
    """拒绝时顺带说清楚挡路的是不是一个"辅助窗"。

    调用方看到 "belongs to Notepad.exe ('主机弹出窗口')" 是懵的——
    它不知道那是应用自己的宿主窗还是一个真对话框，也就不知道该重试还是该改坐标。
    """
    try:
        from windows_mcp.tools.window_ctl import _helper_reason, _rect
        l, t, r, b = _rect(hwnd)
        why = _helper_reason(hwnd, r - l, b - t)
    except Exception:
        return ""
    if not why:
        return ""
    return (" That window is a helper window (%s) rather than a normal app window - usually a "
            "popup host or overlay that an app puts up briefly. If it is still there on a retry, "
            "it is real and you need a different target." % why)


def _check_owner(kwargs, tool_name):
    """动手前当场核实目标坐标归哪个窗口。不符就拒绝，什么都不做。

    opencode 9-15 提的第 2 条，后来在纸牌上看得更清楚：它每点一下牌堆要发
    **三个工具调用**——先 `Window(mode="at", x=, y=)` 问这个点归谁，再 `Click`，
    再 `Screenshot` 看结果。问和点是两次调用，**中间界面可以变**，
    所以那次询问并不能保证点下去的时候还是同一个窗口：竞态照样在，只是变窄了。

    `frame=` 挡的是「**你那张截图**之后有没有变」，要求调用方手里有 frame id；
    这里挡的是「**此刻**这个像素归不归你要的那个东西」，一次调用完成，
    不需要任何前置状态。两者互补，都保留。
    """
    want_p = kwargs.pop("expected_process", None)
    want_h = kwargs.pop("expected_hwnd", None)
    if not want_p and not want_h:
        return
    xy = None
    for key in _COORD_ARGS:
        xy = _as_xy(kwargs.get(key))
        if xy is not None:
            break
    if xy is None:
        raise ValueError(
            "%s: expected_process/expected_hwnd check who owns a COORDINATE, but this call has "
            "no coordinate. Drop them, or pass loc=." % tool_name)

    from windows_mcp.tools.window_ctl import _pid, _proc_name, _root_at, _text

    # 「此刻这个像素归谁」用 WindowFromPoint 问，不用 z 序 + 矩形扫描。
    #
    # 9-15 连撞两次假拒绝才想明白：矩形扫描回答的是"谁的框盖住了这里"，
    # 而我们要问的是"点下去谁收到"——这两件事不一样。
    #   一次是我们自己的操作提示层（鼠标穿透，永远收不到点击）；
    #   一次是记事本的 PopupWindowSiteBridge 宿主窗，**瞬时出现又消失**，
    #     扫描那一刻它可见、有矩形，于是被当成了归属人。
    # WindowFromPoint 是系统自己派发点击时走的那条路：它认命中测试
    # （HTTRANSPARENT）、认窗口区域、认禁用状态，不会把收不到点击的窗口算进来。
    # GA_ROOT 再把 RichEditD2DPT 这类子控件归到真正的顶层窗口上。
    #
    # 顺带修掉一个不一致：Window(mode="at") 用的就是 _root_at，
    # 而这里以前用另一套算法。**同一个问题两套答案**，先问后点时会自相矛盾。
    def _same_app_helper(h):
        """挡路的是不是"目标应用自己的辅助窗"。

        9-15 实测：窗口刚被提到前台时，记事本会在正文上生出一个
        Microsoft.UI.Content.PopupWindowSiteBridge 宿主窗（无边框、很小、标题"主机弹出窗口"），
        而且能持续半秒以上——不是一闪而过，重试等不掉。

        它是真窗口、真命中测试，所以"这一下谁收到"短暂地确实是它。
        但这跟护栏要防的事**不是一回事**：护栏防的是"给 A 的点击落进了 B"
        （测试机器四个微信叠在一起、右键菜单里带删除聊天记录的那次）。
        同一个进程自己的宿主窗接住这一下，点击照样进了这个应用。

        所以判据收得很紧：**同一个进程 + 被判定为辅助窗**，两条都满足才放行。
        不同进程的辅助窗照样拒——别人的下拉菜单、提示气泡吞掉点击是真危险。
        """
        if not h or not want_h:
            return False
        try:
            from windows_mcp.tools.window_ctl import _helper_reason, _rect
            if int(_pid(h)) != int(_pid(int(want_h))):
                return False
            l, t, r, b = _rect(h)
            return _helper_reason(h, r - l, b - t) is not None
        except Exception:
            return False

    def _matches(h):
        if not h:
            return False
        if want_h and int(want_h) != int(h) and not _same_app_helper(h):
            return False
        if want_p:
            pn = (_proc_name(_pid(h)) or "").lower().replace(".exe", "")
            if want_p.strip().lower().replace(".exe", "") not in pn:
                return False
        return True

    owner = _root_at(xy[0], xy[1])

    # 不合就再看几眼，别被一次闪现判死刑。
    #
    # 9-15 实测：窗口刚被提到前台的那一瞬，记事本会生出一个
    # Microsoft.UI.Content.PopupWindowSiteBridge 宿主窗盖在正文上，几十毫秒后自己消失。
    # 它是真窗口、真命中测试，所以"谁收到这一下"确实短暂地变成了它。
    # 但这跟护栏要防的事（点击落到**另一个应用**里去）不是一回事。
    # 因一次闪现就硬拒，代价是调用方白费一整轮推理（实测中位 14.9 秒），
    # 而多看 0.5 秒的代价可以忽略。护栏没有被削弱：持续存在的外来窗口照样拒。
    if owner and not _matches(owner):
        blocker = owner
        for _ in range(5):
            time.sleep(0.1)
            again = _root_at(xy[0], xy[1])
            if _matches(again):
                owner = again
                break
        else:
            owner = blocker

    if not owner:
        raise ValueError(
            "%s REFUSED - nothing owns the pixel at (%d,%d) right now (off-screen, or the "
            "desktop itself). Nothing was clicked." % (tool_name, xy[0], xy[1]))
    proc = _proc_name(_pid(owner)) or ""
    title = (_text(owner) or "")[:40]

    if want_h and int(want_h) != int(owner) and not _same_app_helper(owner):
        raise ValueError(
            "%s REFUSED - (%d,%d) belongs to hwnd=%d (%s '%s'), not the hwnd=%d you expected. "
            "Nothing was clicked. It stayed that way for half a second of re-checking, so this "
            "is not a flicker. Something moved or came to the front since you looked; take a "
            "fresh look rather than clicking anyway.%s"
            % (tool_name, xy[0], xy[1], owner, proc, title, int(want_h), _helper_hint(owner)))

    if want_p and want_p.strip().lower().replace(".exe", "") not in proc.lower().replace(".exe", ""):
        raise ValueError(
            "%s REFUSED - (%d,%d) belongs to %s ('%s'), not %r. Nothing was clicked. This is the "
            "check that stops a click meant for one app from landing in another: a dialog, a "
            "tooltip or another window can arrive between looking and clicking. It stayed that "
            "way for half a second of re-checking, so this is not a flicker.%s"
            % (tool_name, xy[0], xy[1], proc, title, want_p, _helper_hint(owner)))


def _resolve_focus(kwargs, tool_name):
    """按键类工具动手前，确认前台窗口就是调用方想要的那个；不是就提到前面再验一次。

    不指定目标时什么都不做——和 `_ensure_on_top` 一个原则：**只在知道目标是谁的时候才动手**，
    不替调用方猜。指定了却对不上，就拒绝执行并把现在的前台是谁如实说出来，
    让调用方自己决定，而不是把一串按键盲发给不知道哪个窗口。
    """
    want = kwargs.pop("hwnd", None)
    title = kwargs.pop("window", None)
    process = kwargs.pop("process", None)
    if not (want or title or process):
        return None
    from windows_mcp.tools.window_ctl import (_force_front, _proc_name, _pid,
                                              _text, resolve_target)
    if not want:
        want, why = resolve_target(title=title, process=process)
        if not want:
            raise ValueError("%s: %s" % (tool_name, why))
    want = int(want)

    import ctypes
    u = ctypes.WinDLL("user32")          # 私有实例：windll 是进程级共享的，别污染别人
    u.GetForegroundWindow.restype = ctypes.c_void_p

    def front():
        h = u.GetForegroundWindow()
        return int(h) if h else 0

    if front() == want:
        return want
    try:
        _force_front(want)
    except Exception:
        pass
    time.sleep(0.25)
    now = front()
    if now == want:
        return want
    raise ValueError(
        "%s REFUSED - the keystroke would go to the wrong window. You asked for hwnd=%d "
        "('%s' / %s) but the foreground window is hwnd=%d ('%s' / %s), and bringing yours to the "
        "front did not take (Windows refuses a foreground steal while another app holds the "
        "foreground lock). Nothing was pressed. Click inside the window you want first, then retry."
        % (tool_name, want, _text(want)[:30], _proc_name(_pid(want)),
           now, _text(now)[:30] if now else "-", _proc_name(_pid(now)) if now else "-"))


def _with_frame(fn, tool_name):
    """包一层，插入 frame= 关键字参数。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        frame = kwargs.pop("frame", None)
        if tool_name in _TEXT_TARGET_TOOLS:
            _apply_aliases(kwargs)
        text_target = kwargs.pop("click_text", None)
        if text_target:
            if kwargs.get("loc") is not None or kwargs.get("label") is not None:
                raise ValueError(
                    "%s: pass click_text= OR loc=/label=, not both. click_text locates the target "
                    "by OCR at the moment of the click, which is the whole point - a loc= you "
                    "measured earlier may already be stale." % tool_name)
            from windows_mcp.tools.ocr_find import resolve_text, _as_box
            spec = dict(window=kwargs.pop("click_window", None),
                        process=kwargs.pop("click_process", None),
                        hwnd=kwargs.pop("click_hwnd", None),
                        region=_as_box(kwargs.pop("click_region", None)),
                        occurrence=kwargs.pop("click_nth", 0) or 0)
            xy, why, target = resolve_text(text_target, **spec)
            if xy is None:
                raise ValueError("%s could not locate %r: %s" % (tool_name, text_target, why))
            xy, why = _ensure_on_top(target, xy, why, text_target, spec, tool_name)
            # 相对锚点的偏移：图标没有文字，OCR 找不到，但它旁边总有文字。
            # 9-14 的由头是微信朋友圈的删除垃圾桶——它紧挨着「X分钟前」，
            # 而那条动态的正文是我们自己发的、内容唯一，拿正文当锚点最稳。
            dx = int(kwargs.pop("click_dx", None) or 0)
            dy = int(kwargs.pop("click_dy", None) or 0)
            kwargs["loc"] = [xy[0] + dx, xy[1] + dy]
            if dx or dy:
                why += " then offset by (%+d,%+d) -> %s" % (dx, dy, tuple(kwargs["loc"]))
            frame = None          # 现场定位出来的坐标不可能过期，再验 frame 只会自相矛盾
        else:
            for k in ("click_window", "click_process", "click_hwnd", "click_region",
                      "click_nth", "click_dx", "click_dy"):
                kwargs.pop(k, None)
        if frame and tool_name in _FOCUS_TARGET_TOOLS:
            raise ValueError(
                "%s: frame= checks a coordinate against the picture it came from, but %s has no "
                "coordinate - the keystroke goes wherever focus is. Pass window=/process=/hwnd= "
                "instead: that is what actually binds this call to a window." % (tool_name, tool_name))
        if frame:
            checked = False
            from windows_mcp.tools.frame_guard import check
            for key in _COORD_ARGS:
                xy = _as_xy(kwargs.get(key))
                if xy is None:
                    continue
                checked = True
                bad = check(frame, xy[0], xy[1])
                if bad:
                    raise ValueError("%s REFUSED - %s" % (tool_name, bad))
            if not checked and kwargs.get("label") is not None:
                # label 是现查的，本来就不依赖那张截图；不假装验过
                raise ValueError(
                    "%s: frame= only checks coordinates, but you passed label=%r. Labels are resolved "
                    "live against the current UI tree, so there is nothing to go stale - drop frame=."
                    % (tool_name, kwargs.get("label")))
        _check_owner(kwargs, tool_name)

        # Shortcut 这类没有坐标的工具，验的是焦点：这一下按键到底进谁的窗口。
        # 9-15 我自己踩到才补的——发一个 enter 之前根本没人检查前台还是不是目标，
        # 焦点被别的窗口抢走，enter 就进了别人那里。Ctrl+A、Delete 这种更不能乱飞。
        focus_hwnd = None
        if tool_name in _FOCUS_TARGET_TOOLS:
            focus_hwnd = _resolve_focus(kwargs, tool_name)

        # 亮一下操作提示：横幅 + 目标窗口外框 + 光标环。
        # 让坐在电脑前的人知道现在是 AI 在动——三家同类产品都有，我们原来是唯一静默的。
        # 整个调用吞异常，提示层出问题绝不能影响动作本身。
        try:
            from windows_mcp.tools.overlay import activity, take_cancel
            from windows_mcp.tools.window_ctl import _root_at
            # 人按过 Esc 就停手。**这一步要在动作之前**，否则等于没停。
            ago = take_cancel()
            if ago is not None:
                raise ValueError(
                    "STOPPED: somebody pressed Esc %.0f seconds ago, while uia-desktop was operating "
                    "this machine, so this %s was not performed. Nothing was clicked or typed. If this "
                    "was you, say what to do next; if it was the person sitting at the machine, stop "
                    "and ask before touching the desktop again." % (ago, tool_name))
            xy = _as_xy(kwargs.get("loc"))
            owner = focus_hwnd
            if owner is None and xy:
                # 外框画在哪个窗口上，用的判据要和护栏一致，
                # 否则会出现"框住了 A、拒绝理由说是 B"这种自相矛盾的画面
                owner = _root_at(xy[0], xy[1])
            activity(owner, "uia-desktop 正在操控这台电脑 · %s" % tool_name)
        except ValueError:
            raise
        except Exception:
            pass
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            # 拖拽中途抛异常，按键会留在按下状态。松手，并如实说松了什么。
            raise type(e)("%s%s" % (e, _released_note(tool_name, e))) from e

    # FastMCP 按签名生成 schema，所以得把 frame 真的插进签名里，光加 **kwargs 没用。
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values()
              if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    # 必须和 ctx 同类（POSITIONAL_OR_KEYWORD）：keyword-only 参数不能排在 positional-or-keyword 前面。
    added = [("frame", Optional[str]),
             ("expected_process", Optional[str]), ("expected_hwnd", Optional[int])]
    if tool_name in _FOCUS_TARGET_TOOLS:
        added += [("hwnd", Optional[int]), ("window", Optional[str]),
                  ("process", Optional[str])]
    if tool_name in _TEXT_TARGET_TOOLS:
        added += [("click_text", Optional[str]), ("click_window", Optional[str]),
                  ("click_process", Optional[str]), ("click_hwnd", Optional[int]),
                  ("click_region", Optional[object]),
                  ("click_nth", Optional[int]), ("click_dx", Optional[int]),
                  ("click_dy", Optional[int]),
                  ("text", Optional[str]), ("window", Optional[str]),
                  ("process", Optional[str]), ("hwnd", Optional[int]),
                  ("region", Optional[object]), ("nth", Optional[int]),
                  ("dx", Optional[int]), ("dy", Optional[int])]
    # 光改签名不够：pydantic 生成 schema 时读的是 __annotations__，两边都得有，
    # 而且得是真类型对象——字符串注解这里没有作用域可解析，会 KeyError。
    wrapper.__annotations__ = dict(getattr(fn, "__annotations__", {}))
    # ctx 习惯上排最后，新参数插在它前面，读起来顺一点
    at = next((i for i, p in enumerate(params) if p.name == "ctx"), len(params))
    for name, ann in added:
        wrapper.__annotations__[name] = ann
        params.insert(at, inspect.Parameter(
            name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation=ann))
        at += 1
    try:
        wrapper.__signature__ = sig.replace(parameters=params)
    except ValueError:
        # 插入点前面有无默认值的参数时会顶掉顺序；退一步把新参数全挪到最末尾，功能不变
        names = {n for n, _ in added}
        rest = [p for p in params if p.name not in names]
        tail = [p for p in params if p.name in names]
        wrapper.__signature__ = sig.replace(parameters=rest + tail)
    return wrapper


_TEXT_TARGET_TOOLS = ("Click",)

_NOTE = (' Optional expected_process="Weixin" / expected_hwnd=N: checked AT THE MOMENT OF THE CLICK - if that coordinate belongs to anything else right now, the call is refused and nothing is clicked. Use it instead of calling Window(mode="at") first and then clicking: those are two calls and the screen can change in between, which is the exact gap this closes. Optional frame="f-xxxxxxxx" (from the Frame: line of a Snapshot/Screenshot): the call is '
         'refused if the window that owned your target coordinate has moved, closed, or been covered '
         'since that picture was taken. Omit it for coordinates from Find / FindText / Window.')

_FOCUS_NOTE = (
    ' Optional window="微信" / process="Weixin" / hwnd=N: the keystroke is only sent if that window '
    'is actually in the foreground. If it is not, the tool brings it forward and checks again, and '
    'REFUSES (pressing nothing) if the foreground still belongs to someone else. Pass it whenever the '
    'shortcut is destructive or order-dependent (Enter, ctrl+a, delete, ctrl+s) - without it the keys '
    'go to whatever happens to have focus, which is not necessarily the window you were just working in.')

_TEXT_NOTE = (
    ' PREFERRED for self-drawn windows (WeChat, 剪映): instead of loc=, pass click_text="发表" '
    '(+ click_process="Weixin" to read that window even if covered, click_region=[l,t,r,b] to narrow, '
    'click_nth=N to pick among matches). The button is located by OCR AT THE MOMENT OF THE CLICK, so a '
    'coordinate you measured earlier cannot go stale. Use this whenever the target sits below text that '
    'can grow: a long caption pushes the button down and the old coordinate lands on something else.'
    ' For an ICON that OCR cannot read (a trash can, a gear), anchor on the text beside it and add'
    ' click_dx/click_dy, e.g. click_text="<the post text you wrote>", click_dy=40 to hit the row'
    ' below it. Anchoring on unique content is what makes a list safe: after you delete one row'
    ' every remaining coordinate shifts, but the anchor text moves with its own row.')


# 读屏类工具：不动任何东西，但**在看这台机器的屏幕**。
# 横幅要跟着它们一起挂着，否则一个任务里横幅只在动手那 4 秒亮，人根本看不见
# （9-15 维护者盯着屏幕反馈「没看到」，就是这个）。
_READ_TOOLS = ("Screenshot", "Snapshot", "ReadText", "FindText", "FindElements",
               "Find", "Window", "Scrape", "WaitFor", "WaitUntil", "DisplayInventory")

# 会动手、但没有坐标的工具。**提示层要么盖住所有动手的工具，要么就是在用沉默撒谎**——
# 9-15 预检时才发现这一批一直是裸奔的：既不亮提示，也不认 Esc。
# Click/Type/Move/Scroll 走 _TARGETS 那条路（还要验 frame），这批只需要提示 + 叫停。
_ACTION_TOOLS = ("Invoke", "SetValue", "MouseDown", "MouseUp", "PressMouse",
                 "MultiSelect", "MultiEdit", "App")

# 客户端的截止时间是硬的，我们这边任何"能睡多久"的参数都必须落在它里面。
#
# 9-15 复盘 opencode 那次 227 分钟的会话，14 次 -32001 全是整 20.0 秒。
# 翻它的配置：opencode.json 里 uia-desktop 写着 "timeout": 20000。
# 而我上一版把 WAIT_CAP 定成 20，注释还写着"客户端普遍在 30 秒上下超时"——
# **我猜的那个数比真实值大，于是封顶正好落在截止线上，必然超时。**
# 本来是为了防超时加的封顶，反而保证了超时。
#
# 所以不再猜：截止时间从环境变量读（客户端配多少就填多少），
# 留 5 秒余量给传输和序列化，所有阻塞调用一律按余量后的预算封顶。
CLIENT_DEADLINE = 20.0
try:
    CLIENT_DEADLINE = max(5.0, float(os.environ.get("UIA_CLIENT_TIMEOUT", "20")))
except Exception:
    pass
SAFE_BUDGET = max(3.0, CLIENT_DEADLINE - 5.0)
WAIT_CAP = int(SAFE_BUDGET)

# WaitFor 的每一轮轮询都调 desktop.get_state(use_ui_tree=True)——**整桌面扫描控件树**，
# 正是我们自己 SKILL 第 0 条明令禁止、当初把 Snapshot 默认改成 foreground_only 的那件事。
# 而 timeout 只在两轮之间检查，管不住已经开跑的那一轮：
# 9-15 实测 WaitFor(timeout=3) 真实耗时 **12.90 秒**，超了 4.3 倍，
# 客户端直接 -32001。opencode 那轮一连串超时就是这么来的。
#
# 最常用的 active_window 根本不需要控件树——问一句前台窗口是谁就够了。
# 走快路：12.9 秒 → 毫秒级，语义分毫不差（"当前活动窗口"本来就只看前台）。
# 其余条件确实要树，留着不动，但把真实代价写进工具描述，别让调用方蒙在鼓里。
_WAITFOR_FAST = ("active_window",)

# 按下去的鼠标键**绝不能活过一次失败**。
# opencode 9-15 报：MouseDown → Move → MouseUp 中 Move/MouseUp 连续超时，
# 「看门狗到期后服务仍未恢复」。一个卡在按下状态的左键会在桌面上乱拖东西——
# 拖丢文件、拖乱时间轴、把窗口拖到屏幕外，而且事后完全看不出是谁干的。
# 上游有 20 秒看门狗，但那是**兜底**，不是第一道防线：
# 出错的那一刻就该松手，而不是等 20 秒。
_BUTTON_UP = (0x0004, 0x0010, 0x0040)     # LEFTUP / RIGHTUP / MIDDLEUP


def _release_buttons(why):
    """把所有可能按着的鼠标键松开。只在我们自己按过的时候做，绝不乱动人的手。"""
    try:
        from windows_mcp.tools.uia_actions import _HELD
        held = _HELD.get("button")
    except Exception:
        held = None
    if not held:
        return None
    try:
        import ctypes
        u = ctypes.WinDLL("user32")
        for flag in _BUTTON_UP:
            u.mouse_event(flag, 0, 0, 0, 0)
        try:
            t = _HELD.get("timer")
            if t:
                t.cancel()
        except Exception:
            pass
        _HELD.clear()
        return held
    except Exception:
        return None


def _with_safe_destroy(fn, tool_name):
    """`FileSystem` 和 `Registry` 的不可撤销操作，改成留退路。

    理由和取舍写在 safe_destroy.py 顶部：**不禁掉，改成可撤销**。
    禁掉是砍能力，而且被禁的 agent 常常绕路去用 PowerShell 做同一件事，反而更没记录。
    """

    def _pre(kwargs):
        """返回 (要拼进结果的说明, 是否已经替上游把活干了)。"""
        from windows_mcp.tools import safe_destroy as sd
        mode = (kwargs.get("mode") or "").strip().lower()

        if tool_name == "FileSystem" and mode == "delete":
            path = kwargs.get("path") or ""
            why = sd.recycle(path)
            if why:
                return ("Could not move it to the Recycle Bin (%s). NOTHING was deleted - "
                        "uia-desktop does not fall back to a permanent delete." % why), "refuse"
            return ("Moved to the Recycle Bin: %s\n"
                    "It is NOT gone - restore it from the Recycle Bin if this was the wrong "
                    "path. (Upstream windows-mcp deletes permanently here; this build does "
                    "not.)" % path), "done"

        if tool_name == "FileSystem" and mode == "write":
            append = kwargs.get("append")
            append = append is True or (isinstance(append, str) and append.lower() == "true")
            if not append:
                bak, warn = sd.backup_before_write(kwargs.get("path") or "")
                if bak:
                    return "\n[the file already existed; its previous contents were saved to %s]" % bak, None
                if warn:
                    return "\n[WARNING: %s]" % warn, None
            return None, None

        if tool_name == "Registry" and mode == "delete":
            out, warn = sd.export_registry(kwargs.get("path") or "")
            if out:
                return "\n[this key was exported to %s first - double-click that file to put it back]" % out, None
            return ("\n[WARNING: could not export this key first (%s), so the deletion has NO "
                    "way back]" % warn), None

        return None, None

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        note, state = _pre(kwargs)
        if state == "refuse":
            raise ValueError(note)
        if state == "done":
            return note
        out = await fn(*args, **kwargs)
        return ("%s%s" % (out, note)) if note else out

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        note, state = _pre(kwargs)
        if state == "refuse":
            raise ValueError(note)
        if state == "done":
            return note
        out = fn(*args, **kwargs)
        return ("%s%s" % (out, note)) if note else out

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _with_scrape_guard(fn):
    """`Scrape` 是清理掉埋点之后**唯一剩下的出网能力**。

    它是正经功能，不砍；但两条风险要分开治，理由写在 scrape_guard.py 顶部：
    域名白名单（硬控制）、本地审计（看得见）、返回内容标成不可信数据（防注入）。
    """

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        from windows_mcp.tools import scrape_guard as sg
        url = kwargs.get("url") or ""
        why = sg.check(url)
        if why:
            raise ValueError(why)
        out = await fn(*args, **kwargs)
        return sg.wrap(out if isinstance(out, str) else str(out), url)

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        from windows_mcp.tools import scrape_guard as sg
        url = kwargs.get("url") or ""
        why = sg.check(url)
        if why:
            raise ValueError(why)
        out = fn(*args, **kwargs)
        return sg.wrap(out if isinstance(out, str) else str(out), url)

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _with_watch(fn, tool_name):
    """包一层，调用前点亮横幅。整个过程吞异常——提示层绝不能把正事搞失败。"""

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        _watch(tool_name)
        return fn(*args, **kwargs)

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        _watch(tool_name)
        return await fn(*args, **kwargs)

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _watch(tool_name):
    try:
        from windows_mcp.tools.overlay import watching
        watching("uia-desktop 正在读这台电脑的屏幕 · %s" % tool_name)
    except Exception:
        pass


def _act(tool_name, kwargs):
    """没有坐标的动作工具：先认 Esc，再点亮提示层。

    Esc 的检查必须在动手**之前**，否则等于没停。这里和 `_with_frame` 里是同一段语义，
    刻意重复一遍而不是抽公共函数——那边还要验 frame、要解析 click_text，
    硬凑到一起只会让两边都难读。
    """
    from windows_mcp.tools.overlay import activity, take_cancel
    ago = take_cancel()
    if ago is not None:
        raise ValueError(
            "STOPPED: somebody pressed Esc %.0f seconds ago, while uia-desktop was operating this "
            "machine, so this %s was not performed. Nothing was clicked, typed or invoked. If this "
            "was you, say what to do next; if it was the person sitting at the machine, stop and "
            "ask before touching the desktop again." % (ago, tool_name))
    hwnd = kwargs.get("hwnd") or kwargs.get("window_hwnd") or 0
    try:
        hwnd = int(hwnd)
    except Exception:
        hwnd = 0
    activity(hwnd, "uia-desktop 正在操控这台电脑 · %s" % tool_name)


_LOCKED_TOOLS = ("Click", "Type", "Move", "Scroll", "Shortcut",
                 "Invoke", "SetValue", "MouseDown", "MouseUp", "PressMouse",
                 "MultiSelect", "MultiEdit", "App",
                 "Sequence", "ClickAndWatch", "TypeHere")


def _with_desktop_lock(fn, tool_name):
    """动手之前先拿到这只鼠标的使用权；整个调用期间一直拿着。

    两道门，顺序不能反：**人在动就让路，别的 agent 在动就排队**（见 desktop_lock.py）。

    为什么包在最外层：Sequence 一次跑十几步，要是每一步各拿一次锁，
    另一个 agent 就能插在两步中间动手——而中间界面一变，后面的坐标全错，
    那正是 Sequence 本来要消灭的东西。包在最外层，整段序列是一个不可分割的占用。
    Windows 的互斥体对同一线程可重入，所以 Sequence 里面再调动作工具不会自锁。

    只包动作类。读屏不打扰任何人，PrintWindow 连前台都不用抢，
    人在用电脑的时候我们照样可以看——**该让的是手，不是眼睛**。
    """

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        from windows_mcp.tools.desktop_lock import Busy, guard
        try:
            g = guard(tool_name)
            g.__enter__()
        except Busy as e:
            return str(e)
        try:
            return fn(*args, **kwargs)
        finally:
            g.__exit__(None, None, None)

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        from windows_mcp.tools.desktop_lock import Busy, guard
        try:
            g = guard(tool_name)
            g.__enter__()
        except Busy as e:
            return str(e)
        try:
            return await fn(*args, **kwargs)
        finally:
            g.__exit__(None, None, None)

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _with_act(fn, tool_name):
    """包一层：动手前先拿到这只鼠标的使用权，再认 Esc、点亮提示层。

    「使用权」是两道门：**人在动就让路，别的 agent 在动就排队**。
    见 desktop_lock.py。只有动作类工具走这一层——
    读屏不打扰任何人，PrintWindow 连前台都不用抢。
    """

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        _guard_act(tool_name, kwargs)
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            raise type(e)("%s%s" % (e, _released_note(tool_name, e))) from e

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        _guard_act(tool_name, kwargs)
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            raise type(e)("%s%s" % (e, _released_note(tool_name, e))) from e

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _guard_act(tool_name, kwargs):
    try:
        _act(tool_name, kwargs)
    except ValueError:
        raise                      # Esc 叫停必须真的拦住
    except Exception:
        pass                       # 提示层自己出问题，绝不能把正事搞失败


def _released_note(tool_name, e):
    """出错时松手，并把「松了什么」如实拼进报错里。

    不说的话，调用方无从知道鼠标现在是什么状态——
    而它下一步要不要重试、能不能重试，恰恰取决于这个。
    """
    b = _release_buttons("%s failed: %s" % (tool_name, e))
    if not b:
        return ""
    return (" [uia-desktop released the %s mouse button that was still held, so nothing "
            "is being dragged around the desktop right now]" % b)


def register_with_watch(mcp, module, **kw):
    """替一个模块跑一趟 register：读屏工具包上横幅，动手工具包上提示层 + Esc 叫停。"""
    real_tool = mcp.tool

    def shim(*a, **k):
        name = k.get("name")
        deco = real_tool(*a, **k)

        def _lock(fn):
            """桌面使用权包在**最外层**——所有别的包装（提示层、Esc、frame 校验）
            都在拿到使用权之后才跑。顺序反了的话，我们会在还没轮到自己的时候
            就把提示层点亮、把窗口提到前台，等于一边说"我让路"一边动手。"""
            return _with_desktop_lock(fn, name) if name in _LOCKED_TOOLS else fn

        if name in ("FileSystem", "Registry"):
            def safe_deco(fn):
                return deco(_with_safe_destroy(fn, name))
            return safe_deco
        if name == "Scrape":
            def scrape_deco(fn):
                return deco(_with_scrape_guard(_with_watch(fn, name)))
            return scrape_deco
        if name in _READ_TOOLS:
            def read_deco(fn):
                return deco(_with_watch(fn, name))
            return read_deco
        if name in _ACTION_TOOLS:
            def act_deco(fn):
                return deco(_lock(_with_act(fn, name)))
            return act_deco
        if name in _LOCKED_TOOLS:
            def lock_deco(fn):
                return deco(_lock(fn))
            return lock_deco
        return deco

    mcp.tool = shim
    try:
        module.register(mcp, **kw)
    finally:
        mcp.tool = real_tool


def _with_wait_cap(fn):
    """Wait 封顶：最多睡 WAIT_CAP 秒，然后如实说还差多少。

    不替调用方偷偷循环——那样它以为等够了，实际可能又撞上超时。
    把真相还给它，让它自己决定要不要接着等。
    """

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        want = kwargs.get("duration")
        try:
            want = int(want)
        except Exception:
            return await fn(*args, **kwargs)
        if want <= WAIT_CAP:
            return await fn(*args, **kwargs)
        kwargs["duration"] = WAIT_CAP
        await fn(*args, **kwargs)
        left = want - WAIT_CAP
        return ("Waited %d seconds, NOT the %d you asked for. This tool caps a single "
                "call at %d because your client cuts a call off at %d seconds - a longer "
                "call would be cut off and you would not know how long it actually "
                "slept. %d seconds still to go: call Wait again (or WaitUntil, which "
                "watches for the thing you are waiting for instead of guessing a "
                "duration)." % (WAIT_CAP, want, WAIT_CAP, int(CLIENT_DEADLINE), left))

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        want = kwargs.get("duration")
        try:
            want = int(want)
        except Exception:
            return fn(*args, **kwargs)
        if want <= WAIT_CAP:
            return fn(*args, **kwargs)
        kwargs["duration"] = WAIT_CAP
        fn(*args, **kwargs)
        left = want - WAIT_CAP
        return ("Waited %d seconds, NOT the %d you asked for. This tool caps a single "
                "call at %d because your client cuts a call off at %d seconds - a longer "
                "call would be cut off and you would not know how long it actually "
                "slept. %d seconds still to go: call Wait again (or WaitUntil, which "
                "watches for the thing you are waiting for instead of guessing a "
                "duration)." % (WAIT_CAP, want, WAIT_CAP, int(CLIENT_DEADLINE), left))

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _foreground_title():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32")
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    h = u.GetForegroundWindow()
    if not h:
        return 0, ""
    b = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, b, 512)
    return int(h), b.value


# 9-15 玩微软纸牌时逮到的，而且不是游戏专属：
# 我故意把 Q♠ 拖到空列（克朗代克只允许 K 进空列），游戏把牌弹了回去，
# 而工具返回的是 "Dragged from (749,568) to (519,638)." ——
# **一个被应用明确拒绝的操作，读起来和成功一模一样**。
# 应用接不接受这一下，我们没法通用地判断；但**措辞可以诚实**：
# 说清楚这只证明手势发出去了，不证明有效果。
# 相信这句返回的 agent 会在一个假前提上一路走下去，那比报错糟得多。
_DRAG_NOTE = (" NOTE: this confirms the drag GESTURE was performed, not that the application "
              "accepted the drop. Apps silently bounce an illegal drop back to where it came "
              "from and this call still reads as success. Look at the result before building "
              "anything on it.")


def _with_drag_honesty(fn):
    """Move(drag=True) 成功返回时，补一句「这只说明手势做了」。"""

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        out = await fn(*args, **kwargs)
        try:
            if kwargs.get("drag") and isinstance(out, str) and "Dragged" in out:
                return out + _DRAG_NOTE
        except Exception:
            pass
        return out

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        try:
            if kwargs.get("drag") and isinstance(out, str) and "Dragged" in out:
                return out + _DRAG_NOTE
        except Exception:
            pass
        return out

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _with_waitfor_fix(fn):
    """active_window 走快路；其余条件照旧，但超时要如实报真实耗时。

    原来的超时消息写的是「Timed out after 3.00s」——那是**参数值**，不是真花的时间。
    实测要 12.90 秒。报一个假数字比不报更坏：调用方会据此判断该不该重试、该等多久。
    """
    import time as _t

    def _clamp_timeout(want):
        """别收下一个注定活不到头的 timeout。

        非 active_window 的条件每轮都重建整桌面控件树，一轮就要几秒；
        调用方写 timeout=60，这个调用会跑到客户端截止线上被砍掉，
        它拿到的是 -32001，**连"我等过、没等到"都不知道**。
        砍到预算内，超时消息里如实说明砍了。
        """
        try:
            want = float(want)
        except Exception:
            return want
        return min(want, SAFE_BUDGET)

    def _fast(condition, window_name, timeout, interval):
        t0 = _t.time()
        want = (window_name or "").strip()
        polls = 0
        while True:
            polls += 1
            hwnd, title = _foreground_title()
            if want and want in title:
                return ("WaitFor 'active_window' satisfied after %.2fs and %d attempt(s): "
                        "foreground window is hwnd=%d %r."
                        % (_t.time() - t0, polls, hwnd, title))
            if not want and hwnd:
                return ("WaitFor 'active_window' satisfied after %.2fs: foreground window is "
                        "hwnd=%d %r." % (_t.time() - t0, hwnd, title))
            waited = _t.time() - t0
            if waited + interval > timeout:
                raise TimeoutError(
                    "Timed out after %.2fs (%d polls) waiting for 'active_window' to be %r. "
                    "Foreground is currently hwnd=%d %r. Window titles must match as a "
                    "substring - run Window(mode='list') to see the exact titles."
                    % (waited, polls, want, hwnd, title))
            _t.sleep(interval)

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        cond = (kwargs.get("condition") or "").strip().lower()
        if cond in _WAITFOR_FAST:
            return _fast(cond, kwargs.get("window_name"),
                         max(1.0, min(float(kwargs.get("timeout") or 60.0), SAFE_BUDGET)),
                         max(0.2, min(float(kwargs.get("interval") or 1.0), 30.0)))
        kwargs["timeout"] = _clamp_timeout(kwargs.get("timeout"))
        t0 = _t.time()
        try:
            return await fn(*args, **kwargs)
        except TimeoutError as e:
            raise TimeoutError("%s (this call actually took %.1fs - each poll rebuilds the "
                               "whole desktop UI tree, so the timeout you asked for only "
                               "bounds when the NEXT poll starts, not when this returns. "
                               "It was also capped at %.0fs regardless of what you asked for, "
                               "because your client kills a call at %.0fs and you would have "
                               "got nothing back at all. Call again to keep waiting.)"
                               % (e, _t.time() - t0, SAFE_BUDGET, CLIENT_DEADLINE))

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        cond = (kwargs.get("condition") or "").strip().lower()
        if cond in _WAITFOR_FAST:
            return _fast(cond, kwargs.get("window_name"),
                         max(1.0, min(float(kwargs.get("timeout") or 60.0), SAFE_BUDGET)),
                         max(0.2, min(float(kwargs.get("interval") or 1.0), 30.0)))
        kwargs["timeout"] = _clamp_timeout(kwargs.get("timeout"))
        t0 = _t.time()
        try:
            return fn(*args, **kwargs)
        except TimeoutError as e:
            raise TimeoutError("%s (this call actually took %.1fs - each poll rebuilds the "
                               "whole desktop UI tree, so the timeout you asked for only "
                               "bounds when the NEXT poll starts, not when this returns. "
                               "It was also capped at %.0fs regardless of what you asked for, "
                               "because your client kills a call at %.0fs and you would have "
                               "got nothing back at all. Call again to keep waiting.)"
                               % (e, _t.time() - t0, SAFE_BUDGET, CLIENT_DEADLINE))

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def register_input_with_frame(mcp, input_module, **kw):
    """替上游的 input.register 跑一趟，顺手把那几个工具包上 frame=。"""
    real_tool = mcp.tool

    def shim(*a, **k):
        name = k.get("name")
        deco = real_tool(*a, **k)
        if name == "Wait":
            def wait_deco(fn):
                return deco(_with_wait_cap(fn))
            return wait_deco
        if name == "WaitFor":
            def waitfor_deco(fn):
                return deco(_with_waitfor_fix(_with_watch(fn, "WaitFor")))
            return waitfor_deco
        if name not in _TARGETS:
            return deco

        def wrapped_deco(fn):
            wrapped = _with_frame(fn, name)
            if name == "Move":
                wrapped = _with_drag_honesty(wrapped)
            # 桌面使用权在最外层：frame 校验、提示层、Esc 都在拿到使用权之后才跑
            return deco(_with_desktop_lock(wrapped, name))

        return wrapped_deco

    # 顺手把 frame= 写进工具描述，否则调用方不会知道有这个参数
    def shim_with_desc(*a, **k):
        if k.get("name") == "FileSystem" and isinstance(k.get("description"), str):
            k = dict(k, description=k["description"] + (
                " THIS BUILD: 'delete' moves the target to the Recycle Bin instead of erasing it "
                "(upstream windows-mcp erases it), and refuses rather than falling back to a "
                "permanent delete if that is not possible. 'write' over an existing file keeps a "
                "timestamped .bak- copy of the previous contents and tells you where. You still "
                "have to aim at the right path - these only mean a mistake is recoverable."))
        if k.get("name") == "Registry" and isinstance(k.get("description"), str):
            k = dict(k, description=k["description"] + (
                " THIS BUILD: 'delete' exports the key to the local installation's registry-backup folder "
                "first, so a wrong deletion can be put back by double-clicking that .reg file."))
        if k.get("name") == "Click" and isinstance(k.get("description"), str):
            k = dict(k, description=k["description"] + (
                " WHAT THE RESULT MEANS: 'clicked at (x,y)' confirms the mouse event was sent, "
                "NOT that anything happened - a click on a dead spot reads exactly like one that "
                "worked. When you need to know whether it had an effect, use ClickAndWatch "
                "instead: it clicks, waits for the screen to change, and returns the rectangles "
                "that changed, so you can look at just those instead of re-reading the window."))
        if k.get("name") == "Move" and isinstance(k.get("description"), str):
            k = dict(k, description=k["description"] + (
                " DRAGGING: prefer the one-call atomic form Move(drag=True, from_loc=[x1,y1], "
                "loc=[x2,y2]) - press, move and release all happen inside this single call, so "
                "there is no window in which a failure can leave the button held down. Only use "
                "MouseDown/MouseUp when you genuinely need to inspect or act between press and "
                "release, and note that a Move with drag=True BETWEEN them performs its own "
                "press-and-release and thus ends the drag you were holding (a following MouseUp "
                "then correctly reports that nothing was held). Whatever goes wrong, uia-desktop "
                "releases a button it is holding before returning the error, and says so."))
        if k.get("name") == "WaitFor" and isinstance(k.get("description"), str):
            k = dict(k, description=k["description"] + (
                " COST: condition='active_window' is answered by asking who the foreground "
                "window is - milliseconds. EVERY OTHER condition rebuilds the whole desktop "
                "UI tree on each poll, which takes seconds, so timeout= only bounds when the "
                "next poll starts, not when this call returns (measured: timeout=3 took 12.9s). "
                "Raise interval= for those, or use WaitUntil, which watches for on-screen text "
                "by OCR at ~0.2-0.5s per poll."))
        if k.get("name") == "Wait" and isinstance(k.get("description"), str):
            k = dict(k, description=k["description"] + (
                " A single call sleeps at most %d seconds - your client cuts a call off at "
                "%.0fs and this leaves room for that; ask for more and it returns after %d "
                "telling you how much is left. To wait for something specific rather than a "
                "guessed duration, use WaitUntil."
                % (WAIT_CAP, CLIENT_DEADLINE, WAIT_CAP)))
        if k.get("name") in _TARGETS and isinstance(k.get("description"), str):
            if k["name"] in _FOCUS_TARGET_TOOLS:
                desc = k["description"] + _FOCUS_NOTE
            else:
                desc = k["description"] + _NOTE
                if k["name"] in _TEXT_TARGET_TOOLS:
                    desc += _TEXT_NOTE
            k = dict(k, description=desc)
        return shim(*a, **k)

    mcp.tool = shim_with_desc
    try:
        input_module.register(mcp, **kw)
    finally:
        mcp.tool = real_tool
