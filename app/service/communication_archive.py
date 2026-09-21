"""定时收集冻结任务，调用记忆图，提交检索数据并安全驱逐空闲会话。"""

import asyncio
import logging
import math
from typing import Any, Protocol

from app.agent.conversation_memory.node import TaskMemoryOutput
from app.schema.communication_retrieval import TaskRetrievalRecord
from app.core.config import get_settings
from app.schema.communication import ConversationHistoryRecord, TERMINAL_STATUSES
from app.service.communication_history import ArchiveCandidate, ConversationHistoryService


logger = logging.getLogger(__name__)


class ConversationMemoryGraph(Protocol):
    async def ainvoke(self, input: dict[str, Any]) -> dict[str, Any]: ...


def partition_memory_tasks(records: tuple[ConversationHistoryRecord, ...], *, minimum: int = 10, force: bool = False):
    """正常批次从第 minimum 条延伸到首个 completed；驱逐时允许不足额/中断结尾的尾批。"""
    remaining = records
    while remaining:
        end = next((i + 1 for i, record in enumerate(remaining)
                    if i + 1 >= minimum and record.status == 'completed'), None)
        if end is None:
            if force:
                yield remaining
            return
        yield remaining[:end]
        remaining = remaining[end:]


def should_index_task(record: ConversationHistoryRecord) -> bool:
    """外层筛选只依据真实终态和准入标记，不再让MLLM判断任务是否值得记忆。"""
    if record.kind != 'task' or record.status not in TERMINAL_STATUSES:
        raise ValueError('归档筛选仅接受终态任务')
    return record.status not in {'rejected', 'expired'} and record.payload.get('agent_core_ready') is not False


class CommunicationArchiveService:
    def __init__(
        self, graph: ConversationMemoryGraph, history: ConversationHistoryService, *,
        scan_interval_seconds: float = 600, minimum_batch_size: int = 10,
        idle_scan_limit: int = 3, idle_seconds: float = 1800,
        processing_timeout_seconds: float = 600, task_concurrency: int | None = None,
    ) -> None:
        if any(not math.isfinite(v) or v <= 0 for v in (scan_interval_seconds, idle_seconds, processing_timeout_seconds)):
            raise ValueError('扫描、空闲与加工超时必须为有限正数')
        if any(type(v) is not int or v <= 0 for v in (minimum_batch_size, idle_scan_limit)):
            raise ValueError('批量大小与空闲扫描次数必须为正整数')
        task_concurrency = get_settings().embedding.max_concurrent_requests if task_concurrency is None else task_concurrency
        if type(task_concurrency) is not int or task_concurrency <= 0:
            raise ValueError('任务并发数必须为正整数')
        self.task_concurrency = task_concurrency
        self._task_slots = asyncio.Semaphore(task_concurrency)
        self._graph, self._history = graph, history
        self.scan_interval_seconds = scan_interval_seconds
        self.minimum_batch_size = minimum_batch_size
        self.idle_scan_limit, self.idle_seconds = idle_scan_limit, idle_seconds
        self.processing_timeout_seconds = processing_timeout_seconds
        self._worker: asyncio.Task | None = None
        self._jobs: dict[str, asyncio.Task] = {}
        self._scan_lock = asyncio.Lock()
        # 会话批次串行、批内任务有界并发；实际Embedding请求仍共用进程级额度。
        self._model_slot = asyncio.Semaphore(1)
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError('归档服务已关闭')
        if self._worker is None:
            self._worker = asyncio.create_task(self._loop(), name='communication-archive-scan')

    async def _loop(self):
        while True:
            await asyncio.sleep(self.scan_interval_seconds)
            try:
                await self.scan_once()
            except Exception as exc:
                logger.warning('会话归档扫描失败：error_type=%s', type(exc).__name__)

    async def scan_once(self) -> None:
        """一次扫描只派发作业，不等待模型；慢作业不阻止后续定时扫描。"""
        async with self._scan_lock:
            if self._closed:
                return
            candidates = await self._history.archive_candidates(
                interval=self.scan_interval_seconds, idle_limit=self.idle_scan_limit, idle_seconds=self.idle_seconds,
            )
            for candidate in candidates:
                cid = candidate.conversation_id
                previous = self._jobs.get(cid)
                if previous is not None and not previous.done():
                    continue
                job = asyncio.create_task(self._run_candidate(candidate), name=f'communication-archive:{cid}')
                self._jobs[cid] = job
                job.add_done_callback(lambda done, cid=cid: self._forget_job(cid, done))

    def _forget_job(self, cid, job):
        if self._jobs.get(cid) is job:
            del self._jobs[cid]

    async def _run_candidate(self, candidate: ArchiveCandidate):
        try:
            async with self._model_slot:
                records = await self._history.memory_backlog(candidate)
                for batch in partition_memory_tasks(records, minimum=self.minimum_batch_size, force=candidate.force):
                    async with asyncio.timeout(self.processing_timeout_seconds):
                        results = await asyncio.gather(*(self._process_task(r) for r in batch), return_exceptions=True)
                    # 等待本批在途任务清理后才报告失败；取消不转成普通任务失败。
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
                    await self._history.commit_archive(candidate, batch, tuple(r.retrieval for r in results))
                    logger.info('会话记忆归档成功：conversation_id=%s tasks=%s vectors=%s',
                                candidate.conversation_id, len(batch), sum(
                                    getattr(r.retrieval, area+'_embedding') is not None
                                    for r in results for area in ('user_input','intermediate_output','final_output')))
                if candidate.force and await self._history.evict_archived(candidate):
                    logger.info('空闲会话已安全驱逐：conversation_id=%s', candidate.conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('会话记忆归档未完成，保留内存：conversation_id=%s error_type=%s',
                           candidate.conversation_id, type(exc).__name__)

    async def _process_task(self, record: ConversationHistoryRecord) -> TaskMemoryOutput:
        original = record.model_copy(deep=True)
        if not should_index_task(original):
            return TaskMemoryOutput(execution_status='completed', record=original,
                                    retrieval=TaskRetrievalRecord(record_id=original.record_id))
        async with self._task_slots:
            raw = await self._graph.ainvoke({'request':original.model_copy(deep=True)})
        result = TaskMemoryOutput.model_validate(raw)
        if result.execution_status != 'completed' or result.record != original:
            raise ValueError('任务加工失败或改变了原任务内容/身份')
        return result

    async def wait_idle(self) -> None:
        """内部测试/运维入口：等待当前已派发作业，不触发额外扫描。"""
        await asyncio.gather(*tuple(self._jobs.values()), return_exceptions=True)

    async def close(self) -> None:
        async with self._scan_lock:
            self._closed = True
            tasks = ([self._worker] if self._worker is not None else []) + list(self._jobs.values())
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = None
        self._jobs.clear()
