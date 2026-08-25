"""预处理子图文档结构节点的私有状态与结果。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
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
    def from_arguments(cls, arguments: SummaryArguments) -> "DocumentScope":
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
    ) -> "DocumentUnit":
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
    call_id: str
    name: str
    raw_arguments: str
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


class DocumentStructureMetadata(DocumentStructureModel):
    """单元发现节点提供给下游的权威文档结构元数据。"""

    document_id: str
    scope: DocumentScope
    units: tuple[DocumentUnit, ...]


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
]
