"""健康检查响应契约。"""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """服务进程状态。"""

    status: Literal["ok"]
    environment: str
