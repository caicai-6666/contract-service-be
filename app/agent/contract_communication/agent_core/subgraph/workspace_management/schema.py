"""工作区管理的外部契约与压缩依赖契约；子图不负责持久化。"""
from typing import Literal

from pydantic import Field, model_validator
from app.schema.communication_workspace import WorkspaceObject, WorkspacePayload, WorkspaceText


class WorkspaceOperation(WorkspaceObject):
    name: Literal['workspace_replace', 'workspace_add', 'workspace_delete'] = Field(description='本次真实工作区工具调用名称。')
    arguments: WorkspaceText = Field(description='真实工具调用的原始 JSON 参数；复用工具 Schema 解析，不从普通文本猜测操作。')


class WorkspaceManagementRequest(WorkspaceObject):
    workspace: WorkspacePayload = Field(description='调用时的当前工作区快照内容，子图不会原地修改。')
    operation: WorkspaceOperation = Field(description='本次待执行的单个新增、替换或删除操作。')


class WorkspaceManagementResult(WorkspaceObject):
    status: Literal['success', 'error'] = Field(description='成功表示候选工作区可供外部提交；错误表示本次操作整体不提交。')
    workspace: WorkspacePayload | None = Field(default=None, description='成功时的新工作区；错误时为 null，调用方保留输入工作区。')
    tool_feedback: WorkspaceText | None = Field(default=None, description='成功提示与最终工作区使用量（已用 token、预算、百分比）；调用方提交成功后作为工具结果返回，错误时为 null。')
    system_guidence: dict[str, str] | None = Field(default=None, description='成功后的可选 user 角色系统提示，调用方提交成功后追加到任务轨迹。')
    error_feedback: WorkspaceText | None = Field(default=None, description='错误时返回普通工具调用错误反馈，不作为成功系统提示加入轨迹。')

    @model_validator(mode='after')
    def validate_result(self):
        if self.status == 'success':
            if self.workspace is None or self.tool_feedback is None or self.error_feedback is not None:
                raise ValueError('成功必须包含工作区及工具成功反馈，且不能包含错误反馈')
            if self.system_guidence is not None and (
                self.system_guidence.get('role') != 'user' or not self.system_guidence.get('content', '').strip()
            ):
                raise ValueError('系统提示必须是非空 user 消息')
        elif self.workspace is not None or self.tool_feedback is not None or self.system_guidence is not None or self.error_feedback is None:
            raise ValueError('错误只返回错误反馈，不返回候选工作区或成功系统提示')
        return self


class CompressionRequest(WorkspaceObject):
    workspace: WorkspacePayload = Field(description='修改前的工作区，压缩助手仅生成候选副本。')
    operation: WorkspaceOperation = Field(description='压缩后仍需应用的原操作，不允许压缩助手擅自执行或改写。')
    protected_values: dict[str, object] = Field(description='必须逐值保留的路径与原值，覆盖修改目标及本次操作所需的信息引用。')
    reserved_path: str | None = Field(description='预演普通新增时已经分配的实际路径；不得被压缩结果占用。')
    token_budget: int = Field(gt=0, description='工作区硬容量预算，修改后的最终候选必须严格低于此值。')
    target_tokens: int = Field(ge=0, description='压缩副本本身的 token 上限，按严格低于预算 70% 计算，不包含待执行的原操作；保护约束与容量要求都须满足。')
