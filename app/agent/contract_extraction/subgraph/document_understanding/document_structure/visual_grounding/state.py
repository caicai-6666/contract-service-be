"""单元视觉定位节点的私有运行状态与审计结果。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import PDFPromptContext, PreparedPDF
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.state import (
    DocumentStructureMetadata,
)
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.visual_grounding.tool import (
    LocalizationAnchor,
    LocatedBoundingBox,
    VisualGroundingToolFeedback,
)


class VisualGroundingModel(BaseModel):
    """不可变且拒绝额外字段的视觉定位运行模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GroundingToolCallAudit(VisualGroundingModel):
    """一个单元定位会话中的单轮工具调用审计。"""

    round_number: int
    # 协议恢复轮没有服务端 tool_call_id；正常工具调用始终具有该标识。
    call_id: str | None
    name: str
    raw_arguments: str
    # auto 未调用工具时保留受控长度的普通文本，供实验与故障分析定位原因。
    assistant_content: str | None
    feedback: VisualGroundingToolFeedback
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class UnitGroundingSessionResult(VisualGroundingModel):
    """一个单元独立工具循环的完整结果。"""

    unit_id: str
    status: Literal["completed", "failed"]
    page_numbers: tuple[int, ...]
    anchors: tuple[LocalizationAnchor, ...]
    located_boxes: tuple[LocatedBoundingBox, ...]
    finish_reason: str | None
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[GroundingToolCallAudit, ...]
    error: str | None

    @model_validator(mode="after")
    def validate_status(self) -> UnitGroundingSessionResult:
        """完成与失败会话使用互斥的终止信息。"""
        if self.status == "completed" and (
            not self.located_boxes or not self.finish_reason or self.error is not None
        ):
            raise ValueError("completed 会话必须包含定位框和 finish_reason")
        if self.status == "failed" and (
            not self.error or self.finish_reason is not None
        ):
            raise ValueError("failed 会话必须包含 error 且不能包含 finish_reason")
        return self


class UnitVisualGroundingResult(VisualGroundingModel):
    """视觉定位节点对全部并发单元会话的汇总。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    prompt_version: str
    tool_version: str
    units: tuple[UnitGroundingSessionResult, ...]
    elapsed_ms: float


class VisualGroundingState(TypedDict, total=False):
    """视觉定位节点所需输入及其输出状态。"""

    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    document_structure: DocumentStructureMetadata
    unit_grounding: UnitVisualGroundingResult


__all__ = [
    "GroundingToolCallAudit",
    "UnitGroundingSessionResult",
    "UnitVisualGroundingResult",
    "VisualGroundingState",
]
