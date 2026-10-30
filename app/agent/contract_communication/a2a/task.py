"""单任务内存宿主；不创建会话、事件运行时或业务数据库记录。"""
from copy import deepcopy
from time import time
from app.schema.communication import ConversationHistoryRecord
from app.schema.communication_workspace import WorkspaceSnapshot
from ..agent_core.context import assemble_agent_core_context
from ..agent_core.native_messages import validate_native_messages
from ..agent_core.tool.workspace_executor import WorkspaceCommitRejected
from ..agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
from .model import A2ATaskRequest


class InMemoryA2ATask:
    def __init__(self, request, *, history=(), authorize=None):
        self.request = A2ATaskRequest.model_validate(request).model_copy(deep=True)
        self.workspace = self.request.workspace.model_copy(deep=True)
        self.history = deepcopy(tuple(history))
        self.authorize = authorize
        self.exchanges = []
        self.status = 'pending'
        self.result = None

    def start(self):
        # 一个宿主只执行一次；重试必须由调用方明确建立新任务，不能重放副作用。
        if self.status != 'pending':
            raise ValueError('任务已经执行或结束，不能重复运行')
        self.status = 'processing'

    def require_active(self):
        if self.status != 'processing':
            raise ValueError('任务未在执行，拒绝迟到写入')

    async def read_context(self, *, include_reasoning):
        self.require_active()
        if self.authorize is not None: await self.authorize()
        record = ConversationHistoryRecord(record_id=self.request.task_id, sequence=len(self.history)+1, kind='task',
            turn_id=self.request.task_id, status='processing', created_at=self.request.created_at,
            payload={'input': {'text': self.request.content}, 'agent_messages': deepcopy(self.exchanges)})
        return assemble_agent_core_context(workspace=self.workspace, summary=None, records=(*self.history,record),
            current_turn_id=self.request.task_id, include_reasoning=include_reasoning)

    async def commit_workspace(self, payload, revision):
        self.require_active()
        if revision != self.workspace.revision:
            raise WorkspaceCommitRejected('工作区版本冲突')
        candidate = WorkspaceSnapshot(payload=payload, revision=revision + 1, updated_at=int(time() * 1000))
        self.workspace = candidate
        return candidate.model_copy(deep=True)

    async def commit_summary(self, previous, summary, scope):
        # 当前内存入口只有一个活动任务，不能伪造已完成历史供摘要驱逐。
        raise ValueError('当前宿主没有可压缩的已完成任务')

    async def record_exchange(self, assistant, result):
        from ..agent_core.context_rendering.page import render_tool_receipt
        self.require_active()
        accepted = FIFOExecutionResult.model_validate(result)
        if accepted.status != 'succeeded':
            raise ValueError('失败动作不能写入任务轨迹')
        calls = assistant.get('tool_calls', [])
        if len(calls) != 1:
            raise ValueError('必须提供单个已接受调用')
        batch = [assistant, {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': render_tool_receipt(accepted)},
                 *({**g.message, 'source': g.source} for g in accepted.system_guidence)]
        self.exchanges = validate_native_messages([*self.exchanges, *batch])

    async def complete(self, result):
        self.require_active()
        if self.authorize is not None: await self.authorize()
        if result.task_id != self.request.task_id:
            raise ValueError('任务结果归属不一致')
        self.result = result
        self.status = 'completed'

    def abort(self, status):
        if self.status == 'processing':
            self.status = status
