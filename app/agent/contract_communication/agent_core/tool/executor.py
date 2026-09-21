"""按工具名分派；回执只在当前会话执行器生命周期内防止重复副作用。"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping

from ..subgraph.fifo_management.schema import FIFOExecutionResult, FIFOOperation

ToolHandler = Callable[[FIFOOperation], Awaitable[FIFOExecutionResult]]
logger = logging.getLogger(__name__)


class ToolExecutor:
    """每个会话单独创建并复用；注册表在装配时固定，不接受模型修改。"""

    def __init__(self, handlers: Mapping[str, ToolHandler]):
        if any(not isinstance(name, str) or not name.strip() or not callable(handler)
               for name, handler in handlers.items()):
            raise ValueError('工具注册项必须具有非空名称和可调用处理器')
        self._handlers = dict(handlers)
        self._receipts: dict[str, tuple[str, FIFOExecutionResult]] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, operation: FIFOOperation) -> FIFOExecutionResult:
        operation = FIFOOperation.model_validate(operation).model_copy(deep=True)
        fingerprint = operation.model_dump_json()
        # 同一会话串行执行，避免并发重复调用越过回执检查。
        async with self._lock:
            previous = self._receipts.get(operation.call_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    return FIFOExecutionResult(status='failed', tool_result={
                        'error': '调用标识已被其他操作使用，请核实调用轨迹。'})
                return previous[1].model_copy(deep=True)
            handler = self._handlers.get(operation.name)
            if handler is None:
                return FIFOExecutionResult(status='failed', tool_result={'error': '未知或未注册的工具。'})
            # 提前记录不确定状态：即使执行期间取消，也不能自动再次实施副作用。
            result = FIFOExecutionResult(status='unknown', tool_result={
                'error': '工具执行状态无法确认，请先核实，不要重复执行。'})
            self._receipts[operation.call_id] = (fingerprint, result)
            try:
                result = FIFOExecutionResult.model_validate(await handler(operation))
                json.dumps(result.tool_result, ensure_ascii=False, allow_nan=False)
            except Exception:
                logger.exception('工具执行或回执校验异常')
                result = self._receipts[operation.call_id][1]
            self._receipts[operation.call_id] = (fingerprint, result.model_copy(deep=True))
            return result.model_copy(deep=True)
