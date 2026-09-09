"""会话工作区的内部契约；不进入公开轨迹或前端历史响应。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


WorkspaceItem = Annotated[str, StringConstraints(strict=True, pattern=r'\S')]


class WorkspacePayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    achieved_goals: list[WorkspaceItem] = Field(description='已经达成的目标。')
    known_information: list[WorkspaceItem] = Field(description='已确认且可供后续任务使用的信息。')
    next_tasks: list[WorkspaceItem] = Field(description='接下来需要处理的任务。')


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    payload: WorkspacePayload
    revision: int = Field(ge=0, description='工作区内容版本；每次接受内存更新后递增。')
    updated_at: int = Field(ge=0, description='本次工作区内容更新时间，UTC Unix 毫秒。')
