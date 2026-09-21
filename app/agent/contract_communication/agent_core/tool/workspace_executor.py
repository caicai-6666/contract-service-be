"""工作区工具适配：读取快照、运行容量管理、版本提交，最后发布反馈。"""
from collections.abc import Awaitable, Callable

from app.schema.communication_workspace import WorkspaceSnapshot
from ..subgraph.fifo_management.schema import FIFOExecutionResult, FIFOGuidance, FIFOOperation
from ..subgraph.workspace_management.schema import WorkspaceManagementResult

WorkspaceReader = Callable[[], Awaitable[WorkspaceSnapshot]]
WorkspaceCommit = Callable[[dict, int], Awaitable[WorkspaceSnapshot]]


class WorkspaceCommitRejected(Exception):
    """存储适配确认未发生写入时抛出；例如预期版本冲突。"""


class WorkspaceToolHandler:
    def __init__(self, *, graph, read_workspace: WorkspaceReader, commit: WorkspaceCommit):
        self._graph = graph
        self._read_workspace = read_workspace
        self._commit = commit

    async def __call__(self, operation: FIFOOperation) -> FIFOExecutionResult:
        # 必须在 FIFO 执行前压缩结束后读取，避免过早冻结工作区状态。
        snapshot = WorkspaceSnapshot.model_validate(await self._read_workspace()).model_copy(deep=True)
        result = WorkspaceManagementResult.model_validate(await self._graph.ainvoke({
            'workspace': snapshot.payload,
            'operation': {'name': operation.name, 'arguments': operation.arguments},
        }))
        if result.status == 'error':
            return FIFOExecutionResult(status='failed', tool_result={'error': result.error_feedback})
        try:
            accepted = WorkspaceSnapshot.model_validate(await self._commit(
                result.workspace.model_dump(), snapshot.revision))
        except WorkspaceCommitRejected:
            return FIFOExecutionResult(status='failed', tool_result={
                'error': '工作区提交被拒绝，本次修改未生效；请读取最新工作区后重新决策。'})
        # 成功反馈对应计数验收后的同一份内容；异常由外层标为 unknown，禁止重放。
        if accepted.payload != result.workspace or accepted.revision != snapshot.revision + 1:
            raise ValueError('存储回执与提交的工作区或版本不一致')
        return FIFOExecutionResult(status='succeeded', tool_result={
            'status': 'success', 'message': result.tool_feedback, 'revision': accepted.revision,
        }, system_guidence=[FIFOGuidance(source='workspace', message=result.system_guidence)]
            if result.system_guidence else [])
