"""A2A 私有文件访问边界；用户等级不进入模型消息、工具参数或回执。"""
import asyncio
from threading import RLock
from app.infrastructure.contract_metadata_store import ContractMetadataStatus


class A2AContextExpired(RuntimeError):
    pass


class A2AAccessUnavailable(RuntimeError):
    pass


class ContractAccess:
    def __init__(self, *, userid, platform_code, load_level, metadata, previous_ids=()):
        self.userid, self.platform_code = userid, platform_code
        self._load = load_level
        self.raw = metadata
        self._level = None
        self._lock = RLock()
        self.previous_ids = frozenset(previous_ids)
        self.seen_ids = set(previous_ids)
        self.invalidated = False

    async def refresh(self):
        try:
            level = await self._load(self.userid)
            if level is not None and (type(level) is not int or level < 0):
                raise ValueError('权限记录格式错误')
            with self._lock:
                self._level = level
            # 旧上下文可能含原文、摘要或思考，不能靠过滤新结果掩盖权限收缩。
            ids = await asyncio.to_thread(self.ids)
            if not (self.previous_ids | self.seen_ids) <= set(ids):
                self.invalidated = True
                raise A2AContextExpired('上下文已不可续用')
            with self._lock:
                # 全库计数、候选数量也属于历史证据，保守跟踪当时全部可见集合。
                self.seen_ids.update(ids)
            return ids
        except A2AContextExpired:
            raise
        except Exception:
            with self._lock:
                self._level = None
            raise A2AAccessUnavailable('资料读取暂不可用') from None

    def visible(self, row):
        with self._lock:
            return (row is not None and row.status is ContractMetadataStatus.READY
                and type(row.file_level) is int and self._level is not None and 0 <= row.file_level <= self._level)

    def ids(self):
        return tuple(row.document_id for row in self.raw.list_ready() if self.visible(row))

    def get(self, document_id):
        row = self.raw.get(document_id)
        if not self.visible(row):
            return None
        with self._lock:
            self.seen_ids.add(document_id)
        return row

    async def scope(self):
        return await self.refresh()


class VisibleMetadata:
    """显式的只读代理；未实现的方法不能透传到全库。"""
    def __init__(self, access): self.access = access
    @property
    def database_path(self): return self.access.raw.database_path
    def get(self, doc): return self.access.get(doc)
    def list_ready(self, **kwargs):
        return tuple(r for r in self.access.raw.list_ready(**kwargs) if self.access.visible(r))
    def list_ids_by_passport(self, passport):
        return tuple(doc for doc in self.access.raw.list_ids_by_passport(passport) if self.get(doc) is not None)
    def list_notes(self, doc): return self.access.raw.list_notes(doc) if self.get(doc) is not None else []
    def get_note(self, doc, note_id): return self.access.raw.get_note(doc, note_id) if self.get(doc) is not None else None
    def get_summary_embedding(self, doc): return self.access.raw.get_summary_embedding(doc) if self.get(doc) is not None else None
    def get_note_embedding(self, doc, note_id): return self.access.raw.get_note_embedding(doc, note_id) if self.get(doc) is not None else None
    def library_statistics(self, start=None, end=None):
        return self.access.raw.library_statistics(start, end, document_ids=self.access.ids())


class VisibleRelations:
    def __init__(self, service, metadata): self.service, self.metadata = service, metadata
    async def list_relations(self, doc):
        if self.metadata.get(doc) is None: raise ValueError('合同不可用')
        rows = await self.service.list_relations(doc)
        return [r for r in rows if self.metadata.get(r.document_id) is not None]
    async def search_candidates(self, contract_id=None, **kwargs):
        if contract_id is not None and self.metadata.get(contract_id) is None: raise ValueError('合同不可用')
        rows = await self.service.search_candidates(contract_id, document_ids=self.metadata.access.ids(), **kwargs)
        return [r for r in rows if self.metadata.get(r['document_id_a']) is not None and self.metadata.get(r['document_id_b']) is not None]
    async def statistics(self, selected, ready):
        allowed = set(r.document_id for r in self.metadata.list_ready())
        return await self.service.statistics(sorted(set(selected) & allowed), sorted(set(ready) & allowed))
