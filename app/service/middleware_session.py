"""进程级中间件会话：后台登录、心跳续期和有限频率重试。"""
import asyncio
import logging
import time

import httpx

from app.core.config import Settings
from app.infrastructure.middleware import MiddlewareClient

logger = logging.getLogger(__name__)


class MiddlewareSessionService:
    def __init__(self, settings: Settings, *, client=None, clock=time.monotonic):
        self._settings = settings
        self._client = client
        self._clock = clock
        self._task = None
        self._token = None
        self._expires_at = 0.0
        self._closed = False

    async def start(self):
        if self._closed:
            raise RuntimeError('中间件会话服务已关闭')
        if self._task is not None:
            return
        s = self._settings
        if not (s.middleware_base_url and s.middleware_platform_code and s.middleware_secret.get_secret_value()):
            logger.info('中间件配置未完整填写，暂不启动平台连接')
            return
        # 启动只创建后台作业，不等待网络或登录成功。
        self._task = asyncio.create_task(self._run(), name='middleware-session')

    async def _run(self):
        s = self._settings
        while True:
            stage = 'heartbeat' if self._token is not None and self._clock() < self._expires_at else 'login'
            if stage == 'login':
                self._token = None
            try:
                if self._client is None:
                    self._client = MiddlewareClient(str(s.middleware_base_url),
                        timeout_seconds=s.middleware_request_timeout_seconds)
                started = self._clock()
                # 总超时覆盖完整调用，避免慢响应持续占用后台任务。
                async with asyncio.timeout(s.middleware_request_timeout_seconds):
                    if stage == 'login':
                        result = await self._client.login(s.middleware_platform_code, s.middleware_secret)
                    else:
                        result = await self._client.heartbeat(s.middleware_platform_code, self._token)
                if stage == 'login':
                    self._token = result.access_token
                    logger.info('中间件平台登录成功')
                # 从请求开始保守估计失效时刻，避免把网络耗时当成额外有效期。
                self._expires_at = started + result.expires_in_seconds
                delay = max(0, min(result.heartbeat_interval_seconds, self._expires_at - self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status == 401:
                    self._token = None
                # 不输出异常正文、请求体、URL 或令牌，避免凭据进入日志。
                logger.warning('中间件对接失败，将后台重试：stage=%s error_type=%s status=%s',
                    stage, type(exc).__name__, status)
                delay = s.middleware_retry_interval_seconds
                if self._token is not None:
                    delay = min(delay, max(0, self._expires_at - self._clock()))
            await asyncio.sleep(delay)

    def get_access_token(self):
        """只向内部服务提供有效令牌；读取不续期，禁止记录明文。"""
        if self._closed or self._token is None or self._clock() >= self._expires_at:
            return None
        return self._token

    def invalidate_access_token(self, token):
        """仅失效本次请求使用的令牌，避免迟到 401 清掉已经重登的新令牌。"""
        if self._token == token:
            self._token = None
            self._expires_at = 0.0

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._token = None
        if self._client is not None:
            await self._client.close()
            self._client = None
