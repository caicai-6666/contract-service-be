"""服务健康检查接口。"""

from fastapi import APIRouter

from app.core.config import get_settings
from app.schema.health import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="获取服务运行状态")
async def get_health() -> HealthResponse:
    """返回进程状态；不把外部服务连通性混入存活检查。"""
    settings = get_settings()
    return HealthResponse(status="ok", environment=settings.app_env)
