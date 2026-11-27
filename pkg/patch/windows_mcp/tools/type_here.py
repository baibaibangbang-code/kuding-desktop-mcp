"""PATCH 0.5.12：往**当前焦点**里输入，并且当场回读校验。

两个缺口都是 opencode 在真跑时提的，我判断都属于「提高准确率」那一类，所以做：

1. **上游 `Type` 必须给坐标，而且动手前一定先点一下那个坐标。**
   可很多时候焦点已经在正确的地方了——刚点开输入框、刚按了 Tab、刚关掉一个对话框。
   这时再点一次是**净风险**：坐标可能因为布局变化落到别的控件上，
   点在已有文字上还会改变插入点或选中状态。`TypeHere` 不点，只打字。

2. **打完没人核实。** 我们自己的文档早就写着「OCR 只能用来定位，不能用来核对内容」
   （它会把「测试」读成「氵则试」），硬证据是剪贴板回读。可那是三四个动作的事，
   写文档要求不如做进工具——`verify=True` 打完立刻全选复制读回来比对，
   **比对不上就明说比对不上**，不让调用方以为打进去了。

回读是有代价的，所以默认关着：它会全选（改变选中状态）、动剪贴板（用完恢复原值）。
文案、消息、文件名这种「打错了会有后果」的，就该打开。

打字本身复用 `ime_safe_type.route()` 的路由，不重复实现一遍——
中文必须走剪贴板，走 SendKeys 会被输入法吃掉变成拼音（0.5.5 的教训）。
"""

import time

from typing import Optional

from mcp.types import ToolAnnotations
from fastmcp import Context

from windows_mcp.tools.ime_safe_type import ime_off, route


def _clip_get():
    """读剪贴板。读不到就返回 None，不抛——校验失败不该把打字这件事也带崩。"""
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def _clip_set(text):
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            if text is not None:
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def _send(keys, interval=0.04):
    import windows_mcp.uia as uia
    uia.SendKeys(keys, interval=interval, waitTime=0.05)


def _type_no_click(text):
    """把文字送进当前焦点，不点任何地方。路由和 0.5.5 那套一致。"""
    import windows_mcp.uia as uia
    mode = route(text or "")
    if mode == "paste":
        keep = _clip_get()
        _clip_set(text)
        try:
            _send("{Ctrl}v")
            time.sleep(0.15)
        finally:
            # 原来剪贴板里是什么，还回去。人可能正复制着别的东西。
            _clip_set(keep)
        return mode
    if mode == "sendkeys-ime-off":
        with ime_off():
            uia.SendKeys(text, interval=0.04, waitTime=0.05)
        return mode
    uia.SendKeys(text, interval=0.04, waitTime=0.05)
    return mode


def _read_back():
    """全选复制读回来。返回 (读到的文字, 说明)。

    会改变选中状态——所以读完按一下 End 把插入点放到末尾，
    否则调用方接着打的下一个字会把整段替换掉。
    """
    keep = _clip_get()
    try:
        _clip_set("")                  # 先清空：不清空的话复制失败会读到上一次的值，
        time.sleep(0.05)               # 那就是在拿旧数据冒充校验结果
        _send("{Ctrl}a")
        time.sleep(0.1)
        _send("{Ctrl}c")
        time.sleep(0.25)
        got = _clip_get()
        _send("{End}")                 # 取消选中，把插入点放到末尾
        return got, None
    except Exception as e:
        return None, "read-back failed: %s" % e
    finally:
        _clip_set(keep)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="TypeHere",
        description=(
            "Types text into WHATEVER CURRENTLY HAS KEYBOARD FOCUS - no coordinates, and it "
            "does NOT click anything first. Use this right after you clicked into a field, "
            "pressed Tab, or dismissed a dialog: re-clicking the field (which Type does) is "
            "pure added risk, because the coordinate may now land on a different control and "
            "a click inside existing text moves the caret or changes the selection. "
            "Pass window=/process=/hwnd= to require that window to be in the foreground first "
            "(the call is REFUSED, typing nothing, if focus belongs to someone else). "
            "Set verify=True for text that matters - a post, a message, a filename: it selects "
            "all, copies, and compares what is actually in the field against what you sent, and "
            "SAYS SO when they differ. Do not verify with a screenshot: OCR confuses "
            "visually-similar Chinese characters, so it can only tell you where things are, "
            "never what they say. verify=True changes the selection and briefly uses the "
            "clipboard (the previous clipboard content is put back), which is why it is off by "
            "default."
        ),
        annotations=ToolAnnotations(
            title="TypeHere",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def type_here_tool(
        text: str,
        verify: bool = False,
        hwnd: Optional[int] = None,
        window: Optional[str] = None,
        process: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        from windows_mcp.tools.frame_wrap import _resolve_focus
        from windows_mcp.tools.overlay import activity, take_cancel

        ago = take_cancel()
        if ago is not None:
            raise ValueError(
                "STOPPED: somebody pressed Esc %.0f seconds ago, while uia-desktop was "
                "operating this machine, so this TypeHere was not performed. Nothing was "
                "typed. If this was you, say what to do next; if it was the person sitting "
                "at the machine, stop and ask before touching the desktop again." % ago)

        kw = {"hwnd": hwnd, "window": window, "process": process}
        target = _resolve_focus(kw, "TypeHere")
        try:
            activity(target or 0, "uia-desktop 正在操控这台电脑 · TypeHere")
        except Exception:
            pass

        mode = _type_no_click(text)
        if not verify:
            return ("Typed %d chars into the focused control via %s. NOT verified - "
                    "pass verify=True when the text matters." % (len(text), mode))

        got, why = _read_back()
        if why:
            return ("Typed %d chars via %s, but the read-back itself failed (%s), so this "
                    "is UNVERIFIED - do not assume it went in." % (len(text), mode, why))
        # 空串和 None 是一回事：回读前我故意把剪贴板清空了，所以「读回来是空的」
        # 只能说明**没读到**，不能说明框里是空的。两者都不该当成校验通过。
        if not got:
            return ("Typed %d chars via %s, but reading the field back gave nothing. Either "
                    "the control does not support select-all/copy, or the text did not go in. "
                    "UNVERIFIED - check another way before moving on." % (len(text), mode))
        # 比之前先把换行统一。
        #
        # 9-15 自己撞到的假失败：我把一段带空行的多行任务粘进 opencode 的输入框，
        # 内容**完整进去了**（对面收到的一字不差），verify 却报 MISMATCH——
        # 因为编辑框把 \n 规范化成了 \r\n，而我在逐字比。
        # 这正是我一直在别处修的那类错：**做成了却报失败**。
        # 调用方照着这个结论会重发一遍，而重发一条已经发出去的消息是有代价的。
        #
        # 只归一化行尾和首尾空白——**不碰内容本身**。
        # 再宽就会把"真的少了字"也放过去，那就把校验变成了摆设。
        def _norm(v):
            return (v or "").replace("\r\n", "\n").replace("\r", "\n").strip()

        if got == text or _norm(got) == _norm(text):
            extra = ("" if got == text else
                     " (the control normalised line endings; the text itself matches exactly)")
            return ("Typed and VERIFIED %d chars via %s: the field now contains exactly what "
                    "you sent.%s" % (len(text), mode, extra))
        if _norm(text) in _norm(got):
            return ("Typed and verified via %s: your text IS in the field, but the field holds "
                    "more than you sent (%d chars vs %d). It probably already had content - "
                    "pass clear=True on Type, or clear it first. Field now: %r"
                    % (mode, len(got), len(text), got[:200]))
        return ("MISMATCH. You sent %r but the field reads %r (typed via %s). The text did not "
                "go in as sent - a likely cause is an IME swallowing the keystrokes. Do not "
                "carry on as if it worked." % (text[:120], (got or "")[:120], mode))
