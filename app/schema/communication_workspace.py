"""面向任务探索的四区域工作区；字段校验不替代事实核验。"""

import json
from typing import Literal

from pydantic import Field, model_validator

from .communication_workspace_legacy import (
    WorkspaceKey, WorkspaceText, WorkspaceObject,
    read_workspace_payload as _read_legacy_workspace,
)


class WorkspaceTaskConstraints(WorkspaceObject):
    task: WorkspaceText | None = Field(description='用户当前明确提出的任务；尚未确定时为 null，不从旧规划猜测用户要求。')
    supplements: dict[WorkspaceKey, WorkspaceText] = Field(description='按稳定补充 ID 保存的用户立场、范围、条件和交付要求；不记录助手自己的计划。')

    @model_validator(mode='after')
    def validate_supplement_keys(self):
        # 局部更新接口以 task 定位主任务，补充键不能占用该保留名。
        if 'task' in self.supplements:
            raise ValueError('用户补充不能使用保留键 task')
        return self


class WorkspaceKnownInformation(WorkspaceObject):
    content: WorkspaceText = Field(description='可复用的事实、背景或材料线索，保留对象、适用范围和必要限制。')
    source: WorkspaceText | None = Field(description='实际用户陈述、文件及页码、外部来源或分析依据；没有可靠来源时为 null，不猜测。')
    status: Literal['confirmed', 'user_reported', 'unverified', 'conflicted'] = Field(
        description='信息状态：已确认、用户陈述、待核验或存在冲突；用户陈述不自动成为客观事实。')

    @model_validator(mode='after')
    def validate_source(self):
        if self.status == 'confirmed' and self.source is None:
            raise ValueError('已确认信息必须保留来源')
        return self


class WorkspaceRemainingDirection(WorkspaceObject):
    plan: WorkspaceText = Field(description='尚需尝试的探索方向，明确目的、对象和必要前提；不是单次调用流水账。')


class WorkspaceExploredDirection(WorkspaceObject):
    plan: WorkspaceText = Field(description='已经实际尝试的规划及其范围，保留探索目的。')
    outcome: Literal['succeeded', 'failed', 'inconclusive'] = Field(description='探索结果：成功、失败或尚无定论；未找到材料不自动意味着失败或材料不存在。')
    conclusion: WorkspaceText = Field(description='本次探索得到的结论、依据、限制或失败原因，不记录冗长草稿和临时纠错链。')
    information_ids: list[WorkspaceKey] = Field(default_factory=list, description='本次结论引用的已有已知信息 ID；没有引用时为空，不得重复或悬空。')


class WorkspacePayload(WorkspaceObject):
    task_constraints: WorkspaceTaskConstraints = Field(description='用户的当前任务及后续补充要求。')
    known_information: dict[WorkspaceKey, WorkspaceKnownInformation] = Field(description='以稳定信息 ID 定位的可复用事实、背景和材料线索。')
    explored_directions: dict[WorkspaceKey, WorkspaceExploredDirection] = Field(description='以稳定方向 ID 定位的已尝试规划、结果及结论。')
    remaining_directions: dict[WorkspaceKey, WorkspaceRemainingDirection] = Field(description='以稳定方向 ID 定位的剩余规划；执行中的方向在形成结果前仍保留于此。')

    @model_validator(mode='after')
    def validate_references(self):
        if self.explored_directions.keys() & self.remaining_directions.keys():
            raise ValueError('同一方向 ID 不能同时存在于已探索与剩余规划中')
        for key, direction in self.explored_directions.items():
            ids = direction.information_ids
            if len(ids) != len(set(ids)) or any(ref not in self.known_information for ref in ids):
                raise ValueError(f'explored_directions.{key}.information_ids 含重复或不存在的信息引用')
        return self


class WorkspaceSnapshot(WorkspaceObject):
    payload: WorkspacePayload = Field(description='当前会话工作区的四区域内容。')
    revision: int = Field(ge=0, description='工作区内容版本；每次接受内存更新后递增。')
    updated_at: int = Field(ge=0, description='内容更新时间，UTC Unix 毫秒。')


def empty_workspace_payload() -> dict:
    """创建未明确用户任务的空工作区，不生成虚构任务或规划。"""
    return {'task_constraints': {'task': None, 'supplements': {}}, 'known_information': {},
            'explored_directions': {}, 'remaining_directions': {}}


def read_workspace_payload(value: dict) -> WorkspacePayload:
    """只在存储读取边界兼容旧三列表与第一版 KV，不改写磁盘或版本。

    旧目标未必是用户原始任务，保存为待核验信息而不升级为用户要求。
    已完成旧任务缺少结论时记为 inconclusive；取消任务不重新加入剩余规划。
    """
    shapes = ({'achieved_goals', 'known_information', 'next_tasks'}, {'goals', 'constraints', 'information', 'tasks'})
    if not isinstance(value, dict) or set(value) not in shapes:
        return WorkspacePayload.model_validate(value)
    old = _read_legacy_workspace(value)
    result = empty_workspace_payload()
    known = result['known_information']

    def record(key, content, source, status='unverified'):
        candidate = key
        suffix = 1
        while candidate in known:
            candidate = f'{key}_{suffix}'
            suffix += 1
        known[candidate] = {'content': content, 'source': source, 'status': status}

    for key, info in old.information.items():
        status = {'verified': 'confirmed'}.get(info.verification_status, info.verification_status)
        source = f'旧工作区 information.{key}；来源类型：{info.source_type}'
        if info.source_ref is not None:
            source += f'；定位：{info.source_ref}'
        record(key, info.content, source, status)
    for key, goal in old.goals.items():
        record(f'legacy_goal_{key}', json.dumps(goal.model_dump(), ensure_ascii=False), f'旧工作区 goals.{key}')
    for key, text in old.constraints.items():
        result['task_constraints']['supplements'][f'legacy_supplement_{key}'] = f'旧约束 {key}：{text}'
    for key, task in old.tasks.items():
        if task.status == 'cancelled':
            record(f'legacy_cancelled_{key}', json.dumps(task.model_dump(), ensure_ascii=False), f'旧工作区 tasks.{key}，已取消，不自动续做')
        elif task.status == 'completed':
            result['explored_directions'][key] = {
                'plan': task.description, 'outcome': 'inconclusive',
                'conclusion': '旧任务标记为 completed，但未记录具体探索结论；原记录：' + json.dumps(task.model_dump(), ensure_ascii=False),
                'information_ids': [],
            }
        else:
            result['remaining_directions'][key] = {'plan': task.description + '；旧记录：' + json.dumps(task.model_dump(), ensure_ascii=False)}
    return WorkspacePayload.model_validate(result)
