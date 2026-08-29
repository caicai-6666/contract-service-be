"""聚合版本化 API 路由。"""

from fastapi import APIRouter

from app.api.route import contract_extraction, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(contract_extraction.router)
