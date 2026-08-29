"""合同提取任务的进程内运行仓库。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.service.contract_extraction.model import (
    ContractClassificationView,
    ContractExtractionEvent,
    DraftSectionCode,
    ResultStatus,
    StageCode,
    StageProgress,
    StageStatus,
)


class RunNotFoundError(KeyError):
    """请求的运行不存在或已经从内存中释放。"""


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """一次运行唯一持有的原始内存 PDF。"""

    file_name: str
    pdf_bytes: bytes
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

    code: DraftSectionCode
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
    file_size_bytes: int
    classification: ContractClassificationView
    updated_at: datetime
    sections: dict[DraftSectionCode, MutableDraftSection] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class RunAggregate:
    """一次处理的状态、结果、事件和并发控制聚合。"""

    run_id: str
    source: SourceDocument
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    stages: dict[StageCode, MutableStage]
    event_buffer_size: int
    draft: MutableDraft | None = None
    prerequisites: Any | None = None
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
    "MemoryRunRegistry",
    "MutableDraft",
    "MutableDraftSection",
    "MutableStage",
    "MutableStageAttempt",
    "RunAggregate",
    "RunNotFoundError",
    "SourceDocument",
]
