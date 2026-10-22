"""中间件认证后的用户身份；不包含任何登录凭据。"""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

UserId = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r".*\S.*")]


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    userid: UserId = Field(description='中间件返回的稳定用户唯一标识，作为任务与会话所有者。')
    username: UserId = Field(description='中间件返回的当前用户名，只用于展示和业务操作人快照。')
    ttl: int = Field(gt=0, strict=True, description='本次中间件有效登录查询续期后的完整有效秒数。')
