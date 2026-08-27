"""预处理子图文档结构节点的私有状态与结果。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    PDFPromptContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.tool import (
    DocumentScopeDecision,
    GenerateUnitArguments,
    StructureEvidence,
    SummaryArguments,
    ToolFeedback,
    UnitDecision,
)


class DocumentStructureModel(BaseModel):
    """不可变且拒绝额外字段的结构结果基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentScope(DocumentStructureModel):
    """首轮工具确认的合同整体认识。"""

    evidence: tuple[StructureEvidence, ...]
    reasoning_summary: str
    decision: DocumentScopeDecision

    @classmethod
    def from_arguments(cls, arguments: SummaryArguments) -> DocumentScope:
        """把已校验的 summary 参数转换为稳定结果。"""
        return cls(
            evidence=tuple(arguments.evidence),
            reasoning_summary=arguments.reasoning_summary,
            decision=arguments.decision,
        )


class DocumentUnit(DocumentStructureModel):
    """程序编号后的一个宏观连续内容单元。"""

    unit_id: str
    evidence: tuple[StructureEvidence, ...]
    reasoning_summary: str
    decision: UnitDecision

    @classmethod
    def from_arguments(
        cls,
        unit_id: str,
        arguments: GenerateUnitArguments,
    ) -> DocumentUnit:
        """把已接受的 generate_unit 参数转换为稳定结果。"""
        return cls(
            unit_id=unit_id,
            evidence=tuple(arguments.evidence),
            reasoning_summary=arguments.reasoning_summary,
            decision=arguments.decision,
        )


class ToolCallAudit(DocumentStructureModel):
    """不包含 PDF 图像的单轮工具调用审计信息。"""

    round_number: int
    # 协议恢复轮没有可关联的服务端 tool_call_id；其余正常调用始终非空。
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None
    feedback: ToolFeedback
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class UnitDiscoveryResult(DocumentStructureModel):
    """节点一完成后的合同结构发现结果和运行观测。"""

    status: Literal["completed"] = "completed"
    document_id: str
    model: str
    prompt_version: str
    scope: DocumentScope
    units: tuple[DocumentUnit, ...]
    finish_reason: str
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[ToolCallAudit, ...]


class UnitLocationRegion(DocumentStructureModel):
    """视觉定位节点接受的一个单页单元区域。"""

    anchor_ids: tuple[str, ...]
    page_number: int
    bbox_2d: tuple[int, int, int, int]


class UnitLocation(DocumentStructureModel):
    """一个语义单元的最终视觉定位状态。"""

    unit_id: str
    status: Literal["located", "failed"]
    regions: tuple[UnitLocationRegion, ...]
    error: str | None

    @model_validator(mode="after")
    def validate_status(self) -> UnitLocation:
        """成功结果必须有区域，失败结果不得暴露不完整区域。"""
        if self.status == "located" and (not self.regions or self.error is not None):
            raise ValueError("located 单元必须包含区域且不能包含错误")
        if self.status == "failed" and (self.regions or not self.error):
            raise ValueError("failed 单元必须包含错误且不能暴露不完整区域")
        return self


class DocumentStructureMetadata(DocumentStructureModel):
    """预处理子图提供给下游的权威文档结构与视觉定位元数据。"""

    document_id: str
    scope: DocumentScope
    units: tuple[DocumentUnit, ...]
    unit_locations: tuple[UnitLocation, ...] = ()


class DocumentStructureState(TypedDict, total=False):
    """结构发现节点所需的预处理状态子集。"""

    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    unit_discovery: UnitDiscoveryResult
    document_structure: DocumentStructureMetadata


__all__ = [
    "DocumentScope",
    "DocumentStructureMetadata",
    "DocumentStructureState",
    "DocumentUnit",
    "ToolCallAudit",
    "UnitDiscoveryResult",
    "UnitLocation",
    "UnitLocationRegion",
]
