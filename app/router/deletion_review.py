"""合同删除审核查询：只读取当前用户提交或上传的合同所对应的申请。"""
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from app.router.dependency import ReviewerUserDependency
from app.schema.deletion_review import DeletionReviewDetailResponse, DeletionReviewListResponse
from app.service.deletion_review_query import DeletionReviewQueryService

router = APIRouter(prefix='/deletion-reviews', tags=['deletion-review'])
logger = logging.getLogger(__name__)


def get_query_service(request: Request) -> DeletionReviewQueryService:
    return request.app.state.deletion_review_query_service


QueryService = Annotated[DeletionReviewQueryService, Depends(get_query_service)]


@router.get('/list', response_model=DeletionReviewListResponse, summary='获取本人提交或上传合同的删除申请ID列表',
            responses={503: {'description': '删除申请列表暂时无法读取。'}})
async def list_deletion_reviews(service: QueryService, user_name: ReviewerUserDependency, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await service.list_records(user_name)
    except Exception:
        logger.exception('删除申请列表读取失败')
        raise HTTPException(503, '删除申请列表暂时无法读取，请稍后重试') from None


@router.get('/detail/{submission_id}', response_model=DeletionReviewDetailResponse, summary='按申请ID获取本人相关删除审核详情',
            responses={404: {'description': '申请不存在或不在当前用户查询范围内。'},
                       503: {'description': '删除申请详情暂时无法读取。'}})
async def deletion_review_detail(
    submission_id: Annotated[UUID, Path(description='删除申请列表中的 submission_id，不是合同 ID 或消息 ID。')],
    service: QueryService, user_name: ReviewerUserDependency, response: Response,
):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await service.detail(submission_id, user_name)
    except LookupError:
        raise HTTPException(404, '删除申请不存在或不在当前用户查询范围内') from None
    except Exception:
        logger.exception('删除申请详情读取失败')
        raise HTTPException(503, '删除申请详情暂时无法读取，请稍后重试') from None
