"""A2A 合同工具装配；不接收用户附件、会话记忆或未过滤的合同工具。"""
from pathlib import Path
from dataclasses import replace
from typing import Literal
from pydantic import Field
from .access import VisibleMetadata, VisibleRelations
from ..agent_core.tool.registry import RegisteredTool
from ..agent_core.tool.file_viewer import (FileViewer, FileModelCache, FileDescriptor, FileModelKey,
    FileViewError, ViewContractFileArguments, read_contract_file)
from ..agent_core.tool.contract_image_search import (ContractSearchResults, SearchContractsByImageArguments,
    build_contract_image_search_registrations)
from ..agent_core.tool.contract_retrieval import (ContractRetrievalResults, build_contract_retrieval_registration,
    build_contract_candidates_view_registration)
from app.tool.pdf_page import PDFPageRenderConfig


class ContractImageArguments(SearchContractsByImageArguments):
    """以已获得的正式合同查找相似合同，不接收附件来源。"""
    source_type: Literal['contract'] = Field(default='contract', description='源文件固定为本平台正式合同，仅使用实际提供的合同ID。')


class A2AContractTools:
    def __init__(self, *, access, settings, es=None, relations=None, field_catalog=None, category_catalog=None,
                 contract_directory='data/contract', audit=None):
        self.access, self.settings = access, settings
        self.audit = audit if audit is not None else []
        self.metadata = VisibleMetadata(access)
        self.es, self.relations = es, VisibleRelations(relations, self.metadata) if relations is not None else None
        self.field_catalog, self.category_catalog = field_catalog, category_catalog
        self.closed = False
        self.pool = FileModelCache(max_files=settings.communication_contract_file_cache_max_files)
        def resolve(doc):
            if self.closed or (row := self.metadata.get(doc)) is None:
                raise FileViewError('file_unavailable', '文件不可用，请核对文件引用。')
            return FileDescriptor(FileModelKey('contract', access.userid, doc, doc), row.file_name)
        def read(doc):
            resolve(doc)
            content = read_contract_file(doc, directory=Path(contract_directory))
            resolve(doc)
            return content
        vision = settings.mllm.vision
        self.viewer = FileViewer(access.userid, resolve_file=resolve, read_file=read, cache=self.pool,
            config=PDFPageRenderConfig(max_render_scale=vision.max_render_scale,
                visual_token_patch_size=vision.visual_token_patch_size, max_visual_tokens_per_page=vision.max_visual_tokens_per_page))
        self.images = ContractSearchResults(self.metadata, capacity=settings.communication_contract_search_cache_max_queries,
            page_size=settings.communication_contract_search_page_size)
        self.final = ContractRetrievalResults(self.metadata, capacity=settings.communication_contract_retrieval_cache_max_queries,
            page_size=settings.communication_contract_retrieval_page_size)
        self.resolvers = [('file:', self.viewer.resolve_page), ('contract-search:', self.images.resolve_page),
            ('contract-query:', self.final.resolve_page)]
        self.resources = [self.images, self.final]

    async def authorize(self):
        if self.closed: raise ValueError('任务已结束')
        await self.access.refresh()

    def registrations(self):
        from ..agent_core.tool.contract_metadata import build_contract_metadata_registration
        from ..agent_core.tool.contract_notes import ContractNotesViewer, build_contract_notes_registration
        from ..agent_core.tool.contract_passport import ContractPassportViewer, build_contract_passport_registration
        from ..agent_core.tool.contract_statistics import build_contract_statistics_registration
        async def view(operation, args): return await self.viewer.view(**args.model_dump())
        tools = [RegisteredTool('view_contract_file', ViewContractFileArguments.__doc__, ViewContractFileArguments,
                    view, return_types=('foldable',)),
            build_contract_metadata_registration(self.metadata),
            build_contract_retrieval_registration(results=self.images, final_results=self.final,
                metadata_store=self.metadata, settings=self.settings, authorize=self.authorize, es_client=self.es,
                field_catalog=self.field_catalog, category_catalog=self.category_catalog, audit=self.audit, document_scope=self.access.scope),
            build_contract_candidates_view_registration(results=self.final, authorize=self.authorize),
            build_contract_statistics_registration(self.metadata, self.relations)]
        notes = ContractNotesViewer(metadata_store=self.metadata,
            max_resident=self.settings.communication_contract_notes_cache_max_contracts,
            page_size=self.settings.communication_contract_notes_page_size)
        passports = ContractPassportViewer(self.metadata,
            max_resident=self.settings.communication_contract_passport_cache_max_queries,
            page_size=self.settings.communication_contract_passport_page_size)
        self.resources += [notes, passports]
        tools += [build_contract_notes_registration(notes), build_contract_passport_registration(passports)]
        self.resolvers += [('contract-notes:', notes.resolve_page), ('contract-passport:', passports.resolve_page)]
        if self.es is not None:
            entries = build_contract_image_search_registrations(session_viewer=None, contract_viewer=self.viewer,
                results=self.images, final_results=self.final, client=self.es, index_name=self.settings.elasticsearch_index_name,
                metadata_store=self.metadata, settings=self.settings, authorize=self.authorize, document_scope=self.access.scope)
            tools += [replace(entries[0], arguments_model=ContractImageArguments,
                description=ContractImageArguments.__doc__), entries[1]]
        if self.relations is not None:
            from ..agent_core.tool.contract_relations import ContractRelationsViewer, build_contract_relations_registration
            from ..agent_core.tool.contract_relation_search import ContractRelationSearchResults, build_contract_relation_search_registrations
            neighbors = ContractRelationsViewer(relation_service=self.relations, metadata_store=self.metadata,
                max_resident=self.settings.communication_contract_relations_cache_max_contracts,
                page_size=self.settings.communication_contract_relations_page_size)
            edges = ContractRelationSearchResults(self.metadata, self.relations,
                capacity=self.settings.communication_relation_search_cache_max_queries,
                page_size=self.settings.communication_relation_search_page_size)
            self.resources += [neighbors, edges]
            self.resolvers += [('contract-relations:', neighbors.resolve_page), ('relation-search:', edges.resolve_page)]
            tools += [build_contract_relations_registration(neighbors),
                      *build_contract_relation_search_registrations(edges, self.settings)]
        return [self._guard(t) for t in tools]

    def _guard(self, tool):
        async def handler(operation, args):
            await self.authorize()
            # 仅对文件存在性/访问边界隐藏原因；页码、查询与业务校验不能被抹平。
            for field in ('file_id','document_id','contract_id'):
                doc = getattr(args, field, None)
                if doc is not None and self.metadata.get(doc) is None:
                    from ..agent_core.subgraph.fifo_management.schema import FIFOExecutionResult
                    return FIFOExecutionResult(status='failed',tool_result={'status':'error',
                        'message':'资料不可用，请核对引用或调整查找条件。','execution_state':'not_executed'})
            result = await tool.handler(operation, args)
            await self.authorize()
            hidden = {'file_unavailable','contract_not_found','contract_deleting','contract_not_ready','contract_unavailable',
                'session_unavailable','session_closed','session_expired','result_unavailable'}
            if result.status == 'failed' and result.tool_result.get('code') in hidden:
                return result.model_copy(update={'tool_result': {'status':'error', 'message':'资料不可用，请核对引用或调整查找条件。',
                    'execution_state':'not_executed'}})
            return result
        return replace(tool, handler=handler, access_policy=self.access)

    async def resolve_page(self, reference):
        await self.authorize()
        for prefix, resolver in self.resolvers:
            if reference.resource_id.startswith(prefix):
                blocks = await resolver(reference)
                await self.authorize()
                return blocks
        raise ValueError('资料引用不可用')

    async def close(self):
        self.closed = True
        for resource in self.resources: resource.close()
        await self.viewer.aclose()
        await self.pool.aclose()
