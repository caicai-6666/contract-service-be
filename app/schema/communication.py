"""Communication 用户可见事件与展示快照，不作为模型记忆或私有审计。"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


TurnStatus = Literal[
    "pending_activation", "processing", "completed", "cancelled", "superseded", "rejected", "failed", "expired"
]
MessageKind = Literal["intermediate", "final"]
TaskProgressType = Literal["local-search", "online-search", "external-expert", "thinking"]
UserContextStatus = Literal["user_goal_adjusted", "user_manually_stopped"]
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "superseded", "rejected", "failed", "expired"})


class CommunicationModel(BaseModel):
    """冻结已发布数据，禁止额外字段混入公开事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TurnStateModel(CommunicationModel):
    """执行状态与用户控制语义共用唯一映射，避免两个字段发生漂移。"""

    status: TurnStatus
    can_interrupt: bool = Field(default=False, strict=True, description="是否允许用户取消或提交替代轮次；正式工作流仅在完整门禁通过后开放，终态为 false。")
    activated_at: datetime | None = Field(default=None, description="首次成功订阅的 UTC 激活时间；未激活为空，重连不重置。")
    finished_at: datetime | None = Field(default=None, description="进入终态的 UTC 时间；未结束为空。")
    processing_duration_ms: int | None = Field(default=None, ge=0, description="终态固定的总处理时长，单位毫秒，从激活计时；未结束为空，未激活就结束为 0。")

    @model_validator(mode="after")
    def validate_interruption(self) -> Self:
        if self.can_interrupt and self.status in TERMINAL_STATUSES:
            raise ValueError("终态任务不能开放用户中断")
        return self

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
    """当前用户展示状态，不等同于工具调用或轮次生命周期。"""

    event_type: Literal["task.progress"] = "task.progress"
    type: TaskProgressType = Field(description="展示类型：local-search 查阅本地资料；online-search 联网检索；external-expert 咨询外部专家；thinking 理解、规划、分析或组织答复。")
    message: str = Field(min_length=1, max_length=2000, description="新发布的状态说明统一使用“正在＋动作”，不包含内部工具名、调用 ID 或推理过程；状态切换不代表上一步成功，读取兼容旧历史文案。")


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
    status: Literal["completed", "interrupted"] = Field(default="completed", description="消息结束方式：completed 正常完整输出；interrupted 为运行时在任务中断时收束的半成品。")
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


def public_event_data(event: CommunicationEvent) -> dict:
    """SSE 与持久化展示记录共用编码，避免两条展示路径发生漂移。"""
    payload = {"turn_id": event.turn_id, **event.data.model_dump(mode="json", exclude={"event_type"})}
    if isinstance(event.data, TurnStatusData):
        payload["created_at"] = event.created_at.isoformat()
    return payload


class DisplayEvent(CommunicationModel):
    sequence: int | None = Field(ge=1, description="原始 SSE 序号，过滤 delta 后允许不连续；旧轨迹兼容恢复为 null，不作为重连游标。")
    event: Literal["turn.status", "task.progress", "message.completed", "error"]
    data: dict = Field(description="与对应 SSE data 相同的公开负载；旧轨迹引用沿用 type/location。")


class ContractReference(CommunicationModel):
    """请求接收时从正式合同目录读取的快照；不接受客户端提交名称与摘要。"""

    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='正式合同的完整 SHA-256 标识，可用于合同查看工具。')
    file_name: str = Field(min_length=1, max_length=255, description='正式入库时用户确认的展示文件名。')
    summary: str | None = Field(default=None, description='正式入库时用户确认的摘要；旧合同未保存时为空，不补写。')


class ConversationDisplayPayload(CommunicationModel):
    input: dict = Field(description="用户原文、附件列表和 contracts 合同引用快照（document_id、file_name、summary）；附件保留原文件名，display_name、summary 为后端生成的描述，未生成时为 null，旧记录可缺省。")
    events: tuple[DisplayEvent, ...] = Field(description="按原始事件顺序保存的展示记录，不含 delta、心跳、连接错误或工具轨迹。")
    streaming_messages: tuple[dict, ...] = Field(description="仍在处理的消息累积正文，覆盖而非追加；任务结束后为空。")
    last_sequence: int | None = Field(ge=0, description="恢复时已处理的最后 SSE 序号，包含被过滤的 delta；旧轨迹未知时为 null。")
    event_source: Literal["recorded", "legacy"] = Field(description="recorded 为实际记录的事件；legacy 仅从旧轨迹恢复消息，不虚构进度和错误。")


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
    progress: TaskProgressData | None = Field(default=None, description="最近一次展示状态，后续进度整体覆盖；无进度时为空，终态保留但不再表示正在执行。")
    error: ErrorData | None = None


class TurnCreatedResponse(CommunicationModel):
    conversation_id: str
    turn_id: str
    status: Literal["pending_activation"] = "pending_activation"
    can_interrupt: bool = Field(default=False, strict=True, description="是否允许取消或替代；正式工作流待激活时为 false。")
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
    can_interrupt: bool = Field(default=False, strict=True, description="当前驻留任务是否允许取消或替代；持久化终态及摘要为 false。")
    payload: dict
    created_at: int = Field(description="记录创建时间，UTC Unix 毫秒。")
    activated_at: int | None = Field(default=None, description="任务首次激活时间，UTC Unix 毫秒；未激活或旧记录未知时为 null。")
    processing_duration_ms: int | None = Field(default=None, ge=0, strict=True, description="任务从激活到终态的总处理时长，单位毫秒；未激活即结束为 0，旧记录缺失计时或摘要为 null。")

    @model_validator(mode="after")
    def validate_interruption(self) -> Self:
        if self.can_interrupt and (self.kind != 'task' or self.status not in {'pending_activation', 'processing'}):
            raise ValueError("仅非终态任务允许用户中断")
        return self


class ConversationHistoryResponse(ConversationListItem):
    records: tuple[ConversationHistoryRecord, ...] = Field(description="当前驻留的完整历史，按会话 sequence 正序；不是单轮 SSE 快照。")
    has_more: bool = Field(description="当前最早记录之前是否仍有历史可向前加载。")
    model_context_start_sequence: int | None = Field(description="模型历史窗口起点：最新摘要序号，无摘要时为最早记录序号，无记录为 null。")


class ConversationTaskRecord(ConversationHistoryRecord):
    """前端可见的历史记录，不允许包含内部摘要。"""

    kind: Literal["task"]
    payload: ConversationDisplayPayload


class ConversationTaskHistoryResponse(ConversationHistoryResponse):
    """完整驻留范围的任务投影，加载边界仍由内部历史决定。"""

    records: tuple[ConversationTaskRecord, ...] = Field(description="当前驻留范围内的全部任务，已剔除摘要；保留原始 sequence，可能不连续。")
