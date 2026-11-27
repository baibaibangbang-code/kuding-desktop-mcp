"""PATCH: UIA pattern actions — SetValue (write a value directly) and Invoke (native element action).

借鉴 Codex @oai/sky 的 set_value / perform_secondary_action：
- SetValue 走 UIA ValuePattern.SetValue，不模拟敲键盘，不受输入法/焦点/键位影响。
- Invoke 依次尝试 Invoke / Toggle / ExpandCollapse / SelectionItem / LegacyIAccessible 默认动作，
  能触发按钮、勾选框、下拉展开、列表项选中等原生动作，不依赖坐标命中。
两者都按 Snapshot 返回的 label（元素编号）或屏幕坐标寻址，元素通过 ElementFromPoint 重新解析。
"""

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics
import atexit
import windows_mcp.uia as uia


_ROLE_ALIAS = {
    "button": ("按钮", "拆分按钮", "切换按钮"),
    "splitbutton": ("拆分按钮",),
    "togglebutton": ("切换按钮",),
    "edit": ("编辑", "文本框"),
    "text": ("文本", "静态文本"),
    "combobox": ("组合框", "下拉框"),
    "list": ("列表",),
    "listitem": ("列表项目", "列表项"),
    "menuitem": ("菜单项",),
    "menubar": ("菜单栏",),
    "checkbox": ("复选框", "勾选框"),
    "radiobutton": ("单选按钮",),
    "tabitem": ("选项卡项目", "选项卡"),
    "tree": ("树",),
    "treeitem": ("树项目",),
    "dataitem": ("项", "数据项"),
    "hyperlink": ("链接", "超链接"),
    "image": ("图像", "图片"),
    "document": ("文档",),
    "pane": ("窗格",),
    "group": ("组",),
    "window": ("窗口",),
    "table": ("表格",),
    "header": ("标题", "标头"),
    "headeritem": ("标头项",),
    "slider": ("滑块",),
    "scrollbar": ("滚动条",),
    "statusbar": ("状态栏", "状态"),
    "toolbar": ("工具栏",),
    "spinner": ("微调框",),
    "progressbar": ("进度栏", "进度条"),
    "custom": ("自定义",),
}


def _role_variants(v: str):
    """把用户写的 role 展开成候选串（原样 + 中英互译）。"""
    key = v.strip().lower().replace("control", "").replace(" ", "")
    out = {v.strip().lower(), key}
    for zh in _ROLE_ALIAS.get(key, ()):
        out.add(zh.lower())
    for en, zhs in _ROLE_ALIAS.items():
        if any(v.strip() == z for z in zhs):
            out.add(en)
    return {x for x in out if x}


# ---- 选择器（借鉴 terminator 的 role:/name:/nth: 语法）----------------------
_KEYS = ("role", "name", "text", "id", "window", "process", "nth")


def _parse_selector(q: str):
    """'role:Button|name:保存|nth:1' -> ({'role':'Button','name':'保存'}, 1)。裸串按 name 处理。"""
    cond, nth = {}, 0
    for part in [p.strip() for p in q.split("|") if p.strip()]:
        if ":" in part:
            k, v = part.split(":", 1)
            k = k.strip().lower()
            if k in _KEYS:
                if k == "nth":
                    nth = int((v.strip() or "0"))
                else:
                    cond[k] = v.strip()
                continue
        cond["name"] = part
    return cond, nth


def _nodes(desktop, foreground_only=True):
    st = desktop.get_state(use_vision=False, use_dom=False, use_annotation=False,
                           use_ui_tree=True, foreground_only=foreground_only)
    ts = st.tree_state
    out = list(getattr(ts, "interactive_nodes", []) or [])
    out += list(getattr(ts, "scrollable_nodes", []) or [])
    return out


def _node_aid(n):
    aid = getattr(n, "automation_id", None)
    if aid is not None:
        return str(aid)
    return str((getattr(n, "metadata", {}) or {}).get("automation_id", "") or "")


def _ok(n, cond):
    nm = getattr(n, "name", "") or ""
    rl = getattr(n, "control_type", "") or ""
    wn = getattr(n, "window_name", "") or ""
    aid = _node_aid(n)
    if "role" in cond:
        low = rl.lower()
        if not any(v in low or low in v for v in _role_variants(cond["role"])):
            return False
    if "name" in cond and cond["name"].lower() not in nm.lower():
        return False
    if "text" in cond and cond["text"] not in nm:
        return False
    if "id" in cond and cond["id"].lower() != aid.lower():
        return False
    if "window" in cond and cond["window"].lower() not in wn.lower():
        return False
    return True


def _match(nodes, cond):
    return [n for n in nodes if _ok(n, cond)]


# ---- 原生 UIA 范围内搜索：不走全桌面控件树 ----------------------------------
# 9-13 Power BI 实测：全桌面遍历 34 秒且打满元素上限，要找的控件没进树；
# 按窗口收窄后 0.5 秒且完整。所以默认走这条，扫不到任何窗口才退回控件树。
_P_NAME, _P_CTYPE, _P_AID = 30005, 30003, 30011
_P_RECT, _P_OFF, _P_ENABLED, _P_PID = 30001, 30022, 30010, 30002
_TS_ELEMENT, _TS_CHILDREN, _TS_DESCENDANTS = 1, 2, 4
_MAX_SCOPE_WINDOWS = 6


def _iuia():
    from windows_mcp.uia.core import _AutomationClient
    return _AutomationClient.instance().IUIAutomation


class _Pt:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x, self.y = x, y


class _RawHit:
    """跟控件树节点同形的轻量结果，外加 element 供直接调 pattern。"""
    __slots__ = ("element", "name", "control_type", "window_name",
                 "automation_id", "center", "enabled", "offscreen", "width", "height")

    def __init__(self, el, win_name):
        self.element = el
        self.name = el.CachedName or ""
        try:
            self.control_type = uia.ControlTypeNames.get(el.CachedControlType, str(el.CachedControlType))
        except Exception:
            self.control_type = "?"
        try:
            self.automation_id = el.CachedAutomationId or ""
        except Exception:
            self.automation_id = ""
        self.window_name = win_name
        r = el.CachedBoundingRectangle
        self.center = _Pt(int((r.left + r.right) / 2), int((r.top + r.bottom) / 2))
        self.width = int(r.right - r.left)
        self.height = int(r.bottom - r.top)
        try:
            self.enabled = bool(el.CachedIsEnabled)
        except Exception:
            self.enabled = True
        try:
            self.offscreen = bool(el.CachedIsOffscreen)
        except Exception:
            self.offscreen = False

    def usable(self):
        # 零面积元素没有可点的位置，中心点算出来是 (0,0)，点下去就打在屏幕左上角的「开始」热角上。
        # 最小化窗口里这种元素成片出现（本机实测 662 个里有 178 个），必须一起排除。
        if self.width <= 0 or self.height <= 0:
            return False
        return (not self.offscreen) and self.center.x > -30000 and self.center.y > -30000


def _proc_name(pid):
    """实现在 window_ctl。原来这里是一份手抄件，而且抄坏了：
    QueryFullProcessImageNameW 抛异常时它不会 CloseHandle，句柄就漏了
    （window_ctl 那份用的是 try/finally）。9-16 审出来，改成直接用那一份。"""
    try:
        from windows_mcp.tools.window_ctl import _proc_name as _wc
        return _wc(pid)
    except Exception:
        return ""
def _win_class(el):
    """窗口类名是判断「能不能用控件树」最快的线索，比试探一轮便宜得多。"""
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = el.CurrentNativeWindowHandle
        if not hwnd:
            return "?"
        b = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(wintypes.HWND(int(hwnd)), b, 256)
        return b.value or "?"
    except Exception:
        return "?"


def _win32_title(el):
    try:
        import ctypes
        hwnd = el.CurrentNativeWindowHandle
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(int(hwnd), buf, 512)
        return buf.value
    except Exception:
        return ""


def _scope_windows(cond):
    """按 window:/process: 挑顶层窗口；都没给就用前台窗口。返回 ([(element, 名字)], 说明)。

    window: 同时匹配 UIA Name 和 Win32 标题栏文字 —— 两者常常不一样
    （Power BI 的标题栏是「zaigang_dashboard - Power BI Desktop」，UIA Name 只有 zaigang_dashboard）。
    """
    iu = _iuia()
    root = uia.GetRootControl().Element
    true_cond = iu.CreateTrueCondition()
    want_win = (cond.get("window") or "").lower()
    want_proc = (cond.get("process") or "").lower()
    picked = []
    if want_win or want_proc:
        arr = root.FindAll(_TS_CHILDREN, true_cond)
        for i in range(arr.Length):
            w = arr.GetElement(i)
            try:
                nm = w.CurrentName or ""
                pid = int(w.CurrentProcessId)
            except Exception:
                continue
            title = _win32_title(w)
            if want_win and want_win not in nm.lower() and want_win not in title.lower():
                continue
            if want_proc and want_proc not in _proc_name(pid).lower():
                continue
            picked.append((w, nm or title))
            if len(picked) >= _MAX_SCOPE_WINDOWS:
                break
        why = "scope=" + (("window:" + cond["window"]) if want_win else ("process:" + cond["process"]))
        return picked, why
    try:
        fg = uia.GetForegroundControl()
        if fg is not None and fg.Element:
            return [(fg.Element, fg.Name or "")], "scope=foreground"
    except Exception:
        pass
    # 前台窗口取不到（提权窗口、锁屏、切换瞬间）——退而扫前几个顶层窗口，并如实说明
    arr = root.FindAll(_TS_CHILDREN, true_cond)
    for i in range(min(arr.Length, _MAX_SCOPE_WINDOWS)):
        try:
            w = arr.GetElement(i)
            picked.append((w, w.CurrentName or ""))
        except Exception:
            pass
    return picked, "scope=no foreground window, scanned %d top-level windows" % len(picked)


def _raw_search(cond):
    """返回 (hits, scanned, why, roles)。window:/process: 已在选窗阶段用掉，不再逐元素比。"""
    iu = _iuia()
    cr = iu.CreateCacheRequest()
    for p in (_P_NAME, _P_CTYPE, _P_AID, _P_RECT, _P_OFF, _P_ENABLED, _P_PID):
        cr.AddProperty(p)
    cr.TreeScope = _TS_ELEMENT
    true_cond = iu.CreateTrueCondition()
    wins, why = _scope_windows(cond)
    inner = {k: v for k, v in cond.items() if k not in ("window", "process")}
    hits, scanned, roles = [], 0, set()
    hidden, empty = [], []
    for w, wname in wins:
        try:
            arr = w.FindAllBuildCache(_TS_DESCENDANTS, true_cond, cr)
        except Exception:
            continue
        per_win = [0, 0]  # scanned, offscreen
        for i in range(arr.Length):
            scanned += 1
            per_win[0] += 1
            try:
                h = _RawHit(arr.GetElement(i), wname)
            except Exception:
                continue
            if not h.usable():
                per_win[1] += 1
                continue
            roles.add(h.control_type)
            if _ok(h, inner):
                hits.append(h)
        # 最小化/托盘的窗口，元素全在屏幕外（rect 约 -32000），会被 usable() 滤光。
        # 只说 "matched nothing" 会把人往「选择器写错了」的方向带，必须点名是哪个窗口没展开。
        # 按窗口逐个判：范围里有一个窗口是展开的，也不能掩盖另一个是最小化的。
        if per_win[0] and per_win[0] == per_win[1]:
            hidden.append("'%s' (%d elements)" % (wname or "untitled", per_win[0]))
        elif per_win[0] <= 4:
            # 窗口在、后代却只有个位数（而且多半是无名 Pane）= 自绘外壳，UIA 看不进去。
            # 剪映 11.4.2 实测：主窗 Qt622QWindowIcon，0 个子 HWND、0 个 UIA 后代。
            empty.append("'%s' [%s] (%d elements)" % (wname or "untitled", _win_class(w), per_win[0]))
    if hidden:
        why += (" -- MINIMIZED or in the tray, nothing of it can be matched: %s. "
                "Restore it with Window(mode='normalize'), then search again." % "; ".join(hidden))
    try:
        from windows_mcp.tools.window_ctl import session_locked
        if session_locked():
            why += (" !! THE SESSION IS LOCKED - the lock screen is in front, so clicks and screenshots do nothing "
                    "and most windows report no usable elements. Unlock the desktop before retrying.")
    except Exception:
        pass
    if empty:
        why += (" -- self-drawn window, its UIA tree is EMPTY: %s. Selectors cannot work here; use FindText/ReadText "
                "plus coordinates. Class name tells you what it is: Qt* = Qt-rendered (WeChat, JianyingPro); "
                "an 'Internet Explorer_Hidden' top-level window means legacy IE-hosted web content, where "
                "--force-renderer-accessibility does NOT help; a Chromium/CEF host may expose a tree if you relaunch "
                "it with --force-renderer-accessibility, but check the MAIN window class first - embedded web panels "
                "are not the shell." % "; ".join(empty))
    return hits, scanned, why, roles


def _find_hits(desktop, selector, foreground_only=True, scan="auto"):
    """原生优先、控件树兜底。返回 (hits, nth, 说明)。"""
    cond, nth = _parse_selector(selector)
    if scan != "tree":
        try:
            hits, scanned, why, roles = _raw_search(cond)
        except Exception as e:
            hits, scanned, why, roles = [], 0, "raw search failed (%s)" % type(e).__name__, set()
        if scanned or scan == "uia":
            return hits, nth, "UIA %d elements, roles: %s | %s" % (
                scanned, ", ".join(sorted(roles)[:12]) or "-", why)
        # 显式写了 process:/window: 却一个窗口都没匹配上时，绝不退回全桌面扫描：
        # never silently widen an explicit scope - 那样会从别的应用里捞出同名控件，
        # 而且还会顺口建议「加 process:」，可调用方明明已经加了。9-14 自测抓到。
        if cond.get("process") or cond.get("window"):
            want = cond.get("process") or cond.get("window")
            key = "process" if cond.get("process") else "window"
            return [], nth, ("no window matched %s:%s - nothing was searched. Run Window(mode='list') to see the "
                             "real process/title (a minimized window still shows there), and check the app is "
                             "actually running. Not falling back to a whole-desktop scan on purpose: that would "
                             "return elements from a different app." % (key, want))
    nodes = _nodes(desktop, foreground_only)
    tree_hits = _match(nodes, {k: v for k, v in cond.items() if k != "process"})
    roles = sorted({(getattr(n, "control_type", "") or "?") for n in nodes})
    return tree_hits, nth, ("control tree fallback (whole desktop, SLOW and it may truncate before reaching your "
                            "element) - add process: or window: to the selector, %d nodes, roles: %s"
                            % (len(nodes), ", ".join(roles[:12]) or "-"))


def _pick(desktop, selector, foreground_only=True):
    hits, nth, how = _find_hits(desktop, selector, foreground_only)
    if not hits:
        raise LookupError("selector '%s' matched nothing [%s]." % (selector, how))
    if nth >= len(hits):
        raise LookupError("selector '%s' matched %d element(s) [%s]; nth:%d out of range."
                          % (selector, len(hits), how, nth))
    return hits[nth]


def _resolve_selector(desktop, selector, foreground_only=True):
    n = _pick(desktop, selector, foreground_only)
    return n.center.x, n.center.y


def _resolve(desktop, loc, label, selector=None):
    """返回 (x, y, control)。selector 命中原生元素时直接用那个元素，
    不再拿坐标去 ControlFromPoint 反查 —— 否则动作可能落在该坐标最上层的另一个元素上。"""
    el = None
    if selector:
        n = _pick(desktop, selector)
        x, y = n.center.x, n.center.y
        el = getattr(n, "element", None)
    elif label is not None:
        x, y = desktop.get_coordinates_from_label(int(label))
    elif loc is not None:
        x, y = int(loc[0]), int(loc[1])
    else:
        raise ValueError("Provide selector, label or loc=[x, y].")
    ctrl = None
    if el is not None:
        try:
            ctrl = uia.Control(element=el)
            _ = ctrl.ControlTypeName
        except Exception:
            ctrl = None
    if ctrl is None:
        ctrl = uia.ControlFromPoint(x, y)
    if ctrl is None:
        raise RuntimeError(f"No UI element found at ({x}, {y}).")
    return x, y, ctrl


# ---- 按住状态 + 看门狗（防止流程中断后鼠标一直按着到处拖）--------------------
_HELD = {"button": None, "at": None, "timer": None}


def _release_on_exit():
    """进程退出时把按着的鼠标键松开。

    看门狗的 Timer 是 daemon 线程——**进程一死它就跟着死，永远不会触发**。
    也就是说「服务被杀」这个最常见的情形恰恰是看门狗唯一盖不住的：
    我每次重启 opencode 都会杀掉这个服务，要是那一刻正按着键拖拽，
    左键就一直按在那儿，之后人的每一次移动鼠标都在拖东西。
    这台机器上开着微信、钉钉和真实业务数据，这种残留是真会出事的。
    """
    try:
        _release_held("server process exiting")
    except Exception:
        pass


atexit.register(_release_on_exit)


def _release_held(reason="watchdog"):
    b = _HELD.get("button")
    if not b:
        return None
    UP = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}.get(b, 0x0004)
    try:
        uia.mouse_event(UP, 0, 0, 0, 0)
    except Exception:
        pass
    t = _HELD.get("timer")
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
    at = _HELD.get("at")
    _HELD.update({"button": None, "at": None, "timer": None})
    return (b, at, reason)


def _describe(ctrl) -> str:
    try:
        return f'{ctrl.ControlTypeName} "{ctrl.Name}"'
    except Exception:
        return "element"


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="SetValue",
        description=(
            "Write a value directly into an editable element via UI Automation ValuePattern — no keystrokes are "
            "simulated, so it is immune to IME state, focus loss and key-repeat loss. Address the element by its "
            "label (element id from Snapshot) or by loc=[x, y]. Falls back to click + Ctrl+A + typing when the "
            "element has no ValuePattern. Use this instead of Type for text fields, date/time inputs and combo boxes."
        ),
        annotations=ToolAnnotations(
            title="SetValue", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "SetValue-Tool")
    def set_value_tool(
        value: Annotated[str, Field(description="The full replacement value for the element.")],
        selector: Annotated[str | None, Field(description="Semantic selector, e.g. 'role:Edit|name:文件名'. Preferred over label/loc: it survives window moves, resizes and DPI changes, so it can be written into a reusable playbook.")] = None,
        label: Annotated[int | None, Field(description="Element id from the latest Snapshot (changes every snapshot - prefer selector).")] = None,
        loc: Annotated[list[int] | None, Field(description="Screen coordinates [x, y] as a last resort.")] = None,
        press_enter: Annotated[bool, Field(description="Press Enter after setting the value.")] = False,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        x, y, ctrl = _resolve(desktop, loc, label, selector)
        # 有些控件（如 Electron 的日期框）ElementFromPoint 命中的是外层 Group，往里找一层有 ValuePattern 的
        target = ctrl
        vp = None
        for cand in [ctrl] + (ctrl.GetChildren() or [])[:6]:
            try:
                p = cand.GetPattern(uia.PatternId.ValuePattern)
                if p is not None:
                    vp = p
                    target = cand
                    break
            except Exception:
                continue
        if vp is not None:
            try:
                if vp.IsReadOnly:
                    return f"Element {_describe(target)} at ({x},{y}) is read-only; value not changed."
            except Exception:
                pass
            vp.SetValue(value)
            if press_enter:
                uia.SendKeys("{Enter}", waitTime=0.05)
            try:
                back = vp.Value
            except Exception:
                back = "?"
            return f'SetValue OK on {_describe(target)} at ({x},{y}); element now reads "{back}".'
        # 回退：点击 + 全选 + 输入
        desktop.type((x, y), value, clear=True, press_enter=press_enter)
        return f"Element {_describe(ctrl)} at ({x},{y}) has no ValuePattern; fell back to click + select-all + type."

    @mcp.tool(
        name="Invoke",
        description=(
            "Trigger an element's native UI Automation action instead of clicking its coordinates: Invoke (buttons, "
            "menu items, links), Toggle (checkboxes/switches), Expand/Collapse (combo boxes, tree nodes, dropdowns), "
            "Select (list/tab/radio items) or the LegacyIAccessible default action. Address by label (element id from "
            "Snapshot) or loc=[x, y]. action='auto' picks the first pattern the element supports; pass an explicit "
            "action to force one. Returns which pattern fired and the element's resulting state."
        ),
        annotations=ToolAnnotations(
            title="Invoke", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "Invoke-Tool")
    def invoke_tool(
        selector: Annotated[str | None, Field(description="Semantic selector, e.g. 'role:Button|name:确定|nth:1'. Preferred over label/loc - durable across window moves and resizes.")] = None,
        label: Annotated[int | None, Field(description="Element id from the latest Snapshot (changes every snapshot - prefer selector).")] = None,
        loc: Annotated[list[int] | None, Field(description="Screen coordinates [x, y] as a last resort.")] = None,
        action: Annotated[
            Literal["auto", "invoke", "toggle", "expand", "collapse", "select", "default"],
            Field(description="Which pattern to use; 'auto' tries invoke → toggle → expand → select → default."),
        ] = "auto",
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        x, y, ctrl = _resolve(desktop, loc, label, selector)
        who = f"{_describe(ctrl)} at ({x},{y})"
        order = {
            "auto": ["invoke", "toggle", "expand", "select", "default"],
            "invoke": ["invoke"], "toggle": ["toggle"], "expand": ["expand"],
            "collapse": ["collapse"], "select": ["select"], "default": ["default"],
        }[action]
        tried = []
        for step in order:
            try:
                if step == "invoke":
                    p = ctrl.GetPattern(uia.PatternId.InvokePattern)
                    if p: p.Invoke(); return f"Invoke fired on {who}."
                elif step == "toggle":
                    p = ctrl.GetPattern(uia.PatternId.TogglePattern)
                    if p:
                        p.Toggle()
                        return f"Toggle fired on {who}; state now {p.ToggleState}."
                elif step in ("expand", "collapse"):
                    p = ctrl.GetPattern(uia.PatternId.ExpandCollapsePattern)
                    if p:
                        (p.Expand if step == "expand" else p.Collapse)()
                        return f"{step.title()} fired on {who}; state now {p.ExpandCollapseState}."
                elif step == "select":
                    p = ctrl.GetPattern(uia.PatternId.SelectionItemPattern)
                    if p: p.Select(); return f"Select fired on {who}."
                elif step == "default":
                    p = ctrl.GetPattern(uia.PatternId.LegacyIAccessiblePattern)
                    if p:
                        da = p.DefaultAction
                        if da:
                            p.DoDefaultAction()
                            return f'LegacyIAccessible default action "{da}" fired on {who}.'
                tried.append(step)
            except Exception as e:
                tried.append(f"{step}({type(e).__name__})")
        # 全部模式都不支持：退回坐标点击，行为和 Click 一致
        desktop.click((x, y))
        return f"{who} exposes none of {tried}; fell back to a coordinate click."

    @mcp.tool(
        name="Find",
        description=(
            "Locate UI elements by a semantic selector instead of a screenshot index or pixel coordinates. "
            "Syntax: 'role:Button|name:Save' - '|' ANDs the conditions. Keys: role (control type, substring, "
            "case-insensitive), name (substring, case-insensitive), text (case-sensitive), id (exact "
            "AutomationId), window (owning window - matches both the UIA name and the Win32 title bar), "
            "process (executable name, e.g. process:PBIDesktop), nth (0-based pick among matches). "
            "ALWAYS add window: or process: on a big app (Office, Power BI, browsers): without a scope the "
            "search falls back to the foreground window, and a whole-desktop scan can be 70x slower AND "
            "silently miss the element you want. A bare string is "
            "treated as name. Returns each match with role, name, screen coordinates and the label index usable "
            "by Click. Selectors survive window moves, resizes and DPI changes - write selectors, not "
            "coordinates, into a reusable playbook."
        ),
        annotations=ToolAnnotations(
            title="Find", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "Find-Tool")
    def find_tool(
        selector: Annotated[str, Field(description="e.g. 'role:Button|name:确定' or 'process:PBIDesktop|id:$pbi-checkbox-2'.")],
        foreground_only: Annotated[bool, Field(description="Only used by the control-tree fallback.")] = True,
        limit: Annotated[int, Field(description="Max matches to return.")] = 20,
        scan: Annotated[
            Literal["auto", "uia", "tree"],
            Field(description="auto = native UIA scoped search, control tree only if no window could be scanned. 'tree' forces the old whole-desktop tree (slow, may truncate)."),
        ] = "auto",
        ctx: Context = None,
    ) -> str:
        import time as _t
        desktop = get_desktop()
        t0 = _t.perf_counter()
        hits, _nth, how = _find_hits(desktop, selector, foreground_only, scan)
        ms = (_t.perf_counter() - t0) * 1000
        if not hits:
            return "selector '%s' matched nothing in %.0f ms [%s]" % (selector, ms, how)
        lines = ["selector '%s' matched %d element(s) in %.0f ms [%s]:" % (selector, len(hits), ms, how)]
        for i, n in enumerate(hits[:limit]):
            aid = _node_aid(n)
            lines.append('  nth:%d  (%d,%d)  %s "%s"%s'
                         % (i, n.center.x, n.center.y,
                            getattr(n, "control_type", "?"), getattr(n, "name", ""),
                            ("  id=" + aid) if aid else ""))
        if len(hits) > limit:
            lines.append("  ... %d more" % (len(hits) - limit))
        return "\n".join(lines)

    @mcp.tool(
        name="PressMouse",
        description=(
            "Press and hold a mouse button at a position for a given duration, then release. Use this when a "
            "plain Click does not register: self-drawn UIs (WeChat/Qt, Electron canvases, games) often need a "
            "real hold, and some menus only open on a long press or a held right-click. duration is in seconds "
            "(0.05-10). Address by selector, label or loc=[x, y]. Also useful to give a window focus before a "
            "fast follow-up click."
        ),
        annotations=ToolAnnotations(
            title="PressMouse", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "PressMouse-Tool")
    def press_mouse_tool(
        duration: Annotated[float, Field(description="Hold time in seconds, e.g. 2.6 for a long press.")] = 1.0,
        button: Annotated[Literal["left", "right"], Field(description="Which button to hold.")] = "left",
        selector: Annotated[str | None, Field(description="Semantic selector, preferred over label/loc.")] = None,
        label: Annotated[int | None, Field(description="Element id from the latest Snapshot.")] = None,
        loc: Annotated[list[int] | None, Field(description="Screen coordinates [x, y].")] = None,
        move_first: Annotated[bool, Field(description="Move the cursor there before pressing (needed by some apps that track hover).")] = True,
        ctx: Context = None,
    ) -> str:
        import time as _t
        desktop = get_desktop()
        if selector:
            x, y = _resolve_selector(desktop, selector)
        elif label is not None:
            x, y = desktop.get_coordinates_from_label(int(label))
        elif loc is not None:
            x, y = int(loc[0]), int(loc[1])
        else:
            raise ValueError("Provide selector, label or loc=[x, y].")
        d = max(0.05, min(float(duration), 10.0))
        if move_first:
            uia.MoveTo(x, y, waitTime=0.05)
            _t.sleep(0.12)
        DOWN = 0x0008 if button == "right" else 0x0002   # RIGHTDOWN / LEFTDOWN
        UP = 0x0010 if button == "right" else 0x0004     # RIGHTUP / LEFTUP
        uia.SetCursorPos(x, y)
        uia.mouse_event(DOWN, 0, 0, 0, 0)
        try:
            _t.sleep(d)
        finally:
            uia.mouse_event(UP, 0, 0, 0, 0)
        _t.sleep(0.15)
        return "Held %s button at (%d,%d) for %.2fs, then released." % (button, x, y, d)

    @mcp.tool(
        name="MouseDown",
        description=(
            "Press a mouse button and HOLD it. Pair with MouseUp; between the two you can call Move or Scroll "
            "to drag precisely, rubber-band select, or draw. Prefer the one-shot Click / PressMouse / Move(drag=True) "
            "when you do not need intermediate movement. SAFETY: a held button that is never released will drag "
            "things around the desktop, so this tool arms a watchdog that auto-releases after auto_release_sec "
            "(default 30). Calling MouseDown while a button is already held releases the old one first."
        ),
        annotations=ToolAnnotations(
            title="MouseDown", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "MouseDown-Tool")
    def mouse_down_tool(
        button: Annotated[Literal["left", "right", "middle"], Field(description="Which button to hold.")] = "left",
        selector: Annotated[str | None, Field(description="Semantic selector for the press position.")] = None,
        label: Annotated[int | None, Field(description="Element id from the latest Snapshot.")] = None,
        loc: Annotated[list[int] | None, Field(description="Screen coordinates [x, y].")] = None,
        auto_release_sec: Annotated[float, Field(description="Watchdog: auto-release after this many seconds (1-300).")] = 30.0,
        ctx: Context = None,
    ) -> str:
        import threading as _th, time as _t
        desktop = get_desktop()
        if selector:
            x, y = _resolve_selector(desktop, selector)
        elif label is not None:
            x, y = desktop.get_coordinates_from_label(int(label))
        elif loc is not None:
            x, y = int(loc[0]), int(loc[1])
        else:
            raise ValueError("Provide selector, label or loc=[x, y].")
        prev = _release_held("superseded by a new MouseDown")
        uia.MoveTo(x, y, waitTime=0.05)
        _t.sleep(0.1)
        uia.SetCursorPos(x, y)
        DOWN = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}[button]
        uia.mouse_event(DOWN, 0, 0, 0, 0)
        secs = max(1.0, min(float(auto_release_sec), 300.0))
        timer = _th.Timer(secs, _release_held, args=("watchdog timeout",))
        timer.daemon = True
        timer.start()
        _HELD.update({"button": button, "at": (x, y), "timer": timer})
        note = "" if not prev else " (released a previously held %s button first)" % prev[0]
        return ("%s button is now HELD at (%d,%d)%s. Move/Scroll to drag, then call MouseUp. "
                "It will auto-release in %.0fs if you forget." % (button.title(), x, y, note, secs))

    @mcp.tool(
        name="MouseUp",
        description=(
            "Release the mouse button held by MouseDown, optionally moving to a position first (that position "
            "becomes the drop point). Safe to call when nothing is held - it just reports that."
        ),
        annotations=ToolAnnotations(
            title="MouseUp", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
    )
    @with_analytics(get_analytics(), "MouseUp-Tool")
    def mouse_up_tool(
        button: Annotated[
            Literal["left", "right", "middle"] | None,
            Field(description="Which button to release. Defaults to whatever MouseDown is holding; accepted for symmetry with MouseDown."),
        ] = None,
        selector: Annotated[str | None, Field(description="Move here before releasing (drop target).")] = None,
        label: Annotated[int | None, Field(description="Element id to release over.")] = None,
        loc: Annotated[list[int] | None, Field(description="Screen coordinates [x, y] to release over.")] = None,
        ctx: Context = None,
    ) -> str:
        import time as _t
        desktop = get_desktop()
        dest = None
        if selector:
            dest = _resolve_selector(desktop, selector)
        elif label is not None:
            dest = desktop.get_coordinates_from_label(int(label))
        elif loc is not None:
            dest = (int(loc[0]), int(loc[1]))
        if dest:
            uia.MoveTo(dest[0], dest[1], waitTime=0.05)
            _t.sleep(0.12)
            uia.SetCursorPos(dest[0], dest[1])
        held = _HELD.get("button")
        if not held:
            return "Nothing was held; no button released." + (" Cursor moved to (%d,%d)." % dest if dest else "")
        mismatch = ""
        if button and button != held:
            mismatch = " (you asked for %s but %s was the one held; released %s)" % (button, held, held)
        b, at, _ = _release_held("MouseUp")
        where = " at (%d,%d)" % dest if dest else (" at (%d,%d)" % at if at else "")
        moved = "" if not (dest and at) else " (dragged from (%d,%d))" % at
        return "Released the %s button%s%s.%s" % (b, where, moved, mismatch)
