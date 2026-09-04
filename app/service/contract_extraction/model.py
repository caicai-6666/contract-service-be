"""合同处理运行状态、Core/Clause 结果与 SSE 事件的稳定应用契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


class RunStatus(StrEnum):
    """一次内存合同处理任务的用户可见状态。"""

    PROCESSING = "processing"
    NOT_A_CONTRACT = "not_a_contract"
    AWAITING_DEDUPLICATION_REVIEW = "awaiting_deduplication_review"
    PARTIAL_READY = "partial_ready"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INGESTED = "ingested"


class RunListStatus(StrEnum):
    """运行列表用于区分自动推进与等待人工处理的粗粒度状态。"""

    PROCESSING = "processing"
    BLOCKED = "blocked"


class StageCode(StrEnum):
    """不暴露内部节点名称的用户业务阶段。"""

    CONTRACT_DOCUMENT_DETECTION = "contract_document_detection"
    PDF_DEDUPLICATION = "pdf_deduplication"
    CONTRACT_STRUCTURE_RECOGNITION = "contract_structure_recognition"
    CONTRACT_CLASSIFICATION = "contract_classification"
    FILE_NAME_GENERATION = "file_name_generation"
    CORE_EXTRACTION = "core_extraction"
    CLAUSE_EXTRACTION = "clause_extraction"
    RETRIEVAL_PREPARATION = "retrieval_preparation"


class StageStatus(StrEnum):
    """单个用户业务阶段的有限状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"


class DraftSectionCode(StrEnum):
    """提取接口可以独立返回的用户审核分区。"""

    CORE = "core"
    CLAUSE = "clause"


class EventType(StrEnum):
    """SSE 使用的稳定事件名称。"""

    RUN_STARTED = "run.started"
    STAGE_STARTED = "stage.started"
    STAGE_PROGRESS = "stage.progress"
    STAGE_COMPLETED = "stage.completed"
    STAGE_FAILED = "stage.failed"
    STAGE_RETRYING = "stage.retrying"
    RUN_DOCUMENT_REJECTED = "run.document_rejected"
    RUN_DEDUPLICATION_REVIEW_REQUIRED = (
        "run.deduplication_review_required"
    )
    RUN_CONTINUED = "run.continued"
    DRAFT_UPDATED = "draft.updated"
    RUN_REVIEW_READY = "run.review_ready"
    RUN_CANCELLED = "run.cancelled"
    RUN_EXPIRED = "run.expired"
    RUN_INGESTED = "run.ingested"


class ResultStatus(StrEnum):
    """已经提交的阶段结果完整程度。"""

    COMPLETED = "completed"
    PARTIAL = "partial"


class ContractExtractionViewModel(BaseModel):
    """运行快照和事件共用的严格不可变基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StageProgress(ContractExtractionViewModel):
    """仅在总量真实可知时提供的离散进度。"""

    completed: int = Field(ge=0)
    total: int = Field(gt=0)


class StageSnapshot(ContractExtractionViewModel):
    """一个面向非技术用户的阶段状态。"""

    code: StageCode
    name: str
    status: StageStatus
    message: str
    attempt: int = Field(ge=0)
    retryable: bool
    progress: StageProgress | None = None
    result_status: ResultStatus | None = None
    result_revision: int | None = Field(default=None, ge=1)
    started_at: datetime | None = None
    updated_at: datetime


class ContractDocumentEvidenceView(ContractExtractionViewModel):
    """前端可以回到上传 PDF 核对的一条文档类型证据。"""

    page_number: int = Field(ge=1)
    observation: str = Field(min_length=1)


class ContractDocumentDetectionView(ContractExtractionViewModel):
    """合同文档识别形成的紧凑二分类结果。"""

    is_contract: bool
    evidence: tuple[ContractDocumentEvidenceView, ...] = Field(min_length=1)
    reasoning_summary: str = Field(min_length=1)


class DeduplicationCandidateView(ContractExtractionViewModel):
    """一份被判定为重复或相似的 Top-3 召回候选。"""

    rank: int = Field(ge=1, le=3)
    cosine_similarity: float = Field(ge=-1, le=1)
    relation: Literal["duplicate", "similar"]
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name: str = Field(
        min_length=1,
        description="Elasticsearch 合同文档中供前端展示的友好文件名。",
    )
    file_uri: str = Field(
        min_length=1,
        description="Elasticsearch 合同文档中的原始文件地址。",
    )
    page_count: int = Field(gt=0)
    reasoning_summary: str = Field(min_length=1)


class DeduplicationReviewView(ContractExtractionViewModel):
    """查重完成后的暂停点结果与固定审核期限。"""

    status: Literal["unique", "duplicate", "failed"]
    candidates: tuple[DeduplicationCandidateView, ...] = Field(max_length=3)
    review_expires_at: datetime
    continued_at: datetime | None = None


class ProcessedPDFMetadataView(ContractExtractionViewModel):
    """任务持有的处理版 PDF 基本信息。"""

    file_name: str = Field(min_length=1)
    processed_file_size_bytes: int = Field(ge=1)
    page_count: int = Field(ge=1)
    cover_width_pixels: int = Field(ge=1)
    cover_height_pixels: int = Field(ge=1)


class ProcessingRunSnapshot(ContractExtractionViewModel):
    """一次任务当前状态、公共结果及全部用户业务阶段。"""

    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    document: ProcessedPDFMetadataView
    stages: dict[StageCode, StageSnapshot]
    available_sections: tuple[DraftSectionCode, ...]
    document_detection: ContractDocumentDetectionView | None = None
    deduplication: DeduplicationReviewView | None = None
    classification: ContractClassificationView | None = None
    suggested_file_name: SuggestedFileNameView | None = None


class ContractExtractionRunSummary(ContractExtractionViewModel):
    """一项尚未入库且可由前端恢复名称的内存任务摘要。"""

    run_id: str
    document: ProcessedPDFMetadataView
    suggested_file_name: str | None = Field(default=None, min_length=1)
    status: RunListStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class ContractExtractionRunList(
    RootModel[tuple[ContractExtractionRunSummary, ...]]
):
    """按最近更新时间倒序排列的可恢复内存任务列表。"""


class ContractCategoryView(ContractExtractionViewModel):
    """一个命中类别的稳定身份和当前合同实际场景。"""

    code: str
    name: str
    scenario: str


class ContractClassificationView(ContractExtractionViewModel):
    """供用户理解结果背景的紧凑合同分类。"""

    status: Literal["classified", "unmapped", "partial"]
    categories: tuple[ContractCategoryView, ...]
    unmapped_type_description: str | None = None


class SuggestedFileNameEvidenceView(ContractExtractionViewModel):
    """支持建议名称的一条可由前端回到页面核对的证据。"""

    page_number: int = Field(
        ge=1,
        description="证据所在合同页面从 1 开始的物理页码。",
    )
    content: str = Field(
        min_length=1,
        max_length=300,
        description="直接支持建议名称的简短页面原文。",
    )


class SuggestedFileNameView(ContractExtractionViewModel):
    """前端可采用或修改的证据化建议展示名称。"""

    file_name: str = Field(
        min_length=1,
        max_length=255,
        description="不含扩展名、可由审核用户最终修改的建议展示名称。",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=2000,
        description="页面事实与分类摘要如何支持当前建议名称的简洁理由。",
    )
    evidence: tuple[SuggestedFileNameEvidenceView, ...] = Field(
        min_length=1,
        max_length=10,
        description="按页面阅读顺序排列的命名依据。",
    )


FieldScalar = str | int | float | bool
CoreObjectValue = dict[str, FieldScalar]
CoreStoredValue = FieldScalar | CoreObjectValue | tuple[CoreObjectValue, ...]


class CoreDraftData(RootModel[dict[str, CoreStoredValue | None]]):
    """使用 ES Core code 返回的审核值；未提取字段保留为 null。"""


class ClauseView(ContractExtractionViewModel):
    """与 Elasticsearch clauses 元素一致的条款审核值。"""

    clause_id: str
    order: int = Field(ge=1)
    identifier: str
    title: str | None = None
    path: tuple[str, ...]
    parent_clause_id: str | None = None
    level: int = Field(ge=1)
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_page_order(self) -> Self:
        """条款结束页不得早于起始页。"""
        if self.start_page > self.end_page:
            raise ValueError("条款结束页不能早于起始页")
        return self


class ClauseDraftData(RootModel[tuple[ClauseView, ...]]):
    """与 Elasticsearch clauses 数组一致的条款审核值。"""


class RetrievalQuestionView(ContractExtractionViewModel):
    """进入合同向量融合的一条用户式检索问题。"""

    question_id: str
    order: int = Field(ge=1)
    question: str


class RetrievalViewDraftData(ContractExtractionViewModel):
    """检索问题及合同向量的就绪摘要；不传输高维向量。"""

    questions: tuple[RetrievalQuestionView, ...]
    vector_ready: bool
    vector_dimensions: int = Field(gt=0)
    source_question_count: int = Field(gt=0)


class ContractExtractionDraft(ContractExtractionViewModel):
    """提取接口只返回可供用户复核的 Core 和 Clause 值。"""

    core: CoreDraftData | None = None
    clauses: ClauseDraftData | None = None

    @model_validator(mode="after")
    def validate_available_result(self) -> Self:
        """没有任何用户可见分区时不应构造空提取结果。"""
        if self.core is None and self.clauses is None:
            raise ValueError("提取结果必须至少包含 Core 或 Clause")
        return self


class ContractExtractionSnapshot(ContractExtractionViewModel):
    """查询接口返回的状态、分类、建议名称与 Core/Clause 结果。"""

    run: ProcessingRunSnapshot
    draft: ContractExtractionDraft | None


class ContractExtractionEvent(ContractExtractionViewModel):
    """统一 SSE 事件；业务关口事件可以附带对应紧凑结果。"""

    sequence: int = Field(ge=1)
    run_id: str
    event_type: EventType
    overall_status: RunStatus
    message: str
    stage: StageSnapshot | None = None
    draft_revision: int | None = Field(default=None, ge=1)
    available_sections: tuple[DraftSectionCode, ...]
    document_detection: ContractDocumentDetectionView | None = None
    deduplication: DeduplicationReviewView | None = None
    classification: ContractClassificationView | None = None
    suggested_file_name: SuggestedFileNameView | None = None
    occurred_at: datetime


__all__ = [
    "ClauseDraftData",
    "ClauseView",
    "ContractCategoryView",
    "ContractClassificationView",
    "ContractDocumentDetectionView",
    "ContractDocumentEvidenceView",
    "ContractExtractionDraft",
    "ContractExtractionEvent",
    "ContractExtractionRunList",
    "ContractExtractionRunSummary",
    "ContractExtractionSnapshot",
    "DeduplicationCandidateView",
    "DeduplicationReviewView",
    "CoreDraftData",
    "DraftSectionCode",
    "EventType",
    "ProcessedPDFMetadataView",
    "ProcessingRunSnapshot",
    "ResultStatus",
    "RetrievalQuestionView",
    "RetrievalViewDraftData",
    "RunListStatus",
    "RunStatus",
    "StageCode",
    "StageProgress",
    "StageSnapshot",
    "StageStatus",
    "SuggestedFileNameEvidenceView",
    "SuggestedFileNameView",
]
