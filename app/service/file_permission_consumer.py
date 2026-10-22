"""轮询文件权限调整，先提交当前等级及历史，再确认中间件消息。"""
import asyncio
import logging

import httpx

from app.infrastructure.middleware import MiddlewareClient

logger = logging.getLogger(__name__)


class FilePermissionAdjustmentConsumer:
    def __init__(self, store, session, settings, *, client=None):
        self._store, self._session, self._settings = store, session, settings
        self._client = client
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self):
        if self._closed:
            raise RuntimeError('文件等级调整消费者已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='file-permission-adjustment-consumer')

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不输出令牌、备注或远端正文；本地提交失败的消息留待下次处理。
                logger.warning('文件等级调整失败，将保留消息重试：error_type=%s status=%s',
                    type(exc).__name__, exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
            await asyncio.sleep(self._settings.file_permission_adjustment_poll_interval_seconds)

    async def scan_once(self):
        async with self._lock:
            if self._closed:
                return
            for _ in range(self._settings.file_permission_adjustment_batch_size):
                token = self._session.get_access_token()
                if token is None:
                    return
                if self._client is None:
                    self._client = MiddlewareClient(str(self._settings.middleware_base_url),
                        timeout_seconds=self._settings.middleware_request_timeout_seconds)
                delivery = await self._request(self._client.pull_file_permission_adjustment, token=token)
                if delivery is None:
                    return
                record = await asyncio.to_thread(self._store.apply_file_permission_adjustment,
                    delivery, platform_code=self._settings.middleware_platform_code)
                # 本地事务已完成，token 失效时也不回滚；下一次 pull 通过历史消息 ID 幂等恢复。
                token = self._session.get_access_token()
                if token is None:
                    return
                await self._request(self._client.ack_file_permission_adjustment, token=token,
                    message_id=record.message_id, offset=delivery.offset)
                await asyncio.to_thread(self._store.acknowledge_file_permission_adjustment, record.message_id)
                logger.info('文件等级调整已处理：message_id=%s status=%s', record.message_id, record.status)

    async def _request(self, method, *, token, **kwargs):
        try:
            async with asyncio.timeout(self._settings.middleware_request_timeout_seconds):
                return await method(token=token, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._session.invalidate_access_token(token)
            raise

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
