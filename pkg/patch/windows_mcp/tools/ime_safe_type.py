"""PATCH 0.5.5: 打字避开输入法。

来历（9-14 看千问办公的 computer-use 技能文档）：它把「type_text 自动关闭并恢复输入法」
写成了一条产品特性。我们没有这一层，而**我们的目标全是中文应用**。

上游 `Desktop.type` 的路由是：纯文本且长度 >= 20 走剪贴板粘贴，否则走 `SendKeys` 逐键发扫描码。
扫描码这条路上有输入法：中文输入法处于中文态时，发出去的键会被输入法吃掉变成候选词，
结果是框里出现拼音或什么都没有，而工具照样回报「已输入」。短中文文案正好落在这条路上
（「朋友圈测试」才 5 个字，远不到 20）。

两条改法，都很小：
1. **含非 ASCII 的纯文本一律走粘贴**，不看长度。粘贴完全绕开输入法，这是最稳的一条路。
2. 剩下还得走 SendKeys 的（带 {Enter}/{Tab} 这类转义的），临时把输入法切到英文态，打完恢复。

刻意保留上游对**短 ASCII** 的 SendKeys 行为：终端里 Ctrl+V 未必是粘贴（常是 Ctrl+Shift+V），
无差别改成粘贴会在终端上造成新的坏情况。只在真正有输入法风险的那一类上改路由。
"""

import ctypes
from contextlib import contextmanager

IME_CMODE_ALPHANUMERIC = 0
# 换行/制表符仍然交给 SendKeys（调用方可能要的就是"按回车"）；
# 花括号单独处理，见 route()
_CTRL_CHARS = ("\n", "\t")


def escape_braces(text):
    """给 SendKeys 用的字面量转义：{ -> {{}，} -> {}}。

    兜底用，接在 typed() 里真正走 SendKeys 的那个边界上。正常路径下带花括号的文本
    已经被 route() 送去粘贴，所以它多数时候是空操作——护栏就该长这样。
    （9-16 审代码时发现它写好了却没接，等于不存在；已接上。）
    """
    # 必须一趟扫完。先 replace("{","{{}") 再 replace("}","{}}") 会把
    # 第一趟自己插进去的 } 又转义一遍：{F4} 会变成 {{{}}F4{}}。
    out = []
    for c in text:
        out.append("{{}" if c == "{" else "{}}" if c == "}" else c)
    return "".join(out)


def _has_control_chars(text):
    return any(c in text for c in _CTRL_CHARS)


def _has_non_ascii(text):
    return any(ord(c) > 127 for c in text)


def _has_braces(text):
    return "{" in text or "}" in text


def route(text):
    """这段文字该走哪条路。抽成纯函数是为了能不碰桌面就测。

    返回 "paste" / "sendkeys-ime-off" / "sendkeys"。
    """
    if _has_braces(text):
        # 花括号一律走粘贴，**按字面量打进去**。
        #
        # 9-15 撞到的：给 opencode 发的任务里有一段 {"do":"front"}，
        # TypeHere 崩在 uia 的 SendKeys 里——ord() expected a character,
        # but string of length 12 found。SendKeys 把 {...} 当成键名解析。
        #
        # 崩了还算轻的。真正危险的是**没崩的那种**：文本里要是有 {F4}、{Del}、
        # {Enter}，SendKeys 会**真的去按那些键**。Type/TypeHere 的职责是
        # "把这段文字打进去"，按键有 Shortcut 那个专门的工具；
        # 两个工具的说明里都没承诺过花括号是转义序列，所以这里没有兼容包袱。
        # 让别处来的内容（JSON、代码、用户文案）能变成键盘动作，
        # 本质上就是一条注入路径。
        return "paste"
    if _has_control_chars(text):
        # 换行和制表符同样按字面量走粘贴，理由和花括号一模一样。
        #
        # 9-15 Codex 用 TypeHere 往剪映片头里打多行标题时逮到的：
        # verify=true 返回 MISMATCH，**正文里的换行被 SendKeys 吞掉了**，
        # 走的是 sendkeys-ime-off 这条路。丢字已经够糟，
        # 而没被吞掉的那种更糟——SendKeys 会把换行变成真的回车，
        # 在微信聊天框里就是**把没写完的消息直接发出去**。
        #
        # 我同一天早上修花括号时特意把换行留给了 SendKeys，
        # 理由写的是"调用方可能就是要按回车"。那个理由站不住：
        # Type/TypeHere 的职责是"把这段文字打进去"，要按回车有 Shortcut
        # 那个专门的工具；翻遍两个工具的说明和 SKILL.md，
        # 都没有承诺过换行等于回车，所以这里没有兼容包袱。
        return "paste"
    if _has_non_ascii(text):
        return "paste"
    return "sendkeys"


@contextmanager
def ime_off():
    """把前台窗口的输入法临时切到英文态，退出时恢复。

    任何一步失败都直接放行——输入法守卫失败不该把打字这件事本身挡住。
    """
    user32, imm32 = ctypes.windll.user32, ctypes.windll.imm32
    himc = hwnd = None
    saved = None
    try:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            himc = imm32.ImmGetContext(hwnd)
            if himc:
                conv, sent = ctypes.c_uint32(), ctypes.c_uint32()
                if imm32.ImmGetConversionStatus(himc, ctypes.byref(conv), ctypes.byref(sent)):
                    saved = (conv.value, sent.value)
                    imm32.ImmSetConversionStatus(himc, IME_CMODE_ALPHANUMERIC, sent.value)
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            if himc and saved is not None:
                imm32.ImmSetConversionStatus(himc, saved[0], saved[1])
            if himc and hwnd:
                imm32.ImmReleaseContext(hwnd, himc)
        except Exception:
            pass


def install(desktop_cls):
    """给 Desktop.type 套一层路由。重复调用安全（装过就不再装）。"""
    if getattr(desktop_cls.type, "_ime_safe", False):
        return False
    original = desktop_cls.type

    def typed(self, loc, text, *a, **kw):
        mode = route(text or "")
        if mode == "paste":
            # 逼上游走它自己的粘贴分支：把阈值降到 0。
            # 用实例属性而不是类属性，免得两个调用叠在一起互相踩。
            self._LONG_TEXT_PASTE_THRESHOLD = 0
            try:
                return original(self, loc, text, *a, **kw)
            finally:
                try:
                    del self._LONG_TEXT_PASTE_THRESHOLD
                except AttributeError:
                    pass
        # 到这里就是真的要走 SendKeys 了。route() 保证带花括号的文本已经被送去粘贴，
        # 所以下面这层转义**正常情况下是个空操作**——接上它是因为 escape_braces 原本
        # 谁都没调用：一个"以防万一"的护栏没接在边界上，等于不存在。接在这里，
        # 哪天 route() 改了，文本里的 {Del} 也不会突然变成真的删除键。
        safe = escape_braces(text or "")
        if mode == "sendkeys-ime-off":
            with ime_off():
                return original(self, loc, safe, *a, **kw)
        return original(self, loc, safe, *a, **kw)

    typed._ime_safe = True
    desktop_cls.type = typed
    return True
