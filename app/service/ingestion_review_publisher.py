"""定时上传待审快照；依托中间件 source_id 去重，对不确定结果延迟重试。"""
import asyncio
import logging

from pydantic import ValidationError

from app.infrastructure.middleware import MiddlewareClient, MiddlewarePublishFields, MiddlewarePublishOutcome
from app.infrastructure.ingestion_review_store import IngestionReviewFileError

logger = logging.getLogger(__name__)


class IngestionReviewPublisher:
    def __init__(self, store, session, settings, *, client=None):
        self._store, self._session, self._settings = store, session, settings
        self._client = client
        self._task = None
        self._closed = False
        self._scan_lock = asyncio.Lock()
        # 已有远端结果但本地落盘暂时失败，只重试落盘，不重复 HTTP 上传。
        self._receipts = {}

    async def start(self):
        if self._closed:
            raise RuntimeError('待审发送服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='ingestion-review-publisher')

    async def _run(self):
        recovered = False
        while True:
            try:
                if not recovered:
                    await self._write(self._store.recover_interrupted_publications,
                        retry_seconds=self._settings.ingestion_review_publish_retry_seconds)
                    recovered = True
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('待审发送扫描失败，将稍后重试：error_type=%s', type(exc).__name__)
            await asyncio.sleep(self._settings.ingestion_review_scan_interval_seconds)

    @staticmethod
    async def _write(operation, *args, **kwargs):
        # 停止服务时等待 SQLite 线程结束，避免恢复扫描与旧写入交错。
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _flush_receipt(self, sid):
        result = self._receipts[sid]
        await self._write(self._store.finish_publication, sid, state=result.state,
            message_id=result.message_id, error_code=result.error_code,
            retry_seconds=self._settings.ingestion_review_publish_retry_seconds)
        self._receipts.pop(sid, None)
        logger.info('待审消息发送处理完成：submission_id=%s state=%s error_code=%s',
                    sid, result.state, result.error_code)

    async def scan_once(self):
        async with self._scan_lock:
            if self._closed:
                return
            for sid in tuple(self._receipts):
                await self._flush_receipt(sid)
            for _ in range(self._settings.ingestion_review_publish_batch_size):
                token = self._session.get_access_token()
                if token is None:
                    return
                record = await self._write(self._store.claim_next_publication)
                if record is None:
                    return
                sid = record.submission_id
                try:
                    result = await self._publish(record, token)
                    self._receipts[sid] = result
                    if result.unauthorized:
                        self._session.invalidate_access_token(token)
                    await self._flush_receipt(sid)
                    if result.unauthorized:
                        return
                except asyncio.CancelledError:
                    # 取消期间保留明确回执；否则保存不确定状态与下一次重试时间。
                    self._receipts.setdefault(sid, MiddlewarePublishOutcome(
                        state='uncertain', error_code='publication_cancelled'))
                    try:
                        await self._flush_receipt(sid)
                    except Exception:
                        logger.warning('待审取消状态未落盘，重启后将恢复重试：submission_id=%s', sid)
                    raise

    async def _publish(self, record, token):
        snapshot = record.snapshot
        try:
            fields = MiddlewarePublishFields(name=snapshot.file_name, abstract=snapshot.summary,
                source_id=snapshot.document_id, note=record.note, reviewer=snapshot.submitted_by)
            data = await asyncio.to_thread(self._store.read_pdf, record.submission_id)
        except (ValidationError, IngestionReviewFileError, OSError, ValueError):
            return MiddlewarePublishOutcome(state='blocked', error_code='invalid_local_submission')
        try:
            if self._client is None:
                self._client = MiddlewareClient(str(self._settings.middleware_base_url),
                    timeout_seconds=self._settings.ingestion_review_publish_timeout_seconds)
            return await self._client.publish_ingestion(token=token, fields=fields, pdf_bytes=data,
                timeout_seconds=self._settings.ingestion_review_publish_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            return MiddlewarePublishOutcome(state='uncertain', error_code='unexpected_publish_error')

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        async with self._scan_lock:
            if self._client is not None:
                await self._client.close()
                self._client = None
