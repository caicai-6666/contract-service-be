"""多轮合同对话的 HTTP 路由入口，具体接口将在后续逐步实现。"""

import json
import sqlite3
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Path, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from anyio import CancelScope

from app.infrastructure.communication_store import SQLiteCommunicationStore
from app.user import ReviewerUser

from app.router.dependency import AuthenticatedUserDependency
from app.schema.communication import CommunicationEvent, CommunicationSnapshot, ConversationListItem, ConversationRenameRequest, TurnCreatedResponse, ConversationTaskHistoryResponse
from app.service.communication_history import ConversationHistoryService, ConversationNotResidentError
from app.service.communication import (
    ActivationExpiredError, CommunicationCapacityError, CommunicationEventService,
    CommunicationNotFoundError, ReplayUnavailableError, StagedPDF, TurnInput,
)


# 对话接口统一归属此路由；认证由路由聚合层装配，不在此处直接调用智能体。
router = APIRouter(prefix="/communication", tags=["communication"])

ConversationId = Annotated[str, Path(min_length=1, max_length=128, description="服务端创建并返回的会话标识，必须存在且属于当前用户。")]
TurnId = Annotated[str, Path(description="对话轮次标识，具体格式将在轮次功能实现时确定。")]


def get_communication_events(request: Request) -> CommunicationEventService:
    """从应用生命周期读取事件服务，不在路由中创建隐式轮次。"""
    return request.app.state.communication_event_service


EventServiceDependency = Annotated[CommunicationEventService, Depends(get_communication_events)]


def get_communication_store(request: Request) -> SQLiteCommunicationStore:
    return request.app.state.communication_store


StoreDependency = Annotated[SQLiteCommunicationStore, Depends(get_communication_store)]


def get_communication_history(request: Request) -> ConversationHistoryService:
    return request.app.state.communication_history_service


HistoryDependency = Annotated[ConversationHistoryService, Depends(get_communication_history)]


async def require_conversation_owner(
    conversation_id: ConversationId, store: StoreDependency, user: AuthenticatedUserDependency,
) -> str:
    try:
        await run_in_threadpool(store.read_conversation, conversation_id, secret_key=user.secret_key)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="会话存储暂时不可用") from exc
    return user.name


ConversationOwnerDependency = Annotated[str, Depends(require_conversation_owner)]


@router.get(
    "/conversations", response_model=list[ConversationListItem],
    summary="获取当前用户保留的全部会话",
    responses={401: {"description": "免登码无效或过期。"}, 503: {"description": "会话存储暂时不可用。"}},
)
async def list_conversations(store: StoreDependency, user: AuthenticatedUserDependency) -> list[dict]:
    """归属只取认证用户的密钥；在线程中读取持久化目录，不阻塞事件循环。"""
    try:
        return await run_in_threadpool(store.list_conversations, secret_key=user.secret_key)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="会话存储暂时不可用") from exc



@router.post(
    "/conversations/{conversation_id}/open", response_model=ConversationTaskHistoryResponse,
    summary="打开会话并加载最近摘要窗口",
    responses={401: {"description": "未登录。"}, 404: {"description": "会话不存在或不属于当前用户。"},
               409: {"description": "已冻结历史与持久化内容冲突。"},
               503: {"description": "会话存储暂时不可用。"}},
)
async def open_conversation(
    conversation_id: ConversationId, history: HistoryDependency, user: AuthenticatedUserDependency,
) -> ConversationTaskHistoryResponse:
    return await _load_history(history, conversation_id, user, refresh=False)


@router.post(
    "/conversations/{conversation_id}/refresh", response_model=ConversationTaskHistoryResponse,
    summary="向前加载至更早的摘要并返回驻留范围内的全部任务",
    responses={401: {"description": "未登录。"}, 404: {"description": "会话不存在或不属于当前用户。"},
               409: {"description": "会话尚未打开或历史内容冲突。"}, 503: {"description": "存储暂时不可用。"}},
)
async def refresh_conversation(
    conversation_id: ConversationId, history: HistoryDependency, user: AuthenticatedUserDependency,
) -> ConversationTaskHistoryResponse:
    return await _load_history(history, conversation_id, user, refresh=True)


async def _load_history(history: ConversationHistoryService, conversation_id: str,
                        user: ReviewerUser, *, refresh: bool) -> ConversationTaskHistoryResponse:
    try:
        method = history.refresh if refresh else history.open
        snapshot = await method(conversation_id, secret_key=user.secret_key)
        # 只过滤对外投影；内部摘要及加载/模型窗口边界不受展示规则影响。
        return ConversationTaskHistoryResponse(
            **snapshot.model_dump(exclude={"records"}),
            records=tuple(record.model_dump() for record in snapshot.records if record.kind == "task"),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ConversationNotResidentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="历史记录无法加载，请检查记录契约或冻结内容") from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="会话存储暂时不可用") from exc


@router.patch(
    "/conversations/{conversation_id}", response_model=ConversationListItem,
    summary="修改本人会话名称",
    responses={401: {"description": "未登录。"}, 404: {"description": "会话不存在或不属于当前用户。"},
               422: {"description": "名称不合法。"}, 503: {"description": "存储暂时不可用。"}},
)
async def rename_conversation(
    conversation_id: ConversationId, body: ConversationRenameRequest,
    store: StoreDependency, user: AuthenticatedUserDependency,
) -> dict:
    try:
        return await run_in_threadpool(store.rename_conversation, conversation_id,
                                       secret_key=user.secret_key, name=body.name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="会话存储暂时不可用") from exc


@router.delete(
    "/conversations/{conversation_id}", status_code=204, response_class=Response,
    summary="删除本人会话及其记录",
    responses={401: {"description": "未登录。"}, 404: {"description": "会话不存在或不属于当前用户。"},
               503: {"description": "存储暂时不可用。"}},
)
async def delete_conversation(
    conversation_id: ConversationId, history: HistoryDependency,
    user: AuthenticatedUserDependency,
) -> Response:
    try:
        await history.delete(conversation_id, secret_key=user.secret_key)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="会话存储暂时不可用") from exc
    return Response(status_code=204)


def _format_event(event: CommunicationEvent) -> str:
    payload = {
        "turn_id": event.turn_id,
        "created_at": event.created_at.isoformat(),
        **event.data.model_dump(mode="json", exclude={"event_type"}),
    }
    # JSON 编码转义正文中的换行，避免用户文本破坏 SSE 帧边界。
    return (
        f"id: {event.sequence}\nevent: {event.data.event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


@router.post(
    "/conversations/{conversation_id}/turns",
    status_code=status.HTTP_201_CREATED,
    response_model=TurnCreatedResponse,
    summary="暂存输入并创建待激活轮次",
    responses={
        404: {"description": "会话或被替代轮次不存在，或不属于当前用户。"},
        409: {"description": "已有活跃轮次未指定替代，或旧轮次已经结束。"},
        413: {"description": "文件数量、文件大小或上传总量超限。"},
        422: {"description": "输入为空、文件为空或文件名不是 PDF。"},
        503: {"description": "内存暂存容量已满，请稍后重试。"},
    },
)
async def create_turn(
    conversation_id: ConversationId, service: EventServiceDependency,
    reviewer_user_name: ConversationOwnerDependency,
    user: AuthenticatedUserDependency,
    text: Annotated[str | None, Form(max_length=20000, description="用户原始文字；与 files 至少提供一项非空输入。")] = None,
    files: Annotated[list[UploadFile] | None, File(description="可选 PDF 列表；重复使用 files 字段，最多 10 份，每份 10 MiB，总量 20 MiB。")] = None,
    supersedes_turn_id: Annotated[str | None, Form(min_length=1, max_length=128, description="补充或调整执行中请求时，显式指定同会话需要被替代的旧轮次 ID。")] = None,
) -> TurnCreatedResponse:
    return await _submit_turn(
        conversation_id, service, reviewer_user_name, text, files, supersedes_turn_id,
        new_user=user,
    )


@router.post(
    "/conversations", status_code=201, response_model=TurnCreatedResponse,
    summary="创建会话并注册首轮待激活任务",
    responses={
        413: {"description": "PDF 数量或大小超限。"},
        422: {"description": "名称或首次请求输入不合法。"},
        503: {"description": "内存容量已满或数据库暂时不可用。"},
    },
)
async def create_conversation(
    service: EventServiceDependency, store: StoreDependency, user: AuthenticatedUserDependency,
    name: Annotated[str | None, Form(max_length=200, description="可选会话名称；未提供时按北京时间生成年月日时分秒。")] = None,
    text: Annotated[str | None, Form(max_length=20000, description="首次请求文字，与 files 至少提供一项非空输入。")] = None,
    files: Annotated[list[UploadFile] | None, File(description="首次请求 PDF 列表，最多 10 份，每份 10 MiB，合计 20 MiB。")] = None,
) -> TurnCreatedResponse:
    return await _submit_turn(
        str(uuid4()), service, user.name, text, files, None,
        new_store=store, new_user=user, name=name,
    )


async def _submit_turn(
    conversation_id: str, service: CommunicationEventService, owner: str,
    text: str | None, files: list[UploadFile] | None, supersedes_turn_id: str | None,
    *, new_store: SQLiteCommunicationStore | None = None,
    new_user: ReviewerUser | None = None, name: str | None = None,
) -> TurnCreatedResponse:
    """只暂存输入及注册事件源；成功返回前不进行 PDF 渲染或模型调用。"""
    uploads = files or []
    staged_files = []
    total_bytes = 0
    try:
        if len(uploads) > 10:
            raise HTTPException(status_code=413, detail="每轮最多上传 10 份 PDF")
        for upload in uploads:
            file_name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            if not file_name.lower().endswith(".pdf") or len(file_name) > 512:
                raise HTTPException(status_code=422, detail="文件必须使用有效的 PDF 文件名")
            chunks = []
            file_bytes = 0
            while chunk := await upload.read(65536):
                file_bytes += len(chunk)
                total_bytes += len(chunk)
                if file_bytes > 10 * 1024 * 1024 or total_bytes > 20 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="单份 PDF 不得超过 10 MiB，合计不得超过 20 MiB")
                chunks.append(chunk)
            if not file_bytes:
                raise HTTPException(status_code=422, detail="上传的 PDF 不能为空")
            # 文件名仅作元数据；不将客户端路径用于磁盘读写，内容门禁留待激活后执行。
            staged_files.append(StagedPDF(file_name=file_name, content=b"".join(chunks)))
        if not (text and text.strip()) and not staged_files:
            raise HTTPException(status_code=422, detail="请提供文字或至少一份 PDF")
        if name is not None and not name.strip():
            raise HTTPException(status_code=422, detail="会话名称不能为空白")
        turn_id = str(uuid4())
        try:
            # 屏蔽请求取消直到短暂的创建/注册或补偿完成，避免只落库未注册。
            with CancelScope(shield=True):
                created = False
                try:
                    if new_store is not None:
                        await run_in_threadpool(new_store.create_conversation, conversation_id,
                                                secret_key=new_user.secret_key, name=name)
                        created = True
                    snapshot = await service.register_turn(
                        conversation_id, turn_id, owner=owner,
                        supersedes_turn_id=supersedes_turn_id,
                        staged_input=TurnInput(text=text if text and text.strip() else None, files=tuple(staged_files)),
                        secret_key=new_user.secret_key if new_user else None,
                    )
                except BaseException:
                    if created:
                        await run_in_threadpool(new_store.rollback_empty_conversation,
                                                conversation_id, secret_key=new_user.secret_key)
                    raise
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommunicationCapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="会话存储暂时不可用") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="会话附件暂时无法保存") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TurnCreatedResponse(
            conversation_id=conversation_id, turn_id=turn_id,
            activation_expires_at=snapshot.activation_expires_at,
            supersedes_turn_id=supersedes_turn_id,
        )
    finally:
        for upload in uploads:
            await upload.close()


@router.get(
    "/conversations/{conversation_id}/turns/{turn_id}/events",
    response_class=StreamingResponse,
    summary="订阅轮次 SSE 事件流",
    responses={
        200: {"description": "轮次事件、文本增量与注释心跳。", "content": {"text/event-stream": {"schema": {"type": "string"}}}},
        404: {"description": "轮次不存在、过期或不属于当前用户。"},
        409: {"description": "游标不可回放，须先获取快照。"},
        410: {"description": "轮次超过 3 分钟首次激活期限，须重新创建。"},
        422: {"description": "Last-Event-ID 必须为非负整数。"},
    },
)
async def stream_turn_events(
    conversation_id: ConversationId, turn_id: TurnId,
    service: EventServiceDependency, reviewer_user_name: ConversationOwnerDependency,
    last_event_id: Annotated[
        str | None, Header(alias="Last-Event-ID", description="已收到的轮次事件序号；缺省从 0 开始。"),
    ] = None,
) -> StreamingResponse:
    """回放后持续订阅；HTTP 建连前完成身份和游标检查。"""
    if last_event_id is not None and (
        not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 20
    ):
        raise HTTPException(status_code=422, detail="Last-Event-ID 必须是非负整数")
    try:
        stream = await service.subscribe(
            conversation_id, turn_id, owner=reviewer_user_name,
            after_sequence=int(last_event_id) if last_event_id is not None else 0,
        )
    except CommunicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActivationExpiredError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ReplayUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in stream:
                yield ": heartbeat\n\n" if event is None else _format_event(event)
        except (ReplayUnavailableError, CommunicationNotFoundError) as exc:
            # 已发送 200 后无法改 HTTP 状态；无 id 的连接级错误不改动轮次日志和终态。
            payload = {
                "turn_id": turn_id,
                "code": "replay_required" if isinstance(exc, ReplayUnavailableError) else "turn_unavailable",
                "message": str(exc), "retryable": isinstance(exc, ReplayUnavailableError),
            }
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            # 关闭订阅只释放生成器，不取消业务任务，也不丢弃已有事件。
            await stream.aclose()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/conversations/{conversation_id}/turns/{turn_id}",
    response_model=CommunicationSnapshot,
    summary="获取轮次展示快照",
    responses={404: {"description": "轮次不存在、过期或不属于当前用户。"}},
)
async def get_turn(
    conversation_id: ConversationId, turn_id: TurnId,
    service: EventServiceDependency, reviewer_user_name: ConversationOwnerDependency,
) -> CommunicationSnapshot:
    """原子读取事件投影快照，不返回内部上下文或私有审计。"""
    try:
        return await service.snapshot(conversation_id, turn_id, owner=reviewer_user_name)
    except CommunicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/conversations/{conversation_id}/turns/{turn_id}/cancel",
    response_model=CommunicationSnapshot,
    summary="取消对话轮次",
    responses={
        404: {"description": "轮次不存在、已清理或不属于当前用户、会话。"},
        409: {"description": "轮次已进入非取消终态，不允许改写。"},
    },
)
async def cancel_turn(
    conversation_id: ConversationId, turn_id: TurnId,
    service: EventServiceDependency, reviewer_user_name: ConversationOwnerDependency,
) -> CommunicationSnapshot:
    """取消事件轮次并释放输入；实际工作流和模型上下文尚未接入。"""
    try:
        return await service.cancel_turn(conversation_id, turn_id, owner=reviewer_user_name)
    except CommunicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
