"""进程内对话事件日志与展示快照；不创建业务任务或调用智能体。"""

import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter, SecretStr
from app.service.communication_trace import ToolCallTrace, ToolResultTrace

from app.schema.communication import (
    TERMINAL_STATUSES, CommunicationEvent, CommunicationSnapshot, ErrorData,
    EventData, MessageCompletedData, MessageDeltaData,
    MessageSnapshot, TaskProgressData, TurnStatusData,
)


class CommunicationNotFoundError(LookupError):
    """轮次不存在、已过期或不属于当前用户。"""


class ReplayUnavailableError(ValueError):
    """游标超出可回放范围，必须先读取快照。"""


class ActivationExpiredError(ValueError):
    """超过首次激活期限，不允许重新启动该轮次。"""


class CommunicationCapacityError(ValueError):
    """暂存轮次或输入达到进程容量限制。"""


@dataclass(frozen=True)
class StagedPDF:
    """本轮独占的上传字节；尚未经过 PDF 可读性和合同门禁。"""

    file_name: str
    content: bytes


@dataclass(frozen=True)
class TurnInput:
    text: str | None = None
    files: tuple[StagedPDF, ...] = ()

    @property
    def size_bytes(self) -> int:
        return len((self.text or "").encode("utf-8")) + sum(len(file.content) for file in self.files)


@dataclass
class _Turn:
    owner: str
    snapshot: CommunicationSnapshot
    events: deque[CommunicationEvent]
    touched_at: float
    activation_deadline: float
    staged_input: TurnInput | None = None
    active_message_id: str | None = None
    activated_monotonic: float | None = None
    history_registered: bool = False


_EVENT_ADAPTER = TypeAdapter(EventData)


class CommunicationEventService:
    """先提交日志和快照，再唤醒订阅者；慢订阅者不阻塞生产者。"""

    def __init__(
        self, *, event_buffer_size: int = 128, heartbeat_seconds: float = 15,
        ttl_seconds: float = 3600, max_turns: int = 32,
        max_event_bytes: int = 32768, max_message_chars: int = 65536,
        activation_timeout_seconds: float = 180, cleanup_interval_seconds: float = 1,
        max_staged_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        for value in (event_buffer_size, max_turns, max_event_bytes, max_message_chars, max_staged_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("事件容量必须为正整数")
        if any(not math.isfinite(value) or value <= 0 for value in (heartbeat_seconds, ttl_seconds, activation_timeout_seconds, cleanup_interval_seconds)):
            raise ValueError("心跳与保留时间必须为有限正数")
        self.heartbeat_seconds = heartbeat_seconds
        self._ttl = ttl_seconds
        self._buffer_size = event_buffer_size
        self._max_turns = max_turns
        self._max_event_bytes = max_event_bytes
        self._max_message_chars = max_message_chars
        self._activation_timeout = activation_timeout_seconds
        self._cleanup_interval = cleanup_interval_seconds
        self._max_staged_bytes = max_staged_bytes
        self._cleanup_task: asyncio.Task | None = None
        self._turns: dict[tuple[str, str], _Turn] = {}
        self._condition = asyncio.Condition()
        self._closed = False
        self._history = None

    def bind_history(self, history):
        if self._turns or self._cleanup_task is not None:
            raise RuntimeError('历史服务必须在运行时启动前装配')
        self._history = history
        self._condition = asyncio.Condition(history.lock)
        history.evict_runtime = self._evict_conversation_locked

    def _evict_conversation_locked(self, conversation_id):
        for key in tuple(self._turns):
            if key[0] == conversation_id:
                del self._turns[key]
        self._condition.notify_all()

    async def register_turn(
        self, conversation_id: str, turn_id: str, *, owner: str,
        supersedes_turn_id: str | None = None,
        staged_input: TurnInput | None = None,
        secret_key: SecretStr | None = None,
    ) -> CommunicationSnapshot:
        """注册事件源；用户调整时原子替代旧轮次，不等于实际中断模型任务。"""
        if any(not value.strip() or len(value) > 128 for value in (conversation_id, turn_id, owner)):
            raise ValueError("会话、轮次和用户标识必须非空且不超过 128 字符")
        async with self._condition:
            self._prune()
            if self._closed:
                raise RuntimeError("事件服务已关闭")
            key = (conversation_id, turn_id)
            if key in self._turns:
                raise ValueError("轮次事件源已经存在")
            if any(k[0] == conversation_id and turn.owner != owner for k, turn in self._turns.items()):
                raise CommunicationNotFoundError("会话不存在或不属于当前用户")
            old_turn = None
            if supersedes_turn_id is not None:
                old_turn = self._get(conversation_id, supersedes_turn_id, owner)
                if old_turn.snapshot.status in TERMINAL_STATUSES:
                    raise ValueError("旧轮次已经结束，请提交普通新轮次，不得改写历史终态")
            active_turns = [
                turn for k, turn in self._turns.items()
                if k[0] == conversation_id and turn.snapshot.status not in TERMINAL_STATUSES
            ]
            if any(turn is not old_turn for turn in active_turns):
                raise ValueError("同一会话已有执行中的轮次，用户调整须显式指定被替代轮次")
            if len(self._turns) >= self._max_turns:
                raise CommunicationCapacityError("事件轮次容量已满")
            retained_bytes = sum(
                item.staged_input.size_bytes for item in self._turns.values()
                if item is not old_turn and item.staged_input is not None
            )
            if retained_bytes + (staged_input.size_bytes if staged_input else 0) > self._max_staged_bytes:
                raise CommunicationCapacityError("暂存输入容量已满")
            now = datetime.now(timezone.utc)
            turn = _Turn(
                owner=owner,
                snapshot=CommunicationSnapshot(
                    conversation_id=conversation_id, turn_id=turn_id,
                    status="pending_activation", last_sequence=0, earliest_sequence=1,
                    activation_expires_at=now + timedelta(seconds=self._activation_timeout),
                    supersedes_turn_id=supersedes_turn_id,
                ),
                events=deque(maxlen=self._buffer_size), touched_at=time.monotonic(),
                activation_deadline=time.monotonic() + self._activation_timeout,
                staged_input=staged_input,
            )
            # 创建只注册，不发布 processing，也不运行门禁或模型。
            # 提前校验生命周期事件大小，避免接受了输入却无法激活或自动过期。
            for next_status in ("processing", "expired"):
                probe = replace(turn, events=deque(maxlen=self._buffer_size))
                self._commit(probe, TurnStatusData(status=next_status), record_history=False)
            if old_turn is not None:
                # 在副本上完成全部校验；新旧任一事件超限时均不改变原轮次。
                old_copy = replace(old_turn, events=deque(old_turn.events, maxlen=self._buffer_size))
                self._commit(old_copy, TurnStatusData(
                    status="superseded", superseded_by_turn_id=turn_id,
                ), record_history=False)
            if self._history is not None:
                # 所有注册校验完成后才保存附件和加入共享轨迹；失败不终止旧轮次。
                await self._history.register_locked(conversation_id, turn_id, secret_key=secret_key, source=staged_input)
                turn.history_registered = True
            if old_turn is not None:
                if self._history is not None and old_turn.history_registered:
                    self._history.accept_event_locked(conversation_id, old_turn.snapshot.turn_id,
                                                      old_copy.events[-1].data, old_copy.snapshot)
                # 保留旧对象身份，让已连接订阅者收到真实替代终态并正常关闭。
                old_turn.snapshot = old_copy.snapshot
                old_turn.events = old_copy.events
                old_turn.active_message_id = old_copy.active_message_id
                old_turn.touched_at = old_copy.touched_at
                old_turn.staged_input = None
            self._turns[key] = turn
            self._condition.notify_all()
            return turn.snapshot

    async def publish(
        self, conversation_id: str, turn_id: str, *, owner: str, data: EventData,
    ) -> CommunicationEvent:
        """发布已获业务认可的用户可见结果，不接受模型原始轨迹。"""
        # 重新校验复制负载，避免可变容器或绕过构造校验的对象进入日志。
        # context_status 是只读派生语义，不接收调用方注入，重新校验时从原始状态重建。
        payload = _EVENT_ADAPTER.validate_json(_EVENT_ADAPTER.dump_json(data, exclude={"context_status"}))
        if isinstance(payload, TurnStatusData) and payload.status in {"superseded", "pending_activation", "processing", "expired"}:
            raise ValueError("注册、激活、过期及替代状态只能由生命周期管理产生")
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status in TERMINAL_STATUSES:
                raise ValueError("终态轮次禁止继续发布事件")
            if turn.snapshot.status == "pending_activation":
                raise ValueError("轮次尚未激活，禁止发布业务事件")
            event = self._commit(turn, payload)
            self._condition.notify_all()
            return event

    async def cancel_turn(self, conversation_id: str, turn_id: str, *, owner: str) -> CommunicationSnapshot:
        """原子取消本人的非终态轮次；重复取消不增加事件或延长保留期。"""
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status == "cancelled":
                return turn.snapshot
            if turn.snapshot.status in TERMINAL_STATUSES:
                raise ValueError("轮次已结束，不能改写为取消状态")
            # 与激活、替代及发布共用锁，先提交者决定终态；终态阻断迟到结果。
            self._commit(turn, TurnStatusData(status="cancelled"))
            self._condition.notify_all()
            return turn.snapshot

    async def snapshot(self, conversation_id: str, turn_id: str, *, owner: str) -> CommunicationSnapshot:
        async with self._condition:
            return self._get(conversation_id, turn_id, owner).snapshot

    async def subscribe(
        self, conversation_id: str, turn_id: str, *, owner: str, after_sequence: int = 0,
    ) -> AsyncIterator[CommunicationEvent | None]:
        """在 HTTP 200 之前校验身份和游标；None 表示心跳，不占事件序号。"""
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("事件游标必须为非负整数")
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status == "expired":
                raise ActivationExpiredError("轮次超过首次激活期限，请重新创建")
            self._check_cursor(turn, after_sequence)
            if turn.snapshot.status == "pending_activation":
                # 身份和游标检查全部通过才激活，锁内切换确保并发订阅也只执行一次。
                self._commit(turn, TurnStatusData(status="processing"))
                self._condition.notify_all()

        async def iterate() -> AsyncIterator[CommunicationEvent | None]:
            cursor = after_sequence
            while True:
                async with self._condition:
                    # 持有锁完成检查及等待注册，避免回放切换实时订阅时丢失唤醒。
                    current = self._get(conversation_id, turn_id, owner)
                    if current is not turn:
                        raise CommunicationNotFoundError("轮次事件源已失效")
                    self._check_cursor(turn, cursor)
                    batch = tuple(event for event in turn.events if event.sequence > cursor)
                    terminal = turn.snapshot.status in TERMINAL_STATUSES
                    if not batch and not terminal:
                        try:
                            await asyncio.wait_for(
                                self._condition.wait(), timeout=min(self.heartbeat_seconds, self._ttl),
                            )
                            continue
                        except TimeoutError:
                            pass
                for event in batch:
                    cursor = event.sequence
                    yield event
                if terminal:
                    return
                if not batch:
                    yield None

        return iterate()

    async def get_input(self, conversation_id: str, turn_id: str, *, owner: str) -> TurnInput | None:
        """供后续执行层读取已激活输入，不对 HTTP 暴露文件字节。"""
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != "processing":
                raise ValueError("只能读取已激活且仍在处理的轮次输入")
            return turn.staged_input

    async def start(self) -> None:
        """启动过期回收，不依赖客户端再次请求才释放上传资源。"""
        if self._closed:
            raise RuntimeError("事件服务已关闭")
        if self._cleanup_task is None:
            if self._history is not None:
                await self._history.start()
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval)
            async with self._condition:
                self._prune()
                self._condition.notify_all()

    async def close(self) -> None:
        """关闭时释放日志并唤醒等待者，不声称取消了任何业务任务。"""
        async with self._condition:
            self._closed = True
            if self._cleanup_task is not None:
                self._cleanup_task.cancel()
                await asyncio.gather(self._cleanup_task, return_exceptions=True)
                self._cleanup_task = None
            for turn in self._turns.values():
                if turn.snapshot.status not in TERMINAL_STATUSES:
                    self._commit(turn, TurnStatusData(status='failed'))
            self._turns.clear()
            self._condition.notify_all()

    def _prune(self) -> None:
        now = time.monotonic()
        for turn in self._turns.values():
            if turn.snapshot.status == "pending_activation" and now >= turn.activation_deadline:
                self._commit(turn, TurnStatusData(status="expired"))
        for turn in self._turns.values():
            if turn.snapshot.status == 'processing' and now - turn.touched_at >= self._ttl:
                previous_touch = turn.touched_at
                self._commit(turn, TurnStatusData(status='failed'))
                turn.touched_at = previous_touch
        expired = [
            key for key, turn in self._turns.items()
            if turn.snapshot.status != "pending_activation" and now - turn.touched_at >= self._ttl
        ]
        for key in expired:
            del self._turns[key]

    def _get(self, conversation_id: str, turn_id: str, owner: str) -> _Turn:
        self._prune()
        turn = self._turns.get((conversation_id, turn_id))
        if turn is None or turn.owner != owner:
            raise CommunicationNotFoundError("轮次不存在或已经过期")
        return turn

    @staticmethod
    def _check_cursor(turn: _Turn, cursor: int) -> None:
        if cursor < turn.snapshot.earliest_sequence - 1 or cursor > turn.snapshot.last_sequence:
            raise ReplayUnavailableError("事件游标不可回放，请先获取快照")

    async def record_tool_call(self, conversation_id, turn_id, *, owner, **fields):
        await self._record_tool(conversation_id, turn_id, owner, ToolCallTrace(**fields))

    async def record_tool_result(self, conversation_id, turn_id, *, owner, **fields):
        await self._record_tool(conversation_id, turn_id, owner, ToolResultTrace(**fields))

    async def _record_tool(self, conversation_id, turn_id, owner, item):
        async with self._condition:
            turn = self._get(conversation_id, turn_id, owner)
            if turn.snapshot.status != 'processing':
                raise ValueError('只允许处理中的任务记录工具调用')
            if self._history is not None:
                self._history.accept_tool_locked(conversation_id, turn_id, item)
            turn.touched_at = time.monotonic()

    def _commit(self, turn: _Turn, data: EventData, *, record_history=True) -> CommunicationEvent:
        now = datetime.now(timezone.utc)
        monotonic_now = time.monotonic()
        timing = {}
        if isinstance(data, TurnStatusData):
            # 时间由运行时统一生成，不信任发布者传入的时间；事件与快照同批提交。
            timing = {
                "activated_at": turn.snapshot.activated_at,
                "finished_at": None,
                "processing_duration_ms": None,
            }
            if data.status == "processing":
                timing["activated_at"] = now
            elif data.status in TERMINAL_STATUSES:
                timing["finished_at"] = now
                # 单调时钟避免服务器校时影响耗时；未激活结束不计算等待订阅时间。
                timing["processing_duration_ms"] = (
                    max(0, int((monotonic_now - turn.activated_monotonic) * 1000))
                    if turn.activated_monotonic is not None else 0
                )
            data = data.model_copy(update=timing)
        event = CommunicationEvent(
            sequence=turn.snapshot.last_sequence + 1, turn_id=turn.snapshot.turn_id,
            created_at=now, data=data,
        )
        if len(event.model_dump_json().encode("utf-8")) > self._max_event_bytes:
            raise ValueError("单个事件超过大小限制")
        updates = dict(timing)
        active_id = turn.active_message_id
        messages = list(turn.snapshot.messages)
        final_completed = any(
            message.message_kind == "final" and message.status == "completed"
            for message in messages
        )
        # 最终答复交付后只接受轮次终态，不再追加业务内容或恢复旧轮次。
        if final_completed and not (
            isinstance(data, TurnStatusData) and data.status in TERMINAL_STATUSES
        ):
            raise ValueError("最终答复已完成，只能结束本轮；用户补充须创建新轮次")
        if isinstance(data, (MessageDeltaData, MessageCompletedData)):
            if active_id is not None and active_id != data.message_id:
                raise ValueError("同一轮次同时只能生成一条消息")
            index = next((i for i, message in enumerate(messages) if message.message_id == data.message_id), None)
            if index is not None and messages[index].status != "streaming":
                raise ValueError("已结束消息不能再次追加或完成")
            if index is not None and messages[index].message_kind != data.message_kind:
                raise ValueError("同一消息的 message_kind 不能改变")
            previous = messages[index].text if index is not None else ""
            text = previous + data.delta if isinstance(data, MessageDeltaData) else data.text
            if isinstance(data, MessageCompletedData) and previous and previous != text:
                raise ValueError("完成消息必须与已发送的全部增量文本一致")
            if sum(len(message.text) for message in messages) - len(previous) + len(text) > self._max_message_chars:
                raise ValueError("轮次消息文本超过大小限制")
            message = MessageSnapshot(
                message_id=data.message_id, text=text,
                message_kind=data.message_kind,
                status="streaming" if isinstance(data, MessageDeltaData) else "completed",
                references=data.references if isinstance(data, MessageCompletedData) else (),
            )
            if index is None:
                if len(messages) >= 128:
                    raise ValueError("轮次消息数量超过限制")
                messages.append(message)
            else:
                messages[index] = message
            active_id = data.message_id if isinstance(data, MessageDeltaData) else None
            updates["messages"] = tuple(messages)
        elif isinstance(data, TurnStatusData):
            if data.status == "completed" and (active_id is not None or not final_completed):
                raise ValueError("必须先完成本轮 final 消息，再将轮次标记为 completed")
            if data.status in TERMINAL_STATUSES and active_id is not None:
                updates["messages"] = tuple(
                    message.model_copy(update={"status": "interrupted"})
                    if message.message_id == active_id else message for message in messages
                )
                active_id = None
            updates["status"] = data.status
            updates["superseded_by_turn_id"] = data.superseded_by_turn_id
        elif isinstance(data, TaskProgressData):
            updates["progress"] = data
        elif isinstance(data, ErrorData):
            updates["error"] = data
        # 所有校验成功后一次性提交，拒绝的事件不占序号、不污染快照。
        if record_history and turn.history_registered and self._history is not None:
            self._history.accept_event_locked(turn.snapshot.conversation_id, turn.snapshot.turn_id,
                                              data, turn.snapshot.model_copy(update=updates))
        turn.events.append(event)
        updates.update(last_sequence=event.sequence, earliest_sequence=turn.events[0].sequence)
        turn.snapshot = turn.snapshot.model_copy(update=updates)
        turn.active_message_id = active_id
        turn.touched_at = monotonic_now
        if isinstance(data, TurnStatusData) and data.status == "processing":
            turn.activated_monotonic = monotonic_now
        if isinstance(data, TurnStatusData) and data.status in TERMINAL_STATUSES:
            turn.staged_input = None
        return event
