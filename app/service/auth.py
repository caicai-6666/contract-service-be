"""用户认证：有效登录并属于本平台管理员集合才放行，不签发或缓存用户令牌。"""
import asyncio
import logging
import httpx
from pydantic import SecretStr
from app.infrastructure.middleware import MiddlewareClient
from app.schema.auth import AuthenticatedUser

logger = logging.getLogger(__name__)


class AuthenticationUnavailableError(RuntimeError):
    """平台会话或远端认证依赖不可用，不能据此认定用户已退出。"""


class PlatformAccessDeniedError(PermissionError):
    """用户登录有效，但不是当前平台管理员。"""


class AuthService:
    def __init__(self, session, settings, *, client=None):
        self._session, self._settings, self._client = session, settings, client

    async def _request(self, method, *, token, **kwargs):
        try:
            async with asyncio.timeout(self._settings.middleware_request_timeout_seconds):
                return await method(token=token, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if status == 401:
                self._session.invalidate_access_token(token)
            # 不输出请求、响应、URL 或底层异常正文，避免用户和平台令牌泄漏。
            logger.warning('用户认证依赖不可用：error_type=%s status=%s', type(exc).__name__, status)
            raise AuthenticationUnavailableError('用户认证服务暂时不可用，请稍后重试') from None

    def _token(self):
        token = self._session.get_access_token()
        if token is None:
            raise AuthenticationUnavailableError('平台认证连接尚未就绪，请稍后重试')
        return token

    async def resolve_user(self, access_token: str) -> AuthenticatedUser | None:
        token = self._token()
        if self._client is None:
            self._client = MiddlewareClient(str(self._settings.middleware_base_url),
                timeout_seconds=self._settings.middleware_request_timeout_seconds)
        login = await self._request(self._client.query_user_login, token=token,
            user_token=SecretStr(access_token))
        if not login.is_logged_in:
            return None
        # 不持有跨请求的授权快照；每次查询使权限撤销在下一次访问时生效。
        admins = await self._request(self._client.list_platform_admins, token=self._token())
        if login.userid not in admins:
            raise PlatformAccessDeniedError('当前用户无权访问本平台')
        return AuthenticatedUser(userid=login.userid, username=login.username, departments=login.departments, ttl=login.ttl)

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None
