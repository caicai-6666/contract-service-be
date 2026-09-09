"""应用启动期依赖装配与资源生命周期管理。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.contract_document_detection import (
    build_contract_document_detection_graph,
)
from app.agent.contract_extraction.subgraph.classification.catalog import (
    load_contract_category_catalog,
)
from app.agent.contract_extraction.subgraph.field_extraction.catalog import (
    load_field_definition_catalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.catalog import (
    load_retrieval_view_guide_catalog,
)
from app.agent.pdf_deduplication import build_pdf_deduplication_graph
from app.agent.conversation_memory import build_conversation_memory_graph
from app.core.config import get_settings
from app.core.tool_tag import initialize_mllm_tool_tag
from app.infrastructure.contract_index import synchronize_contract_index
from app.infrastructure.model_concurrency import get_model_request_limiter
from app.infrastructure.communication_store import SQLiteCommunicationStore
from app.infrastructure.contract_file_store import LocalContractFileStore
from app.infrastructure.contract_metadata_store import (
    SQLiteContractMetadataStore,
)
from app.infrastructure.elasticsearch import create_elasticsearch_client
from app.infrastructure.pdf_candidate_loader import (
    LocalPDFDuplicateCandidateLoader,
)
from app.service.auth import AuthService, LoginCodeCache
from app.service.communication import CommunicationEventService
from app.service.communication_history import ConversationHistoryService
from app.service.communication_archive import CommunicationArchiveService
from app.service.communication_demo import DemoEventService
from app.service.contract_ingestion import ContractIngestionService
from app.service.contract_extraction import (
    AgentContractDocumentDetectionExecutor,
    AgentContractExtractionExecutor,
    AgentPDFDeduplicationExecutor,
    ContractExtractionService,
)
from app.service.pdf_preparation import AsyncPDFPreparationService
from app.user import load_reviewer_user_catalog

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """装配应用级依赖，并统一管理共享资源的启动与关闭。"""
    settings = get_settings()
    # 模板先于数据库和外部客户端加载；配置错误时尽早中止启动。
    initialize_mllm_tool_tag(settings.mllm)
    logger.info("MLLM 工具调用模板加载完成：file=%s", settings.mllm.tool_tag_file)
    # 在任何工作流启动前固定两类全局请求额度，客户端只复用、不重建。
    get_model_request_limiter("mllm", settings.mllm.max_concurrent_requests)
    get_model_request_limiter("embedding", settings.embedding.max_concurrent_requests)
    # 先初始化持久化结构；归档只消费统一历史，不将 SSE 快照当作原轨迹。
    communication_store = SQLiteCommunicationStore(settings.communication_database_path)
    communication_store.initialize()
    application.state.communication_store = communication_store
    communication_history_service = ConversationHistoryService(communication_store)
    application.state.communication_history_service = communication_history_service
    communication_memory_graph = build_conversation_memory_graph()
    application.state.conversation_memory_graph = communication_memory_graph
    communication_archive_service = CommunicationArchiveService(communication_memory_graph, communication_history_service)
    application.state.communication_archive_service = communication_archive_service

    # 权威业务定义只在进程启动时读取一次，运行期间统一复用不可变快照。
    contract_category_catalog = load_contract_category_catalog(
        settings.contract_category_definition_path
    )
    field_definition_catalog = load_field_definition_catalog(
        settings.field_definition_path
    )
    retrieval_view_guide_catalog = load_retrieval_view_guide_catalog(
        settings.retrieval_view_guide_path,
        known_category_codes={
            category.definition.code
            for category in contract_category_catalog.categories
        },
    )
    reviewer_user_catalog = load_reviewer_user_catalog(
        settings.reviewer_user_path
    )

    application.state.contract_category_catalog = contract_category_catalog
    application.state.field_definition_catalog = field_definition_catalog
    application.state.retrieval_view_guide_catalog = (
        retrieval_view_guide_catalog
    )
    application.state.reviewer_user_catalog = reviewer_user_catalog
    login_code_cache = LoginCodeCache(
        ttl_seconds=settings.auth_login_code_ttl_seconds
    )
    auth_service = AuthService(
        reviewer_users=reviewer_user_catalog,
        login_code_cache=login_code_cache,
    )
    application.state.login_code_cache = login_code_cache
    application.state.auth_service = auth_service
    logger.info(
        "审核用户加载完成：count=%s source=%s login_code_ttl_seconds=%s",
        len(reviewer_user_catalog.users),
        reviewer_user_catalog.source_path,
        settings.auth_login_code_ttl_seconds,
    )

    elasticsearch = create_elasticsearch_client(settings)
    application.state.elasticsearch = elasticsearch
    contract_extraction_service: ContractExtractionService | None = None
    # 联调仅替换激活后的事件生产者，继续复用当前进程的登录、路由和生命周期。
    communication_event_service = (
        DemoEventService() if settings.communication_demo_enabled else CommunicationEventService()
    )
    if settings.communication_demo_enabled:
        logger.warning("Communication 展示工作流已启用：输出均为模拟内容，不执行真实合同分析")
    application.state.communication_event_service = communication_event_service
    communication_event_service.bind_history(communication_history_service)
    try:
        await communication_event_service.start()
        index_sync = await synchronize_contract_index(
            elasticsearch,
            settings,
            field_definition_catalog,
        )
        application.state.contract_index_sync = index_sync
        logger.info(
            "Elasticsearch 合同索引同步完成：index=%s created=%s added_core=%s added_retrieval_questions=%s",
            index_sync.index_name,
            index_sync.created,
            list(index_sync.added_core_fields),
            index_sync.added_retrieval_questions,
        )

        pdf_candidate_loader = LocalPDFDuplicateCandidateLoader(settings.mllm)
        application.state.pdf_duplicate_candidate_loader = pdf_candidate_loader
        contract_file_store = LocalContractFileStore(
            root=pdf_candidate_loader.root
        )
        application.state.contract_file_store = contract_file_store
        contract_document_detection_graph = (
            build_contract_document_detection_graph()
        )
        application.state.contract_document_detection_graph = (
            contract_document_detection_graph
        )
        logger.info("合同文档识别工作流装配完成")
        pdf_deduplication_graph = build_pdf_deduplication_graph(
            elasticsearch,
            index_name=settings.elasticsearch_index_name,
            candidate_loader=pdf_candidate_loader,
        )
        application.state.pdf_deduplication_graph = pdf_deduplication_graph
        logger.info(
            "PDF 查重工作流装配完成：contract_root=%s",
            pdf_candidate_loader.root,
        )

        executor = AgentContractExtractionExecutor(
            category_catalog=contract_category_catalog,
            field_catalog=field_definition_catalog,
            retrieval_guide_catalog=retrieval_view_guide_catalog,
        )
        pdf_preparation_service = AsyncPDFPreparationService(settings.mllm)
        contract_metadata_store = SQLiteContractMetadataStore(
            settings.contract_metadata_database_path
        )
        contract_ingestion_service = ContractIngestionService(
            elasticsearch=elasticsearch,
            index_name=settings.elasticsearch_index_name,
            file_store=contract_file_store,
            metadata_store=contract_metadata_store,
            category_catalog=contract_category_catalog,
            field_catalog=field_definition_catalog,
            vector_dimensions=settings.elasticsearch_vector_dimensions,
        )
        await contract_ingestion_service.initialize()
        application.state.contract_metadata_store = contract_metadata_store
        application.state.contract_ingestion_service = contract_ingestion_service
        logger.info(
            "SQLite 合同元数据目录初始化完成：database=%s",
            contract_metadata_store.database_path,
        )
        contract_extraction_service = ContractExtractionService(
            executor=executor,
            document_detection_executor=(
                AgentContractDocumentDetectionExecutor(
                    contract_document_detection_graph
                )
            ),
            deduplication_executor=AgentPDFDeduplicationExecutor(
                pdf_deduplication_graph
            ),
            pdf_preparation_service=pdf_preparation_service,
            ingestion_service=contract_ingestion_service,
            run_ttl_seconds=settings.contract_extraction_run_ttl_seconds,
            deduplication_review_ttl_seconds=(
                settings.contract_deduplication_review_ttl_seconds
            ),
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
        await communication_archive_service.start()
        yield
    finally:
        # 先停止归档模型作业，再冻结事件生产，最后备份原轨迹及工作区。
        await communication_archive_service.close()
        await communication_event_service.close()
        await communication_history_service.close()
        if contract_extraction_service is not None:
            await contract_extraction_service.close()
        await elasticsearch.close()
