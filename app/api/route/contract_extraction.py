"""合同提取上传、状态快照、SSE 进度与分支重试接口。"""

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

from app.service.contract_extraction.model import (
    ContractExtractionEvent,
    ContractExtractionSnapshot,
    EventType,
    RetryableStageCode,
    StageCode,
)
from app.service.contract_extraction.registry import RunNotFoundError
from app.service.contract_extraction.service import (
    ContractExtractionService,
    StageRetryError,
)

router = APIRouter(prefix="/extraction-runs", tags=["contract extraction"])


def get_contract_extraction_service(
    request: Request,
) -> ContractExtractionService:
    """从应用生命周期中取得唯一的进程内任务服务。"""
    return request.app.state.contract_extraction_service


ContractExtractionServiceDependency = Annotated[
    ContractExtractionService,
    Depends(get_contract_extraction_service),
]


@router.post(
    "",
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
            description="原始 PDF 二进制；本轮处理期间仅保存在服务进程内存中。",
        ),
    ],
    service: ContractExtractionServiceDependency,
) -> ContractExtractionSnapshot:
    """为一份内存 PDF 建立任务；正式文件校验将在后续阶段补充。"""
    return await service.create_run(file_name=file_name, pdf_bytes=pdf_bytes)


@router.get(
    "/{run_id}",
    response_model=ContractExtractionSnapshot,
    summary="获取合同提取的最新状态与草稿",
)
async def get_contract_extraction_run(
    run_id: str,
    service: ContractExtractionServiceDependency,
) -> ContractExtractionSnapshot:
    """获取全量当前快照；SSE 事件不承担完整结果传输。"""
    try:
        return await service.get_snapshot(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在或已经过期",
        ) from exc


@router.get(
    "/{run_id}/events",
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
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    """回放断线后的缓冲事件，并持续发送实时事件和心跳。"""
    after_sequence = _parse_last_event_id(last_event_id)
    try:
        subscription = service.subscribe_events(
            run_id,
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
                if event.event_type is EventType.RUN_EXPIRED:
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
                if event.event_type is EventType.RUN_EXPIRED:
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
    "/{run_id}/stages/{stage_code}/retry",
    response_model=ContractExtractionSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
    summary="单独重试一个合同处理阶段",
)
async def retry_contract_extraction_stage(
    run_id: str,
    stage_code: RetryableStageCode,
    service: ContractExtractionServiceDependency,
) -> ContractExtractionSnapshot:
    """接受分支重试；旧成功结果在新结果提交前始终可查询。"""
    try:
        return await service.retry_stage(run_id, StageCode(stage_code.value))
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
