"""单 worker 事件循环内共享的模型请求配额，不按合同或客户端拆分。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

ModelRequestKind = Literal["mllm", "embedding", "deepseek"]
_LOOP_LIMITS_ATTRIBUTE = "_contract_service_model_request_limits"


@dataclass(frozen=True, slots=True)
class _RequestLimit:
    maximum: int
    semaphore: asyncio.BoundedSemaphore


def get_model_request_limiter(
    kind: ModelRequestKind, maximum: int,
) -> asyncio.BoundedSemaphore:
    """取得当前应用事件循环的共享额度，调用方用 async with 自动归还。

    应用限定单进程、单事件循环；不同模型名、URL 和客户端实例也共享
    同类配额。配额由事件循环持有，避免全局字典通过信号量反向引用旧
    事件循环，导致测试或重复启动留下失效等待者。关闭单个客户端不重置额度。
    """
    if kind not in {"mllm", "embedding", "deepseek"}:
        raise ValueError("未知模型请求类别")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("模型全局并发额度必须是正整数")
    loop = asyncio.get_running_loop()
    limits: dict[ModelRequestKind, _RequestLimit] | None = getattr(
        loop, _LOOP_LIMITS_ATTRIBUTE, None,
    )
    if limits is None:
        limits = {}
        setattr(loop, _LOOP_LIMITS_ATTRIBUTE, limits)
    limit = limits.get(kind)
    if limit is None:
        limit = _RequestLimit(maximum, asyncio.BoundedSemaphore(maximum))
        limits[kind] = limit
    elif limit.maximum != maximum:
        # 禁止因配置漂移创建第二个配额池，或在请求尚未完成时重置计数。
        raise RuntimeError(
            f"{kind} 全局并发配置不一致：已初始化为 {limit.maximum}，收到 {maximum}；"
            "更改额度需要重新启动应用"
        )
    return limit.semaphore
