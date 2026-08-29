"""合同提取的进程内任务、增量草稿与 SSE 事件服务。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.agent.contract_extraction.state import ContractExtractionRequest
from app.service.contract_extraction.executor import (
    ContractExtractionExecutor,
    ExtractionContext,
    PreprocessingOutput,
)
from app.service.contract_extraction.model import (
    ClauseDraftData,
    ContractDocumentView,
    ContractExtractionDraft,
    ContractExtractionEvent,
    ContractExtractionSnapshot,
    CoreDraftData,
    DraftSection,
    DraftSectionCode,
    EventType,
    ProcessingRunSnapshot,
    ResultStatus,
    RetrievalViewDraftData,
    RunStatus,
    StageCode,
    StageProgress,
    StageSnapshot,
    StageStatus,
)
from app.service.contract_extraction.projector import (
    ProjectedSection,
    project_classification,
    project_clause,
    project_core,
    project_retrieval_view,
)
from app.service.contract_extraction.registry import (
    MemoryRunRegistry,
    MutableDraft,
    MutableDraftSection,
    MutableStage,
    MutableStageAttempt,
    RunAggregate,
    SourceDocument,
)

logger = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    """当前运行状态不允许请求的动作。"""


class StageRetryError(RunConflictError):
    """目标阶段不存在、不可重试或已经超过次数限制。"""


_STAGE_ORDER = (
    StageCode.DOCUMENT_READING,
    StageCode.DOCUMENT_UNDERSTANDING,
    StageCode.CONTRACT_CLASSIFICATION,
    StageCode.CORE_EXTRACTION,
    StageCode.CLAUSE_EXTRACTION,
    StageCode.RETRIEVAL_PREPARATION,
)
_BRANCH_STAGES = (
    StageCode.CORE_EXTRACTION,
    StageCode.CLAUSE_EXTRACTION,
    StageCode.RETRIEVAL_PREPARATION,
)
_SECTION_ORDER = (
    DraftSectionCode.CORE,
    DraftSectionCode.CLAUSE,
    DraftSectionCode.RETRIEVAL_VIEW,
)
_STAGE_NAMES = {
    StageCode.DOCUMENT_READING: "读取合同",
    StageCode.DOCUMENT_UNDERSTANDING: "理解文档结构",
    StageCode.CONTRACT_CLASSIFICATION: "识别合同类型",
    StageCode.CORE_EXTRACTION: "提取核心信息",
    StageCode.CLAUSE_EXTRACTION: "提取合同条款",
    StageCode.RETRIEVAL_PREPARATION: "准备智能检索",
}
_PENDING_MESSAGES = {
    StageCode.DOCUMENT_READING: "等待读取上传的 PDF。",
    StageCode.DOCUMENT_UNDERSTANDING: "等待分析合同的页面与内容结构。",
    StageCode.CONTRACT_CLASSIFICATION: "等待识别合同涉及的交易类型。",
    StageCode.CORE_EXTRACTION: "等待提取合同核心信息。",
    StageCode.CLAUSE_EXTRACTION: "等待提取合同条款。",
    StageCode.RETRIEVAL_PREPARATION: "等待生成合同检索信息。",
}
_RUNNING_MESSAGES = {
    StageCode.DOCUMENT_READING: "正在读取并整理合同页面。",
    StageCode.DOCUMENT_UNDERSTANDING: "正在理解合同的内容结构。",
    StageCode.CONTRACT_CLASSIFICATION: "正在识别合同涉及的交易类型。",
    StageCode.CORE_EXTRACTION: "正在提取合同核心信息。",
    StageCode.CLAUSE_EXTRACTION: "正在识别并提取合同条款。",
    StageCode.RETRIEVAL_PREPARATION: "正在生成便于检索合同的信息。",
}
_COMPLETED_MESSAGES = {
    StageCode.DOCUMENT_READING: "合同页面已读取。",
    StageCode.DOCUMENT_UNDERSTANDING: "合同内容结构已识别。",
    StageCode.CONTRACT_CLASSIFICATION: "合同类型已识别。",
    StageCode.CORE_EXTRACTION: "合同核心信息已生成。",
    StageCode.CLAUSE_EXTRACTION: "合同条款已生成。",
    StageCode.RETRIEVAL_PREPARATION: "合同检索信息已准备。",
}
_FAILED_MESSAGES = {
    StageCode.DOCUMENT_READING: "暂时无法读取这份合同，请确认文件后重新上传。",
    StageCode.DOCUMENT_UNDERSTANDING: "暂时无法完成合同结构理解。",
    StageCode.CONTRACT_CLASSIFICATION: "暂时无法识别合同类型。",
    StageCode.CORE_EXTRACTION: "本次未能完成核心信息提取，可以单独重试。",
    StageCode.CLAUSE_EXTRACTION: "本次未能完成合同条款提取，可以单独重试。",
    StageCode.RETRIEVAL_PREPARATION: "本次未能完成检索准备，可以单独重试。",
}
_STAGE_TO_SECTION = {
    StageCode.CORE_EXTRACTION: DraftSectionCode.CORE,
    StageCode.CLAUSE_EXTRACTION: DraftSectionCode.CLAUSE,
    StageCode.RETRIEVAL_PREPARATION: DraftSectionCode.RETRIEVAL_VIEW,
}


class ContractExtractionService:
    """在单进程内协调上传、并行处理、事件订阅和分支重试。"""

    def __init__(
        self,
        *,
        executor: ContractExtractionExecutor,
        run_ttl_seconds: int = 3600,
        cleanup_interval_seconds: int = 30,
        event_buffer_size: int = 256,
        sse_heartbeat_seconds: int = 15,
        max_stage_attempts: int = 3,
        registry: MemoryRunRegistry | None = None,
    ) -> None:
        if run_ttl_seconds <= 0:
            raise ValueError("任务内存保留时间必须大于 0")
        if cleanup_interval_seconds <= 0:
            raise ValueError("任务清理间隔必须大于 0")
        if event_buffer_size <= 0:
            raise ValueError("SSE 事件缓冲区必须大于 0")
        if sse_heartbeat_seconds <= 0:
            raise ValueError("SSE 心跳间隔必须大于 0")
        if max_stage_attempts <= 0:
            raise ValueError("阶段最大尝试次数必须大于 0")
        self._executor = executor
        self._run_ttl = timedelta(seconds=run_ttl_seconds)
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._event_buffer_size = event_buffer_size
        self._sse_heartbeat_seconds = sse_heartbeat_seconds
        self._max_stage_attempts = max_stage_attempts
        self._registry = registry or MemoryRunRegistry()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._run_tasks: dict[str, set[asyncio.Task[None]]] = {}

    @property
    def sse_heartbeat_seconds(self) -> int:
        """返回不进入业务事件序列的连接心跳间隔。"""
        return self._sse_heartbeat_seconds

    async def start(self) -> None:
        """启动轻量 TTL 清理循环；重复调用不会创建多个循环。"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="contract-extraction-memory-cleanup",
            )

    async def close(self) -> None:
        """停止后台任务；进程结束时由内存自然释放 PDF 和草稿。"""
        tasks = [
            task
            for run_tasks in self._run_tasks.values()
            for task in run_tasks
            if not task.done()
        ]
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            tasks.append(self._cleanup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._run_tasks.clear()
        self._cleanup_task = None

    async def create_run(
        self,
        *,
        file_name: str,
        pdf_bytes: bytes,
    ) -> ContractExtractionSnapshot:
        """把一次上传注册为内存任务并立即异步启动处理。"""
        # 正式文件类型、页数、加密与业务规则校验留给后续独立校验阶段；
        # 当前只建立工作流能够安全识别来源所需的最小输入契约。
        request = ContractExtractionRequest(
            file_name=file_name,
            pdf_bytes=pdf_bytes,
        )
        now = _utcnow()
        aggregate = RunAggregate(
            run_id=str(uuid4()),
            source=SourceDocument(
                file_name=request.source_name,
                pdf_bytes=pdf_bytes,
                document_id=hashlib.sha256(pdf_bytes).hexdigest(),
            ),
            created_at=now,
            updated_at=now,
            expires_at=now + self._run_ttl,
            stages={
                code: MutableStage(
                    code=code,
                    name=_STAGE_NAMES[code],
                    status=StageStatus.PENDING,
                    message=_PENDING_MESSAGES[code],
                    updated_at=now,
                )
                for code in _STAGE_ORDER
            },
            event_buffer_size=self._event_buffer_size,
        )
        await self._registry.add(aggregate)
        async with aggregate.lock:
            self._publish_locked(
                aggregate,
                EventType.RUN_STARTED,
                "合同已接收，开始处理。",
            )
            snapshot = self._snapshot_locked(aggregate)
        self._spawn(aggregate.run_id, self._run_pipeline(aggregate, request))
        return snapshot

    async def get_snapshot(self, run_id: str) -> ContractExtractionSnapshot:
        """返回状态与当前草稿在同一把锁下形成的原子快照。"""
        aggregate = await self._registry.get(run_id)
        async with aggregate.lock:
            return self._snapshot_locked(aggregate)

    async def retry_stage(
        self,
        run_id: str,
        stage_code: StageCode,
    ) -> ContractExtractionSnapshot:
        """异步重跑一个业务分支，并在成功前保留旧草稿分区。"""
        if stage_code not in _BRANCH_STAGES:
            raise StageRetryError("只有核心信息、合同条款和检索准备支持单独重试")
        aggregate = await self._registry.get(run_id)
        async with aggregate.lock:
            if aggregate.expired:
                raise StageRetryError("任务已经过期")
            if aggregate.prerequisites is None:
                raise StageRetryError("合同公共处理尚未完成，暂时不能重试该阶段")
            stage = aggregate.stages[stage_code]
            if stage.status in {StageStatus.RUNNING, StageStatus.RETRYING}:
                raise StageRetryError("该阶段正在处理中，请勿重复提交")
            if stage.attempt >= self._max_stage_attempts:
                raise StageRetryError("该阶段已达到最大尝试次数")
            self._begin_stage_locked(aggregate, stage_code, retry=True)
            snapshot = self._snapshot_locked(aggregate)
        self._spawn(
            aggregate.run_id,
            self._execute_branch(aggregate, stage_code, already_started=True),
        )
        return snapshot

    @asynccontextmanager
    async def subscribe_events(
        self,
        run_id: str,
        *,
        after_sequence: int | None = None,
    ) -> AsyncIterator[
        tuple[
            tuple[ContractExtractionEvent, ...],
            asyncio.Queue[ContractExtractionEvent | None],
        ]
    ]:
        """原子注册订阅者并回放断线期间仍在缓冲区中的事件。"""
        aggregate = await self._registry.get(run_id)
        queue: asyncio.Queue[ContractExtractionEvent | None] = asyncio.Queue(
            maxsize=self._event_buffer_size
        )
        async with aggregate.lock:
            threshold = after_sequence or 0
            replay = tuple(
                event for event in aggregate.events if event.sequence > threshold
            )
            aggregate.subscribers.add(queue)
        try:
            yield replay, queue
        finally:
            async with aggregate.lock:
                aggregate.subscribers.discard(queue)

    async def expire_due_runs(self, *, now: datetime | None = None) -> int:
        """释放已到期且没有活跃执行的任务，主要由清理循环和测试调用。"""
        current = now or _utcnow()
        expired_count = 0
        for aggregate in await self._registry.values():
            if aggregate.expires_at > current or self._has_active_task(
                aggregate.run_id
            ):
                continue
            async with aggregate.lock:
                if aggregate.expired or aggregate.expires_at > current:
                    continue
                if any(
                    stage.status in {StageStatus.RUNNING, StageStatus.RETRYING}
                    for stage in aggregate.stages.values()
                ):
                    continue
                aggregate.expired = True
                self._publish_locked(
                    aggregate,
                    EventType.RUN_EXPIRED,
                    "该处理任务已过期，请重新上传合同。",
                    touch=False,
                )
                aggregate.subscribers.clear()
            await self._registry.remove(aggregate.run_id)
            self._run_tasks.pop(aggregate.run_id, None)
            expired_count += 1
        return expired_count

    async def _run_pipeline(
        self,
        aggregate: RunAggregate,
        request: ContractExtractionRequest,
    ) -> None:
        """完成公共前置阶段，再并行且相互隔离地执行三个业务分支。"""
        await self._begin_stage(aggregate, StageCode.DOCUMENT_READING)

        async def on_preprocessing_update(
            node_name: str,
            values: dict[str, Any],
        ) -> None:
            if node_name == "prepare_pdf":
                prepared_pdf = values.get("prepared_pdf")
                page_count = getattr(prepared_pdf, "page_count", None)
                progress = (
                    StageProgress(completed=page_count, total=page_count)
                    if isinstance(page_count, int) and page_count > 0
                    else None
                )
                await self._complete_stage(
                    aggregate,
                    StageCode.DOCUMENT_READING,
                    progress=progress,
                    result=prepared_pdf,
                )
                await self._begin_stage(
                    aggregate,
                    StageCode.DOCUMENT_UNDERSTANDING,
                )
            elif node_name == "discover_document_units":
                await self._report_stage_progress(
                    aggregate,
                    StageCode.DOCUMENT_UNDERSTANDING,
                    "合同内容单元已识别，正在确认页面位置。",
                )

        try:
            preprocessing = await self._executor.preprocess(
                request,
                on_preprocessing_update,
            )
            await self._ensure_preprocessing_stages_completed(
                aggregate,
                preprocessing,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("合同预处理失败，run_id=%s", aggregate.run_id)
            await self._fail_active_prerequisite_stage(aggregate, exc)
            return

        await self._begin_stage(aggregate, StageCode.CONTRACT_CLASSIFICATION)
        try:
            context = await self._executor.classify(preprocessing)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("合同分类失败，run_id=%s", aggregate.run_id)
            await self._fail_stage(
                aggregate,
                StageCode.CONTRACT_CLASSIFICATION,
                exc,
            )
            return

        async with aggregate.lock:
            aggregate.prerequisites = context
            self._complete_stage_locked(
                aggregate,
                StageCode.CONTRACT_CLASSIFICATION,
                result={
                    "classification": context.classification,
                    "classification_audit": context.classification_audit,
                },
            )

        await asyncio.gather(
            *(
                self._execute_branch(aggregate, stage_code)
                for stage_code in _BRANCH_STAGES
            )
        )

    async def _execute_branch(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        *,
        already_started: bool = False,
    ) -> None:
        """执行一个分支；任何异常均只改变当前阶段并保留其他分支结果。"""
        if not already_started:
            await self._begin_stage(aggregate, stage_code)
        context = aggregate.prerequisites
        if not isinstance(context, ExtractionContext):
            await self._fail_stage(
                aggregate,
                stage_code,
                RuntimeError("缺少合同公共处理结果"),
            )
            return

        try:
            if stage_code is StageCode.CORE_EXTRACTION:
                internal_result = await self._executor.extract_core(context)
                projected = project_core(internal_result)
            elif stage_code is StageCode.CLAUSE_EXTRACTION:
                internal_result = await self._executor.extract_clause(context)
                projected = project_clause(internal_result)
            else:
                internal_result = await self._executor.prepare_retrieval_view(context)
                projected = project_retrieval_view(internal_result)
            await self._commit_branch_result(
                aggregate,
                stage_code,
                projected,
                internal_result,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "合同业务分支失败，run_id=%s stage=%s",
                aggregate.run_id,
                stage_code.value,
            )
            await self._fail_stage(aggregate, stage_code, exc)
            return

    async def _commit_branch_result(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        projected: ProjectedSection,
        internal_result: Any,
    ) -> None:
        """在一把锁中替换分区、推进修订号并发布对应事件。"""
        async with aggregate.lock:
            context = aggregate.prerequisites
            if not isinstance(context, ExtractionContext):
                raise TypeError("提交分支结果时缺少合同公共处理结果")
            section_code = _STAGE_TO_SECTION[stage_code]
            expected_data_type = {
                DraftSectionCode.CORE: CoreDraftData,
                DraftSectionCode.CLAUSE: ClauseDraftData,
                DraftSectionCode.RETRIEVAL_VIEW: RetrievalViewDraftData,
            }[section_code]
            if not isinstance(projected.data, expected_data_type):
                raise TypeError(
                    f"{section_code.value} 分支返回了不匹配的草稿数据类型"
                )
            first_draft = aggregate.draft is None
            if first_draft:
                aggregate.draft = MutableDraft(
                    revision=0,
                    page_count=context.prepared_pdf.page_count,
                    file_size_bytes=context.prepared_pdf.file_size_bytes,
                    classification=project_classification(context.classification),
                    updated_at=_utcnow(),
                )
            draft = aggregate.draft
            assert draft is not None
            previous = draft.sections.get(section_code)
            section_revision = 1 if previous is None else previous.revision + 1
            now = _utcnow()
            draft.revision += 1
            draft.updated_at = now
            draft.sections[section_code] = MutableDraftSection(
                code=section_code,
                revision=section_revision,
                result_status=projected.result_status,
                updated_at=now,
                data=projected.data,
                internal_result=internal_result,
            )
            self._complete_stage_locked(
                aggregate,
                stage_code,
                result=internal_result,
                result_status=projected.result_status,
                result_revision=section_revision,
            )
            self._publish_locked(
                aggregate,
                EventType.DRAFT_UPDATED,
                f"{_STAGE_NAMES[stage_code]}结果已更新。",
                stage_code=stage_code,
            )
            if first_draft:
                self._publish_locked(
                    aggregate,
                    EventType.RUN_REVIEW_READY,
                    "已有处理结果可以查看；失败部分可以按阶段单独重试。",
                )

    async def _begin_stage(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
    ) -> None:
        async with aggregate.lock:
            self._begin_stage_locked(aggregate, stage_code, retry=False)

    def _begin_stage_locked(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        *,
        retry: bool,
    ) -> None:
        stage = aggregate.stages[stage_code]
        now = _utcnow()
        stage.attempt += 1
        stage.status = StageStatus.RETRYING if retry else StageStatus.RUNNING
        stage.message = (
            f"正在重新{_STAGE_NAMES[stage_code]}。"
            if retry
            else _RUNNING_MESSAGES[stage_code]
        )
        stage.updated_at = now
        stage.retryable = False
        stage.progress = None
        stage.attempts.append(
            MutableStageAttempt(
                attempt=stage.attempt,
                status=stage.status,
                started_at=now,
            )
        )
        self._publish_locked(
            aggregate,
            EventType.STAGE_RETRYING if retry else EventType.STAGE_STARTED,
            stage.message,
            stage_code=stage_code,
        )

    async def _complete_stage(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        *,
        progress: StageProgress | None = None,
        result: Any | None = None,
    ) -> None:
        async with aggregate.lock:
            stage = aggregate.stages[stage_code]
            if stage.status not in {StageStatus.RUNNING, StageStatus.RETRYING}:
                return
            self._complete_stage_locked(
                aggregate,
                stage_code,
                progress=progress,
                result=result,
            )

    def _complete_stage_locked(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        *,
        result: Any | None = None,
        progress: StageProgress | None = None,
        result_status: ResultStatus | None = None,
        result_revision: int | None = None,
    ) -> None:
        stage = aggregate.stages[stage_code]
        now = _utcnow()
        stage.status = StageStatus.SUCCEEDED
        stage.message = _COMPLETED_MESSAGES[stage_code]
        stage.updated_at = now
        stage.progress = progress
        stage.result_status = result_status
        stage.result_revision = result_revision
        stage.retryable = (
            stage_code in _BRANCH_STAGES
            and stage.attempt < self._max_stage_attempts
        )
        if stage.attempts:
            attempt = stage.attempts[-1]
            attempt.status = StageStatus.SUCCEEDED
            attempt.finished_at = now
            attempt.result = result
        self._publish_locked(
            aggregate,
            EventType.STAGE_COMPLETED,
            stage.message,
            stage_code=stage_code,
        )

    async def _report_stage_progress(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        message: str,
    ) -> None:
        async with aggregate.lock:
            stage = aggregate.stages[stage_code]
            if stage.status not in {StageStatus.RUNNING, StageStatus.RETRYING}:
                return
            stage.message = message
            stage.updated_at = _utcnow()
            self._publish_locked(
                aggregate,
                EventType.STAGE_PROGRESS,
                message,
                stage_code=stage_code,
            )

    async def _fail_stage(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        error: Exception,
    ) -> None:
        async with aggregate.lock:
            stage = aggregate.stages[stage_code]
            now = _utcnow()
            stage.status = StageStatus.FAILED
            stage.retryable = (
                stage_code in _BRANCH_STAGES
                and aggregate.prerequisites is not None
                and stage.attempt < self._max_stage_attempts
            )
            stage.message = _FAILED_MESSAGES[stage_code]
            if stage_code in _BRANCH_STAGES and not stage.retryable:
                stage.message = stage.message.replace(
                    "，可以单独重试。",
                    "，且已达到当前自动尝试上限。",
                )
            stage.updated_at = now
            stage.progress = None
            if stage.attempts:
                attempt = stage.attempts[-1]
                attempt.status = StageStatus.FAILED
                attempt.finished_at = now
                attempt.error = f"{type(error).__name__}: {error}"
            self._publish_locked(
                aggregate,
                EventType.STAGE_FAILED,
                stage.message,
                stage_code=stage_code,
            )

    async def _fail_active_prerequisite_stage(
        self,
        aggregate: RunAggregate,
        error: Exception,
    ) -> None:
        async with aggregate.lock:
            active = next(
                (
                    code
                    for code in (
                        StageCode.DOCUMENT_READING,
                        StageCode.DOCUMENT_UNDERSTANDING,
                    )
                    if aggregate.stages[code].status
                    in {StageStatus.RUNNING, StageStatus.RETRYING}
                ),
                StageCode.DOCUMENT_READING,
            )
        await self._fail_stage(aggregate, active, error)

    async def _ensure_preprocessing_stages_completed(
        self,
        aggregate: RunAggregate,
        preprocessing: PreprocessingOutput,
    ) -> None:
        """兼容测试执行器或未来不逐节点回调的预处理实现。"""
        async with aggregate.lock:
            reading = aggregate.stages[StageCode.DOCUMENT_READING]
            if reading.status is StageStatus.RUNNING:
                self._complete_stage_locked(
                    aggregate,
                    StageCode.DOCUMENT_READING,
                    result=preprocessing.prepared_pdf,
                )
            elif reading.attempts:
                reading.attempts[-1].result = preprocessing.prepared_pdf
            understanding = aggregate.stages[StageCode.DOCUMENT_UNDERSTANDING]
            if understanding.status is StageStatus.PENDING:
                self._begin_stage_locked(
                    aggregate,
                    StageCode.DOCUMENT_UNDERSTANDING,
                    retry=False,
                )
            if understanding.status is StageStatus.RUNNING:
                self._complete_stage_locked(
                    aggregate,
                    StageCode.DOCUMENT_UNDERSTANDING,
                    result={
                        "document_structure": preprocessing.document_structure,
                        "unit_discovery_audit": (
                            preprocessing.unit_discovery_audit
                        ),
                        "unit_grounding_audit": (
                            preprocessing.unit_grounding_audit
                        ),
                    },
                )

    def _publish_locked(
        self,
        aggregate: RunAggregate,
        event_type: EventType,
        message: str,
        *,
        stage_code: StageCode | None = None,
        touch: bool = True,
    ) -> ContractExtractionEvent:
        now = _utcnow()
        if touch:
            aggregate.updated_at = now
            aggregate.expires_at = now + self._run_ttl
        event = ContractExtractionEvent(
            sequence=aggregate.next_sequence,
            run_id=aggregate.run_id,
            event_type=event_type,
            overall_status=self._run_status_locked(aggregate),
            message=message,
            stage=(
                self._stage_snapshot(aggregate.stages[stage_code])
                if stage_code is not None
                else None
            ),
            draft_revision=(
                aggregate.draft.revision if aggregate.draft is not None else None
            ),
            available_sections=self._available_sections_locked(aggregate),
            occurred_at=now,
        )
        aggregate.next_sequence += 1
        aggregate.events.append(event)
        for queue in tuple(aggregate.subscribers):
            self._offer_queue(queue, event)
        return event

    @staticmethod
    def _offer_queue(
        queue: asyncio.Queue[ContractExtractionEvent | None],
        item: ContractExtractionEvent | None,
    ) -> None:
        """慢订阅者只丢最旧事件，不反向阻塞合同处理。"""
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

    def _snapshot_locked(
        self,
        aggregate: RunAggregate,
    ) -> ContractExtractionSnapshot:
        available_sections = self._available_sections_locked(aggregate)
        run = ProcessingRunSnapshot(
            run_id=aggregate.run_id,
            status=self._run_status_locked(aggregate),
            created_at=aggregate.created_at,
            updated_at=aggregate.updated_at,
            expires_at=aggregate.expires_at,
            stages={
                code: self._stage_snapshot(aggregate.stages[code])
                for code in _STAGE_ORDER
            },
            available_sections=available_sections,
        )
        if aggregate.draft is None:
            return ContractExtractionSnapshot(run=run, draft=None)

        draft = aggregate.draft
        core = draft.sections.get(DraftSectionCode.CORE)
        clause = draft.sections.get(DraftSectionCode.CLAUSE)
        retrieval_view = draft.sections.get(DraftSectionCode.RETRIEVAL_VIEW)
        return ContractExtractionSnapshot(
            run=run,
            draft=ContractExtractionDraft(
                revision=draft.revision,
                document=ContractDocumentView(
                    document_id=aggregate.source.document_id,
                    file_name=aggregate.source.file_name,
                    file_size_bytes=draft.file_size_bytes,
                    page_count=draft.page_count,
                ),
                classification=draft.classification,
                core=(
                    DraftSection[CoreDraftData](
                        revision=core.revision,
                        result_status=core.result_status,
                        updated_at=core.updated_at,
                        data=CoreDraftData.model_validate(core.data),
                    )
                    if core is not None
                    else None
                ),
                clause=(
                    DraftSection[ClauseDraftData](
                        revision=clause.revision,
                        result_status=clause.result_status,
                        updated_at=clause.updated_at,
                        data=ClauseDraftData.model_validate(clause.data),
                    )
                    if clause is not None
                    else None
                ),
                retrieval_view=(
                    DraftSection[RetrievalViewDraftData](
                        revision=retrieval_view.revision,
                        result_status=retrieval_view.result_status,
                        updated_at=retrieval_view.updated_at,
                        data=RetrievalViewDraftData.model_validate(
                            retrieval_view.data
                        ),
                    )
                    if retrieval_view is not None
                    else None
                ),
                updated_at=draft.updated_at,
            ),
        )

    def _run_status_locked(self, aggregate: RunAggregate) -> RunStatus:
        if aggregate.expired:
            return RunStatus.EXPIRED
        branch_stages = [aggregate.stages[code] for code in _BRANCH_STAGES]
        if aggregate.draft is not None:
            if all(
                stage.status is StageStatus.SUCCEEDED for stage in branch_stages
            ):
                return RunStatus.READY
            return RunStatus.PARTIAL_READY
        prerequisite_failed = any(
            aggregate.stages[code].status is StageStatus.FAILED
            for code in _STAGE_ORDER[:3]
        )
        all_branches_finished = all(
            stage.status in {StageStatus.SUCCEEDED, StageStatus.FAILED}
            for stage in branch_stages
        )
        if prerequisite_failed or all_branches_finished:
            return RunStatus.FAILED
        return RunStatus.PROCESSING

    @staticmethod
    def _stage_snapshot(stage: MutableStage) -> StageSnapshot:
        return StageSnapshot(
            code=stage.code,
            name=stage.name,
            status=stage.status,
            message=stage.message,
            attempt=stage.attempt,
            retryable=stage.retryable,
            progress=stage.progress,
            result_status=stage.result_status,
            result_revision=stage.result_revision,
            updated_at=stage.updated_at,
        )

    @staticmethod
    def _available_sections_locked(
        aggregate: RunAggregate,
    ) -> tuple[DraftSectionCode, ...]:
        if aggregate.draft is None:
            return ()
        return tuple(
            code for code in _SECTION_ORDER if code in aggregate.draft.sections
        )

    def _spawn(
        self,
        run_id: str,
        coroutine: Coroutine[Any, Any, None],
    ) -> None:
        task = asyncio.create_task(
            coroutine,
            name=f"contract-extraction:{run_id}",
        )
        run_tasks = self._run_tasks.setdefault(run_id, set())
        run_tasks.add(task)

        def discard(completed: asyncio.Task[None]) -> None:
            current = self._run_tasks.get(run_id)
            if current is not None:
                current.discard(completed)
                if not current:
                    self._run_tasks.pop(run_id, None)

        task.add_done_callback(discard)

    def _has_active_task(self, run_id: str) -> bool:
        return any(
            not task.done() for task in self._run_tasks.get(run_id, ())
        )

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval_seconds)
            await self.expire_due_runs()


def _utcnow() -> datetime:
    """集中生成带时区时间，避免快照混入朴素时间。"""
    return datetime.now(UTC)


__all__ = [
    "ContractExtractionService",
    "RunConflictError",
    "StageRetryError",
]
