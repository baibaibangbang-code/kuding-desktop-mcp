"""PATCH 0.5.5: 画面帧登记 —— 让「过期坐标」变成可检测的错误，而不是静默点错地方。

来历（9-14 观察 ZCode 的电脑控制插件）：它每张截图都带一个 frame_id，点击时把 frame_id
一起传回去：`left_click{target:{frame_id:"frame-cd0d8186", x:351, y:448}}`。
这一条解决的是我们现在真实存在的窟窿——

    坐标是从某一张画面上量出来的，量完到点下去之间，窗口可能已经移动、关闭、或者被盖住了。
    点下去不会报错，只会打到别的东西上。测试机器 9-14 那次险情就是这样：四个微信窗叠在一起，
    右键落到了下面的聊天列表上，弹出的菜单里带「删除聊天记录」。

`_root_at()` 已经能回答「**现在**这个点归谁」。这里补上另一半：「**当时**这个点归谁」，
两边一对比就知道坐标还算不算数。

刻意的取舍：
- 只在调用方显式传 `frame=` 时才拦截。不传就保持原样——因为 Find/FindText/Window
  返回的坐标是调用那一刻现取的，本来就不属于任何一张截图，无差别拦截会制造大量假警报。
- 判据是「这个点的归属窗口变了没有」，不是全屏指纹。窗口挪了、关了、被新窗口盖住了，
  都会被抓到；同一个窗口内部内容变了（比如列表滚了一格）抓不到——这一点如实写在提示里，
  不要让调用方以为通过了检查就万事大吉。
"""

import ctypes
import threading
import time
import uuid
from ctypes import wintypes

user32 = ctypes.windll.user32

_LOCK = threading.Lock()
_FRAMES = {}          # frame_id -> {"t": float, "wins": [(hwnd, (l,t,r,b)), ...] 顶层在前}
_ORDER = []           # frame_id 的先后，用来淘汰老帧
_MAX_FRAMES = 24      # 够用就行，帧本身很小，但没必要无限长

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _rect(h):
    """取不到就回 None。实现在 window_ctl，这里只是换个名字用——
    两份各写各的时，一份查返回值一份不查（9-16 审出来的）。"""
    from windows_mcp.tools.window_ctl import _rect as _wc_rect
    return _wc_rect(h, strict=True)


GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020

_GWL = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_GWL.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
_GWL.argtypes = [wintypes.HWND, ctypes.c_int]


def _click_through(h):
    """这个窗口是不是鼠标穿透的（WS_EX_TRANSPARENT）。

    穿透窗口按定义**收不到点击**——点下去落到它下面那层。
    所以问"我这一下会打到谁"的时候，它根本不是候选人。
    """
    try:
        return bool(int(_GWL(wintypes.HWND(h), GWL_EXSTYLE)) & WS_EX_TRANSPARENT)
    except Exception:
        return False


def _zorder_windows():
    """可见顶层窗口，**按 z 序**（最上面的在前）。EnumWindows 本来就是这个顺序。

    9-15 被 opencode 逮到的一个尴尬 bug：它的 Sequence 报
    「(290,330) belongs to python.exe ('')，不是 ApplicationFrameHost，拒绝点击」——
    那个 python.exe 空标题窗**就是我们自己画的操作提示层**（光标光环跟着鼠标走，
    正好压在它要点的那个点上）。
    **给人看的安全提示，把自己的安全护栏给弄瘫了**，而且拒绝得理直气壮。

    根因是这里：这个函数按 z 序 + 矩形命中判归属，而
    WindowFromPoint 会自动跳过穿透窗口，它不会。两套判断不一致。
    修法不是把我们的提示层按名字排除掉（那是特例），而是按语义排除：
    **鼠标穿透的窗口收不到点击，就不该算作点击的归属。** 谁的都一样。
    """
    out = []

    def cb(h, _):
        if user32.IsWindowVisible(h) and not _click_through(h):
            r = _rect(h)
            # 零面积窗口（很多隐藏的消息窗）没有归属意义，留着只会污染命中判断
            if r and r[2] > r[0] and r[3] > r[1]:
                out.append((int(h), r))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def new_frame(target_hwnd=None):
    """登记一张画面的窗口布局，返回 frame id。截图/快照返回前调用。

    `target_hwnd` 是**按窗口截图专用**的，不传就是整屏截图的老行为。

    为什么要分这一下（9-15 opencode 实跑逮到的，它的原话是
    「`frame` 曾给出错误安全感」）：
    按窗口截图走 `PrintWindow`，拍到的是**窗口自绘的内容，不管谁盖在上面**。
    于是模型看到一张干干净净的图、从上面量坐标，而那一刻遮罩窗口
    （它遇到的是 PickerHost）**本来就已经在那儿了**。
    `check()` 比的是「当时归谁 vs 现在归谁」——两边都是遮罩窗口，一致，于是放行。
    **护栏说没问题，点击却进了别人家。**

    所以按窗口截图的 frame 必须多带一个约束：这个点现在**得归那个被拍的窗口**。
    整屏截图不需要，因为整屏拍到的就是遮挡后的样子，模型看到什么就是什么。
    """
    fid = "f-" + uuid.uuid4().hex[:8]
    with _LOCK:
        _FRAMES[fid] = {"t": time.time(), "wins": _zorder_windows(),
                        "target": int(target_hwnd) if target_hwnd else None}
        _ORDER.append(fid)
        while len(_ORDER) > _MAX_FRAMES:
            _FRAMES.pop(_ORDER.pop(0), None)
    return fid


def _owner_at(wins, x, y):
    """在给定的 z 序窗口表里，(x, y) 落在谁身上。返回 (hwnd, rect) 或 (None, None)。"""
    for h, r in wins:
        if r[0] <= x < r[2] and r[1] <= y < r[3]:
            return h, r
    return None, None


def check(frame_id, x, y):
    """坐标 (x, y) 相对 frame_id 那张画面还成不成立。

    返回 None 表示可以继续；返回字符串表示应当拒绝，字符串就是拒绝理由。
    """
    if not frame_id:
        return None
    with _LOCK:
        f = _FRAMES.get(str(frame_id).strip())
    if f is None:
        return ("frame %r is unknown - it has expired (only the last %d frames are kept) or it was "
                "never issued by this server. Take a fresh Snapshot/Screenshot and use the frame id "
                "it prints, or drop the frame= argument to skip this check."
                % (frame_id, _MAX_FRAMES))

    now = _zorder_windows()
    # 按窗口截图的 frame：先验归属，再验有没有变。
    # 顺序要紧——归属不对时，说"没变化"是误导。
    tgt = f.get("target")
    if tgt:
        own, _r = _owner_at(now, x, y)
        if own and int(own) != int(tgt):
            from windows_mcp.tools.window_ctl import _pid, _proc_name, _text
            return ("REFUSED - that picture was the window's OWN content, drawn by hwnd=%d "
                    "regardless of what sits on top of it. Right now (%d,%d) belongs to hwnd=%d "
                    "(%s '%s'), so a click there would hit that window instead. Nothing was done. "
                    "Bring hwnd=%d to the front first, or act on the window that is actually on "
                    "top. (This is the check a plain frame= cannot make: frame= only asks whether "
                    "the owner CHANGED since the picture, and a window that was already covering "
                    "you has not changed.)"
                    % (tgt, x, y, own, _proc_name(_pid(own)), (_text(own) or "")[:26], tgt))
    return _verdict(f["wins"], now, x, y, frame_id, time.time() - f["t"])


def _verdict(then_wins, now_wins, x, y, frame_id, age):
    """纯比对：当时这个点归谁 vs 现在归谁。抽出来是为了能脱离真实桌面做确定性测试。"""
    then_h, then_r = _owner_at(then_wins, x, y)
    now_h, now_r = _owner_at(now_wins, x, y)

    if then_h is None and now_h is None:
        return None

    def _describe(h):
        """安全版：帧里记下的窗口这会儿可能已经没了，描述不出来也不能把拦截本身搞崩。

        描述不出来时说「窗口 #句柄（已不存在）」，别输出 `('')` 那种看着像空标题的假象。
        """
        try:
            from windows_mcp.tools.window_ctl import _describe as _d
            d = _d(h)
            if d.get("process") or d.get("title"):
                return d
        except Exception:
            pass
        return {"process": "window #%s" % h, "title": "no longer exists"}

    if then_h is None:
        d = _describe(now_h)
        return ("STALE COORDINATE (frame %s, %.1fs old): nothing was at (%d,%d) when that frame was "
                "taken, but %s ('%s') covers it now. Look again before clicking."
                % (frame_id, age, x, y, d["process"], d["title"][:30]))

    if now_h is None:
        return ("STALE COORDINATE (frame %s, %.1fs old): the window that owned (%d,%d) in that frame "
                "is gone - nothing is there now. Look again before clicking."
                % (frame_id, age, x, y))

    if now_h != then_h:
        dn, dt = _describe(now_h), _describe(then_h)
        return ("STALE COORDINATE (frame %s, %.1fs old): (%d,%d) belonged to %s ('%s') in that frame, "
                "but belongs to %s ('%s') now - something moved, closed, or opened on top. "
                "Clicking would hit the wrong window. Take a fresh look."
                % (frame_id, age, x, y, dt["process"], dt["title"][:30],
                   dn["process"], dn["title"][:30]))

    if now_r != then_r:
        return ("STALE COORDINATE (frame %s, %.1fs old): the right window still owns (%d,%d), but it "
                "moved or was resized since that frame (%s -> %s), so everything inside it shifted. "
                "Take a fresh look." % (frame_id, age, x, y, then_r, now_r))

    return None


def actionable(target_hwnd, x, y):
    """(x, y) 现在点下去会不会打到 target_hwnd 上。返回 (bool, 说明)。

    ZCode 把这件事做成了 image_ref 上的 `actionable: true` 字段，我们原来只有一句散文警告。
    同样的信息，字段不会被读漏，散文会。
    """
    if not target_hwnd:
        return True, ""
    try:
        from windows_mcp.tools.window_ctl import _root_at, _describe
        top = _root_at(x, y)
        if top and top != target_hwnd:
            d = _describe(top)
            return False, "%s ('%s') is on top here" % (d["process"], d["title"][:30])
    except Exception:
        pass
    return True, ""
