"""历史工作区读取兼容契约；仅供新结构转换使用，禁止用于正式写入。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


WorkspaceText = Annotated[str, StringConstraints(strict=True, pattern=r'\S')]
WorkspaceKey = Annotated[str, StringConstraints(strict=True, pattern=r'^[a-z][a-z0-9_]*$')]
GoalStatus = Literal['pending', 'in_progress', 'completed', 'cancelled']
TaskStatus = Literal['pending', 'in_progress', 'blocked', 'completed', 'cancelled']


class WorkspaceObject(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class WorkspaceGoal(WorkspaceObject):
    description: WorkspaceText = Field(description='用户目标的明确描述，包含需要完成的对象与范围。')
    status: GoalStatus = Field(description='目标状态：待处理、处理中、已完成或已取消；不因本轮回复结束而自动完成。')


class WorkspaceInformation(WorkspaceObject):
    content: WorkspaceText = Field(description='可供继续任务使用的信息，保留必要限定，不把推断写成原文事实。')
    source_type: Literal['user', 'contract', 'external', 'analysis', 'legacy'] = Field(
        description='来源类型：用户陈述、合同原文、外部资料、分析结论或旧版记录；legacy 仅用于兼容历史。')
    source_ref: WorkspaceText | None = Field(
        description='实际取得的消息、文件与页码、外部资料或分析依据的定位；没有可靠定位时为 null，不猜测。')
    verification_status: Literal['user_reported', 'verified', 'unverified', 'conflicted'] = Field(
        description='信息核验状态：用户自述、已核验、待核验或存在冲突；已保存不等于已核验。')

    @model_validator(mode='after')
    def validate_evidence(self):
        if self.verification_status == 'user_reported' and self.source_type != 'user':
            raise ValueError('user_reported 仅适用于 user 来源')
        if self.verification_status == 'verified' and (self.source_ref is None or self.source_type == 'legacy'):
            raise ValueError('已核验信息必须具有实际来源定位，不能仅依据旧版记录认定')
        return self


class WorkspaceTask(WorkspaceObject):
    goal_id: WorkspaceKey | None = Field(description='所属目标的已有稳定键；独立任务或旧记录缺少关联时为 null。')
    description: WorkspaceText = Field(description='具体行动及其对象，必要时说明完成条件和等待条件。')
    status: TaskStatus = Field(description='任务状态：待处理、处理中、受阻、已完成或已取消。')
    depends_on: list[WorkspaceKey] = Field(description='前置任务的已有稳定键列表；没有依赖时为空，不允许重复、自引用或环。')


class WorkspacePayload(WorkspaceObject):
    goals: dict[WorkspaceKey, WorkspaceGoal] = Field(description='以稳定目标 ID 为键的目标对象；创建后不得因改名或状态变化更换键。')
    constraints: dict[WorkspaceKey, WorkspaceText] = Field(description='明确约束的键值映射，例如 perspective、scope；键只含小写字母、数字和下划线且以字母开头。')
    information: dict[WorkspaceKey, WorkspaceInformation] = Field(description='以稳定信息 ID 为键的信息对象，分别记录内容、来源与核验状态。')
    tasks: dict[WorkspaceKey, WorkspaceTask] = Field(description='以稳定任务 ID 为键的任务对象，分别记录目标关联、描述、状态和依赖。')

    @model_validator(mode='after')
    def validate_relations(self):
        # 全量结构校验也用于局部修改后的候选快照，避免删除或修改产生悬空引用。
        remaining = {}
        dependents: dict[str, list[str]] = {key: [] for key in self.tasks}
        for key, task in self.tasks.items():
            if task.goal_id is not None and task.goal_id not in self.goals:
                raise ValueError(f'tasks.{key}.goal_id 引用不存在的目标')
            if len(task.depends_on) != len(set(task.depends_on)):
                raise ValueError(f'tasks.{key}.depends_on 含重复依赖')
            remaining[key] = len(task.depends_on)
            for dependency in task.depends_on:
                if dependency not in self.tasks or dependency == key:
                    raise ValueError(f'tasks.{key}.depends_on 含不存在的任务或自引用')
                dependents[dependency].append(key)
        ready = [key for key, count in remaining.items() if count == 0]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for dependent in dependents[key]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
        if visited != len(self.tasks):
            raise ValueError('任务依赖不允许形成环')
        return self


class WorkspaceSnapshot(WorkspaceObject):
    payload: WorkspacePayload = Field(description='当前会话工作区的四分区 KV 内容。')
    revision: int = Field(ge=0, description='工作区内容版本；每次接受内存更新后递增。')
    updated_at: int = Field(ge=0, description='本次工作区内容更新时间，UTC Unix 毫秒。')


class _LegacyWorkspacePayload(WorkspaceObject):
    achieved_goals: list[WorkspaceText]
    known_information: list[WorkspaceText]
    next_tasks: list[WorkspaceText]


def read_workspace_payload(value: dict) -> WorkspacePayload:
    """只在持久化读取边界兼容旧格式；普通写入仍必须遵循新的 KV 契约。

    稳定键按原始顺序生成，重复读取不改变身份，不推断旧记录缺失的来源和关联。
    读取本身不修改数据库、内容版本和更新时间。
    """
    legacy_fields = {'achieved_goals', 'known_information', 'next_tasks'}
    if isinstance(value, dict) and set(value) == legacy_fields:
        old = _LegacyWorkspacePayload.model_validate(value)
        value = {
            'goals': {f'legacy_goal_{i:04d}': {'description': text, 'status': 'completed'}
                      for i, text in enumerate(old.achieved_goals, 1)},
            'constraints': {},
            'information': {f'legacy_info_{i:04d}': {
                'content': text, 'source_type': 'legacy', 'source_ref': None, 'verification_status': 'unverified',
            } for i, text in enumerate(old.known_information, 1)},
            'tasks': {f'legacy_task_{i:04d}': {
                'goal_id': None, 'description': text, 'status': 'pending', 'depends_on': [],
            } for i, text in enumerate(old.next_tasks, 1)},
        }
    return WorkspacePayload.model_validate(value)
