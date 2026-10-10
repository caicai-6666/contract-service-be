"""定时清理已完成待审申请；不访问正式合同存储。"""
import asyncio
import logging
from datetime import datetime, UTC, timedelta

logger = logging.getLogger(__name__)


class IngestionReviewCleanupService:
    def __init__(self, store, settings, *, clock=None):
        self._store, self._settings = store, settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self):
        if self._closed:
            raise RuntimeError('待审清理服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='ingestion-review-cleanup')

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('待审清理扫描失败，将稍后重试：error_type=%s', type(exc).__name__)
            await asyncio.sleep(self._settings.ingestion_review_cleanup_interval_seconds)

    async def scan_once(self):
        async with self._lock:
            if self._closed:
                return 0
            cutoff = self._clock() - timedelta(seconds=self._settings.ingestion_review_retention_seconds)
            candidates = await asyncio.to_thread(self._store.list_cleanup_candidates, cutoff=cutoff,
                limit=self._settings.ingestion_review_cleanup_batch_size)
            cleaned = 0
            for sid in candidates:
                try:
                    # 关闭时等待当前删除事务退出，避免后台线程在服务生命周期外继续操作。
                    operation = asyncio.create_task(asyncio.to_thread(self._store.cleanup_completed, sid, cutoff=cutoff))
                    try:
                        removed = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        await asyncio.gather(operation, return_exceptions=True)
                        raise
                    if removed:
                        cleaned += 1
                        logger.info('已清理完成的待审申请：submission_id=%s', sid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 单份文件失败不能阻止本轮其他候选；保留记录，下轮重试。
                    logger.warning('待审申请清理失败，保留记录重试：submission_id=%s error_type=%s', sid, type(exc).__name__)
            return cleaned

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        # 同时等待外部手动触发的扫描，不留下尚未结束的文件删除。
        async with self._lock:
            pass
