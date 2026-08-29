"""FastAPI 应用工厂与生命周期管理。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.agent.contract_extraction.subgraph.classification.catalog import (
    load_contract_category_catalog,
)
from app.agent.contract_extraction.subgraph.field_extraction.catalog import (
    load_field_definition_catalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.catalog import (
    load_retrieval_view_guide_catalog,
)
from app.api.router import api_router
from app.core.config import get_settings
from app.infrastructure.elasticsearch import create_elasticsearch_client
from app.service.contract_extraction import (
    AgentContractExtractionExecutor,
    ContractExtractionService,
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """加载只读业务目录，并管理共享的 Elasticsearch 客户端。"""
    settings = get_settings()
    contract_category_catalog = load_contract_category_catalog(
        settings.contract_category_definition_path
    )
    application.state.contract_category_catalog = contract_category_catalog
    application.state.field_definition_catalog = load_field_definition_catalog(
        settings.field_definition_path
    )
    application.state.retrieval_view_guide_catalog = (
        load_retrieval_view_guide_catalog(
            settings.retrieval_view_guide_path,
            known_category_codes={
                category.definition.code
                for category in contract_category_catalog.categories
            },
        )
    )
    elasticsearch = create_elasticsearch_client(settings)
    application.state.elasticsearch = elasticsearch
    contract_extraction_service: ContractExtractionService | None = None
    try:
        executor = AgentContractExtractionExecutor(
            category_catalog=application.state.contract_category_catalog,
            field_catalog=application.state.field_definition_catalog,
            retrieval_guide_catalog=(
                application.state.retrieval_view_guide_catalog
            ),
        )
        contract_extraction_service = ContractExtractionService(
            executor=executor,
            run_ttl_seconds=settings.contract_extraction_run_ttl_seconds,
            cleanup_interval_seconds=(
                settings.contract_extraction_cleanup_interval_seconds
            ),
            event_buffer_size=settings.contract_extraction_event_buffer_size,
            sse_heartbeat_seconds=(
                settings.contract_extraction_sse_heartbeat_seconds
            ),
            max_stage_attempts=settings.contract_extraction_max_stage_attempts,
        )
        await contract_extraction_service.start()
        application.state.contract_extraction_service = (
            contract_extraction_service
        )
        yield
    finally:
        if contract_extraction_service is not None:
            await contract_extraction_service.close()
        await elasticsearch.close()


def create_app() -> FastAPI:
    """创建供 ASGI 服务器和测试使用的应用实例。"""
    application = FastAPI(
        title="Contract Processor API",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(api_router, prefix="/contract/api")
    return application


app = create_app()


def run() -> None:
    """按环境配置启动支持热更新的本地 ASGI 服务。"""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=settings.app_reload,
    )


if __name__ == "__main__":
    run()
