"""合同提取运行状态、草稿与 SSE 事件的稳定应用契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunStatus(StrEnum):
    """一次内存合同处理任务的用户可见状态。"""

    PROCESSING = "processing"
    PARTIAL_READY = "partial_ready"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class StageCode(StrEnum):
    """不暴露内部节点名称的用户业务阶段。"""

    DOCUMENT_READING = "document_reading"
    DOCUMENT_UNDERSTANDING = "document_understanding"
    CONTRACT_CLASSIFICATION = "contract_classification"
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
    """可以独立生成和替换的合同草稿分区。"""

    CORE = "core"
    CLAUSE = "clause"
    RETRIEVAL_VIEW = "retrieval_view"


class RetryableStageCode(StrEnum):
    """HTTP 接口允许单独重试的三个业务阶段。"""

    CORE_EXTRACTION = StageCode.CORE_EXTRACTION.value
    CLAUSE_EXTRACTION = StageCode.CLAUSE_EXTRACTION.value
    RETRIEVAL_PREPARATION = StageCode.RETRIEVAL_PREPARATION.value


class EventType(StrEnum):
    """SSE 使用的稳定事件名称。"""

    RUN_STARTED = "run.started"
    STAGE_STARTED = "stage.started"
    STAGE_PROGRESS = "stage.progress"
    STAGE_COMPLETED = "stage.completed"
    STAGE_FAILED = "stage.failed"
    STAGE_RETRYING = "stage.retrying"
    DRAFT_UPDATED = "draft.updated"
    RUN_REVIEW_READY = "run.review_ready"
    RUN_EXPIRED = "run.expired"


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
    updated_at: datetime


class ProcessingRunSnapshot(ContractExtractionViewModel):
    """一次任务当前状态及全部用户业务阶段。"""

    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    stages: dict[StageCode, StageSnapshot]
    available_sections: tuple[DraftSectionCode, ...]


class ContractDocumentView(ContractExtractionViewModel):
    """合同草稿引用的稳定源文档信息。"""

    document_id: str
    file_name: str
    file_size_bytes: int = Field(gt=0)
    page_count: int = Field(gt=0)


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


class TextEvidenceView(ContractExtractionViewModel):
    """用户审核字段时可回到原 PDF 核对的一条文本证据。"""

    page_number: int = Field(ge=1)
    content: str


FieldScalar = str | int | float | bool


class CoreFieldObjectView(ContractExtractionViewModel):
    """一个已经通过字段 Schema 校验的扁平对象。"""

    evidence: tuple[TextEvidenceView, ...]
    reasoning: str
    value: dict[str, FieldScalar]


class CoreFieldView(ContractExtractionViewModel):
    """一个 Core 定义在当前合同上的审核结果。"""

    name: str
    cardinality: Literal["single", "multiple"]
    status: Literal["extracted", "abandoned", "failed"]
    property_names: tuple[str, ...]
    objects: tuple[CoreFieldObjectView, ...]
    reasoning: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        """让状态和用户可见负载保持互斥且完整。"""
        if self.status == "extracted" and (
            not self.objects or self.reasoning is not None or self.message is not None
        ):
            raise ValueError("extracted Core 字段只能包含一个或多个提取对象")
        if self.status == "abandoned" and (
            self.objects or not self.reasoning or self.message is not None
        ):
            raise ValueError("abandoned Core 字段只能包含放弃理由")
        if self.status == "failed" and (
            not self.message or self.reasoning is not None
        ):
            raise ValueError("failed Core 字段必须包含用户消息且不能伪装理由")
        return self


class CoreDraftData(ContractExtractionViewModel):
    """面向审核端的完整 Core 分区。"""

    fields: tuple[CoreFieldView, ...]


class ClausePathSegmentView(ContractExtractionViewModel):
    """条款在原合同层级路径中的一个可见结构段。"""

    identifier: str
    title_hint: str | None


class ClauseBoundaryAnchorView(ContractExtractionViewModel):
    """条款边界的一条单页原文锚点。"""

    page_number: int = Field(ge=1)
    anchor: str


class ClauseBoundaryView(ContractExtractionViewModel):
    """条款自身的包含式起止锚点。"""

    start: ClauseBoundaryAnchorView
    end: ClauseBoundaryAnchorView


class ClauseView(ContractExtractionViewModel):
    """一个按合同顺序排列的条款审核结果。"""

    candidate_id: str
    order: int = Field(ge=1)
    identifier: str
    title_hint: str | None
    document_path: tuple[ClausePathSegmentView, ...]
    parent_candidate_id: str | None
    level: int = Field(ge=1)
    evidence: ClauseBoundaryView
    status: Literal["extracted", "failed"]
    reasoning_summary: str | None = None
    content: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        """成功条款只承载正文，失败条款只承载可执行提示。"""
        if self.status == "extracted":
            if not self.reasoning_summary or not self.content or self.message is not None:
                raise ValueError("extracted 条款必须包含理由和正文")
        elif (
            not self.message
            or self.reasoning_summary is not None
            or self.content is not None
        ):
            raise ValueError("failed 条款只能包含用户可见失败消息")
        return self


class ClauseDraftData(ContractExtractionViewModel):
    """面向审核端的完整条款分区。"""

    clauses: tuple[ClauseView, ...]


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


DraftData = TypeVar("DraftData", bound=BaseModel)


class DraftSection(ContractExtractionViewModel, Generic[DraftData]):
    """可独立重试和原子替换的一份草稿结果。"""

    revision: int = Field(ge=1)
    result_status: ResultStatus
    updated_at: datetime
    data: DraftData


class ContractExtractionDraft(ContractExtractionViewModel):
    """至少一个并行分支成功后形成的可审核合同草稿。"""

    revision: int = Field(ge=1)
    document: ContractDocumentView
    classification: ContractClassificationView
    core: DraftSection[CoreDraftData] | None = None
    clause: DraftSection[ClauseDraftData] | None = None
    retrieval_view: DraftSection[RetrievalViewDraftData] | None = None
    updated_at: datetime


class ContractExtractionSnapshot(ContractExtractionViewModel):
    """普通查询接口返回的当前原子快照。"""

    run: ProcessingRunSnapshot
    draft: ContractExtractionDraft | None


class ContractExtractionEvent(ContractExtractionViewModel):
    """统一 SSE 事件；完整结果始终通过快照接口获取。"""

    sequence: int = Field(ge=1)
    run_id: str
    event_type: EventType
    overall_status: RunStatus
    message: str
    stage: StageSnapshot | None = None
    draft_revision: int | None = Field(default=None, ge=1)
    available_sections: tuple[DraftSectionCode, ...]
    occurred_at: datetime


__all__ = [
    "ClauseBoundaryAnchorView",
    "ClauseBoundaryView",
    "ClauseDraftData",
    "ClausePathSegmentView",
    "ClauseView",
    "ContractCategoryView",
    "ContractClassificationView",
    "ContractDocumentView",
    "ContractExtractionDraft",
    "ContractExtractionEvent",
    "ContractExtractionSnapshot",
    "CoreDraftData",
    "CoreFieldObjectView",
    "CoreFieldView",
    "DraftSection",
    "DraftSectionCode",
    "EventType",
    "ProcessingRunSnapshot",
    "ResultStatus",
    "RetrievalQuestionView",
    "RetrievalViewDraftData",
    "RetryableStageCode",
    "RunStatus",
    "StageCode",
    "StageProgress",
    "StageSnapshot",
    "StageStatus",
]
