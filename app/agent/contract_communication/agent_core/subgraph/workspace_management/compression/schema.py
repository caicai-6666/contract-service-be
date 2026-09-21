"""自动压缩子 Agent 只返回候选；操作应用及最终容量验收由父图负责。"""
from typing import Literal
from pydantic import Field, model_validator

from app.schema.communication_workspace import WorkspaceObject, WorkspacePayload, WorkspaceText


class CompressionResult(WorkspaceObject):
    private_audit: list[dict] = Field(default_factory=list, exclude=True, description='私有追加审计，不进入序列化结果、模型消息或工作区。')
    status: Literal['success', 'error'] = Field(description='success 仅表示压缩候选通过结构和保护校验，不表示工作区已提交。')
    workspace: WorkspacePayload | None = Field(default=None, description='成功时的压缩候选，不包含待执行操作；错误时为空。')
    error_feedback: WorkspaceText | None = Field(default=None, description='错误时的最小反馈，成功时为空。')

    @model_validator(mode='after')
    def validate_result(self):
        if self.status == 'success':
            if self.workspace is None or self.error_feedback is not None:
                raise ValueError('成功必须包含候选且不能包含错误反馈')
        elif self.workspace is not None or self.error_feedback is None:
            raise ValueError('错误只能包含错误反馈')
        return self
