"""执行结果与提交后的本地状态变更；回调不进入模型 Schema 或 FIFO 状态。"""
from collections.abc import Callable
from dataclasses import dataclass
from ..subgraph.fifo_management.schema import FIFOExecutionResult


@dataclass(frozen=True)
class ToolExecution:
    result: FIFOExecutionResult
    after_commit: Callable[[], None] | None = None
