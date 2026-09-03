"""跨业务路由复用的 FastAPI 依赖。"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.service.auth import AuthService


def get_auth_service(request: Request) -> AuthService:
    """从应用生命周期中取得进程内认证服务。"""
    return request.app.state.auth_service


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def _bearer_login_code(authorization: str | None) -> str | None:
    """解析严格的 `Authorization: Bearer <免登码>` 请求头。"""
    if authorization is None:
        return None
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1] or None


async def require_reviewer_user(
    service: AuthServiceDependency,
    authorization: Annotated[
        str | None,
        Header(
            alias="Authorization",
            description="使用登录接口取得的 Bearer 免登码。",
        ),
    ] = None,
) -> str:
    """校验免登码并向受保护接口注入审核人名称。"""
    login_code = _bearer_login_code(authorization)
    user_name = (
        await service.resolve_user_name(login_code)
        if login_code is not None
        else None
    )
    if user_name is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="免登码无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_name


ReviewerUserDependency = Annotated[str, Depends(require_reviewer_user)]

__all__ = [
    "AuthServiceDependency",
    "ReviewerUserDependency",
    "get_auth_service",
    "require_reviewer_user",
]
