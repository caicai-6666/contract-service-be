"""本地资源文件读取接口。"""

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from app.router.contract import ContractExtractionServiceDependency
from app.router.communication import HistoryDependency
from app.router.dependency import ReviewerUserDependency, AuthenticatedUserDependency
from app.service.communication_history import ConversationFileNotFoundError
from app.service.contract_extraction.registry import RunNotFoundError

from app.infrastructure.contract_file_store import (
    ContractFileNotFoundError,
    InvalidContractFileAddressError,
    LocalContractFileStore,
)

router = APIRouter(prefix="/resource", tags=["resource"])


def get_contract_file_store(request: Request) -> LocalContractFileStore:
    """从应用生命周期中取得本地合同文件存储。"""
    return request.app.state.contract_file_store


ContractFileStoreDependency = Annotated[
    LocalContractFileStore,
    Depends(get_contract_file_store),
]


@router.get(
    "/contract",
    response_class=FileResponse,
    summary="根据文件地址读取正式合同 PDF",
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "data/contract 中与文件地址对应的 PDF。",
        },
        400: {"description": "文件地址不符合本地合同文件协议。"},
        404: {"description": "合同 PDF 不存在。"},
    },
)
async def get_contract_file(
    file_uri: Annotated[
        str,
        Query(
            min_length=1,
            max_length=1024,
            description=(
                "Elasticsearch 合同文档中的 file_uri；当前必须形如 "
                "/<document_id>.pdf。"
            ),
        ),
    ],
    store: ContractFileStoreDependency,
) -> FileResponse:
    """安全解析 `file_uri`，并以内联方式流式返回对应 PDF。"""
    try:
        path = store.resolve(file_uri)
    except InvalidContractFileAddressError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ContractFileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="合同文件不存在",
        ) from exc

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get(
    "/extraction-pdf/{file_id}",
    response_class=StreamingResponse,
    summary="按 UUID 读取本人提取任务的内存处理版 PDF",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "渲染、压缩并重新封装后的 PDF，不是原始上传文件。"},
        404: {"description": "处理版 PDF 不存在、任务已释放或不属于当前用户。"},
        422: {"description": "file_id 不是合法 UUID。"},
    },
)
async def get_extraction_pdf(
    file_id: UUID,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> StreamingResponse:
    """UUID 仅定位资源，归属仍由服务端校验；不向前端暴露页面缓存或服务器路径。"""
    try:
        metadata, content = await service.get_processed_pdf(
            str(file_id), reviewer_user_name=reviewer_user_name,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="处理版 PDF 不存在或已经释放") from exc

    async def chunks():
        # 仅持有不可变字节引用；不创建第二份完整文件副本，也不让慢连接占用聚合锁。
        for offset in range(0, len(content), 64 * 1024):
            yield content[offset:offset + 64 * 1024]

    return StreamingResponse(
        chunks(), media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(metadata.file_name, safe='')}",
            "Content-Length": str(len(content)),
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    '/conversations/{conversation_id}/files/{file_id}',
    response_class=StreamingResponse,
    summary='读取本人已驻留任务轨迹中获准使用的会话 PDF 附件',
    responses={
        200: {'content': {'application/pdf': {}}, 'description': '原始上传 PDF，优先内存，归档后读取磁盘。'},
        401: {'description': '免登码缺失、无效或过期。'},
        404: {'description': '附件不存在、所属任务未驻留、不属于本人或附件未获准使用。'},
        422: {'description': 'conversation_id 长度不合法或 file_id 不是合法 UUID。'},
    },
)
async def get_communication_pdf(
    conversation_id: Annotated[str, Path(min_length=1, max_length=128, description='文件所属会话 ID，须属于当前用户且已驻留；不自动加载历史。')],
    file_id: Annotated[UUID, Path(description='会话任务轨迹 input.files 中的 file_id；所属任务必须已驻留且附件已获准使用。')],
    history: HistoryDependency, user: AuthenticatedUserDependency,
) -> StreamingResponse:
    """文件 UUID 只定位资源，密钥归属及驻留准入状态由服务端检查。"""
    try:
        name, content = await history.read_file(conversation_id, str(file_id), secret_key=user.secret_key)
    except ConversationFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='会话附件不存在或不可用') from exc

    async def chunks():
        # 传输不持有会话锁；删除/驱逐后的新请求失败，已授权的在途响应允许结束。
        for offset in range(0, len(content), 64 * 1024):
            yield content[offset:offset + 64 * 1024]

    return StreamingResponse(chunks(), media_type='application/pdf', headers={
        'Content-Disposition': f"inline; filename*=UTF-8''{quote(name, safe='')}",
        'Content-Length': str(len(content)), 'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    })


__all__ = ["router"]
