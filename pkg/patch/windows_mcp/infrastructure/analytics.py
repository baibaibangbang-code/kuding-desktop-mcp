"""Local-only diagnostics for Kuding Desktop MCP.

Replaces the upstream 0.8.5 PostHog sender without changing the
async/thread scheduling interface used by the tool modules.
No telemetry client, persistent anonymous identifier, or network
transport is created here. Error text is logged locally.
UIA_DESKTOP_CALL_LOG optionally selects a local log file.
"""

from typing import Dict, Any, TypeVar, Callable, Protocol, Awaitable
from windows_mcp.infrastructure.config import CONFIG_DIR
from fastmcp import Context
from functools import wraps
import asyncio
import inspect
import logging
import os
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("[%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# 可选的本地落盘：每次工具调用一行，纯本地，不发任何地方。
_CALL_LOG = os.getenv("UIA_DESKTOP_CALL_LOG", "").strip()
if _CALL_LOG:
    try:
        _fh = logging.FileHandler(_CALL_LOG, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(_fh)
    except Exception:
        pass

T = TypeVar("T")


class Analytics(Protocol):
    """保持和上游一样的形状，这样类型注解和 import 都不用改。"""

    async def track_tool(self, tool_name: str, result: Dict[str, Any]) -> None:
        ...

    async def track_error(self, error: Exception, context: Dict[str, Any]) -> None:
        ...

    async def is_feature_enabled(self, feature: str) -> bool:
        ...

    async def close(self) -> None:
        ...


class PostHogAnalytics:
    """名字保留（`__main__` 和 `infrastructure/__init__` 都按这个名字导入），
    但**不再是 PostHog**：不建客户端、不联网、不落匿名编号文件。

    名字不改是为了让上游那几处 import 原样可用；行为改了，
    所以这段注释必须在——**同名不同行为最容易骗到以后读代码的人**。
    """

    USER_DATA_DIR = CONFIG_DIR

    def __init__(self) -> None:
        self.client = None

    @property
    def user_id(self) -> str:
        # 不生成、不持久化任何标识。上游会把 uuid 写进
        # ~/.windows-mcp/.windows-mcp-user-id 作为跨会话的固定身份。
        return "local"

    async def track_tool(self, tool_name: str, result: Dict[str, Any]) -> None:
        duration = result.get("duration_ms", 0)
        mark = "SUCCESS" if result.get("success") else "FAILED"
        logger.info(f"{tool_name}: {mark} ({duration}ms)")

    async def track_error(self, error: Exception, context: Dict[str, Any]) -> None:
        logger.error(f"ERROR in {context.get('tool_name')}: {error}")

    async def is_feature_enabled(self, feature: str) -> bool:
        return False

    async def close(self) -> None:
        return None


def with_analytics(analytics_instance: "Analytics | None", tool_name: str):
    """包一层：计时 + 本地记一行。

    **`asyncio.to_thread` 那一段必须原样保留**——同步的工具函数要靠它
    挪出事件循环，去掉的话整个服务会被一次慢调用卡住。
    这个装饰器名义上叫 analytics，实际承担的是调度，删之前先看清楚。
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            start = time.time()
            try:
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    # 同步函数丢进线程，别堵住事件循环
                    result = await asyncio.to_thread(func, *args, **kwargs)

                duration_ms = int((time.time() - start) * 1000)
                if analytics_instance:
                    await analytics_instance.track_tool(
                        tool_name, {"duration_ms": duration_ms, "success": True}
                    )
                return result
            except Exception as error:
                duration_ms = int((time.time() - start) * 1000)
                if analytics_instance:
                    await analytics_instance.track_error(
                        error, {"tool_name": tool_name, "duration_ms": duration_ms}
                    )
                raise error

        return wrapper

    return decorator
