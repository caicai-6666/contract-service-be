"""聚合版本化 API 路由。"""

from fastapi import APIRouter

from app.api.route import health

api_router = APIRouter()
api_router.include_router(health.router)
