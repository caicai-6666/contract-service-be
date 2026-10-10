"""删除审核只读投影；按提交人或上传人限定查询，不触发正式删除。"""
import asyncio
from datetime import datetime, UTC, timedelta
from urllib.parse import urlencode
from uuid import UUID

from app.infrastructure.contract_file_store import ContractFileNotFoundError
from app.schema.deletion_review import DeletionReviewDetailResponse, DeletionReviewListResponse


class DeletionReviewQueryService:
    def __init__(self, store, metadata_store, file_store, settings):
        self._store = store
        self._metadata = metadata_store
        self._files = file_store
        self._retention = settings.deletion_review_retention_seconds

    async def list_records(self, user_name: str) -> DeletionReviewListResponse:
        ids = await asyncio.to_thread(self._store.list_ids_for_user, user_name)
        return DeletionReviewListResponse(submission_ids=list(ids))

    async def detail(self, submission_id: UUID, user_name: str) -> DeletionReviewDetailResponse:
        def read():
            record = self._store.get_for_user(submission_id, user_name)
            completed = record.review_status == 'rejected' or (
                record.review_status == 'approved' and record.deletion_status == 'succeeded')
            eligible_at = None
            if completed and record.review_message_id is not None and record.review_processed_at is not None:
                eligible_at = record.review_processed_at + timedelta(seconds=self._retention)
            url = None
            if record.deletion_status != 'succeeded':
                metadata = self._metadata.get(record.document_id)
                # 删除审核引用的是原入库合同，不能让旧申请预览后来的重新入库版本。
                if metadata is not None and (
                    metadata.ingested_at == record.contract_ingested_at
                    and metadata.passport == record.passport
                    and metadata.file_uri == record.file_uri
                ):
                    try:
                        self._files.resolve(record.file_uri)
                    except ContractFileNotFoundError:
                        pass
                    else:
                        url = '/contract/api/resource/contract?' + urlencode({'file_uri': record.file_uri})
            return DeletionReviewDetailResponse(
                **{key: getattr(record, key) for key in (
                    'submission_id', 'document_id', 'passport', 'file_name', 'summary',
                    'uploader', 'requested_by', 'contract_ingested_at', 'note', 'created_at', 'updated_at')},
                delivery={'status': record.delivery_status,
                          'message_id': record.message_id},
                review={'status': record.review_status, 'note': record.review_note,
                        'reviewer': record.reviewed_by, 'reviewed_at': record.reviewed_at},
                deletion={'status': record.deletion_status, 'completed_at': record.deletion_completed_at},
                cleanup={'review_processed_at': record.review_processed_at, 'eligible_at': eligible_at},
                pdf={'file_uri': record.file_uri, 'url': url}, server_time=datetime.now(UTC))
        return await asyncio.to_thread(read)
