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
        self._heartbeat_interval = None
        self._wake_up = asyncio.Event()
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
            self._wake_up.clear()
            token = self.get_access_token()
            stage = 'heartbeat' if token is not None else 'login'
            login_succeeded = False
            if stage == 'login':
                self._token = None
                self._expires_at = 0.0
            try:
                if self._client is None:
                    self._client = MiddlewareClient(str(s.middleware_base_url),
                        timeout_seconds=s.middleware_request_timeout_seconds)
                started = self._clock()
                # 总超时覆盖完整调用，避免慢响应持续占用后台任务。
                async with asyncio.timeout(s.middleware_request_timeout_seconds):
                    if stage == 'login':
                        result = await self._client.login(s.middleware_platform_code, s.middleware_secret)
                        login_succeeded = True
                        token = result.access_token
                        self._heartbeat_interval = result.heartbeat_interval_seconds
                        # 中间件先准备消息资源，再签发令牌。短 TTL 下登录耗时可能
                        # 已超过有效期，先用新令牌心跳确认，不能立即丢弃后反复登录 409。
                        if self._clock() >= started + result.expires_in_seconds:
                            started = self._clock()
                            result = await self._client.heartbeat(s.middleware_platform_code, token)
                    else:
                        result = await self._client.heartbeat(s.middleware_platform_code, token)
                self._heartbeat_interval = result.heartbeat_interval_seconds
                expires_at = started + result.expires_in_seconds
                if self._clock() >= expires_at:
                    raise TimeoutError('本次响应已超出可确认的会话有效期')
                # 消费或认证请求可能在心跳期间收到 401；迟到的成功心跳不能
                # 恢复已撤销令牌，也不能覆盖随后需要重新登录的状态。
                if stage == 'heartbeat' and self._token != token:
                    continue
                if stage == 'login':
                    self._token = token
                    logger.info('中间件平台登录成功')
                # 从请求开始保守估计失效时刻，避免把网络耗时当成额外有效期。
                self._expires_at = expires_at
                delay = max(0, min(result.heartbeat_interval_seconds, self._expires_at - self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status == 401:
                    self.invalidate_access_token(token)
                # 不输出异常正文、请求体、URL 或令牌，避免凭据进入日志。
                logger.warning('中间件对接失败，将后台重试：stage=%s error_type=%s status=%s',
                    stage, type(exc).__name__, status)
                delay = s.middleware_retry_interval_seconds
                # 已取得服务端周期后，恢复也跟随该周期，不要求人工同步短 TTL；
                # 尚无响应参数或登录凭证被拒绝时才使用本地失败重试兜底。
                credentials_rejected = stage == 'login' and not login_succeeded and status in (401, 403)
                if self._heartbeat_interval is not None and not credentials_rejected:
                    delay = min(delay, self._heartbeat_interval)
                if (stage == 'heartbeat' or login_succeeded) and status == 401:
                    delay = 0
                if self._token is not None:
                    delay = min(delay, max(0, self._expires_at - self._clock()))
            await self._wait_next_request(delay)

    async def _wait_next_request(self, delay):
        """按服务端周期等待；其他请求撤销令牌时立即唤醒唯一登录任务。"""
        if delay <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._wake_up.wait(), timeout=delay)
        except TimeoutError:
            pass

    def get_access_token(self):
        """只向内部服务提供有效令牌；读取不续期，禁止记录明文。"""
        if self._closed or self._token is None or self._clock() >= self._expires_at:
            return None
        return self._token

    def invalidate_access_token(self, token):
        """仅失效本次请求使用的令牌，避免迟到 401 清掉已经重登的新令牌。"""
        if token is not None and self._token == token:
            self._token = None
            self._expires_at = 0.0
            self._wake_up.set()

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
