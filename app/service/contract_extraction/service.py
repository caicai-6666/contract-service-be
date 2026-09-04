"""合同提取的进程内任务、增量草稿、SSE 事件与断点重试服务。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.agent.contract_extraction.progress import (
    ParallelProgressPhase,
    ParallelProgressUpdate,
)
from app.agent.contract_extraction.state import ContractExtractionRequest
from app.service.contract_extraction.deduplication import PDFDeduplicationExecutor
from app.service.contract_extraction.document_detection import (
    ContractDocumentDetectionExecutor,
)
from app.service.contract_extraction.executor import (
    ContractExtractionExecutor,
    DocumentUnderstandingOutput,
    ExtractionContext,
    RetrievalViewOutput,
)
from app.service.contract_extraction.model import (
    ClauseDraftData,
    ContractDocumentDetectionView,
    ContractDocumentEvidenceView,
    ContractExtractionDraft,
    ContractExtractionEvent,
    ContractExtractionRunList,
    ContractExtractionRunSummary,
    ContractExtractionSnapshot,
    CoreDraftData,
    DeduplicationCandidateView,
    DeduplicationReviewView,
    DraftSectionCode,
    EventType,
    ProcessedPDFMetadataView,
    ProcessingRunSnapshot,
    ResultStatus,
    RetrievalViewDraftData,
    RunListStatus,
    RunStatus,
    StageCode,
    StageProgress,
    StageSnapshot,
    StageStatus,
    SuggestedFileNameView,
)
from app.service.contract_extraction.projector import (
    ProjectedSection,
    project_classification,
    project_clause,
    project_core,
    project_retrieval_view,
    project_suggested_file_name,
)
from app.service.contract_extraction.registry import (
    InternalDraftSectionCode,
    MemoryRunRegistry,
    MutableDraft,
    MutableDraftSection,
    MutableStage,
    MutableStageAttempt,
    RunAggregate,
    RunNotFoundError,
    SourceDocument,
)
from app.service.pdf_preparation import (
    PDFPreparationError,
    PDFPreparationService,
)
from app.service.contract_ingestion import (
    ContractIngestionResult,
    ContractIngestionService,
)

logger = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    """当前运行状态不允许请求的动作。"""


class StageRetryError(RunConflictError):
    """目标阶段未失败、缺少成功前置结果或已经超过次数限制。"""


_STAGE_ORDER = (
    StageCode.CONTRACT_DOCUMENT_DETECTION,
    StageCode.PDF_DEDUPLICATION,
    StageCode.CONTRACT_STRUCTURE_RECOGNITION,
    StageCode.CONTRACT_CLASSIFICATION,
    StageCode.FILE_NAME_GENERATION,
    StageCode.CORE_EXTRACTION,
    StageCode.CLAUSE_EXTRACTION,
    StageCode.RETRIEVAL_PREPARATION,
)
_BRANCH_STAGES = (
    StageCode.CORE_EXTRACTION,
    StageCode.CLAUSE_EXTRACTION,
    StageCode.RETRIEVAL_PREPARATION,
)
_LISTABLE_RUN_STATUSES = {
    RunStatus.PROCESSING,
    RunStatus.AWAITING_DEDUPLICATION_REVIEW,
    RunStatus.PARTIAL_READY,
    RunStatus.READY,
    RunStatus.FAILED,
}
_PUBLIC_SECTION_ORDER = (
    InternalDraftSectionCode.CORE,
    InternalDraftSectionCode.CLAUSE,
)
_INTERNAL_TO_PUBLIC_SECTION = {
    InternalDraftSectionCode.CORE: DraftSectionCode.CORE,
    InternalDraftSectionCode.CLAUSE: DraftSectionCode.CLAUSE,
}
_STAGE_NAMES = {
    StageCode.CONTRACT_DOCUMENT_DETECTION: "确认合同文档",
    StageCode.CONTRACT_STRUCTURE_RECOGNITION: "识别合同结构",
    StageCode.PDF_DEDUPLICATION: "检查重复合同",
    StageCode.CONTRACT_CLASSIFICATION: "识别合同类型",
    StageCode.FILE_NAME_GENERATION: "生成建议名称",
    StageCode.CORE_EXTRACTION: "提取核心信息",
    StageCode.CLAUSE_EXTRACTION: "提取合同条款",
    StageCode.RETRIEVAL_PREPARATION: "准备智能检索",
}
_PENDING_MESSAGES = {
    StageCode.CONTRACT_DOCUMENT_DETECTION: "等待确认上传内容是否属于合同文档。",
    StageCode.CONTRACT_STRUCTURE_RECOGNITION: "等待识别合同的页面与内容结构。",
    StageCode.PDF_DEDUPLICATION: "等待检查是否存在重复合同。",
    StageCode.CONTRACT_CLASSIFICATION: "等待识别合同涉及的交易类型。",
    StageCode.FILE_NAME_GENERATION: "等待根据合同内容生成建议名称。",
    StageCode.CORE_EXTRACTION: "等待提取合同核心信息。",
    StageCode.CLAUSE_EXTRACTION: "等待提取合同条款。",
    StageCode.RETRIEVAL_PREPARATION: "等待生成合同检索信息。",
}
_RUNNING_MESSAGES = {
    StageCode.CONTRACT_DOCUMENT_DETECTION: "正在确认上传内容是否属于合同文档。",
    StageCode.CONTRACT_STRUCTURE_RECOGNITION: "正在识别合同的内容结构。",
    StageCode.PDF_DEDUPLICATION: "正在检查是否存在重复合同。",
    StageCode.CONTRACT_CLASSIFICATION: "正在识别合同涉及的交易类型。",
    StageCode.FILE_NAME_GENERATION: "正在根据合同内容生成建议名称。",
    StageCode.CORE_EXTRACTION: "正在提取合同核心信息。",
    StageCode.CLAUSE_EXTRACTION: "正在识别并提取合同条款。",
    StageCode.RETRIEVAL_PREPARATION: "正在生成便于检索合同的信息。",
}
_COMPLETED_MESSAGES = {
    StageCode.CONTRACT_DOCUMENT_DETECTION: "合同文档属性已确认。",
    StageCode.CONTRACT_STRUCTURE_RECOGNITION: "合同内容结构已识别。",
    StageCode.PDF_DEDUPLICATION: "重复合同检查已完成。",
    StageCode.CONTRACT_CLASSIFICATION: "合同类型已识别。",
    StageCode.FILE_NAME_GENERATION: "建议名称已生成。",
    StageCode.CORE_EXTRACTION: "合同核心信息已生成。",
    StageCode.CLAUSE_EXTRACTION: "合同条款已生成。",
    StageCode.RETRIEVAL_PREPARATION: "合同检索信息已准备。",
}
_FAILED_MESSAGES = {
    StageCode.CONTRACT_DOCUMENT_DETECTION: "暂时无法确认上传内容是否属于合同文档",
    StageCode.CONTRACT_STRUCTURE_RECOGNITION: "暂时无法识别合同结构",
    StageCode.PDF_DEDUPLICATION: "暂时无法完成重复合同检查",
    StageCode.CONTRACT_CLASSIFICATION: "暂时无法识别合同类型",
    StageCode.FILE_NAME_GENERATION: "本次未能生成建议名称",
    StageCode.CORE_EXTRACTION: "本次未能完成核心信息提取",
    StageCode.CLAUSE_EXTRACTION: "本次未能完成合同条款提取",
    StageCode.RETRIEVAL_PREPARATION: "本次未能完成检索准备",
}
_STAGE_TO_SECTION = {
    StageCode.CORE_EXTRACTION: InternalDraftSectionCode.CORE,
    StageCode.CLAUSE_EXTRACTION: InternalDraftSectionCode.CLAUSE,
    StageCode.RETRIEVAL_PREPARATION: (
        InternalDraftSectionCode.RETRIEVAL_VIEW
    ),
}
_COUNTING_MESSAGES = {
    StageCode.CONTRACT_CLASSIFICATION: "正在统计待识别的合同类别数量。",
    StageCode.CORE_EXTRACTION: "正在统计待提取的核心字段数量。",
    StageCode.CLAUSE_EXTRACTION: "正在统计待提取的合同条款数量。",
    StageCode.RETRIEVAL_PREPARATION: "正在统计待生成的检索问题数量。",
}
_COUNTED_MESSAGE_TEMPLATES = {
    StageCode.CONTRACT_CLASSIFICATION: "数量统计完成，共需识别 {total} 个合同类别。",
    StageCode.CORE_EXTRACTION: "数量统计完成，共需提取 {total} 个核心字段。",
    StageCode.CLAUSE_EXTRACTION: "数量统计完成，共需提取 {total} 个合同条款。",
    StageCode.RETRIEVAL_PREPARATION: "数量统计完成，共需生成 {total} 个检索问题。",
}
_EXTRACTING_MESSAGE_TEMPLATES = {
    StageCode.CONTRACT_CLASSIFICATION: (
        "正在识别合同类别，已完成 {completed} / {total}。"
    ),
    StageCode.CORE_EXTRACTION: (
        "正在提取核心字段，已完成 {completed} / {total}。"
    ),
    StageCode.CLAUSE_EXTRACTION: (
        "正在提取合同条款，已完成 {completed} / {total}。"
    ),
    StageCode.RETRIEVAL_PREPARATION: (
        "正在生成检索问题，已完成 {completed} / {total}。"
    ),
}


class ContractExtractionService:
    """在单进程内协调上传、并行处理、事件订阅和失败断点重试。"""

    def __init__(
        self,
        *,
        executor: ContractExtractionExecutor,
        document_detection_executor: ContractDocumentDetectionExecutor,
        deduplication_executor: PDFDeduplicationExecutor,
        pdf_preparation_service: PDFPreparationService,
        ingestion_service: ContractIngestionService,
        run_ttl_seconds: int = 3600,
        deduplication_review_ttl_seconds: int = 600,
        cleanup_interval_seconds: int = 30,
        event_buffer_size: int = 256,
        sse_heartbeat_seconds: int = 15,
        max_stage_attempts: int = 3,
        registry: MemoryRunRegistry | None = None,
    ) -> None:
        if run_ttl_seconds <= 0:
            raise ValueError("任务内存保留时间必须大于 0")
        if not 0 < deduplication_review_ttl_seconds <= 600:
            raise ValueError("查重审核等待时间必须在 1 到 600 秒之间")
        if cleanup_interval_seconds <= 0:
            raise ValueError("任务清理间隔必须大于 0")
        if event_buffer_size <= 0:
            raise ValueError("SSE 事件缓冲区必须大于 0")
        if sse_heartbeat_seconds <= 0:
            raise ValueError("SSE 心跳间隔必须大于 0")
        if max_stage_attempts <= 0:
            raise ValueError("阶段最大尝试次数必须大于 0")
        self._executor = executor
        self._document_detection_executor = document_detection_executor
        self._deduplication_executor = deduplication_executor
        self._pdf_preparation_service = pdf_preparation_service
        self._ingestion_service = ingestion_service
        self._run_ttl = timedelta(seconds=run_ttl_seconds)
        self._deduplication_review_ttl = timedelta(
            seconds=deduplication_review_ttl_seconds
        )
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._event_buffer_size = event_buffer_size
        self._sse_heartbeat_seconds = sse_heartbeat_seconds
        self._max_stage_attempts = max_stage_attempts
        self._registry = registry or MemoryRunRegistry()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._run_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._review_expiry_tasks: dict[str, asyncio.Task[None]] = {}

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
        tasks.extend(
            task for task in self._review_expiry_tasks.values() if not task.done()
        )
        for task in self._review_expiry_tasks.values():
            task.cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._run_tasks.clear()
        self._review_expiry_tasks.clear()
        self._cleanup_task = None

    async def create_run(
        self,
        *,
        reviewer_user_name: str,
        file_name: str,
        pdf_bytes: bytes,
    ) -> ContractExtractionSnapshot:
        """请求内准备 PDF，通过校验后注册任务并先确认是否为合同。"""
        try:
            request = ContractExtractionRequest(
                file_name=file_name,
                pdf_bytes=pdf_bytes,
            )
        except ValueError as exc:
            raise PDFPreparationError("PDF 文件名或内容无效") from exc
        prepared_pdf = await self._pdf_preparation_service.prepare(request)
        now = _utcnow()
        aggregate = RunAggregate(
            run_id=str(uuid4()),
            reviewer_user_name=reviewer_user_name,
            source=SourceDocument(
                file_name=request.source_name,
                document_id=prepared_pdf.document_id,
            ),
            prepared_pdf=prepared_pdf,
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
            self._begin_stage_locked(
                aggregate,
                StageCode.CONTRACT_DOCUMENT_DETECTION,
                retry=False,
            )
            snapshot = self._snapshot_locked(aggregate)
        self._spawn(
            aggregate,
            self._run_from_document_detection(aggregate),
        )
        return snapshot

    async def get_snapshot(
        self,
        run_id: str,
        *,
        reviewer_user_name: str,
    ) -> ContractExtractionSnapshot:
        """返回状态与当前草稿在同一把锁下形成的原子快照。"""
        aggregate = await self._get_live_aggregate(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
        async with aggregate.lock:
            if aggregate.cancelled or aggregate.expired or aggregate.ingested:
                raise RunNotFoundError(run_id)
            return self._snapshot_locked(aggregate)

    async def list_unpersisted_runs(
        self,
        *,
        reviewer_user_name: str,
    ) -> ContractExtractionRunList:
        """列出仍在自动处理或等待人工操作、但尚未入库的任务。"""
        # 列表读取不续期；先清理已到期且没有活跃执行的任务，避免前端
        # 选择一个随即返回 404 的陈旧 run_id。
        await self.expire_due_runs()
        summaries: list[ContractExtractionRunSummary] = []
        for aggregate in await self._registry.values():
            async with aggregate.lock:
                if (
                    aggregate.cancelled
                    or aggregate.expired
                    or aggregate.ingested
                    or aggregate.reviewer_user_name != reviewer_user_name
                ):
                    continue
                run_status = self._run_status_locked(aggregate)
                if run_status not in _LISTABLE_RUN_STATUSES:
                    continue
                summaries.append(
                    ContractExtractionRunSummary(
                        run_id=aggregate.run_id,
                        document=self._processed_pdf_metadata_locked(
                            aggregate
                        ),
                        suggested_file_name=(
                            aggregate.suggested_file_name_view.file_name
                            if aggregate.suggested_file_name_view is not None
                            else None
                        ),
                        status=self._run_list_status_locked(
                            aggregate,
                            run_status,
                        ),
                        created_at=aggregate.created_at,
                        updated_at=aggregate.updated_at,
                        expires_at=aggregate.expires_at,
                    )
                )
        summaries.sort(
            key=lambda item: (item.updated_at, item.run_id),
            reverse=True,
        )
        return ContractExtractionRunList(root=tuple(summaries))

    async def cancel_run(
        self,
        run_id: str,
        *,
        reviewer_user_name: str,
    ) -> None:
        """终止当前用户的运行，并释放其全部进程内资源。"""
        aggregate = await self._get_live_aggregate(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
        async with aggregate.lock:
            # 与继续、重试的状态变更共用聚合锁；标记后即使其请求已经
            # 通过初次查询，也不能在锁释放后重新创建后台任务。
            if aggregate.cancelled or aggregate.expired or aggregate.ingested:
                raise RunNotFoundError(run_id)
            aggregate.cancelled = True
            aggregate.awaiting_deduplication_review = False
            self._publish_locked(
                aggregate,
                EventType.RUN_CANCELLED,
                "合同处理任务已取消。",
                touch=False,
            )
            # run.cancelled 必须成为每个订阅队列中的最终可见项；不能再
            # 追加 None，否则容量为 1 时会把取消事件本身淘汰。
            aggregate.subscribers.clear()
            removed = await self._registry.remove(run_id)
            if removed is not aggregate:
                raise RunNotFoundError(run_id)

        run_tasks = tuple(self._run_tasks.pop(run_id, ()))
        expiry_task = self._review_expiry_tasks.pop(run_id, None)
        tasks = [task for task in run_tasks if not task.done()]
        if expiry_task is not None and not expiry_task.done():
            tasks.append(expiry_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def ingest_run(
        self,
        run_id: str,
        *,
        reviewer_user_name: str,
        file_name: str,
        core: CoreDraftData,
        clauses: ClauseDraftData,
    ) -> ContractIngestionResult:
        """用用户最终审核值覆盖自动草稿，并正式保存合同。"""
        aggregate = await self._get_live_aggregate(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
        async with aggregate.lock:
            if aggregate.cancelled or aggregate.expired or aggregate.ingested:
                raise RunNotFoundError(run_id)
            if any(
                aggregate.stages[code].status is not StageStatus.SUCCEEDED
                for code in _STAGE_ORDER
            ):
                raise RunConflictError("全部合同处理阶段成功后才能正式入库")

            draft = aggregate.draft
            classification = aggregate.classification_view
            deduplication = aggregate.deduplication_result
            if draft is None or classification is None or deduplication is None:
                raise RunConflictError("合同运行缺少正式入库所需的前置结果")
            if any(
                code not in draft.sections
                for code in (
                    InternalDraftSectionCode.CORE,
                    InternalDraftSectionCode.CLAUSE,
                    InternalDraftSectionCode.RETRIEVAL_VIEW,
                )
            ):
                raise RunConflictError("合同运行的 Core、Clause 或检索结果尚未就绪")

            retrieval_result = draft.sections[
                InternalDraftSectionCode.RETRIEVAL_VIEW
            ].internal_result
            if not isinstance(retrieval_result, RetrievalViewOutput):
                raise RunConflictError("合同运行缺少完整检索向量结果")
            question_vector = retrieval_result.vector.vector
            if question_vector is None:
                raise RunConflictError("合同运行尚未形成问题融合向量")

            result = await self._ingestion_service.ingest(
                document_id=aggregate.source.document_id,
                processed_pdf_bytes=aggregate.prepared_pdf.processed_pdf_bytes,
                page_count=aggregate.prepared_pdf.page_count,
                file_name=file_name,
                reviewer=reviewer_user_name,
                classification=classification,
                core=core,
                clauses=clauses,
                question_fusion_vector=question_vector,
                page_fusion_vector=deduplication.page_fusion_vector.vector,
            )

            # 只有 SQLite 已发布 ready 且 PDF、ES 均成功后才使运行失效。
            # 终态事件先进入已有订阅队列，随后移除注册表，避免成功响应后
            # 仍能重复入库。
            aggregate.ingested = True
            self._publish_locked(
                aggregate,
                EventType.RUN_INGESTED,
                "合同已完成正式入库。",
                touch=False,
            )
            aggregate.subscribers.clear()
            removed = await self._registry.remove(run_id)
            if removed is not aggregate:
                raise RuntimeError("正式入库成功后未能释放对应内存运行")

        run_tasks = tuple(self._run_tasks.pop(run_id, ()))
        expiry_task = self._review_expiry_tasks.pop(run_id, None)
        tasks = [task for task in run_tasks if not task.done()]
        if expiry_task is not None and not expiry_task.done():
            tasks.append(expiry_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return result

    @staticmethod
    def _run_list_status_locked(
        aggregate: RunAggregate,
        run_status: RunStatus,
    ) -> RunListStatus:
        """把细粒度运行状态投影为列表中的自动推进/人工介入状态。"""
        if run_status is RunStatus.PROCESSING:
            # 公共前置节点衔接的极短间隔内可能暂时没有 running 阶段，
            # 但后台协程仍会继续推进，不应在列表中闪烁为阻塞。
            return RunListStatus.PROCESSING
        if run_status is RunStatus.PARTIAL_READY and any(
            stage.status in {StageStatus.RUNNING, StageStatus.RETRYING}
            for stage in aggregate.stages.values()
        ):
            return RunListStatus.PROCESSING
        # 查重暂停、失败节点、分支停止以及提取完成后都需要用户确认、
        # 重试或入库操作，统一标记为 blocked。
        return RunListStatus.BLOCKED

    async def _get_live_aggregate(
        self,
        run_id: str,
        *,
        reviewer_user_name: str,
    ) -> RunAggregate:
        """让任何访问都严格执行暂停截止时间，不依赖定时任务调度精度。"""
        aggregate = await self._registry.get(run_id)
        cancelled = False
        expired = False
        ingested = False
        async with aggregate.lock:
            if aggregate.reviewer_user_name != reviewer_user_name:
                raise RunNotFoundError(run_id)
            if aggregate.cancelled:
                cancelled = True
            elif aggregate.ingested:
                ingested = True
            elif aggregate.expired:
                expired = True
            elif (
                aggregate.awaiting_deduplication_review
                and aggregate.expires_at <= _utcnow()
            ):
                self._expire_locked(aggregate)
                expired = True
        # 取消路径自行负责取消并等待后台协程；并发读取不能调用过期清理
        # 抢先弹出任务索引，否则可能令被取消协程失去引用后继续执行。
        if cancelled or ingested:
            raise RunNotFoundError(run_id)
        if expired:
            await self._remove_expired_run(aggregate)
            raise RunNotFoundError(run_id)
        return aggregate

    async def continue_run(
        self,
        run_id: str,
        *,
        reviewer_user_name: str,
    ) -> ContractExtractionSnapshot:
        """消费唯一暂停点并继续执行结构识别及后续提取。"""
        aggregate = await self._get_live_aggregate(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
        expired = False
        async with aggregate.lock:
            now = _utcnow()
            # 存活检查和暂停点消费必须在同一把锁中完成，避免截止时刻
            # “到期任务”和“继续请求”竞态时错误放行已过期任务。
            if aggregate.cancelled or aggregate.ingested:
                raise RunNotFoundError(run_id)
            if (
                aggregate.expired
                or (
                    aggregate.awaiting_deduplication_review
                    and aggregate.expires_at <= now
                )
            ):
                self._expire_locked(aggregate)
                expired = True
            elif not aggregate.awaiting_deduplication_review:
                raise RunConflictError("任务当前不在查重结果等待确认状态")
            else:
                aggregate.awaiting_deduplication_review = False
                aggregate.continued_at = now
                aggregate.updated_at = now
                aggregate.expires_at = now + self._run_ttl
                self._publish_locked(
                    aggregate,
                    EventType.RUN_CONTINUED,
                    "查重结果已确认，继续提取合同内容。",
                )
                self._begin_stage_locked(
                    aggregate,
                    StageCode.CONTRACT_STRUCTURE_RECOGNITION,
                    retry=False,
                )
                snapshot = self._snapshot_locked(aggregate)

        if expired:
            await self._remove_expired_run(aggregate)
            raise RunNotFoundError(run_id)
        expiry_task = self._review_expiry_tasks.pop(run_id, None)
        if expiry_task is not None:
            expiry_task.cancel()
        self._spawn(
            aggregate,
            self._run_after_deduplication_review(
                aggregate,
                structure_recognition_already_started=True,
            ),
        )
        return snapshot

    async def retry_stage(
        self,
        run_id: str,
        stage_code: StageCode,
        *,
        reviewer_user_name: str,
    ) -> ContractExtractionSnapshot:
        """异步重跑失败阶段，并从该阶段已有的成功前置结果继续。"""
        aggregate = await self._get_live_aggregate(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
        understanding: DocumentUnderstandingOutput | None = None
        async with aggregate.lock:
            if aggregate.cancelled or aggregate.expired or aggregate.ingested:
                raise RunNotFoundError(run_id)
            stage = aggregate.stages[stage_code]
            if stage.status is not StageStatus.FAILED:
                raise StageRetryError("只有执行失败的阶段可以重试")
            if stage.attempt >= self._max_stage_attempts:
                raise StageRetryError("该阶段已达到最大尝试次数")
            self._validate_retry_prerequisites_locked(aggregate, stage_code)
            if stage_code is StageCode.CONTRACT_CLASSIFICATION:
                understanding = aggregate.structure_result
                assert isinstance(understanding, DocumentUnderstandingOutput)
            self._begin_stage_locked(aggregate, stage_code, retry=True)
            snapshot = self._snapshot_locked(aggregate)

        if stage_code is StageCode.CONTRACT_DOCUMENT_DETECTION:
            operation = self._run_from_document_detection(aggregate)
        elif stage_code is StageCode.PDF_DEDUPLICATION:
            operation = self._run_until_deduplication_review(aggregate)
        elif stage_code is StageCode.CONTRACT_STRUCTURE_RECOGNITION:
            operation = self._run_after_deduplication_review(
                aggregate,
                structure_recognition_already_started=True,
            )
        elif stage_code is StageCode.CONTRACT_CLASSIFICATION:
            assert understanding is not None
            operation = self._run_from_classification(
                aggregate,
                understanding,
                classification_already_started=True,
            )
        elif stage_code is StageCode.FILE_NAME_GENERATION:
            operation = self._run_from_file_name_generation(
                aggregate,
                already_started=True,
            )
        else:
            operation = self._execute_branch(
                aggregate,
                stage_code,
                already_started=True,
            )
        self._spawn(
            aggregate,
            operation,
        )
        return snapshot

    @staticmethod
    def _validate_retry_prerequisites_locked(
        aggregate: RunAggregate,
        stage_code: StageCode,
    ) -> None:
        """确认断点之前的权威结果仍在，不允许为重试倒退执行流程。"""
        if stage_code is StageCode.CONTRACT_DOCUMENT_DETECTION:
            return
        if stage_code is StageCode.PDF_DEDUPLICATION:
            detection = aggregate.document_detection_result
            if detection is None or not detection.is_contract:
                raise StageRetryError("合同文档识别结果不可用，无法重试查重")
            return
        if stage_code is StageCode.CONTRACT_STRUCTURE_RECOGNITION:
            if (
                aggregate.stages[StageCode.PDF_DEDUPLICATION].status
                is not StageStatus.SUCCEEDED
                or aggregate.continued_at is None
            ):
                raise StageRetryError("查重确认结果不可用，无法重试结构识别")
            return
        if stage_code is StageCode.CONTRACT_CLASSIFICATION:
            if not isinstance(
                aggregate.structure_result,
                DocumentUnderstandingOutput,
            ):
                raise StageRetryError("合同结构识别结果不可用，无法重试分类")
            return
        if stage_code is StageCode.FILE_NAME_GENERATION:
            if not isinstance(aggregate.prerequisites, ExtractionContext):
                raise StageRetryError("合同分类结果不可用，无法重试建议名称生成")
            return
        if not isinstance(aggregate.prerequisites, ExtractionContext):
            raise StageRetryError("合同分类结果不可用，无法重试提取阶段")

    @asynccontextmanager
    async def subscribe_events(
        self,
        run_id: str,
        *,
        reviewer_user_name: str,
        after_sequence: int | None = None,
    ) -> AsyncIterator[
        tuple[
            tuple[ContractExtractionEvent, ...],
            asyncio.Queue[ContractExtractionEvent | None],
        ]
    ]:
        """原子注册订阅者并回放断线期间仍在缓冲区中的事件。"""
        aggregate = await self._get_live_aggregate(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
        queue: asyncio.Queue[ContractExtractionEvent | None] = asyncio.Queue(
            maxsize=self._event_buffer_size
        )
        async with aggregate.lock:
            if aggregate.cancelled or aggregate.expired or aggregate.ingested:
                raise RunNotFoundError(run_id)
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
            if (
                aggregate.cancelled
                or aggregate.ingested
                or aggregate.expires_at > current
                or self._has_active_task(aggregate.run_id)
            ):
                continue
            async with aggregate.lock:
                if (
                    aggregate.cancelled
                    or aggregate.expired
                    or aggregate.ingested
                    or aggregate.expires_at > current
                ):
                    continue
                if any(
                    stage.status in {StageStatus.RUNNING, StageStatus.RETRYING}
                    for stage in aggregate.stages.values()
                ):
                    continue
                self._expire_locked(aggregate)
            await self._remove_expired_run(aggregate)
            expired_count += 1
        return expired_count

    def _schedule_review_expiry(self, aggregate: RunAggregate) -> None:
        """为 10 分钟暂停点建立精确到期任务，不依赖周期扫描延迟。"""
        previous = self._review_expiry_tasks.pop(aggregate.run_id, None)
        if previous is not None:
            previous.cancel()

        async def expire_at_deadline() -> None:
            delay = max(
                0.0,
                (aggregate.expires_at - _utcnow()).total_seconds(),
            )
            await asyncio.sleep(delay)
            try:
                current = await self._registry.get(aggregate.run_id)
            except RunNotFoundError:
                return
            async with current.lock:
                if (
                    current.cancelled
                    or current.expired
                    or current.ingested
                    or not current.awaiting_deduplication_review
                    or current.expires_at > _utcnow()
                ):
                    return
                self._expire_locked(current)
            await self._remove_expired_run(current)

        task = asyncio.create_task(
            expire_at_deadline(),
            name=f"contract-deduplication-review-expiry:{aggregate.run_id}",
        )
        self._review_expiry_tasks[aggregate.run_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._review_expiry_tasks.get(aggregate.run_id) is completed:
                self._review_expiry_tasks.pop(aggregate.run_id, None)

        task.add_done_callback(discard)

    def _expire_locked(self, aggregate: RunAggregate) -> None:
        """在聚合锁内发布最终事件并使所有后续操作失效。"""
        if aggregate.expired:
            return
        aggregate.expired = True
        self._publish_locked(
            aggregate,
            EventType.RUN_EXPIRED,
            "该处理任务已过期，请重新上传合同。",
            touch=False,
        )
        aggregate.subscribers.clear()

    async def _remove_expired_run(self, aggregate: RunAggregate) -> None:
        """从注册表和任务索引中释放过期聚合的全部强引用。"""
        await self._registry.remove(aggregate.run_id)
        self._run_tasks.pop(aggregate.run_id, None)
        expiry_task = self._review_expiry_tasks.pop(aggregate.run_id, None)
        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()

    async def _run_from_document_detection(
        self,
        aggregate: RunAggregate,
    ) -> None:
        """先识别合同文档；仅可靠判定为合同时启动查重。"""
        try:
            detection = await self._document_detection_executor.detect(
                aggregate.prepared_pdf
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("合同文档识别失败，run_id=%s", aggregate.run_id)
            await self._fail_stage(
                aggregate,
                StageCode.CONTRACT_DOCUMENT_DETECTION,
                exc,
            )
            return

        if detection.status == "failed":
            async with aggregate.lock:
                aggregate.document_detection_result = detection
            await self._fail_stage(
                aggregate,
                StageCode.CONTRACT_DOCUMENT_DETECTION,
                RuntimeError(detection.error or "合同文档识别没有形成可靠结果"),
            )
            return

        async with aggregate.lock:
            aggregate.document_detection_result = detection
            self._complete_stage_locked(
                aggregate,
                StageCode.CONTRACT_DOCUMENT_DETECTION,
                result=detection,
            )
            if not detection.is_contract:
                self._publish_locked(
                    aggregate,
                    EventType.RUN_DOCUMENT_REJECTED,
                    "上传内容不属于合同文档，处理已停止。",
                    document_detection=(
                        self._document_detection_view_locked(aggregate)
                    ),
                )
                return
            self._begin_stage_locked(
                aggregate,
                StageCode.PDF_DEDUPLICATION,
                retry=False,
            )

        await self._run_until_deduplication_review(aggregate)

    async def _run_until_deduplication_review(
        self,
        aggregate: RunAggregate,
    ) -> None:
        """完成 PDF 查重，然后在用户可见暂停点停止推进。"""
        prepared_pdf = aggregate.prepared_pdf
        try:
            deduplication = await self._deduplication_executor.deduplicate(
                prepared_pdf
            )
            if deduplication.status == "failed":
                raise RuntimeError(
                    deduplication.error or "PDF 查重没有形成可靠结果"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("PDF 查重失败，run_id=%s", aggregate.run_id)
            await self._fail_stage(
                aggregate,
                StageCode.PDF_DEDUPLICATION,
                exc,
            )
            return

        async with aggregate.lock:
            aggregate.deduplication_result = deduplication
            self._complete_stage_locked(
                aggregate,
                StageCode.PDF_DEDUPLICATION,
                result=deduplication,
            )
            now = _utcnow()
            aggregate.awaiting_deduplication_review = True
            aggregate.updated_at = now
            review_expires_at = now + self._deduplication_review_ttl
            aggregate.deduplication_review_expires_at = review_expires_at
            aggregate.expires_at = review_expires_at
            # 先建立精确定时器再公开暂停事件，确保前端收到事件后立即
            # 提交 continue 时一定能取消对应任务，不遗留长时间空转任务。
            self._schedule_review_expiry(aggregate)
            self._publish_locked(
                aggregate,
                EventType.RUN_DEDUPLICATION_REVIEW_REQUIRED,
                "重复合同检查已完成，请处理候选结果后继续。",
                deduplication=self._deduplication_view_locked(aggregate),
                touch=False,
            )

    async def _run_after_deduplication_review(
        self,
        aggregate: RunAggregate,
        *,
        structure_recognition_already_started: bool = False,
    ) -> None:
        """继续执行结构、分类、建议命名及三个隔离分支。"""
        if not structure_recognition_already_started:
            await self._begin_stage(
                aggregate,
                StageCode.CONTRACT_STRUCTURE_RECOGNITION,
            )

        async def on_structure_recognition_update(
            node_name: str,
            values: dict[str, Any],
        ) -> None:
            if node_name == "discover_document_units":
                await self._report_stage_progress(
                    aggregate,
                    StageCode.CONTRACT_STRUCTURE_RECOGNITION,
                    "合同内容单元已识别，正在确认页面位置。",
                )

        try:
            understanding = await self._executor.understand_document(
                aggregate.prepared_pdf,
                on_structure_recognition_update,
            )
            async with aggregate.lock:
                aggregate.structure_result = understanding
                self._complete_stage_locked(
                    aggregate,
                    StageCode.CONTRACT_STRUCTURE_RECOGNITION,
                    result={
                        "document_structure": understanding.document_structure,
                        "unit_discovery_audit": understanding.unit_discovery_audit,
                        "unit_grounding_audit": understanding.unit_grounding_audit,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("合同结构识别失败，run_id=%s", aggregate.run_id)
            await self._fail_stage(
                aggregate,
                StageCode.CONTRACT_STRUCTURE_RECOGNITION,
                exc,
            )
            return

        await self._run_from_classification(aggregate, understanding)

    async def _run_from_classification(
        self,
        aggregate: RunAggregate,
        understanding: DocumentUnderstandingOutput,
        *,
        classification_already_started: bool = False,
    ) -> None:
        """复用结构识别结果执行分类，成功后开始生成建议名称。"""
        if not classification_already_started:
            await self._begin_stage(
                aggregate,
                StageCode.CONTRACT_CLASSIFICATION,
            )

        async def on_classification_progress(
            update: ParallelProgressUpdate,
        ) -> None:
            await self._report_parallel_progress(
                aggregate,
                StageCode.CONTRACT_CLASSIFICATION,
                update,
            )

        try:
            context = await self._executor.classify(
                understanding,
                on_progress=on_classification_progress,
            )
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
            classification_view = project_classification(
                context.classification
            )
            # 分类投影独立保存，使分支尚未提交或 SSE 事件已淘汰时，
            # 单任务快照仍可稳定恢复分类结果。
            aggregate.classification_view = classification_view
            self._complete_stage_locked(
                aggregate,
                StageCode.CONTRACT_CLASSIFICATION,
                result={
                    "classification": context.classification,
                    "classification_audit": context.classification_audit,
                },
                classification=classification_view,
            )

        await self._run_from_file_name_generation(aggregate)

    async def _run_from_file_name_generation(
        self,
        aggregate: RunAggregate,
        *,
        already_started: bool = False,
    ) -> None:
        """生成证据化建议名称，成功后再启动三个相互隔离的分支。"""
        if not already_started:
            await self._begin_stage(aggregate, StageCode.FILE_NAME_GENERATION)
        context = aggregate.prerequisites
        if not isinstance(context, ExtractionContext):
            await self._fail_stage(
                aggregate,
                StageCode.FILE_NAME_GENERATION,
                RuntimeError("缺少合同分类结果"),
            )
            return

        try:
            internal_result = await self._executor.generate_suggested_file_name(
                context
            )
            suggested_file_name = project_suggested_file_name(internal_result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("建议名称生成失败，run_id=%s", aggregate.run_id)
            await self._fail_stage(
                aggregate,
                StageCode.FILE_NAME_GENERATION,
                exc,
            )
            return

        async with aggregate.lock:
            # 与分类投影一样独立保存，确保 SSE 丢失或历史列表恢复后
            # 仍能获得当前正式建议；完整工具审计只留在阶段尝试历史中。
            aggregate.suggested_file_name_view = suggested_file_name
            self._complete_stage_locked(
                aggregate,
                StageCode.FILE_NAME_GENERATION,
                result=internal_result,
                suggested_file_name=suggested_file_name,
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
            async def on_progress(update: ParallelProgressUpdate) -> None:
                await self._report_parallel_progress(
                    aggregate,
                    stage_code,
                    update,
                )

            if stage_code is StageCode.CORE_EXTRACTION:
                internal_result = await self._executor.extract_core(
                    context,
                    on_progress=on_progress,
                )
                projected = project_core(internal_result)
            elif stage_code is StageCode.CLAUSE_EXTRACTION:
                internal_result = await self._executor.extract_clause(
                    context,
                    on_progress=on_progress,
                )
                projected = project_clause(internal_result)
            else:
                internal_result = await self._executor.prepare_retrieval_view(
                    context,
                    on_progress=on_progress,
                )
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
                InternalDraftSectionCode.CORE: CoreDraftData,
                InternalDraftSectionCode.CLAUSE: ClauseDraftData,
                InternalDraftSectionCode.RETRIEVAL_VIEW: RetrievalViewDraftData,
            }[section_code]
            if not isinstance(projected.data, expected_data_type):
                raise TypeError(
                    f"{section_code.value} 分支返回了不匹配的草稿数据类型"
                )
            if aggregate.draft is None:
                aggregate.draft = MutableDraft(
                    revision=0,
                    page_count=context.prepared_pdf.page_count,
                    processed_file_size_bytes=(
                        context.prepared_pdf.processed_file_size_bytes
                    ),
                    classification=project_classification(context.classification),
                    updated_at=_utcnow(),
                )
            draft = aggregate.draft
            assert draft is not None
            had_public_result = any(
                code in draft.sections for code in _PUBLIC_SECTION_ORDER
            )
            previous = draft.sections.get(section_code)
            section_revision = 1 if previous is None else previous.revision + 1
            now = _utcnow()
            draft.sections[section_code] = MutableDraftSection(
                code=section_code,
                revision=section_revision,
                result_status=projected.result_status,
                updated_at=now,
                data=projected.data,
                internal_result=internal_result,
            )
            is_public_section = section_code in _PUBLIC_SECTION_ORDER
            if is_public_section:
                draft.revision += 1
                draft.updated_at = now
            self._complete_stage_locked(
                aggregate,
                stage_code,
                result=internal_result,
                result_status=projected.result_status,
                result_revision=section_revision,
            )
            if not is_public_section:
                return
            self._publish_locked(
                aggregate,
                EventType.DRAFT_UPDATED,
                f"{_STAGE_NAMES[stage_code]}结果已更新。",
                stage_code=stage_code,
            )
            if not had_public_result:
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
        classification: ContractClassificationView | None = None,
        suggested_file_name: SuggestedFileNameView | None = None,
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
                classification=classification,
                suggested_file_name=suggested_file_name,
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
        classification: ContractClassificationView | None = None,
        suggested_file_name: SuggestedFileNameView | None = None,
    ) -> None:
        stage = aggregate.stages[stage_code]
        now = _utcnow()
        stage.status = StageStatus.SUCCEEDED
        stage.message = _COMPLETED_MESSAGES[stage_code]
        stage.updated_at = now
        # 逐项进度在成功后仍应保留为 m / m，供完成事件与后续快照展示。
        if progress is not None:
            stage.progress = progress
        stage.result_status = result_status
        stage.result_revision = result_revision
        # 一旦阶段成功，即便结果完整程度为 partial，也禁止再次执行。
        stage.retryable = False
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
            classification=classification,
            suggested_file_name=suggested_file_name,
        )

    async def _report_stage_progress(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        message: str,
        *,
        progress: StageProgress | None = None,
    ) -> None:
        async with aggregate.lock:
            stage = aggregate.stages[stage_code]
            if stage.status not in {StageStatus.RUNNING, StageStatus.RETRYING}:
                return
            stage.message = message
            stage.updated_at = _utcnow()
            stage.progress = progress
            self._publish_locked(
                aggregate,
                EventType.STAGE_PROGRESS,
                message,
                stage_code=stage_code,
            )

    async def _report_parallel_progress(
        self,
        aggregate: RunAggregate,
        stage_code: StageCode,
        update: ParallelProgressUpdate,
    ) -> None:
        """把 Agent 内部计数投影为稳定的用户消息和 SSE 离散进度。"""
        if update.phase is ParallelProgressPhase.COUNTING:
            message = _COUNTING_MESSAGES[stage_code]
            progress = None
        elif update.phase is ParallelProgressPhase.COUNTED:
            assert update.total is not None
            message = _COUNTED_MESSAGE_TEMPLATES[stage_code].format(
                total=update.total,
            )
            progress = StageProgress(completed=0, total=update.total)
        else:
            assert update.completed is not None
            assert update.total is not None
            message = _EXTRACTING_MESSAGE_TEMPLATES[stage_code].format(
                completed=update.completed,
                total=update.total,
            )
            progress = StageProgress(
                completed=update.completed,
                total=update.total,
            )
        await self._report_stage_progress(
            aggregate,
            stage_code,
            message,
            progress=progress,
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
            stage.retryable = stage.attempt < self._max_stage_attempts
            stage.message = (
                f"{_FAILED_MESSAGES[stage_code]}，可以从该阶段重试。"
                if stage.retryable
                else f"{_FAILED_MESSAGES[stage_code]}，且已达到最大尝试次数。"
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

    @staticmethod
    def _document_detection_view_locked(
        aggregate: RunAggregate,
    ) -> ContractDocumentDetectionView | None:
        """隐藏模型运行轨迹，只投影可靠二分类、证据和简洁理由。"""
        result = aggregate.document_detection_result
        if result is None or result.status == "failed":
            return None
        assert result.is_contract is not None
        assert result.reasoning_summary is not None
        return ContractDocumentDetectionView(
            is_contract=result.is_contract,
            evidence=tuple(
                ContractDocumentEvidenceView(
                    page_number=item.page_number,
                    observation=item.observation,
                )
                for item in result.evidence
            ),
            reasoning_summary=result.reasoning_summary,
        )

    def _deduplication_view_locked(
        self,
        aggregate: RunAggregate,
    ) -> DeduplicationReviewView | None:
        """从私有向量、工具审计和错误中投影最小前端查重结果。"""
        result = aggregate.deduplication_result
        review_expires_at = aggregate.deduplication_review_expires_at
        if result is None or review_expires_at is None:
            return None

        judgments = {
            judgment.candidate_document_id: judgment
            for judgment in result.judgments
        }
        candidates: list[DeduplicationCandidateView] = []
        for candidate in result.candidate_set.candidates:
            judgment = judgments[candidate.document_id]
            # 不同合同和失败判断仍保留在内部结果与审计中；前端只需要
            # 处理确认为重复或相似的合同，不公开无操作价值的候选。
            if judgment.status not in {"duplicate", "similar"}:
                continue
            # cosine dense_vector 的 ES _score 为 (1 + cosine) / 2；公共契约
            # 返回原始 cosine，避免把两种分数口径都称为“相似度”。
            cosine_similarity = max(-1.0, min(1.0, 2 * candidate.score - 1))
            candidates.append(
                DeduplicationCandidateView(
                    rank=candidate.rank,
                    cosine_similarity=cosine_similarity,
                    relation=judgment.status,
                    document_id=candidate.document_id,
                    file_name=candidate.file_name,
                    file_uri=candidate.file_uri,
                    page_count=candidate.page_count,
                    reasoning_summary=judgment.reasoning_summary,
                )
            )
        return DeduplicationReviewView(
            status=result.status,
            candidates=tuple(candidates),
            review_expires_at=review_expires_at,
            continued_at=aggregate.continued_at,
        )

    def _publish_locked(
        self,
        aggregate: RunAggregate,
        event_type: EventType,
        message: str,
        *,
        stage_code: StageCode | None = None,
        document_detection: ContractDocumentDetectionView | None = None,
        deduplication: DeduplicationReviewView | None = None,
        classification: ContractClassificationView | None = None,
        suggested_file_name: SuggestedFileNameView | None = None,
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
                aggregate.draft.revision
                if aggregate.draft is not None
                and aggregate.draft.revision > 0
                else None
            ),
            available_sections=self._available_sections_locked(aggregate),
            document_detection=document_detection,
            deduplication=deduplication,
            classification=classification,
            suggested_file_name=suggested_file_name,
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
            document=self._processed_pdf_metadata_locked(aggregate),
            stages={
                code: self._stage_snapshot(aggregate.stages[code])
                for code in _STAGE_ORDER
            },
            available_sections=available_sections,
            document_detection=self._document_detection_view_locked(aggregate),
            deduplication=self._deduplication_view_locked(aggregate),
            classification=aggregate.classification_view,
            suggested_file_name=aggregate.suggested_file_name_view,
        )
        if aggregate.draft is None:
            return ContractExtractionSnapshot(run=run, draft=None)

        draft = aggregate.draft
        core = draft.sections.get(InternalDraftSectionCode.CORE)
        clause = draft.sections.get(InternalDraftSectionCode.CLAUSE)
        if core is None and clause is None:
            return ContractExtractionSnapshot(run=run, draft=None)
        return ContractExtractionSnapshot(
            run=run,
            draft=ContractExtractionDraft(
                core=(
                    CoreDraftData.model_validate(core.data)
                    if core is not None
                    else None
                ),
                clauses=(
                    ClauseDraftData.model_validate(clause.data)
                    if clause is not None
                    else None
                ),
            ),
        )

    @staticmethod
    def _processed_pdf_metadata_locked(
        aggregate: RunAggregate,
    ) -> ProcessedPDFMetadataView:
        """从处理版 PDF 投影文件大小、页数和压缩后封面像素尺寸。"""
        try:
            cover = aggregate.prepared_pdf.pages[0]
        except IndexError as exc:
            raise RuntimeError("处理版 PDF 缺少封面页面") from exc
        return ProcessedPDFMetadataView(
            file_name=aggregate.source.file_name,
            processed_file_size_bytes=(
                aggregate.prepared_pdf.processed_file_size_bytes
            ),
            page_count=aggregate.prepared_pdf.page_count,
            cover_width_pixels=cover.width_pixels,
            cover_height_pixels=cover.height_pixels,
        )

    def _run_status_locked(self, aggregate: RunAggregate) -> RunStatus:
        if aggregate.ingested:
            return RunStatus.INGESTED
        if aggregate.cancelled:
            return RunStatus.CANCELLED
        if aggregate.expired:
            return RunStatus.EXPIRED
        detection = aggregate.document_detection_result
        if detection is not None and detection.status == "not_contract":
            return RunStatus.NOT_A_CONTRACT
        if aggregate.awaiting_deduplication_review:
            return RunStatus.AWAITING_DEDUPLICATION_REVIEW
        branch_stages = [aggregate.stages[code] for code in _BRANCH_STAGES]
        if aggregate.draft is not None and any(
            code in aggregate.draft.sections
            for code in _PUBLIC_SECTION_ORDER
        ):
            if all(
                stage.status is StageStatus.SUCCEEDED for stage in branch_stages
            ):
                return RunStatus.READY
            return RunStatus.PARTIAL_READY
        prerequisite_failed = any(
            aggregate.stages[code].status is StageStatus.FAILED
            for code in _STAGE_ORDER[:5]
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
            # 公共阶段以当前尝试为准；重试会开启新的计时，未开始则为空。
            started_at=(
                stage.attempts[-1].started_at if stage.attempts else None
            ),
            updated_at=stage.updated_at,
        )

    @staticmethod
    def _available_sections_locked(
        aggregate: RunAggregate,
    ) -> tuple[DraftSectionCode, ...]:
        if aggregate.draft is None:
            return ()
        return tuple(
            _INTERNAL_TO_PUBLIC_SECTION[code]
            for code in _PUBLIC_SECTION_ORDER
            if code in aggregate.draft.sections
        )

    def _spawn(
        self,
        aggregate: RunAggregate,
        coroutine: Coroutine[Any, Any, None],
    ) -> None:
        if aggregate.cancelled or aggregate.expired or aggregate.ingested:
            # continue/retry 与取消并发时，操作协程可能已经构造但不再
            # 允许调度；显式关闭可避免未等待协程告警。
            coroutine.close()
            return
        run_id = aggregate.run_id
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
