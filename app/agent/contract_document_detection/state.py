"""合同文档识别工作流的输入、审计与结果契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self, TypedDict

from app.agent.contract_document_detection.tool import (
    ContractDocumentDetectionToolFeedback,
    ContractDocumentEvidence,
)
from app.agent.contract_extraction.state import PreparedPDF


class ContractDocumentDetectionModel(BaseModel):
    """合同文档识别状态使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractDocumentDetectionToolCallAudit(ContractDocumentDetectionModel):
    """不包含 PDF 图像的一轮模型工具调用私有审计。"""

    round_number: int = Field(ge=1)
    call_id: str | None
    name: str = Field(min_length=1)
    raw_arguments: str
    assistant_content: str | None = None
    feedback: ContractDocumentDetectionToolFeedback
    elapsed_ms: float = Field(ge=0)
    response_id: str | None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)


class ContractDocumentDetectionResult(ContractDocumentDetectionModel):
    """合同二分类决定或没有伪结果的技术失败。"""

    status: Literal["contract", "not_contract", "failed"]
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_contract: bool | None = None
    evidence: tuple[ContractDocumentEvidence, ...] = ()
    reasoning_summary: str | None = None
    model: str | None = Field(default=None, min_length=1)
    prompt_version: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    rounds: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    tool_calls: tuple[ContractDocumentDetectionToolCallAudit, ...] = ()
    error: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        """成功决定与技术失败必须使用互斥负载。"""
        if self.status == "failed":
            if (
                self.is_contract is not None
                or self.evidence
                or self.reasoning_summary is not None
                or not self.error
            ):
                raise ValueError("failed 合同识别只能包含技术错误和运行审计")
            return self

        expected = self.status == "contract"
        if (
            self.is_contract is not expected
            or not self.evidence
            or not self.reasoning_summary
            or not self.model
            or self.error is not None
        ):
            raise ValueError("可靠合同识别必须包含一致的决定、证据和推理摘要")
        return self


class ContractDocumentDetectionState(TypedDict, total=False):
    """在单节点图中传递处理版 PDF 与合同识别结果。"""

    prepared_pdf: PreparedPDF
    result: ContractDocumentDetectionResult


__all__ = [
    "ContractDocumentDetectionModel",
    "ContractDocumentDetectionResult",
    "ContractDocumentDetectionState",
    "ContractDocumentDetectionToolCallAudit",
]
