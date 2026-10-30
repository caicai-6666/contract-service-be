"""按工具名分派；回执只在当前会话执行器生命周期内防止重复副作用。"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping

from ..subgraph.fifo_management.schema import FIFOExecutionResult, FIFOOperation

from .execution import ToolExecution

ToolHandler = Callable[[FIFOOperation], Awaitable[FIFOExecutionResult | ToolExecution]]
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
        self._pending_commits = {}

    async def __call__(self, operation: FIFOOperation) -> FIFOExecutionResult:
        operation = FIFOOperation.model_validate(operation).model_copy(deep=True)
        fingerprint = operation.model_dump_json()
        # 同一会话串行执行，避免并发重复调用越过回执检查。
        async with self._lock:
            previous = self._receipts.get(operation.call_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    return FIFOExecutionResult(status='failed', tool_result={
                        'error': '调用标识已被其他操作使用。','required_action':'新动作使用新的调用ID，不复用其他操作的标识。','execution_state':'not_executed'})
                return previous[1].model_copy(deep=True)
            handler = self._handlers.get(operation.name)
            if handler is None:
                return FIFOExecutionResult(status='failed', tool_result={'error': '该工具未注册，当前不可调用。','required_action':'只选择本次实际提供的工具名称。','execution_state':'not_executed'})
            # 提前记录不确定状态：即使执行期间取消，也不能自动再次实施副作用。
            result = FIFOExecutionResult(status='unknown', tool_result={
                'error': '工具执行状态无法确认。','required_action':'先核实执行结果，不要自动重试同一动作。','execution_state':'unknown'})
            self._receipts[operation.call_id] = (fingerprint, result)
            try:
                execution = await handler(operation)
                result = FIFOExecutionResult.model_validate(execution.result if isinstance(execution, ToolExecution) else execution)
                json.dumps(result.tool_result, ensure_ascii=False, allow_nan=False)
                if isinstance(execution, ToolExecution) and result.status == 'succeeded' and execution.after_commit is not None:
                    self._pending_commits[operation.call_id] = (fingerprint, execution.after_commit)
            except Exception:
                logger.exception('工具执行或回执校验异常')
                result = self._receipts[operation.call_id][1]
            self._receipts[operation.call_id] = (fingerprint, result.model_copy(deep=True))
            return result.model_copy(deep=True)

    async def commit(self, operation: FIFOOperation) -> None:
        """宿主成功记录且管理校验通过后调用；重放不能二次修改本地游标或计数。"""
        async with self._lock:
            pending = self._pending_commits.get(operation.call_id)
            if pending is None:
                return
            if pending[0] != operation.model_dump_json():
                raise ValueError('提交身份与已执行调用不一致')
            self._pending_commits.pop(operation.call_id)
            pending[1]()
