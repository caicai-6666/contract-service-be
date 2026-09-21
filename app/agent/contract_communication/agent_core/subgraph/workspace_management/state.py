"""两个外部输入、工具反馈与候选工作区输出，以及仅供节点消费的内部状态。"""
from typing import Literal
from typing_extensions import TypedDict

from app.schema.communication_workspace import WorkspacePayload
from ...tool.workspace import WorkspaceEdit
from .schema import WorkspaceOperation, CompressionRequest


class WorkspaceManagementInput(TypedDict):
    workspace: WorkspacePayload | dict
    operation: WorkspaceOperation | dict


class WorkspaceManagementOutput(TypedDict):
    status: Literal['success', 'error']
    workspace: dict | None
    tool_feedback: str | None
    system_guidence: dict[str, str] | None
    error_feedback: str | None


class WorkspaceManagementState(WorkspaceManagementInput, total=False):
    original: WorkspacePayload
    pending_operation: WorkspaceOperation
    preview: WorkspaceEdit
    projected_tokens: int
    branch: Literal['normal', 'warning', 'full', 'error']
    compression_audit: list[dict]
    compression_request: CompressionRequest
    compressed: WorkspacePayload
    candidate: WorkspacePayload
    final_tokens: int
    error: str | None
    status: Literal['success', 'error']
    tool_feedback: str | None
    system_guidence: dict[str, str] | None
    error_feedback: str | None
