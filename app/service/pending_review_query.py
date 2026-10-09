"""待审查询投影：公开基本信息和清理时间，不触发发布、审核或正式入库。"""
import asyncio
from datetime import datetime, UTC, timedelta

from app.schema.pending_review import PendingReviewCleanupPolicy, PendingReviewDetailResponse, PendingReviewListResponse


class PendingReviewQueryService:
    def __init__(self, store, metadata_store, settings):
        self._store = store
        self._metadata = metadata_store
        self._retention = settings.pending_review_retention_seconds
        self._interval = settings.pending_review_cleanup_interval_seconds

    def cleanup_policy(self):
        return PendingReviewCleanupPolicy(retention_seconds=self._retention,
            cleanup_interval_seconds=self._interval, server_time=datetime.now(UTC))

    async def list_records(self):
        ids = await asyncio.to_thread(self._store.list_ids)
        return PendingReviewListResponse(submission_ids=list(ids))

    async def detail(self, submission_id):
        def read():
            row = self._store.get_basic(submission_id)
            detail = PendingReviewDetailResponse(
                **{key: row[key] for key in ('submission_id','run_id','document_id','file_name','summary',
                    'page_count','submitted_by','note','created_at','updated_at')},
                delivery={'status':row['delivery_status'],'message_id':row['message_id']},
                review={'status':row['review_status'],'note':row['review_note'],'reviewer':row['reviewed_by'],
                    'reviewed_at':row['reviewed_at'],'passport':row['passport']},
                ingestion={'status':row['ingestion_status']},
                cleanup={'review_processed_at':row['review_processed_at']},
                pdf={'relative_path':row['pdf_relative_path'],
                    'url':f'/contract/api/resource/pending-review-pdf/{row["submission_id"]}'},
                server_time=datetime.now(UTC))
            # 清理时间只由已确认处理完成的状态产生，不能根据提交或审核时间推算。
            completed = detail.review.status == 'rejected' or (
                detail.review.status == 'approved' and detail.ingestion.status == 'succeeded')
            if completed and row['review_message_id'] and detail.cleanup.review_processed_at is not None:
                detail.cleanup.eligible_at = detail.cleanup.review_processed_at + timedelta(seconds=self._retention)
            if detail.review.status == 'approved' and detail.ingestion.status == 'succeeded':
                metadata = self._metadata.get(detail.document_id)
                if metadata is not None and metadata.passport == detail.review.passport:
                    detail.ingestion.ingested_at = metadata.ingested_at
            return detail
        return await asyncio.to_thread(read)

    async def read_pdf(self, submission_id):
        return await asyncio.to_thread(self._store.read_pdf, submission_id)
