"""Communication 用户可见事件与展示快照，不作为模型记忆或私有审计。"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


TurnStatus = Literal[
    "pending_activation", "processing", "completed", "cancelled", "superseded", "rejected", "failed", "expired"
]
MessageKind = Literal["intermediate", "final"]
UserContextStatus = Literal["user_goal_adjusted", "user_manually_stopped"]
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "superseded", "rejected", "failed", "expired"})


class CommunicationModel(BaseModel):
    """冻结已发布数据，禁止额外字段混入公开事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TurnStateModel(CommunicationModel):
    """执行状态与用户控制语义共用唯一映射，避免两个字段发生漂移。"""

    status: TurnStatus
    activated_at: datetime | None = Field(default=None, description="首次成功订阅的 UTC 激活时间；未激活为空，重连不重置。")
    finished_at: datetime | None = Field(default=None, description="进入终态的 UTC 时间；未结束为空。")
    processing_duration_ms: int | None = Field(default=None, ge=0, description="终态固定的总处理时长，单位毫秒，从激活计时；未结束为空，未激活就结束为 0。")

    @computed_field(description="供后续上下文构造使用：用户目标调整或用户手动终止；其他状态为空。")
    @property
    def context_status(self) -> UserContextStatus | None:
        if self.status == "superseded":
            return "user_goal_adjusted"
        if self.status == "cancelled":
            return "user_manually_stopped"
        return None


class TurnStatusData(TurnStateModel):
    event_type: Literal["turn.status"] = "turn.status"
    superseded_by_turn_id: str | None = Field(
        default=None, min_length=1, max_length=128,
        description="仅 superseded 终态填写，指向同会话中接替执行的新轮次。",
    )

    @model_validator(mode="after")
    def validate_replacement(self) -> Self:
        if (self.status == "superseded") != (self.superseded_by_turn_id is not None):
            raise ValueError("只有 superseded 状态必须且可以关联替代轮次")
        return self


class TaskProgressData(CommunicationModel):
    event_type: Literal["task.progress"] = "task.progress"
    message: str = Field(min_length=1, max_length=2000)


class MessageDeltaData(CommunicationModel):
    event_type: Literal["message.delta"] = "message.delta"
    message_id: str = Field(min_length=1, max_length=128)
    message_kind: MessageKind = Field(description="intermediate 为阶段提示；final 为本轮最终答复，包括澄清或确认问题。")
    delta: str = Field(min_length=1)


class MessageReference(CommunicationModel):
    """只暴露可追溯引用，不接收服务器文件系统路径。"""

    document_id: str = Field(min_length=1, max_length=128)
    page_number: int | None = Field(default=None, ge=1)


class MessageCompletedData(CommunicationModel):
    event_type: Literal["message.completed"] = "message.completed"
    message_id: str = Field(min_length=1, max_length=128)
    message_kind: MessageKind = Field(description="必须与同一消息的增量类型一致；final 不表示整体业务目标已经完成。")
    text: str = Field(min_length=1)
    references: tuple[MessageReference, ...] = ()


class ErrorData(CommunicationModel):
    event_type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool


EventData = Annotated[
    TurnStatusData | TaskProgressData | MessageDeltaData
    | MessageCompletedData | ErrorData,
    Field(discriminator="event_type"),
]


class CommunicationEvent(CommunicationModel):
    sequence: int = Field(ge=1)
    turn_id: str
    created_at: datetime
    data: EventData


class MessageSnapshot(CommunicationModel):
    message_id: str
    message_kind: MessageKind = Field(description="消息用途：阶段提示 intermediate 或本轮最终答复 final。")
    text: str
    status: Literal["streaming", "completed", "interrupted"]
    references: tuple[MessageReference, ...] = ()


class CommunicationSnapshot(TurnStateModel):
    conversation_id: str
    turn_id: str
    activation_expires_at: datetime | None = Field(default=None, description="首次订阅激活截止时间；激活后不再作为执行或重连时限。")
    supersedes_turn_id: str | None = Field(default=None, description="本轮因用户调整而接替的旧轮次；普通新轮次为空。")
    superseded_by_turn_id: str | None = Field(default=None, description="替代本轮的新轮次；仅本轮已被替代时有值。")
    last_sequence: int = Field(ge=0)
    earliest_sequence: int = Field(ge=1)
    messages: tuple[MessageSnapshot, ...] = ()
    progress: TaskProgressData | None = None
    error: ErrorData | None = None


class TurnCreatedResponse(CommunicationModel):
    conversation_id: str
    turn_id: str
    status: Literal["pending_activation"] = "pending_activation"
    activation_expires_at: datetime
    supersedes_turn_id: str | None = None


class ConversationListItem(CommunicationModel):
    """会话目录只暴露展示元数据，不包含密钥、轨迹或工作区。"""

    conversation_id: str = Field(description="服务端生成的会话标识。")
    name: str = Field(description="当前会话名称，包括用户修改后的名称。")
    created_at: int = Field(description="会话创建时间，UTC Unix 毫秒时间戳。")


class ConversationRenameRequest(CommunicationModel):
    name: str = Field(min_length=1, max_length=200, description="新的会话名称，去除首尾空白后不得为空。")


class ConversationHistoryRecord(CommunicationModel):
    """驻留历史的单条任务或摘要，不包含向量和用户密钥。"""

    record_id: str
    sequence: int = Field(ge=1, description="会话内任务与摘要共用的排序号。")
    kind: Literal["task", "summary"]
    turn_id: str | None
    status: TurnStatus | None
    payload: dict
    created_at: int = Field(description="记录创建时间，UTC Unix 毫秒。")
    activated_at: int | None = Field(default=None, description="任务首次激活时间，UTC Unix 毫秒；未激活或旧记录未知时为 null。")
    processing_duration_ms: int | None = Field(default=None, ge=0, strict=True, description="任务从激活到终态的总处理时长，单位毫秒；未激活即结束为 0，旧记录缺失计时或摘要为 null。")


class ConversationHistoryResponse(ConversationListItem):
    records: tuple[ConversationHistoryRecord, ...] = Field(description="当前驻留的完整历史，按会话 sequence 正序；不是单轮 SSE 快照。")
    has_more: bool = Field(description="当前最早记录之前是否仍有历史可向前加载。")
    model_context_start_sequence: int | None = Field(description="模型历史窗口起点：最新摘要序号，无摘要时为最早记录序号，无记录为 null。")


class ConversationTaskRecord(ConversationHistoryRecord):
    """前端可见的历史记录，不允许包含内部摘要。"""

    kind: Literal["task"]


class ConversationTaskHistoryResponse(ConversationHistoryResponse):
    """完整驻留范围的任务投影，加载边界仍由内部历史决定。"""

    records: tuple[ConversationTaskRecord, ...] = Field(description="当前驻留范围内的全部任务，已剔除摘要；保留原始 sequence，可能不连续。")
