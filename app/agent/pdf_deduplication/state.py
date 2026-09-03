"""PDF 查重工作流的输入、中间状态与最终结果契约。"""

from __future__ import annotations

import math
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self, TypedDict

from app.agent.contract_extraction.state import PreparedPDF
from app.agent.pdf_deduplication.prompt import PDFPageEmbeddingInputVersion


class PDFDeduplicationModel(BaseModel):
    """PDF 查重状态使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PDFPageFusionVector(PDFDeduplicationModel):
    """处理版 PDF 全部页面向量融合后的合同级向量。"""

    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str = Field(min_length=1)
    embedding_input_version: PDFPageEmbeddingInputVersion
    fusion_version: str = Field(min_length=1)
    fusion_method: Literal["weighted_mean_l2_normalized"]
    dimensions: int = Field(gt=0)
    normalized: bool
    source_page_numbers: tuple[int, ...]
    vector: tuple[float, ...]
    elapsed_ms: float = Field(ge=0)

    @field_validator("source_page_numbers")
    @classmethod
    def validate_source_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """融合必须覆盖按物理页码升序排列的全部非重复页面。"""
        if not value:
            raise ValueError("页面融合向量至少需要一个来源页面")
        if any(page_number <= 0 for page_number in value):
            raise ValueError("来源页面必须使用从 1 开始的物理页码")
        if tuple(sorted(set(value))) != value:
            raise ValueError("来源页面必须按升序排列且不能重复")
        return value

    @model_validator(mode="after")
    def validate_vector(self) -> Self:
        """拒绝维度不符、非有限或伪装成成功结果的零向量。"""
        if len(self.vector) != self.dimensions:
            raise ValueError("页面融合向量维度与 dimensions 不一致")
        if not all(math.isfinite(value) for value in self.vector):
            raise ValueError("页面融合向量包含非有限数值")
        if not any(value != 0 for value in self.vector):
            raise ValueError("页面融合向量不能是零向量")
        return self


class PDFDuplicateCandidate(PDFDeduplicationModel):
    """Elasticsearch 页面融合向量召回的一份候选合同。"""

    rank: int = Field(ge=1, le=3)
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name: str = Field(min_length=1)
    file_uri: str = Field(min_length=1)
    page_count: int = Field(gt=0)
    score: float

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: float) -> float:
        """召回分数必须是可以排序和审计的有限数值。"""
        if not math.isfinite(value):
            raise ValueError("候选召回分数必须是有限数值")
        return value


class PDFDuplicateCandidateSet(PDFDeduplicationModel):
    """固定 Top 3 边界内、按相似度排序的候选集合。"""

    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    top_k: Literal[3] = 3
    candidates: tuple[PDFDuplicateCandidate, ...]
    elapsed_ms: float = Field(ge=0)

    @field_validator("candidates")
    @classmethod
    def validate_candidates(
        cls,
        value: tuple[PDFDuplicateCandidate, ...],
    ) -> tuple[PDFDuplicateCandidate, ...]:
        """候选不得超过三份，身份唯一且排名从 1 连续增长。"""
        if len(value) > 3:
            raise ValueError("PDF 查重候选不能超过 3 份")
        document_ids = [candidate.document_id for candidate in value]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("PDF 查重候选 document_id 不能重复")
        ranks = tuple(candidate.rank for candidate in value)
        if ranks != tuple(range(1, len(value) + 1)):
            raise ValueError("PDF 查重候选 rank 必须从 1 连续增长")
        return value


class PDFDuplicateEvidence(PDFDeduplicationModel):
    """支持一份候选是否重复的可核对跨文档页面证据。"""

    uploaded_page_number: int = Field(gt=0)
    candidate_page_number: int = Field(gt=0)
    observation: str = Field(min_length=1)


class PDFCandidateToolFeedback(PDFDeduplicationModel):
    """写回单候选短期上下文的最小工具反馈。"""

    ok: bool
    message: str = Field(min_length=1)


class PDFCandidateToolCallAudit(PDFDeduplicationModel):
    """不包含 PDF 图像的单候选完整工具调用审计。"""

    round_number: int = Field(ge=1)
    call_id: str | None
    name: str = Field(min_length=1)
    raw_arguments: str
    assistant_content: str | None = None
    feedback: PDFCandidateToolFeedback
    elapsed_ms: float = Field(ge=0)
    response_id: str | None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)


class PDFCandidateJudgmentBase(PDFDeduplicationModel):
    """候选三分类关系与失败结果共享的运行信息。"""

    candidate_document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    rank: int = Field(ge=1, le=3)
    model: str | None = Field(default=None, min_length=1)
    prompt_version: str | None = Field(default=None, min_length=1)
    rounds: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    tool_calls: tuple[PDFCandidateToolCallAudit, ...] = ()


class DuplicatePDFCandidate(PDFCandidateJudgmentBase):
    """MLLM 已确认上传 PDF 与候选处理版 PDF 重复。"""

    status: Literal["duplicate"] = "duplicate"
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    evidence: tuple[PDFDuplicateEvidence, ...] = Field(min_length=1)
    reasoning_summary: str = Field(min_length=1)


class ExactDocumentDuplicateCandidate(PDFCandidateJudgmentBase):
    """处理版 PDF 字节哈希一致形成的确定性重复判断。"""

    status: Literal["duplicate"] = "duplicate"
    match_basis: Literal["processed_pdf_sha256"] = "processed_pdf_sha256"
    reasoning_summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_without_inference(self) -> Self:
        """精确身份判断不能伪造模型调用、提示词或页面证据。"""
        if self.model is not None or self.prompt_version is not None:
            raise ValueError("精确哈希重复判断不能携带模型或提示词版本")
        if self.rounds != 0 or self.tool_calls:
            raise ValueError("精确哈希重复判断不能携带模型轮次或工具审计")
        if any(
            value is not None
            for value in (
                self.prompt_tokens,
                self.completion_tokens,
                self.cached_tokens,
            )
        ):
            raise ValueError("精确哈希重复判断不能携带模型 token 用量")
        return self


class SimilarPDFCandidate(PDFCandidateJudgmentBase):
    """MLLM 已确认两份 PDF 相关或高度相似，但应当独立保留。"""

    status: Literal["similar"] = "similar"
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    evidence: tuple[PDFDuplicateEvidence, ...] = Field(min_length=1)
    reasoning_summary: str = Field(min_length=1)


class DifferentPDFCandidate(PDFCandidateJudgmentBase):
    """MLLM 已确认两份 PDF 属于没有实质关联的不同合同。"""

    status: Literal["different"] = "different"
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    evidence: tuple[PDFDuplicateEvidence, ...] = Field(min_length=1)
    reasoning_summary: str = Field(min_length=1)


class FailedPDFCandidateJudgment(PDFCandidateJudgmentBase):
    """候选 PDF 加载、模型请求或协议未形成可靠决定。"""

    status: Literal["failed"] = "failed"
    error: str = Field(min_length=1)


PDFCandidateJudgment: TypeAlias = (
    ExactDocumentDuplicateCandidate
    | DuplicatePDFCandidate
    | SimilarPDFCandidate
    | DifferentPDFCandidate
    | FailedPDFCandidateJudgment
)


class PDFDeduplicationResult(PDFDeduplicationModel):
    """页面融合向量、召回候选和逐候选判定的最终查重结果。"""

    status: Literal["unique", "duplicate", "failed"]
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_fusion_vector: PDFPageFusionVector
    candidate_set: PDFDuplicateCandidateSet
    judgments: tuple[PDFCandidateJudgment, ...]
    duplicate_document_ids: tuple[str, ...]
    elapsed_ms: float = Field(ge=0)
    error: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """最终状态必须与候选、判断覆盖和重复身份保持一致。"""
        if self.page_fusion_vector.document_id != self.document_id:
            raise ValueError("页面融合向量与查重结果 document_id 不一致")
        if self.candidate_set.document_id != self.document_id:
            raise ValueError("候选集合与查重结果 document_id 不一致")

        candidate_ids = tuple(
            candidate.document_id for candidate in self.candidate_set.candidates
        )
        judgment_ids = tuple(
            judgment.candidate_document_id for judgment in self.judgments
        )
        if judgment_ids != candidate_ids:
            raise ValueError("逐候选判断必须按召回顺序完整覆盖候选集合")
        candidate_ranks = tuple(
            candidate.rank for candidate in self.candidate_set.candidates
        )
        judgment_ranks = tuple(judgment.rank for judgment in self.judgments)
        if judgment_ranks != candidate_ranks:
            raise ValueError("逐候选判断 rank 必须与召回候选一致")

        duplicate_ids = tuple(
            judgment.candidate_document_id
            for judgment in self.judgments
            if judgment.status == "duplicate"
        )
        if self.duplicate_document_ids != duplicate_ids:
            raise ValueError("duplicate_document_ids 与逐候选判断不一致")
        has_failed = any(judgment.status == "failed" for judgment in self.judgments)
        if self.status == "duplicate" and not duplicate_ids:
            raise ValueError("duplicate 状态至少需要一个重复候选")
        if self.status == "unique" and (duplicate_ids or has_failed):
            raise ValueError("unique 状态要求全部候选均可靠判定为 similar 或 different")
        if self.status == "failed" and duplicate_ids:
            raise ValueError("已发现重复候选时最终状态必须为 duplicate")
        if self.status == "failed" and not self.error:
            raise ValueError("failed 状态必须提供错误说明")
        if self.status != "failed" and self.error is not None:
            raise ValueError("非 failed 状态不能携带 error")
        return self


class PDFDeduplicationState(TypedDict, total=False):
    """三个查重节点之间传递的私有共享状态。"""

    prepared_pdf: PreparedPDF
    page_fusion_vector: PDFPageFusionVector
    duplicate_candidates: PDFDuplicateCandidateSet
    result: PDFDeduplicationResult


__all__ = [
    "DifferentPDFCandidate",
    "DuplicatePDFCandidate",
    "ExactDocumentDuplicateCandidate",
    "FailedPDFCandidateJudgment",
    "PDFCandidateJudgment",
    "PDFCandidateToolCallAudit",
    "PDFCandidateToolFeedback",
    "PDFDeduplicationResult",
    "PDFDeduplicationState",
    "PDFDuplicateCandidate",
    "PDFDuplicateCandidateSet",
    "PDFDuplicateEvidence",
    "PDFPageFusionVector",
    "SimilarPDFCandidate",
]
