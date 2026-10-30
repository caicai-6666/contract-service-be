"""中间件认证后的用户身份；不包含任何登录凭据。"""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, AfterValidator

UserId = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r".*\S.*")]


DepartmentName = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


def _unique_departments(values):
    # 与中间件一致：保存名称而非部门ID，规范化后仍不允许重复。
    if len(set(values)) != len(values):
        raise ValueError('部门名称不能重复')
    return values


Departments = Annotated[tuple[DepartmentName, ...], AfterValidator(_unique_departments),
    Field(json_schema_extra={'uniqueItems': True})]


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    userid: UserId = Field(description='中间件返回的稳定用户唯一标识，作为任务与会话所有者。')
    username: UserId = Field(description='中间件返回的当前用户名，只用于展示和业务操作人快照。')
    departments: Departments = Field(default=(), description='中间件返回的当前部门名称列表，无部门时为空；仅作用户资料，不代表权限、管理员身份或主部门。')
    ttl: int = Field(gt=0, strict=True, description='本次中间件有效登录查询续期后的完整有效秒数。')
