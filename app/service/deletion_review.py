"""删除申请存入及标志协调；不执行正式删除或中间件发布。"""
import asyncio
import logging
from uuid import UUID, uuid4

from app.infrastructure.contract_metadata_store import (
    ContractMetadataStatus, ContractMetadataNotFoundError, ContractMetadataStateError,
    SQLiteContractMetadataStore,
)
from app.infrastructure.deletion_review_store import (
    SQLiteDeletionReviewStore, DeletionReviewConflictError,
)
from app.schema.deletion_review import (
    DeletionReviewSnapshot, DeletionReviewFeedback, DeletionReviewSubmissionRequest,
    ProcessedDeletionReviewReceipt,
)

logger = logging.getLogger(__name__)


class DeletionReviewService:
    def __init__(self, store: SQLiteDeletionReviewStore, metadata_store: SQLiteContractMetadataStore,
                 *, lock_documents=None):
        self._store = store
        self._metadata = metadata_store
        self._lock_documents = lock_documents

    async def initialize(self):
        await asyncio.to_thread(self._store.initialize)

    async def _run_locked(self, document_id, operation):
        if self._lock_documents is None:
            raise RuntimeError('删除申请流程必须注入正式入库服务的文档锁')
        async with self._lock_documents(document_id):
            # HTTP 中断不能让线程在文档锁释放后继续执行跨库写入。
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise

    @staticmethod
    def _matches(record, metadata):
        return metadata is not None and (
            record.contract_ingested_at == metadata.ingested_at
            and record.passport == metadata.passport)

    def _sync_flag(self, record):
        latest = self._store.get_latest_for_document(record.document_id)
        if latest is None:
            return
        if latest.submission_id != record.submission_id:
            # 已有后续申请时无需恢复旧拒绝的标志，但仍可以完成旧申请的本地处理。
            if record.review_status == 'rejected':
                self._store.mark_rejection_processed(record.submission_id)
            return
        metadata = self._metadata.get(record.document_id)
        if not self._matches(record, metadata):
            # 原合同已不存在或已重新入库时，拒绝结果不应修改新实例，也不必永久阻塞清理。
            if record.review_status == 'rejected':
                self._store.mark_rejection_processed(record.submission_id)
            return
        if metadata.status is not ContractMetadataStatus.READY:
            return
        desired = record.review_status == 'rejected'
        if metadata.can_delete != desired:
            self._metadata.set_can_delete(document_id=record.document_id,
                ingestion_id=metadata.ingestion_id, can_delete=desired)
        if record.review_status == 'rejected':
            self._store.mark_rejection_processed(record.submission_id)

    async def reconcile_flags(self):
        """申请是恢复依据：补齐待审锁定、拒绝恢复，且不影响重新入库的合同。"""
        records = await asyncio.to_thread(self._store.list_latest_records)
        unprocessed = await asyncio.to_thread(self._store.list_unprocessed_rejections)
        records = {record.submission_id: record for record in (*records, *unprocessed)}.values()
        for record in records:
            # 锁内重新读取，防止扫描快照之后已收到新的审核结果。
            try:
                await self._run_locked(record.document_id,
                    lambda record=record: self._sync_flag(self._store.get(record.submission_id)))
            except Exception as exc:
                logger.warning('删除申请标志协调失败，将再次重试：submission_id=%s error_type=%s',
                    record.submission_id, type(exc).__name__)

    async def submit(self, *, document_id: str, requested_by: str, note: str):
        # 内部调用同样遵守 HTTP 备注契约，在跨库写入前完成校验。
        note = DeletionReviewSubmissionRequest(note=note).note
        def write():
            metadata = self._metadata.get(document_id)
            if metadata is None:
                raise ContractMetadataNotFoundError('合同不存在或已删除')
            if metadata.status is not ContractMetadataStatus.READY:
                raise ContractMetadataStateError('合同尚未就绪或正在删除，不能申请删除')
            latest = self._store.get_latest_for_document(document_id)
            if latest is not None and latest.review_status != 'rejected':
                # 只恢复同一提交人且备注未变的请求，防止重试替换已冻结的申请。
                if (latest.review_status == 'pending_send' and latest.requested_by == requested_by
                        and latest.note == note
                        and self._matches(latest, metadata) and metadata.can_delete):
                    self._sync_flag(latest)
                    return latest
                raise DeletionReviewConflictError('该合同已有删除申请，不能重复提交')
            if latest is not None:
                self._sync_flag(latest)
                metadata = self._metadata.get(document_id)
            if not metadata.can_delete:
                raise ContractMetadataStateError('该合同当前不允许申请删除')
            snapshot = DeletionReviewSnapshot(document_id=metadata.document_id,
                passport=metadata.passport, file_name=metadata.file_name, summary=metadata.summary,
                uploader=metadata.uploader, requested_by=requested_by,
                file_uri=metadata.file_uri, contract_ingested_at=metadata.ingested_at)
            # 先保存恢复入口，再锁定标志。若第二步失败，申请唯一索引仍阻止重复申请，
            # 启动/后台扫描按快照补齐标志，不撤销已经持久化的申请。
            record = self._store.save(uuid4(), snapshot, note=note)
            self._sync_flag(record)
            return record
        return await self._run_locked(document_id, write)

    async def save(self, *, submission_id: UUID, document_id: str, requested_by: str, note: str = ''):
        def write():
            # 重试优先读取原申请；正式合同变化不能重建并覆盖已经冻结的快照。
            try:
                old = self._store.get(submission_id)
            except LookupError:
                old = None
            if old is not None:
                if (old.document_id, old.requested_by, old.note) != (document_id, requested_by, note):
                    raise DeletionReviewConflictError('同一删除申请不能更换合同、操作人或备注')
                return old
            metadata = self._metadata.get(document_id)
            if metadata is None:
                raise ContractMetadataNotFoundError('合同不存在')
            if metadata.status is not ContractMetadataStatus.READY or not metadata.can_delete:
                raise ContractMetadataStateError('合同尚未就绪或已禁止申请删除')
            snapshot = DeletionReviewSnapshot(document_id=metadata.document_id,
                passport=metadata.passport, file_name=metadata.file_name, summary=metadata.summary,
                uploader=metadata.uploader, requested_by=requested_by,
                file_uri=metadata.file_uri, contract_ingested_at=metadata.ingested_at)
            return self._store.save(submission_id, snapshot, note=note)
        return await asyncio.to_thread(write)

    async def get(self, submission_id: UUID):
        return await asyncio.to_thread(self._store.get, submission_id)

    async def list_ids(self):
        return await asyncio.to_thread(self._store.list_ids)

    async def bind_message_id(self, submission_id: UUID, message_id: str):
        return await asyncio.to_thread(self._store.bind_message_id, submission_id, message_id)

    async def get_by_message_id(self, message_id: str):
        return await asyncio.to_thread(self._store.get_by_message_id, message_id)

    async def save_review_result(self, *, message_id: str, feedback: DeletionReviewFeedback):
        if self._lock_documents is None:
            return await asyncio.to_thread(self._store.save_review_result, message_id=message_id, feedback=feedback)
        receipt = await asyncio.to_thread(self._store.get_processed_receipt, message_id=message_id, feedback=feedback)
        if receipt is not None:
            return receipt
        try:
            record = await self.get_by_message_id(message_id)
        except LookupError:
            # 清理可能发生在回执检查与申请读取之间；只有精确匹配的凭据才接受重放。
            receipt = await asyncio.to_thread(self._store.get_processed_receipt, message_id=message_id, feedback=feedback)
            if receipt is not None:
                return receipt
            raise
        def write():
            saved = self._store.save_review_result(message_id=message_id, feedback=feedback)
            if not isinstance(saved, ProcessedDeletionReviewReceipt):
                self._sync_flag(saved)
                # 投影需要包含本次恢复标志后产生的本地完成时间。
                saved = self._store.get(saved.submission_id)
            return saved
        return await self._run_locked(record.document_id, write)

    async def cleanup_completed(self, submission_id: UUID, *, cutoff):
        try:
            record = await self.get(submission_id)
        except LookupError:
            return False
        # 清理与提交/反馈复用合同锁，避免状态协调跨库读取时被删除恢复依据。
        return await self._run_locked(record.document_id,
            lambda: self._store.cleanup_completed(submission_id, cutoff=cutoff))
