"""审核反馈轮询：本地持久化处理成功后才确认远端消息。"""
import asyncio
import logging

import httpx

from app.infrastructure.middleware import MiddlewareClient

logger = logging.getLogger(__name__)


class IngestionReviewConsumer:
    def __init__(self, store, session, ingestion_service, settings, *, client=None):
        self._store, self._session = store, session
        self._ingestion, self._settings = ingestion_service, settings
        self._client = client
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self):
        if self._closed:
            raise RuntimeError('审核反馈服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='ingestion-review-consumer')

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不记录响应正文、令牌或业务备注；异常消息不 ack、不跳过。
                logger.warning('审核反馈处理失败，保留消息稍后重试：error_type=%s status=%s',
                    type(exc).__name__, exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
            await asyncio.sleep(self._settings.ingestion_review_result_poll_interval_seconds)

    async def scan_once(self):
        async with self._lock:
            if self._closed:
                return
            for _ in range(self._settings.ingestion_review_result_batch_size):
                token = self._session.get_access_token()
                if token is None:
                    return
                if self._client is None:
                    self._client = MiddlewareClient(str(self._settings.middleware_base_url),
                        timeout_seconds=self._settings.middleware_request_timeout_seconds)
                delivery = await self._request(self._client.pull_review_result, token=token)
                if delivery is None:
                    return
                if delivery.message.target_platform_code != self._settings.middleware_platform_code:
                    raise ValueError('审核反馈目标平台不匹配')
                record = await asyncio.to_thread(self._store.receive_review, delivery)
                # 已清理申请返回最小完成凭据，同样跳过入库并直接 ack。
                if record.review_processed_at is None:
                    await self._process(record)
                # 入库可能耗时很长，必须重新读取心跳维护后的有效令牌。
                token = self._session.get_access_token()
                if token is None:
                    return
                await self._request(self._client.ack_review_result, token=token,
                    message_id=delivery.message.message_id, offset=delivery.offset)
                await asyncio.to_thread(self._store.update_review_processing, record.submission_id,
                    state='acknowledged')
                logger.info('审核反馈处理完成：submission_id=%s review_message_id=%s',
                    record.submission_id, delivery.message.message_id)

    async def _request(self, method, *, token, **kwargs):
        try:
            async with asyncio.timeout(self._settings.middleware_request_timeout_seconds):
                return await method(token=token, **kwargs)
        except httpx.HTTPStatusError as exc:
            # 只失效中间件请求的令牌；模型或正式存储的 401 不属于该会话。
            if exc.response.status_code == 401:
                self._session.invalidate_access_token(token)
            # 409 后重新 pull，不能盲目确认历史 ID；503 同样通过重投恢复。
            raise

    async def _process(self, record):
        sid = record.submission_id
        if record.passport:
            await asyncio.to_thread(self._store.update_review_processing, sid, state='ingesting')
            try:
                snapshot = record.snapshot
                pdf = await asyncio.to_thread(self._store.read_pdf, sid)
                await self._ingestion.ingest(document_id=snapshot.document_id,
                    processed_pdf_bytes=pdf, page_count=snapshot.page_count,
                    file_name=snapshot.file_name, summary=snapshot.summary,
                    uploader=snapshot.submitted_by, passport=record.passport,
                    classification=snapshot.classification, category_reasoning=snapshot.category_reasoning,
                    core=snapshot.core, clauses=snapshot.clauses,
                    retrieval_questions=snapshot.retrieval_questions,
                    question_fusion_vector=snapshot.question_fusion_vector,
                    page_fusion_vector=snapshot.page_fusion_vector)
            except asyncio.CancelledError:
                # ingesting 是可恢复状态；取消不等于外部数据库没有写入。
                raise
            except Exception as exc:
                await asyncio.to_thread(self._store.update_review_processing, sid,
                    state='failed', error_code=type(exc).__name__)
                raise
        await asyncio.to_thread(self._store.update_review_processing, sid, state='completed')

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
