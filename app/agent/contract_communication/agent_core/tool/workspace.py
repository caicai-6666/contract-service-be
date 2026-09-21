"""模型可见的三个工作区工具；纯校验与候选编辑和受权提交分离。"""

from app.infrastructure.model_json import load_model_json, normalize_model_json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.schema.communication_workspace import (
    WorkspaceObject, WorkspaceText, WorkspaceKey, WorkspacePayload, WorkspaceSnapshot,
    WorkspaceKnownInformation, WorkspaceRemainingDirection, WorkspaceExploredDirection,
)


WorkspacePath = Annotated[str, StringConstraints(strict=True, pattern=r'^/[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$')]


class ExplorationResult(WorkspaceObject):
    outcome: Literal['succeeded', 'failed', 'inconclusive'] = Field(description='本次探索成功、失败或尚无定论；未找到材料不能直接认定材料不存在。')
    conclusion: WorkspaceText = Field(description='本次已完成探索得到的精简结论、依据、限制或失败原因，不提交临时草稿。')
    information_ids: list[WorkspaceKey] = Field(default_factory=list, description='结论引用的已有已知信息 ID，不能重复或悬空；无引用时为空列表。')


class WorkspaceReplaceArguments(WorkspaceObject):
    path: WorkspacePath = Field(description='已有字段或条目的绝对路径，例如 /task_constraints/task、/task_constraints/supplements/supplement_001、/known_information/info_001/status；不允许根、区域容器、数组下标或通配符。')
    value: WorkspaceText | None | list[WorkspaceKey] | WorkspaceKnownInformation | WorkspaceRemainingDirection | WorkspaceExploredDirection = Field(
        description='完全替换目标位置的新值；类型与该位置一致。整体条目须提供完整对象；null 仅可用于 task 或 source；information_ids 用完整 ID 列表替换。未指定位置不变。')


class WorkspaceAddArguments(WorkspaceObject):
    path: Literal['/task_constraints/supplements', '/known_information', '/remaining_directions', '/explored_directions'] = Field(
        description='新增条目的目标容器，不能附加自拟 ID。用户补充传文本，已知信息或剩余方向传完整对象；已探索区传探索结果并指定 direction_id。')
    value: WorkspaceText | WorkspaceKnownInformation | WorkspaceRemainingDirection | ExplorationResult = Field(
        description='新增内容：补充文本、{content, source, status} 信息、{plan} 规划，或 {outcome, conclusion, information_ids} 探索结果；不携带条目 ID。')
    direction_id: WorkspaceKey | None = Field(default=None,
        description='仅向 /explored_directions 新增结果时必填，必须是剩余规划中已有的方向 ID；程序保留原规划和 ID 并原子移除剩余项。其他新增必须省略或为 null。')

    @model_validator(mode='after')
    def validate_target(self):
        types = {'/task_constraints/supplements': str, '/known_information': WorkspaceKnownInformation,
                 '/remaining_directions': WorkspaceRemainingDirection, '/explored_directions': ExplorationResult}
        if not isinstance(self.value, types[self.path]):
            raise ValueError('value 类型与新增目标容器不一致')
        if (self.path == '/explored_directions') != (self.direction_id is not None):
            raise ValueError('direction_id 仅在新增已探索结果时必填，其他位置不得提供')
        return self


class WorkspaceDeleteArguments(WorkspaceObject):
    path: WorkspacePath = Field(description='要删除的已有完整条目路径，例如 /remaining_directions/direction_001；仅支持补充、已知信息、已探索或剩余方向条目，不删除根、容器和必填字段。不自动删除关联引用。')


_MODELS = {'workspace_replace': WorkspaceReplaceArguments, 'workspace_add': WorkspaceAddArguments,
           'workspace_delete': WorkspaceDeleteArguments}
_DESCRIPTIONS = {
    'workspace_replace': '当你需要更新工作区中已有的信息、修正任务约束或整理已有条目时，使用这个工具。用新值替换已有字段或完整条目，保留稳定 ID 和其他内容；路径不存在、类型或引用不合法时整次拒绝。',
    'workspace_add': '当你需要保存后续步骤仍会使用的重要信息、任务补充、探索计划或探索结果时，使用这个工具。向指定工作区容器新增一个条目，新增位置以最新工作区为准。普通新增由程序生成 ID；新增已探索结果时必须指定剩余方向 ID，原子转移并保留原规划。',
    'workspace_delete': '当你需要清理工作区中过时、重复或已无保留价值的条目，为后续信息腾出空间时，使用这个工具。删除指定条目，不级联删除其他信息；必要字段、容器、缺失条目或仍被引用的信息不能删除。',
}


def build_workspace_tools() -> list[dict]:
    """按固定顺序从唯一参数模型生成工具 Schema，每次返回独立对象。"""
    return [{'type': 'function', 'function': {'name': name, 'description': _DESCRIPTIONS[name],
             'parameters': model.model_json_schema(), 'strict': False}} for name, model in _MODELS.items()]


def parse_workspace_tool_arguments(name: str, raw_arguments: str) -> BaseModel:
    """只解析真实工具调用参数；拒绝重复键与非标准数值，不降级解析普通文本。"""
    if name not in _MODELS:
        raise ValueError('未知工作区工具')

    decoded = load_model_json(raw_arguments)
    schema = _MODELS[name].model_json_schema()
    # value 是文本/对象的联合类型，必须由目标路径消歧，不能按字符串外观猜测。
    if isinstance(decoded, dict) and isinstance(decoded.get('path'), str):
        path = decoded['path']
        target = None
        if name == 'workspace_add':
            target = {'/known_information': WorkspaceKnownInformation,
                      '/remaining_directions': WorkspaceRemainingDirection,
                      '/explored_directions': ExplorationResult}.get(path)
        elif name == 'workspace_replace':
            parts = path.split('/')[1:]
            if len(parts) == 2:
                target = {'known_information': WorkspaceKnownInformation,
                          'remaining_directions': WorkspaceRemainingDirection,
                          'explored_directions': WorkspaceExploredDirection}.get(parts[0])
            elif len(parts) == 3 and parts[0] == 'explored_directions' and parts[2] == 'information_ids':
                schema['properties']['value'] = {'type': 'array', 'items': {'type': 'string'}}
        if target is not None:
            target_schema = target.model_json_schema()
            schema.setdefault('$defs', {}).update(target_schema.pop('$defs', {}))
            schema['properties']['value'] = target_schema
    return _MODELS[name].model_validate(normalize_model_json(decoded, schema))


@dataclass(frozen=True, slots=True)
class WorkspaceEdit:
    """已通过校验但尚未提交的候选内容；不是工具执行成功结果。"""
    payload: WorkspacePayload
    path: str
    removed_path: str | None = None


_ENTRY_REGIONS = ('known_information', 'explored_directions', 'remaining_directions')


def _is_entry(parts: list[str]) -> bool:
    return (len(parts) == 2 and parts[0] in _ENTRY_REGIONS) or (
        len(parts) == 3 and parts[:2] == ['task_constraints', 'supplements'])


def _locate(payload: dict, parts: list[str]):
    current = payload
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise ValueError('path 指向不存在的位置')
        current = current[part]
    return current


def prepare_workspace_edit(snapshot: WorkspaceSnapshot, name: str, raw_arguments: str) -> WorkspaceEdit:
    """在深拷贝中应用一次操作，再校验完整工作区；失败不修改原快照。"""
    arguments = parse_workspace_tool_arguments(name, raw_arguments)
    payload = snapshot.payload.model_dump()
    parts = arguments.path.split('/')[1:]
    removed_path = None
    path = arguments.path
    if isinstance(arguments, WorkspaceAddArguments):
        value = arguments.model_dump()['value']
        if arguments.path == '/explored_directions':
            key = arguments.direction_id
            if key not in payload['remaining_directions']:
                raise ValueError('direction_id 不在剩余规划中')
            direction = payload['remaining_directions'].pop(key)
            payload['explored_directions'][key] = {'plan': direction['plan'], **value}
            removed_path = f'/remaining_directions/{key}'
        else:
            target = _locate(payload, parts)
            prefix = {'/task_constraints/supplements': 'supplement', '/known_information': 'info',
                      '/remaining_directions': 'direction'}[arguments.path]
            key = f'{prefix}_{uuid4().hex}'
            while key in target or key in payload['explored_directions']:
                key = f'{prefix}_{uuid4().hex}'
            target[key] = value
        path += f'/{key}'
    else:
        entry = _is_entry(parts)
        if isinstance(arguments, WorkspaceReplaceArguments):
            allowed = entry or parts == ['task_constraints', 'task'] or (
                len(parts) == 3 and parts[0] in _ENTRY_REGIONS)
            if not allowed:
                raise ValueError('path 不是允许替换的字段或条目')
        elif not entry:
            raise ValueError('path 不是允许删除的完整条目')
        parent = _locate(payload, parts[:-1])
        if parts[-1] not in parent:
            raise ValueError('path 指向不存在的位置')
        if isinstance(arguments, WorkspaceReplaceArguments):
            parent[parts[-1]] = arguments.model_dump()['value']
        else:
            del parent[parts[-1]]
            removed_path = path
    validated = WorkspacePayload.model_validate(payload)
    return WorkspaceEdit(payload=validated, path=path, removed_path=removed_path)


async def execute_workspace_tool(
    *, name: str, raw_arguments: str, snapshot: WorkspaceSnapshot,
    commit: Callable[[dict, int], Awaitable[WorkspaceSnapshot]],
) -> dict:
    """通过注入的授权提交函数执行工具；只有成功提交后返回新工作区。

    调用方负责单工具协议、审计、纠错清理和取消/替代隔离；commit 必须绑定
    当前用户与会话并校验 expected_revision，不从模型参数读取这些执行身份。
    """
    edit = prepare_workspace_edit(snapshot, name, raw_arguments)
    accepted = await commit(edit.payload.model_dump(), snapshot.revision)
    return {'status': 'accepted', 'path': edit.path, 'removed_path': edit.removed_path,
            'revision': accepted.revision, 'workspace': accepted.payload.model_dump()}


def build_workspace_registrations(handler):
    """路径消歧继续复用原解析器；管理图仍独立验收完整候选工作区。"""
    from functools import partial
    from .registry import RegisteredTool
    async def execute(operation, arguments):
        # 保留原始参数供图内校验、保护修改目标及审计，不把解析后的副本写回轨迹。
        return await handler(operation)
    return [RegisteredTool(name, _DESCRIPTIONS[name], model, execute,
                           partial(parse_workspace_tool_arguments, name), return_types=('ordinary',))
            for name, model in _MODELS.items()]
