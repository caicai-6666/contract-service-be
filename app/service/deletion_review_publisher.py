"""定时发布删除审核申请；保留不确定回执，依托中间件去重延迟重试。"""
import asyncio
import hashlib
import logging

from pydantic import ValidationError

from app.infrastructure.contract_file_store import ContractFileNotFoundError, InvalidContractFileAddressError
from app.infrastructure.middleware import MiddlewareClient, MiddlewareDeletionPublishFields, MiddlewarePublishOutcome

logger = logging.getLogger(__name__)


class DeletionReviewPublisher:
    def __init__(self, store, session, metadata_store, file_store, settings, *, lock_documents, client=None):
        self._store, self._session = store, session
        self._metadata, self._files = metadata_store, file_store
        self._settings, self._lock_documents, self._client = settings, lock_documents, client
        self._task = None
        self._closed = False
        self._scan_lock = asyncio.Lock()
        self._receipts = {}

    async def start(self):
        if self._closed:
            raise RuntimeError('删除送审服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='deletion-review-publisher')

    @staticmethod
    async def _write(operation, *args, **kwargs):
        # 取消协程不能中断 SQLite 线程；等待事务完成再交还控制，避免重启恢复与旧线程交错。
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _run(self):
        recovered = False
        while True:
            try:
                if not recovered:
                    await self._write(self._store.recover_interrupted_publications,
                        retry_seconds=self._settings.deletion_review_publish_retry_seconds)
                    recovered = True
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('删除送审扫描失败，将重试：error_type=%s', type(exc).__name__)
            await asyncio.sleep(self._settings.deletion_review_publish_scan_interval_seconds)

    async def _flush_receipt(self, sid):
        result = self._receipts[sid]
        await self._write(self._store.finish_publication, sid, state=result.state,
            message_id=result.message_id, error_code=result.error_code,
            retry_seconds=self._settings.deletion_review_publish_retry_seconds)
        self._receipts.pop(sid, None)
        logger.info('删除送审处理完成：submission_id=%s state=%s error_code=%s', sid, result.state, result.error_code)

    async def scan_once(self):
        async with self._scan_lock:
            if self._closed:
                return
            for sid in tuple(self._receipts):
                # 单条回执落盘失败不阻塞其他合同；原记录保持 publishing，绝不重复上传。
                try:
                    await self._flush_receipt(sid)
                except Exception as exc:
                    logger.warning('删除送审回执保存失败：submission_id=%s error_type=%s', sid, type(exc).__name__)
            for _ in range(self._settings.deletion_review_publish_batch_size):
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
                    try:
                        await self._flush_receipt(sid)
                    except Exception as exc:
                        logger.warning('删除送审回执保存失败：submission_id=%s error_type=%s', sid, type(exc).__name__)
                    if result.unauthorized:
                        return
                except asyncio.CancelledError:
                    self._receipts.setdefault(sid, MiddlewarePublishOutcome(
                        state='uncertain', error_code='publication_cancelled'))
                    try:
                        await self._flush_receipt(sid)
                    except Exception:
                        logger.warning('删除送审取消状态未保存，重启后恢复重试：submission_id=%s', sid)
                    raise

    async def _prepare_pdf(self, record):
        def read():
            metadata = self._metadata.get(record.document_id)
            # 不上传同哈希后来重新入库的版本，也不在本地锁定失败时先发布外部申请。
            if metadata is None or (metadata.ingested_at != record.contract_ingested_at
                    or metadata.passport != record.passport or metadata.file_uri != record.file_uri
                    or metadata.status != 'ready' or metadata.can_delete):
                raise ValueError('原合同身份或删除锁定状态不匹配')
            data = self._files.resolve(record.file_uri).read_bytes()
            if hashlib.sha256(data).hexdigest() != record.document_id:
                raise ValueError('合同文件哈希不匹配')
            return data
        async with self._lock_documents(record.document_id):
            task = asyncio.create_task(asyncio.to_thread(read))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise

    async def _publish(self, record, token):
        try:
            fields = MiddlewareDeletionPublishFields(source_id=record.document_id, passport=record.passport,
                name=record.file_name, abstract=record.summary, uploader=record.uploader,
                applicant=record.requested_by, note=record.note)
            data = await self._prepare_pdf(record)
        except (ValidationError, ContractFileNotFoundError, InvalidContractFileAddressError, OSError, ValueError):
            return MiddlewarePublishOutcome(state='blocked', error_code='invalid_local_submission')
        except asyncio.CancelledError:
            raise
        except Exception:
            # 尚未开始 HTTP 上传，本地存储暂时故障可安全重试。
            return MiddlewarePublishOutcome(state='retry', error_code='local_read_unavailable')
        try:
            if self._client is None:
                self._client = MiddlewareClient(str(self._settings.middleware_base_url),
                    timeout_seconds=self._settings.deletion_review_publish_timeout_seconds)
            return await self._client.publish_deletion(token=token, fields=fields, pdf_bytes=data,
                timeout_seconds=self._settings.deletion_review_publish_timeout_seconds)
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
        # 外部调用 scan_once 也须完成取消处理或回执落盘，才能关闭 HTTP 连接。
        async with self._scan_lock:
            if self._client is not None:
                await self._client.close()
                self._client = None
