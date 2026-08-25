"""FastAPI 应用工厂与生命周期管理。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.router import api_router
from app.core.config import get_settings
from app.infrastructure.elasticsearch import create_elasticsearch_client


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """在应用启动和关闭时管理共享的 Elasticsearch 客户端。"""
    application.state.elasticsearch = create_elasticsearch_client(get_settings())
    try:
        yield
    finally:
        await application.state.elasticsearch.close()


def create_app() -> FastAPI:
    """创建供 ASGI 服务器和测试使用的应用实例。"""
    application = FastAPI(
        title="Contract Processor API",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(api_router, prefix="/api")
    return application


app = create_app()
