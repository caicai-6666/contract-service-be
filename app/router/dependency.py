"""跨业务路由复用的 FastAPI 依赖。"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.service.auth import AuthService
from app.user import ReviewerUser


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


async def require_authenticated_user(
    service: AuthServiceDependency,
    authorization: Annotated[
        str | None,
        Header(
            alias="Authorization",
            description="使用登录接口取得的 Bearer 免登码。",
        ),
    ] = None,
) -> ReviewerUser:
    """校验免登码，取得服务端用户身份及权限。"""
    login_code = _bearer_login_code(authorization)
    user = (
        await service.resolve_user(login_code)
        if login_code is not None
        else None
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="免登码无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


AuthenticatedUserDependency = Annotated[
    ReviewerUser, Depends(require_authenticated_user)
]


async def require_reviewer_user(user: AuthenticatedUserDependency) -> str:
    """保持原有所有者隔离接口的名称注入契约。"""
    return user.name


async def require_contract_add(user: AuthenticatedUserDependency) -> None:
    """新增的完整流程（含取消未入库任务）仅允许 1、2 级。"""
    if user.permission_level > 2:
        raise HTTPException(status_code=403, detail="当前用户没有新增合同权限")


async def require_contract_delete(user: AuthenticatedUserDependency) -> None:
    """正式合同删除仅允许 1 级，不与取消内存任务混淆。"""
    if user.permission_level != 1:
        raise HTTPException(status_code=403, detail="当前用户没有删除合同权限")


ReviewerUserDependency = Annotated[str, Depends(require_reviewer_user)]

__all__ = [
    "AuthenticatedUserDependency",
    "AuthServiceDependency",
    "ReviewerUserDependency",
    "get_auth_service",
    "require_reviewer_user",
    "require_authenticated_user",
    "require_contract_add",
    "require_contract_delete",
]
