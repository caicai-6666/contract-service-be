"""审核用户登录接口。"""

from fastapi import APIRouter, HTTPException, status

from app.router.dependency import AuthServiceDependency
from app.schema.auth import LoginRequest, LoginResponse
from app.service.auth import InvalidReviewerSecretError

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post(
    "/login",
    response_model=LoginResponse,
    summary="使用审核用户密钥获取免登码",
)
async def login(
    payload: LoginRequest,
    service: AuthServiceDependency,
) -> LoginResponse:
    """校验唯一审核用户密钥并返回审核人名称与限时免登码。"""
    try:
        result = await service.login(payload.secret_key.get_secret_value())
    except InvalidReviewerSecretError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="审核用户密钥无效",
        ) from exc
    return LoginResponse(
        login_code=result.login_code,
        user_name=result.user_name,
    )


__all__ = ["router"]
