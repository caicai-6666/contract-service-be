"""合同删除待审区的轻量快照与审核记录。"""
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MessageId = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class DeletionReviewStatus(StrEnum):
    PENDING_SEND = 'pending_send'
    PENDING_REVIEW = 'pending_review'
    APPROVED = 'approved'
    REJECTED = 'rejected'


class ReviewDeletionStatus(StrEnum):
    PENDING = 'pending'
    DELETING = 'deleting'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'


class DeletionReviewSnapshot(BaseModel):
    """申请时冻结的合同身份；仅引用正式 PDF，不复制合同正文和向量。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    passport: str | None = None
    file_name: NonEmptyText
    summary: NonEmptyText | None = None
    uploader: NonEmptyText
    requested_by: NonEmptyText = Field(description='本平台发起删除申请的登录用户。')
    file_uri: str = Field(description='正式合同 PDF 的根相对地址，不是删除临时区文件地址。')
    contract_ingested_at: datetime

    @field_validator('passport')
    @classmethod
    def valid_passport(cls, value):
        if value is not None and (not value or value != value.strip()):
            raise ValueError('通行证必须为非空且无首尾空白的标识，历史缺失值使用 null')
        return value

    @field_validator('contract_ingested_at')
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError('合同入库时间必须带时区')
        return value

    @model_validator(mode='after')
    def valid_file_reference(self):
        if self.file_uri != f'/{self.document_id}.pdf':
            raise ValueError('正式文件地址必须与合同 document_id 一致')
        return self


class DeletionReviewFeedback(BaseModel):
    """内部审核结果存储契约；由后续中间件适配器明确转换，不猜测结果。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    review_message_id: MessageId
    approved: bool = Field(strict=True)
    reviewed_by: NonEmptyText
    review_note: str = Field(max_length=10000)
    reviewed_at: datetime

    @field_validator('review_message_id')
    @classmethod
    def valid_message_id(cls, value):
        if value != value.strip():
            raise ValueError('反馈消息 ID 不得包含首尾空白')
        return value

    @field_validator('reviewed_at')
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError('审核时间必须带时区')
        return value


class DeletionReviewRecord(DeletionReviewSnapshot):
    submission_id: UUID
    note: str = Field(default='', max_length=10000, description='删除申请人的审核沟通备注。')
    message_id: str | None = None
    delivery_status: Literal['pending', 'publishing', 'published', 'uncertain', 'blocked'] = 'pending'
    publish_attempts: int = Field(default=0, ge=0)
    next_publish_at: float | None = None
    last_publish_error: str | None = None
    review_status: DeletionReviewStatus
    review_message_id: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None
    deletion_status: ReviewDeletionStatus = ReviewDeletionStatus.PENDING
    deletion_attempts: int = Field(default=0, ge=0)
    next_deletion_at: float | None = None
    last_deletion_error: str | None = None
    deletion_completed_at: datetime | None = None
    review_processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DeletionReviewListResponse(BaseModel):
    submission_ids: list[UUID]


class DeletionReviewSubmissionRequest(BaseModel):
    """删除申请人的备注；申请人身份由登录信息取得。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    note: str = Field(
        strict=True, max_length=10000,
        description='必填的删除申请人审核沟通备注或删除原因，允许空字符串；最多 10000 字符，仅保存至删除审核记录并发送中间件。',
    )


class DeletionReviewSubmissionResponse(BaseModel):
    submission_id: UUID = Field(description='删除审核申请 ID，可用于查询申请详情。')
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='本次申请删除的正式合同 ID。')
    review_status: Literal['pending_send'] = 'pending_send'
    can_delete: Literal[False] = False


class ProcessedDeletionReviewReceipt(BaseModel):
    """清理后的最小处理凭据，不保留合同和人员信息。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    submission_id: UUID
    review_message_id: str
    review_processed_at: datetime


class DeletionReviewCleanupView(BaseModel):
    review_processed_at: datetime | None
    eligible_at: datetime | None


class DeletionReviewDeliveryView(BaseModel):
    status: Literal['pending', 'publishing', 'published', 'uncertain', 'blocked'] = Field(
        description='pending 待发送；publishing 正在发送；published 已确认发布或关联重复申请；uncertain 发布结果不确定，按原 source_id 定时重试；blocked 本地数据或权限需处理。')
    message_id: str | None


class DeletionReviewFeedbackView(BaseModel):
    status: DeletionReviewStatus
    note: str | None
    reviewer: str | None
    reviewed_at: datetime | None


class DeletionReviewExecutionView(BaseModel):
    status: ReviewDeletionStatus
    completed_at: datetime | None


class DeletionReviewPDFView(BaseModel):
    file_uri: str
    url: str | None = Field(description='原合同身份仍匹配且正式 PDF 存在时返回预览 URL；否则为 null。')


class DeletionReviewDetailResponse(BaseModel):
    """删除申请的稳定只读投影；审核结论与实际删除结果分开呈现。"""
    submission_id: UUID
    document_id: str
    passport: str | None
    file_name: str
    summary: str | None
    uploader: str
    requested_by: str
    contract_ingested_at: datetime
    note: str
    created_at: datetime
    updated_at: datetime
    delivery: DeletionReviewDeliveryView
    review: DeletionReviewFeedbackView
    deletion: DeletionReviewExecutionView
    cleanup: DeletionReviewCleanupView
    pdf: DeletionReviewPDFView
    server_time: datetime
