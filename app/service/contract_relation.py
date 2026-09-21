"""协调关系创建与合同入库、删除，共用进程内文档锁。"""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import EmbeddingSettings
from app.service.contract_relation_embedding import embed_relation_description

from app.infrastructure.contract_graph_store import ContractGraphStore, ContractRelation, ContractNeighbor
from app.infrastructure.contract_metadata_store import SQLiteContractMetadataStore, ContractMetadataStatus
from app.schema.contract import ContractRelationRequest
from app.service.contract_ingestion import ContractDocumentNotFoundError, ContractDocumentConflictError


class ContractRelationService:
    def __init__(self, *, graph_store: ContractGraphStore,
                 metadata_store: SQLiteContractMetadataStore,
                 embedding_settings: EmbeddingSettings,
                 lock_documents: Callable[..., AbstractAsyncContextManager[None]]):
        self._graph = graph_store
        self._metadata = metadata_store
        self._lock_documents = lock_documents
        self._embedding_settings = embedding_settings

    async def create(self, request: ContractRelationRequest, *, reviewer: str) -> ContractRelation:
        a, b = sorted((request.document_id_a, request.document_id_b))
        # 在状态检查到图事务完成之间保持两端锁，删除不能插入该窗口。
        async with self._lock_documents(a, b):
            for document_id in (a, b):
                metadata = await asyncio.to_thread(self._metadata.get, document_id)
                if metadata is None:
                    raise ContractDocumentNotFoundError("关联合同不存在或已删除")
                if metadata.status is not ContractMetadataStatus.READY:
                    raise ContractDocumentConflictError("关联合同尚未完成入库或正在删除")
            # 先完成外部编码，再开启图事务；失败时不留下缺少向量的新关系。
            embedding = await embed_relation_description(request.description, self._embedding_settings)
            relation = ContractRelation(str(uuid4()), a, b, request.description,
                                        datetime.now(UTC), reviewer)
            return await self._graph.create_relation(
                relation, description_embedding=embedding.vector,
                embedding_model=embedding.model, embedding_version=embedding.version,
                embedding_dimensions=self._embedding_settings.dimensions,
            )


    async def delete(self, relation_id: str) -> bool:
        """独立删除只操作图；旧 ID 的重试不会误删两端重建的新关系。"""
        return await self._graph.delete_relation(relation_id)


    async def list_relations(self, document_id: str) -> list[ContractNeighbor]:
        """以 SQLite 正式目录约束结果，排除删除中及已失效的对方合同。"""
        async with self._lock_documents(document_id):
            metadata = await asyncio.to_thread(self._metadata.get, document_id)
            if metadata is None:
                raise ContractDocumentNotFoundError("合同不存在或已删除")
            if metadata.status is not ContractMetadataStatus.READY:
                raise ContractDocumentConflictError("合同尚未完成入库或正在删除")
            neighbors = await self._graph.list_relations(document_id)

            def visible_neighbors() -> list[ContractNeighbor]:
                visible = []
                for neighbor in neighbors:
                    other = self._metadata.get(neighbor.document_id)
                    if other is not None and other.status is ContractMetadataStatus.READY:
                        visible.append(neighbor)
                return visible

            return await asyncio.to_thread(visible_neighbors)


    async def search_candidates(self, contract_id=None, *, limit=10000, relation_ids=None):
        """SQLite 正式状态约束两端，先过滤再交给检索器排名。"""
        if contract_id is not None:
            item = await asyncio.to_thread(self._metadata.get, contract_id)
            if item is None or item.status is not ContractMetadataStatus.READY:
                raise ContractDocumentNotFoundError('指定合同不存在或不可用')
        rows = await self._graph.search_candidates(contract_id,limit=limit,relation_ids=relation_ids)
        def visible():
            cache={}
            for row in rows:
                for key in ('document_id_a','document_id_b'):
                    doc=row[key]
                    if doc not in cache:
                        m=self._metadata.get(doc)
                        cache[doc]=m is not None and m.status is ContractMetadataStatus.READY
            return [r for r in rows if cache[r['document_id_a']] and cache[r['document_id_b']]]
        return await asyncio.to_thread(visible)


    async def statistics(self, selected_ids: list[str], ready_ids: list[str]) -> dict:
        """复用本次 SQLite 快照的可见集合，避免逐合同查询和重复计算无向边。"""
        return await self._graph.statistics(selected_ids, ready_ids)
