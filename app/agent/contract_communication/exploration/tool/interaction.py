"""探索模式的中途与最终输出定义，均通过受权回调交付。"""
from pydantic import Field
from app.schema.communication_workspace import WorkspaceObject, WorkspaceText
from app.infrastructure.model_json import load_model_json, validate_model_payload
from ...agent_core.tool.output import build_output_registration


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


def build_interaction_registrations(publish):
    return [build_output_registration(name=name, description=_DESCRIPTIONS[name], arguments_model=model,
        kind='final' if name == 'finish_task' else 'intermediate', publish=publish)
        for name, model in _MODELS.items()]


def build_interaction_handlers(publish):
    return {tool.name: tool.execute for tool in build_interaction_registrations(publish)}


class InteractionToolHandler:
    """兼容既有调用入口；实际执行绑定仍来自唯一注册项。"""
    def __init__(self, publish):
        self._tools = {tool.name: tool for tool in build_interaction_registrations(publish)}

    async def __call__(self, operation):
        return await self._tools[operation.name].execute(operation)

    async def execute_parsed(self, operation, arguments):
        return await self._tools[operation.name].handler(operation, arguments)
