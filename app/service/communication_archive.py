"""定时收集冻结任务，调用记忆图，提交检索数据并安全驱逐空闲会话。"""

import asyncio
import logging
import math
from typing import Any, Protocol

from app.agent.conversation_memory.state import MemoryGenerationInput, MemoryGenerationOutput, MemoryTaskInput
from app.schema.communication import ConversationHistoryRecord
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


class CommunicationArchiveService:
    def __init__(
        self, graph: ConversationMemoryGraph, history: ConversationHistoryService, *,
        scan_interval_seconds: float = 600, minimum_batch_size: int = 10,
        idle_scan_limit: int = 3, idle_seconds: float = 1800,
        processing_timeout_seconds: float = 600,
    ) -> None:
        if any(not math.isfinite(v) or v <= 0 for v in (scan_interval_seconds, idle_seconds, processing_timeout_seconds)):
            raise ValueError('扫描、空闲与加工超时必须为有限正数')
        if any(type(v) is not int or v <= 0 for v in (minimum_batch_size, idle_scan_limit)):
            raise ValueError('批量大小与空闲扫描次数必须为正整数')
        self._graph, self._history = graph, history
        self.scan_interval_seconds = scan_interval_seconds
        self.minimum_batch_size = minimum_batch_size
        self.idle_scan_limit, self.idle_seconds = idle_scan_limit, idle_seconds
        self.processing_timeout_seconds = processing_timeout_seconds
        self._worker: asyncio.Task | None = None
        self._jobs: dict[str, asyncio.Task] = {}
        self._scan_lock = asyncio.Lock()
        # 跨会话只执行一个图；图内仍使用既有 Send 并发配额，避免批间叠加压垮模型。
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
                    request = MemoryGenerationInput(tasks=tuple(MemoryTaskInput(
                        task_id=r.record_id, status=r.status, payload=r.payload,
                        created_at=r.created_at, activated_at=r.activated_at,
                        processing_duration_ms=r.processing_duration_ms,
                    ) for r in batch))
                    async with asyncio.timeout(self.processing_timeout_seconds):
                        raw = await self._graph.ainvoke({'request': request.model_copy(deep=True)})
                    result = MemoryGenerationOutput.model_validate(raw)
                    # 整批通过后才落库；部分失败不拆散原批的上下文，下一扫描重试原批。
                    if result.execution_status != 'summarized' or result.pending_records is None:
                        raise ValueError('记忆加工未完整成功')
                    if [r.task_id for r in result.pending_records] != [r.task_id for r in request.tasks]:
                        raise ValueError('记忆图返回任务范围或顺序错误')
                    await self._history.commit_archive(candidate, batch, result.pending_records)
                    logger.info('会话记忆归档成功：conversation_id=%s tasks=%s vectors=%s',
                                candidate.conversation_id, len(batch), sum(r.embedding is not None for r in result.pending_records))
                if candidate.force and await self._history.evict_archived(candidate):
                    logger.info('空闲会话已安全驱逐：conversation_id=%s', candidate.conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('会话记忆归档未完成，保留内存：conversation_id=%s error_type=%s',
                           candidate.conversation_id, type(exc).__name__)

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
