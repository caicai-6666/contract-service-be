"""正式会话的文件工具装配：应用共享合同池，驻留会话独立阅读状态。"""
import asyncio
import hashlib
import logging
from threading import Event
from pathlib import Path

from app.agent.contract_communication.agent_core.tool.file_viewer import (
    FileDescriptor, FileModelKey, FileModelCache, FileViewer, FileViewError,
    read_contract_file, build_file_view_registrations,
)
from app.infrastructure.pdf_candidate_loader import DEFAULT_CONTRACT_FILE_ROOT
from app.tool.pdf_page import PDFPageRenderConfig

logger = logging.getLogger(__name__)


class CommunicationFileTools:
    def __init__(self, history, settings, *, metadata_store, elasticsearch=None, field_catalog=None, category_catalog=None, contract_directory=DEFAULT_CONTRACT_FILE_ROOT):
        self._field_catalog = field_catalog
        self._category_catalog = category_catalog
        self._metadata_store = metadata_store
        self._elasticsearch = elasticsearch
        self._history = history
        self._settings = settings
        self._contract_directory = Path(contract_directory)
        self._upload_directory = settings.communication_database_path.parent / 'upload'
        self.contract_pool = FileModelCache(max_files=settings.communication_contract_file_cache_max_files)
        from app.agent.contract_communication.agent_core.tool.web_search import WebSearchService
        self._web_search = WebSearchService(
            timeout=settings.communication_web_search_timeout_seconds,
            max_results=settings.communication_web_search_max_results,
            concurrency=settings.communication_web_search_max_concurrent_requests)
        self._sessions = {}
        self._cleanups = set()
        self._closed = False
        self._lock = asyncio.Lock()

    async def prepare(self, conversation_id, owner):
        async with self._lock:
            if self._closed:
                raise RuntimeError('文件工具服务已关闭')
            residency, _ = await self._history.file_tool_access(conversation_id, secret_key=owner)
            previous = self._sessions.get(conversation_id)
            if previous and previous['residency'] != residency:
                self.evict(conversation_id)
                previous = None
            if previous:
                return previous['tools'], previous['resolver']
            loop = asyncio.get_running_loop()
            revoked = Event()
            pool = FileModelCache(max_files=self._settings.communication_session_file_cache_max_files)

            def access(file_id=None):
                # 仅由 FileViewer 的工作线程调用；主循环仍负责历史锁与用户隔离。
                if revoked.is_set():
                    raise PermissionError('会话已释放')
                current, name = asyncio.run_coroutine_threadsafe(
                    self._history.file_tool_access(conversation_id, secret_key=owner, file_id=file_id), loop,
                ).result()
                if revoked.is_set() or current != residency:
                    raise PermissionError('会话驻留身份已变化')
                return name

            def authorize(file_id):
                access(file_id)
                return True

            def read_session(file_id):
                access(file_id)
                # 当前任务附件可能尚未落盘：优先读取历史服务持有的内存字节。
                _, content = asyncio.run_coroutine_threadsafe(
                    self._history.read_file(conversation_id, file_id, secret_key=owner), loop,
                ).result()
                access(file_id)
                return content

            def resolve_session(file_id):
                name = access(file_id)
                content = read_session(file_id)
                return FileDescriptor(FileModelKey('session', residency, file_id,
                                      hashlib.sha256(content).hexdigest()), name)

            def read_contract(file_id):
                access()
                content = read_contract_file(file_id, directory=self._contract_directory)
                access()
                return content

            def resolve_contract(file_id):
                read_contract(file_id)  # 每次访问核对磁盘存在性与内容身份，包括缓存命中。
                # 展示名称使用入库元数据，不将内容哈希或 PDF 扩展名拼进名称。
                metadata = self._metadata_store.get(file_id)
                if metadata is None or not metadata.file_name.strip():
                    raise FileViewError('file_unavailable', '合同名称信息不可用，请重新确认合同。')
                return FileDescriptor(FileModelKey('contract', 'global', file_id, file_id), metadata.file_name)

            vision = self._settings.mllm.vision
            config = PDFPageRenderConfig(max_render_scale=vision.max_render_scale,
                visual_token_patch_size=vision.visual_token_patch_size,
                max_visual_tokens_per_page=vision.max_visual_tokens_per_page)
            session = FileViewer(conversation_id, resolve_file=resolve_session, read_file=read_session,
                                 cache=pool, config=config)
            contract = FileViewer(conversation_id, resolve_file=resolve_contract, read_file=read_contract,
                                  cache=self.contract_pool, config=config)
            from app.agent.contract_communication.agent_core.tool.contract_image_search import (
                ContractSearchResults, build_contract_image_search_registrations,
                build_contract_search_view_registration,
            )
            search_results = ContractSearchResults(self._metadata_store,
                capacity=self._settings.communication_contract_search_cache_max_queries,
                page_size=self._settings.communication_contract_search_page_size)
            from app.agent.contract_communication.agent_core.tool.contract_retrieval import (
                ContractRetrievalResults, build_contract_retrieval_registration, build_contract_candidates_view_registration)
            final_results = ContractRetrievalResults(self._metadata_store,
                capacity=self._settings.communication_contract_retrieval_cache_max_queries,
                page_size=self._settings.communication_contract_retrieval_page_size)
            async def authorize_search():
                if revoked.is_set():
                    raise PermissionError('会话已释放')
                current, _ = await self._history.file_tool_access(conversation_id, secret_key=owner)
                if revoked.is_set() or current != residency:
                    raise PermissionError('会话驻留身份已变化')
            tools = build_file_view_registrations(session_viewer=session, contract_viewer=contract)
            if self._elasticsearch is not None:
                tools = (*tools, *build_contract_image_search_registrations(
                    session_viewer=session, contract_viewer=contract, results=search_results, final_results=final_results,
                    client=self._elasticsearch, index_name=self._settings.elasticsearch_index_name,
                    metadata_store=self._metadata_store, settings=self._settings, authorize=authorize_search))
            from app.agent.contract_communication.agent_core.tool.web_search import WebSearchPool, build_web_search_registrations
            web_results = WebSearchPool(capacity=self._settings.communication_web_search_cache_max_queries,
                page_size=self._settings.communication_web_search_page_size)
            tools = (*tools, *build_web_search_registrations(pool=web_results, service=self._web_search,
                authorize=authorize_search))
            from app.agent.contract_communication.agent_core.tool.web_page import WebPageViewer, build_open_web_page_registration
            web_pages = WebPageViewer(sources=web_results, settings=self._settings, authorize=authorize_search,
                capacity=self._settings.communication_web_page_cache_max_entries,
                page_chars=self._settings.communication_web_page_chars)
            tools = (*tools, build_open_web_page_registration(web_pages))
            search_audit = []
            tools = (*tools, build_contract_retrieval_registration(results=search_results,final_results=final_results,
                metadata_store=self._metadata_store, settings=self._settings, authorize=authorize_search,
                es_client=self._elasticsearch, audit=search_audit, field_catalog=self._field_catalog, category_catalog=self._category_catalog),
                build_contract_candidates_view_registration(results=final_results,authorize=authorize_search))

            async def resolve_page(reference):
                if reference.resource_id.startswith('web-page:'):
                    return await web_pages.resolve_page(reference)
                if reference.resource_id.startswith('web-search:'):
                    await authorize_search()
                    return await web_results.resolve_page(reference)
                if reference.resource_id.startswith('contract-query:'):
                    await authorize_search()
                    return await final_results.resolve_page(reference)
                if search_results is not None and reference.resource_id.startswith('contract-search:'):
                    return await search_results.resolve_page(reference)
                # 引用由查看器签发；只把 invalid_reference 当作路由未命中，其他错误直接保留。
                for viewer in (session, contract):
                    try:
                        return await viewer.resolve_page(reference)
                    except FileViewError as exc:
                        if exc.code != 'invalid_reference':
                            raise
                raise FileViewError('invalid_reference', '页面引用无效，请重新查看文件。')

            self._sessions[conversation_id] = dict(residency=residency, revoked=revoked, pool=pool, web_results=web_results, web_pages=web_pages,
                session=session, contract=contract, tools=tools, resolver=resolve_page, search_results=search_results, search_audit=search_audit, final_results=final_results)
            return tools, resolve_page

    def evict(self, conversation_id):
        """历史锁内仅撤销并调度清理，不等待可能正在访问历史的工作线程。"""
        state = self._sessions.pop(conversation_id, None)
        if state is None:
            return
        state['revoked'].set()
        state['web_results'].close()
        state['web_pages'].close()
        state['final_results'].close()
        if state['search_results'] is not None:
            state['search_results'].close()
        async def cleanup():
            await state['session'].aclose()
            await state['contract'].aclose()
            await state['pool'].aclose()
        task = asyncio.create_task(cleanup())
        self._cleanups.add(task)
        def done(task):
            self._cleanups.discard(task)
            if not task.cancelled() and task.exception():
                logger.error('会话文件资源释放失败', exc_info=task.exception())
        task.add_done_callback(done)

    async def close(self):
        self._closed = True
        for conversation_id in tuple(self._sessions):
            self.evict(conversation_id)
        await asyncio.gather(*tuple(self._cleanups))
        await self.contract_pool.aclose()
