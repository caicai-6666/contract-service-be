"""FIFO 骨架契约；任务身份由调用方提供，不作为模型工具参数。"""
from app.schema.agent_tool_content import ToolContent, OrdinaryToolContent
from typing import Literal
from pydantic import Field, model_validator
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText


class FIFOTask(WorkspaceObject):
    task_id: WorkspaceText = Field(description='程序分配的任务标识，FIFO 内唯一并按时间排序。')
    status: Literal['completed', 'active', 'interrupted', 'failed'] = Field(description='任务状态；当前活动任务不可驱逐，压缩末项必须 completed。')
    messages: list[dict] = Field(description='已做来源封装的交互；当前任务保留原生工具协议，历史用于摘要输入，包含系统提示但不包含私有审计。')
    rendered_content: WorkspaceText | None = Field(default=None, description='已封闭任务的完整展示文本，用于实际历史块计数；当前任务不得设置。')

    @model_validator(mode='after')
    def validate_rendering(self):
        if self.status == 'active' and self.rendered_content is not None:
            raise ValueError('当前任务必须保留原生消息，不能使用历史渲染块')
        return self


class FIFOOperation(WorkspaceObject):
    call_id: WorkspaceText = Field(description='待执行的唯一调用标识，用于审计和防止重放；不是自动幂等保证。')
    task_id: WorkspaceText = Field(description='本步所属活动任务标识。')
    name: WorkspaceText = Field(description='经过入口协议校验的单个工具名称，包含工作区工具。')
    arguments: WorkspaceText = Field(description='真实工具调用参数 JSON 字符串；由对应工具进一步校验。')


class FIFOManagementRequest(WorkspaceObject):
    fifo: list[FIFOTask] = Field(description='按时间排序的近期任务快照；系统提示也参与容量计数。')
    summary: dict | None = Field(default=None, description='已有结构化累计摘要，独立于 FIFO 预算；具体结构另行定义。')
    operation: FIFOOperation = Field(description='主助手当前待执行的一次工具操作，子 Agent 内部调用不得进入此入口。')

    @model_validator(mode='after')
    def validate_tasks(self):
        ids = [task.task_id for task in self.fifo]
        if len(ids) != len(set(ids)):
            raise ValueError('FIFO task_id 必须唯一')
        active = [task for task in self.fifo if task.status == 'active']
        if len(active) != 1 or active[0].task_id != self.operation.task_id or self.fifo[-1] != active[0]:
            raise ValueError('待执行操作必须属于 FIFO 尾部唯一活动任务')
        return self


class FIFORange(WorkspaceObject):
    start_task_id: WorkspaceText = Field(description='压缩前缀首个任务标识。')
    end_task_id: WorkspaceText = Field(description='压缩前缀最后一个 completed 任务标识。')
    task_ids: list[str] = Field(description='按顺序列出的完整前缀任务标识，禁止跳过中间任务。')
    tokens: int = Field(ge=0, description='选定完整前缀的 token 数，允许超过总用量 70%。')


class FIFOGuidance(WorkspaceObject):
    source: Literal['fifo', 'workspace', 'tool'] = Field(description='提示来源，防止不同容量的反馈互相覆盖。')
    message: dict[str, str] = Field(description='统一构造器生成的 role=user、content 非空的 system-guidence 消息。')

    @model_validator(mode='after')
    def validate_message(self):
        if self.message.get('role') != 'user' or not self.message.get('content', '').strip():
            raise ValueError('系统提示必须是非空 user 消息')
        return self


class FIFOExecutionResult(WorkspaceObject):
    status: Literal['succeeded', 'failed', 'unknown'] = Field(description='执行适配确认的动作状态；成功须已完成实际提交，异常副作用不明时使用 unknown。')
    content: ToolContent = Field(default_factory=OrdinaryToolContent, description='程序消费的内容类别及页面引用；与动作状态独立，旧结果缺省为普通结果。')
    tool_result: dict = Field(description='只包含模型允许读取的工具结果，不包含私有审计或完整内部状态。')
    system_guidence: list[FIFOGuidance] = Field(default_factory=list, description='工具或工作区返回的全部系统提示，顺序保留。')

    @model_validator(mode='after')
    def validate_content_status(self):
        if self.status != 'succeeded' and self.content.type != 'ordinary':
            raise ValueError('失败或不确定结果不能发布页面内容')
        return self


class FIFOManagementResult(WorkspaceObject):
    status: Literal['success', 'error'] = Field(description='是否完成本次管理流程，不代替工具实际执行状态。')
    execution_status: Literal['not_executed', 'succeeded', 'failed', 'unknown'] = Field(description='操作执行事实；执行后容量失败不得改写为未执行或自动重放。')
    result_type: Literal['tool_result', 'tool_error', 'capacity_error', 'not_implemented', 'input_error', 'unsupported_content'] = Field(description='返回类型，供外部选择正常工具反馈或容量恢复处理。')
    content: ToolContent = Field(default_factory=OrdinaryToolContent, description='已验收工具返回的内容类别；不能与管理 result_type 混用。')
    tool_result: dict | None = Field(default=None, description='已有执行结果，即使执行后容量处理失败也必须保留。')
    fifo: list[FIFOTask] = Field(description='已确认的当前 FIFO；失败不能发布未验收的压缩候选。')
    summary: dict | None = Field(default=None, description='已确认累计摘要；压缩失败保留此前版本。')
    system_guidence: list[FIFOGuidance] = Field(default_factory=list, description='按顺序保留所有来源的提示；其实际消息必须计入最终容量。')
    compression_range: FIFORange | None = Field(default=None, description='计数节点确认的完整任务前缀，无法找到安全边界时为 null。')
    fifo_budget_tokens: int | None = Field(default=None, gt=0, description='压缩改变摘要后重算的 FIFO 预算，外部下一次调用应使用该值或重算完整预算；未压缩时为 null。')
    can_continue: bool = Field(description='是否允许下一次主助手模型请求；不能只看工具执行成功。')
    error_feedback: str | None = Field(default=None, description='管理失败的最小反馈，不包含原始异常、认证信息或私有审计。')
