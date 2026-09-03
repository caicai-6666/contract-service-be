"""本地资源文件读取接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

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


__all__ = ["router"]
