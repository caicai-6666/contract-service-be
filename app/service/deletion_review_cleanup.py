"""定时清理已完成的删除审核申请；不删除正式合同或其文件引用。"""
import asyncio
import logging
from datetime import datetime, UTC, timedelta

logger = logging.getLogger(__name__)


class DeletionReviewCleanupService:
    def __init__(self, store, review_service, settings, *, clock=None):
        self._store, self._review, self._settings = store, review_service, settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self):
        if self._closed:
            raise RuntimeError('删除审核清理服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='deletion-review-cleanup')

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('删除审核清理扫描失败，将稍后重试：error_type=%s', type(exc).__name__)
            await asyncio.sleep(self._settings.deletion_review_cleanup_interval_seconds)

    async def scan_once(self):
        async with self._lock:
            if self._closed:
                return 0
            cutoff = self._clock() - timedelta(seconds=self._settings.deletion_review_retention_seconds)
            candidates = await asyncio.to_thread(self._store.list_cleanup_candidates, cutoff=cutoff,
                limit=self._settings.deletion_review_cleanup_batch_size)
            cleaned = 0
            for sid in candidates:
                if self._closed:
                    break
                try:
                    # 退出时等待当前回执/审核行事务结束，再停止正式删除执行器。
                    operation = asyncio.create_task(self._review.cleanup_completed(sid, cutoff=cutoff))
                    try:
                        removed = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        await asyncio.gather(operation, return_exceptions=True)
                        raise
                    if removed:
                        cleaned += 1
                        logger.info('已清理完成的删除审核申请：submission_id=%s', sid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning('删除审核申请清理失败，保留记录重试：submission_id=%s error_type=%s',
                        sid, type(exc).__name__)
            return cleaned

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        async with self._lock:
            pass
