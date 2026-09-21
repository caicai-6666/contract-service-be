"""外部请求与内部状态分离；调用者不能注入执行成功或跳过容量检查。"""
from typing_extensions import TypedDict
from app.schema.agent_tool_content import ToolContent
from .schema import FIFOManagementRequest, FIFOManagementResult, FIFORange


class FIFOManagementInput(TypedDict):
    request: FIFOManagementRequest | dict


class FIFOManagementOutput(TypedDict):
    result: FIFOManagementResult


class FIFOManagementState(FIFOManagementInput, total=False):
    validated: FIFOManagementRequest
    candidate: FIFOManagementRequest
    fifo_budget: int
    pending_compression_guidence: list
    tokens: int
    task_tokens: list[int]
    compression_range: FIFORange | None
    count_render_version: str
    branch: str
    post_full: bool
    warning: bool
    execution_status: str
    tool_result: dict | None
    content: ToolContent
    system_guidence: list
    error: str
    result_type: str
    can_continue: bool
    result: FIFOManagementResult
