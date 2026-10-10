"""定时执行已批准的合同删除；成功后保存完成状态，失败时延迟重试。"""
import asyncio
import logging

from app.service.contract_ingestion import ContractDocumentNotFoundError

logger = logging.getLogger(__name__)


class DeletionReviewExecutor:
    def __init__(self, store, ingestion_service, settings, *, review_service=None):
        self._store, self._ingestion, self._settings = store, ingestion_service, settings
        self._review_service = review_service
        self._task = None
        self._closed = False
        self._recovered = False
        self._lock = asyncio.Lock()
        # 正式删除已结束但审核库暂不可写时，只重试记录结果，避免重复外部操作。
        self._outcomes = {}

    async def start(self):
        if self._closed:
            raise RuntimeError('删除审核执行服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='deletion-review-executor')

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('删除审核扫描失败，将稍后重试：error_type=%s', type(exc).__name__)
            await asyncio.sleep(self._settings.deletion_review_scan_interval_seconds)

    async def _flush_outcome(self, sid):
        succeeded, error_code = self._outcomes[sid]
        await asyncio.to_thread(self._store.finish_deletion, sid, succeeded=succeeded,
            error_code=error_code, retry_seconds=self._settings.deletion_review_retry_seconds)
        del self._outcomes[sid]

    async def _execute(self, record):
        sid = record.submission_id
        try:
            await self._ingestion.delete_document(record.document_id, reviewer=record.requested_by,
                expected_ingested_at=record.contract_ingested_at, expected_passport=record.passport)
        except ContractDocumentNotFoundError:
            # 正式库最后才移除元数据；它已不存在时视为之前的删除已完成。
            self._outcomes[sid] = (True, None)
        except Exception as exc:
            self._outcomes[sid] = (False, type(exc).__name__[:128])
            logger.warning('审核批准合同删除失败，保留申请重试：submission_id=%s error_type=%s', sid, type(exc).__name__)
        else:
            self._outcomes[sid] = (True, None)
        succeeded = self._outcomes[sid][0]
        await self._flush_outcome(sid)
        if succeeded:
            logger.info('审核批准合同删除完成：submission_id=%s document_id=%s', sid, record.document_id)
        return succeeded

    async def scan_once(self):
        async with self._lock:
            if self._closed:
                return 0
            if not self._recovered:
                await asyncio.to_thread(self._store.recover_interrupted_deletions)
                self._recovered = True
            if self._review_service is not None:
                await self._review_service.reconcile_flags()
            completed = 0
            for sid in tuple(self._outcomes):
                succeeded = self._outcomes[sid][0]
                await self._flush_outcome(sid)
                completed += int(succeeded)
            for _ in range(self._settings.deletion_review_batch_size):
                if self._closed:
                    break
                record = await asyncio.to_thread(self._store.claim_next_deletion)
                if record is None:
                    break
                # 关闭时等待当前文档删除及 SQLite 回执结束，再释放 ES/Neo4j 连接。
                operation = asyncio.create_task(self._execute(record))
                try:
                    succeeded = await asyncio.shield(operation)
                except asyncio.CancelledError:
                    await asyncio.gather(operation, return_exceptions=True)
                    raise
                completed += int(succeeded)
            return completed

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        async with self._lock:
            pass
