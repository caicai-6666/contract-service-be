"""主助手工具的唯一注册契约；定义、兼容解析和执行绑定共同发布。"""
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
import re

from pydantic import BaseModel, ValidationError
from app.schema.agent_tool_content import ToolContentType
from app.infrastructure.model_json import load_model_json, validate_model_payload
from ..subgraph.fifo_management.schema import FIFOOperation, FIFOExecutionResult

from .progress import DEFAULT_TOOL_PROGRESS, ToolProgress, ToolProgressPublisher

ParsedToolHandler = Callable[[FIFOOperation, BaseModel], Awaitable[FIFOExecutionResult]]
ArgumentParser = Callable[[str], BaseModel]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: ParsedToolHandler
    parser: ArgumentParser | None = None
    return_types: tuple[ToolContentType, ...] = ('ordinary',)
    progress: ToolProgress = DEFAULT_TOOL_PROGRESS
    progress_factory: Callable[[BaseModel], ToolProgress] | None = None

    def definition(self) -> dict:
        return {'type': 'function', 'function': {'name': self.name,
            'description': self.description, 'parameters': self.arguments_model.model_json_schema(), 'strict': False}}

    async def execute(self, operation: FIFOOperation, *, publish_progress: ToolProgressPublisher | None = None) -> FIFOExecutionResult:
        try:
            arguments = (self.parser(operation.arguments) if self.parser is not None else
                         validate_model_payload(self.arguments_model, load_model_json(operation.arguments)))
            if not isinstance(arguments, self.arguments_model):
                raise TypeError('解析器返回类型与注册契约不一致')
        except (ValueError, TypeError) as exc:
            # 只捕获解析阶段错误；执行异常交给 Executor 标记 unknown，禁止误称未生效。
            fields = ('、'.join('.'.join(map(str, e['loc'])) or '整体参数' for e in exc.errors()[:5])
                      if isinstance(exc, ValidationError) else '整体参数')
            return FIFOExecutionResult(status='failed', tool_result={
                'error': f'工具参数校验失败（{fields}），请按工具定义修正；本次操作未执行。'})
        # 参数合法后才显示正在执行；回执重放在外层 Executor 短路，不重复发状态。
        if publish_progress is not None:
            progress = self.progress_factory(arguments) if self.progress_factory else self.progress
            if not isinstance(progress, ToolProgress):
                raise TypeError('动态工具状态必须返回 ToolProgress')
            await publish_progress(progress)
        try:
            result = FIFOExecutionResult.model_validate(await self.handler(operation, arguments))
            # 返回校验在执行之后，失败不能声称副作用未发生；交给 Executor 保留 unknown 回执。
            # 工具即使只声明页面成功结果，参数/业务错误仍统一返回普通反馈。
            if result.status == 'succeeded' and result.content.type not in self.return_types:
                raise ValueError('工具实际返回类型不在注册声明中')
            return result
        finally:
            if publish_progress is not None:
                await publish_progress(DEFAULT_TOOL_PROGRESS)


class ToolRegistry:
    """装配时冻结名称集合；模型 Schema 和执行映射始终由同一批注册项生成。"""
    def __init__(self, entries: Iterable[RegisteredTool]):
        self._entries = tuple(entries)
        names = set()
        for entry in self._entries:
            if not isinstance(entry, RegisteredTool):
                raise TypeError('工具必须以 RegisteredTool 注册')
            if not isinstance(entry.name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', entry.name):
                raise ValueError('工具名称不符合函数调用契约')
            if entry.name in names:
                raise ValueError(f'工具名称重复，禁止覆盖：{entry.name}')
            names.add(entry.name)
            if not isinstance(entry.description, str) or not entry.description.strip():
                raise ValueError(f'工具缺少说明：{entry.name}')
            if not isinstance(entry.arguments_model, type) or not issubclass(entry.arguments_model, BaseModel):
                raise TypeError('工具参数必须使用 Pydantic 模型')
            if not callable(entry.handler) or (entry.parser is not None and not callable(entry.parser)):
                raise TypeError('工具处理器与解析器必须可调用')
            if (not isinstance(entry.return_types, tuple) or not entry.return_types
                    or any(kind not in ('ordinary', 'foldable') for kind in entry.return_types)
                    or len(set(entry.return_types)) != len(entry.return_types)):
                raise ValueError('工具返回类型必须为非空、不重复的 ordinary/foldable 元组')
            if entry.progress_factory is not None and not callable(entry.progress_factory):
                raise TypeError('动态工具状态构造器必须可调用')
            if not isinstance(entry.progress, ToolProgress):
                raise TypeError('工具状态必须使用 ToolProgress 配置')
            schema = entry.definition()['function']['parameters']
            if schema.get('type') != 'object':
                raise ValueError('工具参数顶层必须为对象')
            self._check_descriptions(schema, entry.name)

    @staticmethod
    def _check_descriptions(value, path):
        if isinstance(value, dict):
            for name, field in value.get('properties', {}).items():
                if not isinstance(field.get('description'), str) or not field['description'].strip():
                    raise ValueError(f'工具参数缺少字段说明：{path}.{name}')
            for key, child in value.items():
                ToolRegistry._check_descriptions(child, f'{path}.{key}')
        elif isinstance(value, list):
            for child in value:
                ToolRegistry._check_descriptions(child, path)

    def definitions(self) -> list[dict]:
        """每次生成独立 Schema，调用方不能改写注册表。"""
        return [entry.definition() for entry in self._entries]

    def require_supported_content(self, supported=('ordinary',)):
        """在任何动作执行前检查宿主能力，禁止把未接通展示的页面当成普通 JSON 使用。"""
        for entry in self._entries:
            if not set(entry.return_types) <= set(supported):
                raise ValueError(f'当前宿主尚不支持工具返回的页面内容：{entry.name}')

    def handlers(self, *, publish_progress: ToolProgressPublisher | None = None) -> dict:
        return {entry.name: (entry.execute if publish_progress is None else
                             partial(entry.execute, publish_progress=publish_progress))
                for entry in self._entries}


def build_agent_tool_registry(*, workspace_handler, publish_output, additional_tools=()) -> ToolRegistry:
    """每轮绑定受权回调，内建和扩展工具统一拒绝名称冲突。"""
    from .workspace import build_workspace_registrations
    from .interaction import build_interaction_registrations
    from .date_calculator import build_date_calculation_registration
    return ToolRegistry([build_date_calculation_registration(), *build_workspace_registrations(workspace_handler),
                         *(build_interaction_registrations(publish_output) if publish_output is not None else []), *additional_tools])
