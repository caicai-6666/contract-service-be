"""路由聚合：健康检查公开，业务路由统一注入中间件用户访问验证。"""
from fastapi import APIRouter, Depends
from app.router import auth, communication, contract, health, resource, ingestion_review, deletion_review
from app.router.dependency import require_platform_user

router = APIRouter()
router.include_router(health.router)
# /auth/me 自身依赖同一验证器；不再注册任何本地登录或令牌签发接口。
router.include_router(auth.router)
for protected in (resource.router, contract.router, communication.router, ingestion_review.router, deletion_review.router):
    router.include_router(protected, dependencies=[Depends(require_platform_user)], responses={
        401: {'description':'用户访问令牌缺失、格式错误或登录已失效。'},
        403: {'description':'用户登录有效，但不属于本平台管理员。'},
        503: {'description':'平台会话、登录状态或管理员资格查询暂不可用。'},
    })

__all__ = ['router']
