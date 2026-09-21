"""工具执行状态的程序侧展示配置；不属于模型参数或工具返回结果。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.schema.communication import TaskProgressData, TaskProgressType


@dataclass(frozen=True)
class ToolProgress:
    type: TaskProgressType = 'thinking'
    message: str = '正在思考'

    def __post_init__(self):
        TaskProgressData(type=self.type, message=self.message)
        # 在注册入口约束新状态文案，不收紧历史事件的读取 Schema。
        if not self.message.startswith('正在') or not self.message[2:].strip():
            raise ValueError('工具状态说明必须以“正在”开头，并包含具体动作')


DEFAULT_TOOL_PROGRESS = ToolProgress()
ToolProgressPublisher = Callable[[ToolProgress], Awaitable[None]]
