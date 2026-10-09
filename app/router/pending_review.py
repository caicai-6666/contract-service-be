"""待入库申请的只读查询与详情；统一使用平台登录认证。"""
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.schema.pending_review import PendingReviewCleanupPolicy, PendingReviewListResponse, PendingReviewDetailResponse
from app.service.pending_review_query import PendingReviewQueryService

router = APIRouter(prefix='/pending-reviews', tags=['pending-review'])
logger = logging.getLogger(__name__)


def get_query_service(request: Request) -> PendingReviewQueryService:
    return request.app.state.pending_review_query_service


QueryService = Annotated[PendingReviewQueryService, Depends(get_query_service)]


@router.get('/cleanup-policy', response_model=PendingReviewCleanupPolicy, summary='获取待审数据清理策略')
async def cleanup_policy(service: QueryService, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return service.cleanup_policy()


@router.get('/list', response_model=PendingReviewListResponse, summary='获取全部尚未清理的待入库申请ID')
async def list_pending_reviews(service: QueryService, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await service.list_records()
    except Exception:
        logger.exception('待入库申请列表读取失败')
        raise HTTPException(503, '待入库申请列表暂时无法读取，请稍后重试') from None


@router.get('/detail/{submission_id}', response_model=PendingReviewDetailResponse, summary='按申请ID获取待入库详情')
async def pending_review_detail(submission_id: UUID, service: QueryService, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await service.detail(submission_id)
    except LookupError:
        raise HTTPException(404, '待入库申请不存在或已清理') from None
    except Exception:
        logger.exception('待入库申请详情读取失败')
        raise HTTPException(503, '待入库申请详情暂时无法读取，请稍后重试') from None
