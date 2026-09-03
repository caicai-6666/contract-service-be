"""合同提取并行任务的轻量运行期进度通知。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

logger = logging.getLogger(__name__)
_ProgressResult = TypeVar("_ProgressResult")


class ParallelProgressPhase(StrEnum):
    """并行内容处理对应用层公开的三个稳定阶段。"""

    COUNTING = "counting"
    COUNTED = "counted"
    EXTRACTING = "extracting"


@dataclass(frozen=True, slots=True)
class ParallelProgressUpdate:
    """一次真实离散进度更新，不使用模型耗时估算百分比。"""

    phase: ParallelProgressPhase
    completed: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        if self.phase is ParallelProgressPhase.COUNTING:
            if self.completed is not None or self.total is not None:
                raise ValueError("统计中进度不能提前携带数量")
            return
        if self.total is None or self.total <= 0:
            raise ValueError("数量统计完成后 total 必须大于 0")
        if self.completed is None or not 0 <= self.completed <= self.total:
            raise ValueError("completed 必须位于 0 到 total 之间")
        if self.phase is ParallelProgressPhase.COUNTED and self.completed != 0:
            raise ValueError("数量统计完成事件的 completed 必须为 0")
        if self.phase is ParallelProgressPhase.EXTRACTING and self.completed == 0:
            raise ValueError("逐项处理事件的 completed 必须大于 0")

    @classmethod
    def counting(cls) -> ParallelProgressUpdate:
        """构造正在统计数量的更新。"""
        return cls(phase=ParallelProgressPhase.COUNTING)

    @classmethod
    def counted(cls, total: int) -> ParallelProgressUpdate:
        """构造数量统计完成且尚未处理任何项目的更新。"""
        return cls(
            phase=ParallelProgressPhase.COUNTED,
            completed=0,
            total=total,
        )

    @classmethod
    def extracting(
        cls,
        *,
        completed: int,
        total: int,
    ) -> ParallelProgressUpdate:
        """构造一个并行项目完成后的离散更新。"""
        return cls(
            phase=ParallelProgressPhase.EXTRACTING,
            completed=completed,
            total=total,
        )


ParallelProgressCallback = Callable[
    [ParallelProgressUpdate],
    Awaitable[None],
]

_progress_callback: ContextVar[ParallelProgressCallback | None] = ContextVar(
    "contract_extraction_parallel_progress_callback",
    default=None,
)


@contextmanager
def bind_parallel_progress_callback(
    callback: ParallelProgressCallback | None,
) -> Iterator[None]:
    """把回调绑定到当前异步任务，并在执行结束后恢复原上下文。"""
    token = _progress_callback.set(callback)
    try:
        yield
    finally:
        _progress_callback.reset(token)


async def report_parallel_progress(update: ParallelProgressUpdate) -> None:
    """尽力报告观察性进度；SSE 异常不能反向破坏正式提取。"""
    callback = _progress_callback.get()
    if callback is None:
        return
    try:
        await callback(update)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("合同提取并行进度回调失败，已继续正式处理")


class ParallelProgressTracker:
    """为单个并行节点串行化完成计数与回调发布。"""

    def __init__(self, total: int) -> None:
        if total <= 0:
            raise ValueError("并行任务总数必须大于 0")
        self._total = total
        self._completed = 0
        self._lock = asyncio.Lock()

    async def report_counted(self) -> None:
        """发布真实总量，并以 0 / total 初始化离散进度。"""
        await report_parallel_progress(
            ParallelProgressUpdate.counted(self._total)
        )

    async def track(
        self,
        operation: Awaitable[_ProgressResult],
    ) -> _ProgressResult:
        """等待一个项目形成终态后递增计数，并原样返回结果。"""
        result = await operation
        async with self._lock:
            self._completed += 1
            await report_parallel_progress(
                ParallelProgressUpdate.extracting(
                    completed=self._completed,
                    total=self._total,
                )
            )
        return result


__all__ = [
    "ParallelProgressCallback",
    "ParallelProgressPhase",
    "ParallelProgressTracker",
    "ParallelProgressUpdate",
    "bind_parallel_progress_callback",
    "report_parallel_progress",
]
