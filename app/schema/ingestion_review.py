"""待外部审核的不可变合同快照及持久化记录，不包含 Agent 运行轨迹。"""
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID
import math

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from app.service.contract_extraction.model import (
    ClauseDraftData, ContractClassificationView, CoreDraftData,
)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
VectorValue = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class ReviewStatus(StrEnum):
    PENDING_SEND = 'pending_send'
    PENDING_REVIEW = 'pending_review'
    APPROVED = 'approved'
    REJECTED = 'rejected'


class ReviewIngestionStatus(StrEnum):
    PENDING = 'pending'
    INGESTING = 'ingesting'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'


class IngestionReviewSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    run_id: NonEmptyText
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    file_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
    submitted_by: NonEmptyText
    page_count: int = Field(gt=0, strict=True)
    classification: ContractClassificationView
    category_reasoning: dict[str, str]
    core: CoreDraftData
    clauses: ClauseDraftData
    retrieval_questions: tuple[NonEmptyText, ...] = Field(min_length=1)
    embedding_model: NonEmptyText
    vector_dimensions: int = Field(gt=0, strict=True)
    question_fusion_vector: tuple[VectorValue, ...]
    page_fusion_vector: tuple[VectorValue, ...]

    @model_validator(mode='after')
    def complete_snapshot(self):
        for vector in (self.question_fusion_vector, self.page_fusion_vector):
            if len(vector) != self.vector_dimensions or math.hypot(*vector) == 0:
                raise ValueError('融合向量必须维度匹配、非零且数值有限')
        if not self.clauses.root:
            raise ValueError('待审合同条款不能为空')
        if any(c.end_page > self.page_count for c in self.clauses.root):
            raise ValueError('条款页码超出待审 PDF 页数')
        return self


class IngestionReviewRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    submission_id: UUID
    snapshot: IngestionReviewSnapshot
    note: str = Field(default="", max_length=10000, description="入库员审核沟通备注，仅属于待审申请，不进入正式合同快照。")
    message_id: str | None = Field(default=None, description="中间件成功接收后返回的消息 ID，发送前为空。")
    review_message_id: str | None = None
    review_offset: int | None = None
    review_note: str | None = Field(default=None, description="审核平台给出的备注，与入库员 note 分开保存。")
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    passport: str | None = None
    review_processed_at: datetime | None = None
    review_acked_at: datetime | None = None
    last_review_error: str | None = None
    delivery_status: Literal['pending', 'publishing', 'published', 'uncertain', 'blocked'] = 'pending'
    publish_attempts: int = 0
    next_publish_at: float | None = None
    last_publish_error: str | None = None
    pdf_relative_path: str = Field(description="相对待审 files 目录的 PDF 文件名，不包含 files/ 前缀。")
    review_status: ReviewStatus
    ingestion_status: ReviewIngestionStatus
    created_at: datetime
    updated_at: datetime


class ProcessedReviewReceipt(BaseModel):
    """清理快照后的最小幂等回执，不包含合同、提交人或审核备注。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    submission_id: UUID
    review_message_id: str
    review_processed_at: datetime


class IngestionReviewListResponse(BaseModel):
    submission_ids: list[UUID]


class IngestionReviewDeliveryView(BaseModel):
    status: Literal['pending', 'publishing', 'published', 'uncertain', 'blocked']
    message_id: str | None


class IngestionReviewFeedbackView(BaseModel):
    status: ReviewStatus
    note: str | None
    reviewer: str | None
    reviewed_at: datetime | None
    passport: str | None


class IngestionReviewIngestionView(BaseModel):
    status: ReviewIngestionStatus
    ingested_at: datetime | None = Field(default=None, description='正式合同元数据中的入库时间；未成功入库或正式记录已不存在时为空。')


class IngestionReviewCleanupView(BaseModel):
    review_processed_at: datetime | None
    eligible_at: datetime | None = Field(default=None, description='允许自动清理的时刻，未完成处理时为空。')


class IngestionReviewPDFView(BaseModel):
    relative_path: str
    url: str = Field(description='需登录认证的待审PDF预览接口地址。')


class IngestionReviewDetailResponse(BaseModel):
    """稳定的分区响应，各阶段未产生的可选值明确返回 null。"""
    submission_id: UUID
    run_id: str
    document_id: str
    file_name: str
    summary: str
    page_count: int
    submitted_by: str
    note: str
    created_at: datetime
    updated_at: datetime
    delivery: IngestionReviewDeliveryView
    review: IngestionReviewFeedbackView
    ingestion: IngestionReviewIngestionView
    cleanup: IngestionReviewCleanupView
    pdf: IngestionReviewPDFView
    server_time: datetime
