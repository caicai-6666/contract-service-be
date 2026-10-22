"""受保护接口统一查询中间件登录与当前平台管理员资格。"""
from typing import Annotated
from fastapi import Depends, Header, HTTPException, Request
from app.service.auth import AuthService, AuthenticationUnavailableError, PlatformAccessDeniedError
from app.schema.auth import AuthenticatedUser


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


async def require_platform_user(service: AuthServiceDependency,
    authorization: Annotated[str | None, Header(alias='Authorization',
        description='统一登录平台签发的用户 access_token，格式为 Bearer <access_token>。')] = None,
) -> AuthenticatedUser:
    parts = authorization.strip().split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise HTTPException(401, detail='用户访问令牌缺失或格式错误', headers={'WWW-Authenticate':'Bearer'})
    try:
        user = await service.resolve_user(parts[1])
    except AuthenticationUnavailableError as exc:
        raise HTTPException(503, detail=str(exc)) from None
    except PlatformAccessDeniedError as exc:
        raise HTTPException(403, detail=str(exc)) from None
    if user is None:
        raise HTTPException(401, detail='用户未登录或登录已失效', headers={'WWW-Authenticate':'Bearer'})
    return user


PlatformUserDependency = Annotated[AuthenticatedUser, Depends(require_platform_user)]


async def require_username(user: PlatformUserDependency) -> str:
    """操作人名称只用于展示，不作为用户所有权标识。"""
    return user.username


async def require_userid(user: PlatformUserDependency) -> str:
    return user.userid


UserNameDependency = Annotated[str, Depends(require_username)]
UserIdDependency = Annotated[str, Depends(require_userid)]
