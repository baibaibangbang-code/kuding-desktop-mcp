"""tools subpackage — registers all MCP tool definitions on a FastMCP instance."""

from windows_mcp.tools import (
    app,
    clipboard,
    display,
    filesystem,
    input,
    multi,
    notification,
    process,
    registry,
    scrape,
    shell,
    snapshot,
    uia_actions,  # PATCH
    ocr_find,     # PATCH
    window_ctl,   # PATCH
    wait_until,   # PATCH
    visual_find,  # PATCH
    type_here,    # PATCH
    read_color,   # PATCH
    sequence,     # PATCH
    watch_change, # PATCH
)


_MODULES = [
    app,
    display,
    shell,
    filesystem,
    snapshot,
    input,
    scrape,
    multi,
    clipboard,
    process,
    notification,
    registry,
    uia_actions,  # PATCH
    ocr_find,     # PATCH
    window_ctl,   # PATCH
    wait_until,   # PATCH
    visual_find,  # PATCH
    type_here,    # PATCH
    read_color,   # PATCH
    sequence,     # PATCH
    watch_change, # PATCH
]


def register_all(mcp, *, get_desktop, get_analytics):
    """Register every tool module on *mcp*.

    *get_desktop* and *get_analytics* are zero-arg callables that return the
    current ``Desktop`` and ``PostHogAnalytics`` instances (resolved lazily so
    that tools can be registered before ``lifespan`` initializes the singletons).
    """
    # PATCH 0.5.5: 短中文走 SendKeys 会被输入法吃掉，改成走粘贴。
    # 装在 Desktop 类上（Type 工具最终调的是 desktop.type），失败不影响其它工具注册。
    try:
        from windows_mcp.desktop.service import Desktop as _Desktop
        from windows_mcp.tools.ime_safe_type import install as _install_ime
        _install_ime(_Desktop)
    except Exception:
        pass

    for mod in _MODULES:
        if mod is input:
            # PATCH 0.5.5: Click / Type / Move / Scroll 多一个可选的 frame=，用来挡过期坐标。
            # 走包装而不是改 input.py，免得为一个参数维护一份上游分叉。
            from windows_mcp.tools.frame_wrap import register_input_with_frame
            register_input_with_frame(
                mcp, input, get_desktop=get_desktop, get_analytics=get_analytics)
            continue
        # PATCH 0.5.7: 读屏类工具也点亮顶部横幅。
        # 动作之间隔一两分钟，那段时间我们在看这台机器的屏幕——对坐在电脑前的人来说
        # 「AI 正在看我的屏幕」和「AI 正在动我的鼠标」都该知道，横幅也才能连续挂着。
        from windows_mcp.tools.frame_wrap import register_with_watch
        register_with_watch(
            mcp, mod, get_desktop=get_desktop, get_analytics=get_analytics)
