"""与传输方式无关的输出提交对象和可选流式观察接口。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from pydantic import Field
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText

@dataclass(frozen=True)
class UserOutput:
    """程序提交对象；身份不来自模型参数，content 已通过工具校验。"""
    task_id: str
    call_id: str
    kind: Literal['intermediate', 'final']
    content: str


class UserOutputReceipt(WorkspaceObject):
    message_id: WorkspaceText = Field(description='消息提交成功后由程序返回的稳定消息标识；不是模型生成值。')


class UserOutputRejected(Exception):
    """仅当回调确定未提交任何消息时使用，例如活动任务已失效。"""


UserOutputPublisher = Callable[[UserOutput], Awaitable[UserOutputReceipt]]



class OutputObserver(Protocol):
    async def on_delta(self, message: dict) -> None: ...
    async def interrupt(self) -> None: ...
    async def complete(self, output: UserOutput) -> str: ...
