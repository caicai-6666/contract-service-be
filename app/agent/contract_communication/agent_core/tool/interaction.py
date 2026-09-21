"""用户交互工具；消息提交回调绑定会话身份，任务封闭由主循环完成。"""
from app.infrastructure.model_json import load_model_json, validate_model_payload
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from typing import Literal

from pydantic import Field
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText


class EmitProgressArguments(WorkspaceObject):
    content: WorkspaceText = Field(description='直接展示给用户的阶段性结果、必要解释或下一步安排；区分已完成事实与计划。本次输出后继续执行本轮，不用于结束或暂停等待用户。')


class FinishTaskArguments(WorkspaceObject):
    content: WorkspaceText = Field(description='直接展示给用户的本轮最终反馈：有依据的答案、已完成范围与具体限制，或必须由用户回答的澄清问题；成功提交后结束本轮，不将部分完成宣称为全部目标完成。')


_MODELS = {'emit_progress': EmitProgressArguments, 'finish_task': FinishTaskArguments}
_DESCRIPTIONS = {
    'emit_progress': '当你需要向用户报告阶段性发现、处理进展或接下来的操作安排，同时还要继续执行当前任务时，使用这个工具。展示中途内容；成功后继续执行，本轮任务不结束，也不暂停等待用户。',
    'finish_task': '当你需要提交最终答复，或因缺少必要信息而需要用户补充后才能继续时，使用这个工具。向用户提交最终反馈并正式结束本轮任务；可交付答案、说明限制或请求必要信息，成功后不再继续本轮调用。',
}


def build_interaction_tools() -> list[dict]:
    return [{'type': 'function', 'function': {'name': name, 'description': _DESCRIPTIONS[name],
        'parameters': model.model_json_schema(), 'strict': False}} for name, model in _MODELS.items()]


def parse_interaction_tool_arguments(name: str, raw_arguments: str):
    if name not in _MODELS:
        raise ValueError('未知用户交互工具')

    return validate_model_payload(_MODELS[name], load_model_json(raw_arguments))


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


class InteractionToolHandler:
    def __init__(self, publish: UserOutputPublisher):
        self._publish = publish

    async def __call__(self, operation):
        # 延迟导入避免工具定义加载时反向初始化管理图。
        from ..subgraph.fifo_management.schema import FIFOExecutionResult

        try:
            arguments = parse_interaction_tool_arguments(operation.name, operation.arguments)
        except (ValueError, TypeError):
            return FIFOExecutionResult(status='failed', tool_result={
                'error': '交互工具参数非法：仅允许 content，且必须为非空文本；请按工具定义修正。'})
        return await self.execute_parsed(operation, arguments)

    async def execute_parsed(self, operation, arguments):
        from ..subgraph.fifo_management.schema import FIFOExecutionResult
        final = operation.name == 'finish_task'
        try:
            receipt = UserOutputReceipt.model_validate(await self._publish(UserOutput(
                task_id=operation.task_id, call_id=operation.call_id,
                kind='final' if final else 'intermediate', content=arguments.content)))
        except UserOutputRejected:
            return FIFOExecutionResult(status='failed', tool_result={
                'error': '用户输出被拒绝，消息未提交；请依据当前任务状态处理。'})
        # 未知异常交给 executor 标记 unknown，不能声称消息未发送或任务已结束。
        # 先由 FIFO 记录反馈，再由主循环消费结束信号并封闭任务，避免提前驱逐。
        return FIFOExecutionResult(status='succeeded', tool_result={
            'status': 'success', 'message_id': receipt.message_id,
            'finish_requested': final,
            'message': '最终反馈已提交，本轮不再继续调用工具。' if final else '中途输出已提交，请继续执行本轮任务。',
        })


def build_interaction_handlers(publish: UserOutputPublisher) -> dict:
    """注册表入口；两个工具共享同一个受权消息提交回调。"""
    handler = InteractionToolHandler(publish)
    return {name: handler for name in _MODELS}


def build_interaction_registrations(publish):
    from .registry import RegisteredTool
    handler = InteractionToolHandler(publish)
    return [RegisteredTool(name, _DESCRIPTIONS[name], model, handler.execute_parsed, return_types=('ordinary',))
            for name, model in _MODELS.items()]
