"""审核用户登录与免登码进程内缓存。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from time import monotonic

from app.user import ReviewerUserCatalog


@dataclass(frozen=True, slots=True)
class LoginCodeEntry:
    """一个免登码对应的审核人名称和单调时钟过期点。"""

    user_name: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class AuthLoginResult:
    """登录成功后返回给接口层的审核人身份与免登码。"""

    login_code: str
    user_name: str


class LoginCodeCache:
    """并发安全的进程内“免登码 → 审核人名称”限时缓存。"""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        clock: Callable[[], float] = monotonic,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("免登码存活时间必须大于 0 秒")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._code_factory = code_factory or (lambda: token_urlsafe(32))
        self._entries: dict[str, LoginCodeEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> int:
        """返回每个新免登码的固定存活秒数。"""
        return self._ttl_seconds

    def _remove_expired_locked(self, now: float) -> None:
        """在已持锁条件下清除全部过期条目。"""
        expired_codes = [
            code
            for code, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for code in expired_codes:
            del self._entries[code]

    async def issue(self, user_name: str) -> str:
        """为审核人签发唯一免登码并写入缓存。"""
        if not user_name.strip():
            raise ValueError("免登码必须关联非空审核人名称")
        async with self._lock:
            now = self._clock()
            self._remove_expired_locked(now)
            for _ in range(10):
                login_code = self._code_factory()
                if login_code and login_code not in self._entries:
                    self._entries[login_code] = LoginCodeEntry(
                        user_name=user_name,
                        expires_at=now + self._ttl_seconds,
                    )
                    return login_code
        raise RuntimeError("连续生成重复或空免登码，无法完成签发")

    async def resolve(self, login_code: str) -> str | None:
        """返回未过期免登码对应的审核人名称，并刷新其过期点。"""
        async with self._lock:
            now = self._clock()
            self._remove_expired_locked(now)
            entry = self._entries.get(login_code)
            if entry is None:
                return None
            self._entries[login_code] = LoginCodeEntry(
                user_name=entry.user_name,
                expires_at=now + self._ttl_seconds,
            )
            return entry.user_name

    async def snapshot(self) -> dict[str, str]:
        """返回当前未过期的“免登码 → 审核人名称”只读副本。"""
        async with self._lock:
            self._remove_expired_locked(self._clock())
            return {
                code: entry.user_name for code, entry in self._entries.items()
            }


class InvalidReviewerSecretError(ValueError):
    """提交的密钥未对应任何审核用户。"""


class AuthService:
    """校验审核用户密钥并签发限时免登码。"""

    def __init__(
        self,
        *,
        reviewer_users: ReviewerUserCatalog,
        login_code_cache: LoginCodeCache,
    ) -> None:
        self._reviewer_users = reviewer_users
        self._login_code_cache = login_code_cache

    @property
    def login_code_ttl_seconds(self) -> int:
        """返回免登码存活秒数。"""
        return self._login_code_cache.ttl_seconds

    async def login(self, secret_key: str) -> AuthLoginResult:
        """仅凭密钥识别审核人，并返回其名称与新签发的免登码。"""
        user = self._reviewer_users.find_by_secret_key(secret_key)
        if user is None:
            raise InvalidReviewerSecretError("审核用户密钥无效")
        login_code = await self._login_code_cache.issue(user.name)
        return AuthLoginResult(login_code=login_code, user_name=user.name)

    async def resolve_user_name(self, login_code: str) -> str | None:
        """解析仍然有效的免登码。"""
        return await self._login_code_cache.resolve(login_code)


__all__ = [
    "AuthLoginResult",
    "AuthService",
    "InvalidReviewerSecretError",
    "LoginCodeCache",
    "LoginCodeEntry",
]
