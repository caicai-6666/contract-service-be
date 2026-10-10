"""HTTP 路由聚合入口。"""

from fastapi import APIRouter, Depends

from app.router import auth, communication, contract, health, resource, ingestion_review, deletion_review
from app.router.dependency import require_reviewer_user

router = APIRouter()
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(
    resource.router,
    dependencies=[Depends(require_reviewer_user)],
    responses={
        401: {
            "description": "免登码缺失、格式错误、无效或已经过期。",
        }
    },
)
router.include_router(
    contract.router,
    dependencies=[Depends(require_reviewer_user)],
    responses={
        401: {
            "description": "免登码缺失、格式错误、无效或已经过期。",
        }
    },
)

router.include_router(
    communication.router,
    dependencies=[Depends(require_reviewer_user)],
    responses={
        401: {
            "description": "免登码缺失、格式错误、无效或已经过期。",
        }
    },
)

router.include_router(
    ingestion_review.router,
    dependencies=[Depends(require_reviewer_user)],
    responses={401: {"description": "免登码缺失、无效或已过期。"}},
)

router.include_router(
    deletion_review.router,
    dependencies=[Depends(require_reviewer_user)],
    responses={401: {"description": "免登码缺失、无效或已过期。"}},
)

__all__ = ["router"]
