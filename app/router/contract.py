"""合同相关 HTTP/SSE 接口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldDefinitionCatalog,
)
from app.router.dependency import ReviewerUserDependency
from app.schema.contract import (
    ContractIngestionAuditResponse,
    ContractIngestionRequest,
    ContractIngestionResponse,
    CoreDefinitionCatalogResponse,
    project_core_definition_catalog,
)
from app.service.contract_ingestion import (
    ContractPersistenceError,
    ContractReviewValidationError,
)
from app.service.contract_extraction.model import (
    ContractExtractionEvent,
    ContractExtractionRunList,
    ContractExtractionSnapshot,
    EventType,
    StageCode,
)
from app.service.contract_extraction.registry import RunNotFoundError
from app.service.contract_extraction.service import (
    ContractExtractionService,
    RunConflictError,
    StageRetryError,
)
from app.service.pdf_preparation import PDFPreparationError

router = APIRouter(prefix="/contract", tags=["contract"])


def get_contract_extraction_service(
    request: Request,
) -> ContractExtractionService:
    """从应用生命周期中取得唯一的进程内任务服务。"""
    return request.app.state.contract_extraction_service


ContractExtractionServiceDependency = Annotated[
    ContractExtractionService,
    Depends(get_contract_extraction_service),
]


def get_field_definition_catalog(request: Request) -> FieldDefinitionCatalog:
    """从应用生命周期中取得启动时固定的 Core 定义目录。"""
    return request.app.state.field_definition_catalog


FieldDefinitionCatalogDependency = Annotated[
    FieldDefinitionCatalog,
    Depends(get_field_definition_catalog),
]


@router.get(
    "/core-definitions",
    response_model=CoreDefinitionCatalogResponse,
    summary="获取 Core 审核表单定义",
)
async def get_core_definitions(
    catalog: FieldDefinitionCatalogDependency,
) -> CoreDefinitionCatalogResponse:
    """返回字段基数、属性名称、类型与必填约束。"""
    return project_core_definition_catalog(catalog)


@router.post(
    "/extraction-runs",
    response_model=ContractExtractionSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传 PDF 并启动合同提取",
)
async def create_contract_extraction_run(
    file_name: Annotated[
        str,
        Query(
            min_length=1,
            max_length=255,
            description="前端展示使用的原始 PDF 文件名。",
        ),
    ],
    pdf_bytes: Annotated[
        bytes,
        Body(
            min_length=1,
            media_type="application/pdf",
            description="原始 PDF 二进制；仅在创建请求期间保存在服务进程内存中。",
        ),
    ],
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> ContractExtractionSnapshot:
    """异步检查并渲染内存 PDF，通过后建立后台提取任务。"""
    try:
        return await service.create_run(
            reviewer_user_name=reviewer_user_name,
            file_name=file_name,
            pdf_bytes=pdf_bytes,
        )
    except PDFPreparationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get(
    "/extraction-runs",
    response_model=ContractExtractionRunList,
    summary="列出尚未入库的可恢复合同处理任务",
)
async def list_contract_extraction_runs(
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> ContractExtractionRunList:
    """列出进行中或因等待人工操作而阻塞的可恢复 run_id。"""
    return await service.list_unpersisted_runs(
        reviewer_user_name=reviewer_user_name
    )


@router.get(
    "/extraction-runs/{run_id}",
    response_model=ContractExtractionSnapshot,
    summary="获取合同状态、建议名称与 Core/Clause 结果",
)
async def get_contract_extraction_run(
    run_id: str,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> ContractExtractionSnapshot:
    """获取当前状态、可恢复建议名称与 Core/Clause 结果。"""
    try:
        return await service.get_snapshot(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已经过期",
        ) from exc


@router.delete(
    "/extraction-runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="取消合同处理任务",
)
async def cancel_contract_extraction_run(
    run_id: str,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> Response:
    """取消当前用户的任务，并立即释放 PDF、中间结果和后台协程。"""
    try:
        await service.cancel_run(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已经结束",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/extraction-runs/{run_id}/continue",
    response_model=ContractExtractionSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
    summary="确认查重结果并继续合同提取",
)
async def continue_contract_extraction_run(
    run_id: str,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> ContractExtractionSnapshot:
    """消费查重暂停点；前端的候选处理动作通过其他接口独立完成。"""
    try:
        return await service.continue_run(
            run_id,
            reviewer_user_name=reviewer_user_name,
        )
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已经过期",
        ) from exc
    except RunConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get(
    "/extraction-runs/{run_id}/events",
    response_class=StreamingResponse,
    summary="订阅合同提取进度",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "用户可读的阶段状态事件流。",
        }
    },
)
async def stream_contract_extraction_events(
    run_id: str,
    request: Request,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    """回放断线后的缓冲事件，并持续发送实时事件和心跳。"""
    after_sequence = _parse_last_event_id(last_event_id)
    try:
        subscription = service.subscribe_events(
            run_id,
            reviewer_user_name=reviewer_user_name,
            after_sequence=after_sequence,
        )
        # 在返回 200 之前进入上下文，确保未知任务得到正常的 404，且注册与回放原子。
        replay, queue = await subscription.__aenter__()
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已经过期",
        ) from exc

    async def event_stream() -> AsyncIterator[str]:
        try:
            for event in replay:
                yield _format_event(event)
                if event.event_type in {
                    EventType.RUN_CANCELLED,
                    EventType.RUN_EXPIRED,
                    EventType.RUN_INGESTED,
                }:
                    return
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=service.sse_heartbeat_seconds,
                    )
                except TimeoutError:
                    yield _format_heartbeat(run_id)
                    continue
                if event is None:
                    return
                yield _format_event(event)
                if event.event_type in {
                    EventType.RUN_CANCELLED,
                    EventType.RUN_EXPIRED,
                    EventType.RUN_INGESTED,
                }:
                    return
        finally:
            await subscription.__aexit__(None, None, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/extraction-runs/{run_id}/stages/{stage_code}/retry",
    response_model=ContractExtractionSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
    summary="单独重试一个合同处理阶段",
)
async def retry_contract_extraction_stage(
    run_id: str,
    stage_code: StageCode,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> ContractExtractionSnapshot:
    """从失败阶段断点续跑，并复用此前已经成功的处理结果。"""
    try:
        return await service.retry_stage(
            run_id,
            stage_code,
            reviewer_user_name=reviewer_user_name,
        )
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已经过期",
        ) from exc
    except StageRetryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/extraction-runs/{run_id}/ingestion",
    response_model=ContractIngestionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="提交最终审核值并正式入库合同",
    responses={
        404: {"description": "任务不存在、已经过期、已经入库或不属于当前用户。"},
        409: {"description": "运行阶段或内部结果尚未满足正式入库条件。"},
        422: {"description": "最终文件名、Core 或 Clause 不符合入库契约。"},
        502: {"description": "SQLite、处理版 PDF 或 Elasticsearch 持久化失败。"},
    },
)
async def ingest_contract_extraction_run(
    run_id: str,
    payload: ContractIngestionRequest,
    service: ContractExtractionServiceDependency,
    reviewer_user_name: ReviewerUserDependency,
) -> ContractIngestionResponse:
    """按运行身份补齐分类、向量、PDF 和最终入库责任信息。"""
    try:
        result = await service.ingest_run(
            run_id,
            reviewer_user_name=reviewer_user_name,
            file_name=payload.file_name,
            core=payload.core,
            clauses=payload.clauses,
        )
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在、已经过期或已经入库",
        ) from exc
    except RunConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ContractReviewValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ContractPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return ContractIngestionResponse(
        status="ingested",
        document_id=result.document_id,
        file_name=result.file_name,
        file_uri=result.file_uri,
        page_count=result.page_count,
        ingestion=ContractIngestionAuditResponse(
            reviewer=result.reviewer,
            ingested_at=result.ingested_at,
        ),
    )


def _parse_last_event_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        sequence = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID 必须是非负整数",
        ) from exc
    if sequence < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID 必须是非负整数",
        )
    return sequence


def _format_event(event: ContractExtractionEvent) -> str:
    """按照 SSE 帧格式编码稳定业务事件。"""
    return (
        f"id: {event.sequence}\n"
        f"event: {event.event_type.value}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


def _format_heartbeat(run_id: str) -> str:
    """心跳不占用可恢复业务序列号。"""
    payload = json.dumps(
        {
            "run_id": run_id,
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: heartbeat\ndata: {payload}\n\n"


__all__ = ["router"]
