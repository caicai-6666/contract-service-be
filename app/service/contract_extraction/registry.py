"""合同提取任务的进程内运行仓库。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.agent.contract_document_detection.state import (
    ContractDocumentDetectionResult,
)
from app.agent.contract_extraction.state import PreparedPDF
from app.agent.pdf_deduplication.state import PDFDeduplicationResult
from app.service.contract_extraction.model import (
    ContractClassificationView,
    ContractExtractionEvent,
    ResultStatus,
    StageCode,
    StageProgress,
    StageStatus,
)


class InternalDraftSectionCode(StrEnum):
    """仅用于内存聚合的分区；检索结果不进入 HTTP 提取结果。"""

    CORE = "core"
    CLAUSE = "clause"
    RETRIEVAL_VIEW = "retrieval_view"


class RunNotFoundError(KeyError):
    """请求的运行不存在或已经从内存中释放。"""


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """一次运行持有的处理版身份；原始上传字节不进入后台工作流。"""

    file_name: str
    document_id: str


@dataclass(slots=True)
class MutableStageAttempt:
    """单阶段一次不可覆盖的执行记录。"""

    attempt: int
    status: StageStatus
    started_at: datetime
    finished_at: datetime | None = None
    result: Any | None = None
    error: str | None = None


@dataclass(slots=True)
class MutableStage:
    """一个用户业务阶段的当前内存状态。"""

    code: StageCode
    name: str
    status: StageStatus
    message: str
    updated_at: datetime
    attempt: int = 0
    retryable: bool = False
    progress: StageProgress | None = None
    result_status: ResultStatus | None = None
    result_revision: int | None = None
    attempts: list[MutableStageAttempt] = field(default_factory=list)


@dataclass(slots=True)
class MutableDraftSection:
    """草稿中当前生效的分区及其完整内部结果。"""

    code: InternalDraftSectionCode
    revision: int
    result_status: ResultStatus
    updated_at: datetime
    data: BaseModel
    internal_result: Any


@dataclass(slots=True)
class MutableDraft:
    """任意业务分支成功后增量形成的可审核草稿。"""

    revision: int
    page_count: int
    processed_file_size_bytes: int
    classification: ContractClassificationView
    updated_at: datetime
    sections: dict[InternalDraftSectionCode, MutableDraftSection] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class RunAggregate:
    """一次处理的状态、结果、事件和并发控制聚合。"""

    run_id: str
    reviewer_user_name: str
    source: SourceDocument
    prepared_pdf: PreparedPDF
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    stages: dict[StageCode, MutableStage]
    event_buffer_size: int
    draft: MutableDraft | None = None
    document_detection_result: ContractDocumentDetectionResult | None = None
    structure_result: Any | None = None
    deduplication_result: PDFDeduplicationResult | None = None
    classification_view: ContractClassificationView | None = None
    prerequisites: Any | None = None
    awaiting_deduplication_review: bool = False
    deduplication_review_expires_at: datetime | None = None
    continued_at: datetime | None = None
    cancelled: bool = False
    expired: bool = False
    next_sequence: int = 1
    events: deque[ContractExtractionEvent] = field(init=False)
    subscribers: set[asyncio.Queue[ContractExtractionEvent | None]] = field(
        default_factory=set
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.events = deque(maxlen=self.event_buffer_size)


class MemoryRunRegistry:
    """以 `run_id` 管理进程内聚合，不执行任何持久化。"""

    def __init__(self) -> None:
        self._runs: dict[str, RunAggregate] = {}
        self._lock = asyncio.Lock()

    async def add(self, aggregate: RunAggregate) -> None:
        """注册新任务，并拒绝极低概率的身份碰撞。"""
        async with self._lock:
            if aggregate.run_id in self._runs:
                raise RuntimeError(f"运行标识重复：{aggregate.run_id}")
            self._runs[aggregate.run_id] = aggregate

    async def get(self, run_id: str) -> RunAggregate:
        """返回仍驻留内存的任务。"""
        async with self._lock:
            try:
                return self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(run_id) from exc

    async def remove(self, run_id: str) -> RunAggregate | None:
        """从注册表移除任务并返回其聚合。"""
        async with self._lock:
            return self._runs.pop(run_id, None)

    async def values(self) -> tuple[RunAggregate, ...]:
        """为 TTL 清理返回当前聚合的稳定快照。"""
        async with self._lock:
            return tuple(self._runs.values())


__all__ = [
    "InternalDraftSectionCode",
    "MemoryRunRegistry",
    "MutableDraft",
    "MutableDraftSection",
    "MutableStage",
    "MutableStageAttempt",
    "RunAggregate",
    "RunNotFoundError",
    "SourceDocument",
]
