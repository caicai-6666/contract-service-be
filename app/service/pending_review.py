"""待审快照存入服务；阻塞文件和 SQLite 操作放在线程中，不执行正式入库。"""
import asyncio
from uuid import UUID

from app.infrastructure.pending_review_store import SQLitePendingReviewStore
from app.schema.pending_review import PendingReviewRecord, PendingReviewSnapshot


class PendingReviewService:
    def __init__(self, store: SQLitePendingReviewStore, *, vector_dimensions: int):
        if type(vector_dimensions) is not int or vector_dimensions <= 0:
            raise ValueError('待审向量维度必须为正整数')
        self._store = store
        self._vector_dimensions = vector_dimensions

    async def initialize(self):
        await asyncio.to_thread(self._store.initialize)

    async def save(self, *, submission_id: UUID, snapshot: PendingReviewSnapshot,
                   processed_pdf_bytes: bytes, note: str = "") -> PendingReviewRecord:
        # 跨线程前复制，防止调用者并发修改快照嵌套内容。
        snapshot = PendingReviewSnapshot.model_validate(snapshot.model_dump(mode='json'))
        if snapshot.vector_dimensions != self._vector_dimensions:
            raise ValueError('待审融合向量维度与当前配置不一致')
        # 取消等待不代表线程未提交，调用方须保留 submission_id 并以同 ID 重试。
        return await asyncio.to_thread(self._store.save, submission_id, snapshot, processed_pdf_bytes, note=note)

    async def get(self, submission_id: UUID) -> PendingReviewRecord:
        return await asyncio.to_thread(self._store.get, submission_id)

    async def read_pdf(self, submission_id: UUID) -> bytes:
        return await asyncio.to_thread(self._store.read_pdf, submission_id)

    async def bind_message_id(self, submission_id: UUID, message_id: str) -> PendingReviewRecord:
        return await asyncio.to_thread(self._store.bind_message_id, submission_id, message_id)

    async def get_by_message_id(self, message_id: str) -> PendingReviewRecord:
        return await asyncio.to_thread(self._store.get_by_message_id, message_id)
