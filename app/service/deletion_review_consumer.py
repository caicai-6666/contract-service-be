"""删除审批反馈消费：先落本地结论并完成删除或拒绝恢复，再确认远端消息。"""
import asyncio
import logging

import httpx

from app.infrastructure.middleware import MiddlewareClient
from app.schema.deletion_review import DeletionReviewFeedback, ProcessedDeletionReviewReceipt

logger = logging.getLogger(__name__)


class DeletionReviewConsumer:
    def __init__(self, review_service, session, executor, settings, *, client=None):
        self._review, self._session = review_service, session
        self._executor, self._settings, self._client = executor, settings, client
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self):
        if self._closed:
            raise RuntimeError('删除审批反馈服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='deletion-review-consumer')

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('删除审批反馈处理失败，保留消息稍后重试：error_type=%s status=%s',
                    type(exc).__name__, exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
            await asyncio.sleep(self._settings.deletion_review_result_poll_interval_seconds)

    async def _request(self, method, *, token, **kwargs):
        try:
            async with asyncio.timeout(self._settings.middleware_request_timeout_seconds):
                return await method(token=token, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._session.invalidate_access_token(token)
            raise

    async def scan_once(self):
        async with self._lock:
            if self._closed:
                return
            for _ in range(self._settings.deletion_review_result_batch_size):
                token = self._session.get_access_token()
                if token is None:
                    return
                if self._client is None:
                    self._client = MiddlewareClient(str(self._settings.middleware_base_url),
                        timeout_seconds=self._settings.middleware_request_timeout_seconds)
                delivery = await self._request(self._client.pull_deletion_result, token=token)
                if delivery is None:
                    return
                message = delivery.message
                if message.target_platform_code != self._settings.middleware_platform_code:
                    raise ValueError('删除审批反馈目标平台不匹配')
                feedback = DeletionReviewFeedback(review_message_id=message.message_id,
                    approved=message.approved, reviewed_by=message.reviewer,
                    review_note=message.note, reviewed_at=message.created_at)
                record = await self._review.save_review_result(message_id=message.request_id, feedback=feedback)
                if not isinstance(record, ProcessedDeletionReviewReceipt) and record.review_processed_at is None:
                    if message.approved:
                        # 与原后台扫描复用执行器的互斥锁、领取及失败退避，不另写删除逻辑。
                        await self._executor.scan_once()
                        # 再次核验反馈并读取最新结果，同时兼容清理服务已生成的最小回执。
                        record = await self._review.save_review_result(message_id=message.request_id, feedback=feedback)
                    if record.review_processed_at is None:
                        # 执行失败、尚在退避或本轮有较早申请待处理时，保留队首反馈，下轮再拉。
                        return
                # 删除可能耗时较长，不能沿用 pull 时的令牌；心跳可能已经重新登录。
                token = self._session.get_access_token()
                if token is None:
                    return
                await self._request(self._client.ack_deletion_result, token=token,
                    message_id=message.message_id, offset=delivery.offset)
                logger.info('删除审批反馈处理完成：submission_id=%s review_message_id=%s',
                    record.submission_id, message.message_id)

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        async with self._lock:
            if self._client is not None:
                await self._client.close()
                self._client = None
