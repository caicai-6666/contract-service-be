"""独立 SQLite 保存删除待审记录；不修改正式合同或删除 PDF。"""
from contextlib import contextmanager
from datetime import datetime, UTC
import os
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import math
from uuid import UUID

from app.schema.deletion_review import (
    DeletionReviewFeedback, DeletionReviewRecord, DeletionReviewSnapshot,
    ProcessedDeletionReviewReceipt,
)


class DeletionReviewConflictError(ValueError):
    """重复申请、消息身份或不可变快照发生冲突。"""


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS deletion_reviews (
    submission_id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL CHECK(length(document_id)=64),
    passport TEXT,
    file_name TEXT NOT NULL CHECK(length(trim(file_name))>0),
    summary TEXT CHECK(summary IS NULL OR length(trim(summary))>0),
    uploader TEXT NOT NULL CHECK(length(trim(uploader))>0),
    requested_by TEXT NOT NULL CHECK(length(trim(requested_by))>0),
    file_uri TEXT NOT NULL,
    contract_ingested_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=10000),
    message_id TEXT UNIQUE,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(delivery_status IN ('pending','publishing','published','uncertain','blocked')),
    publish_attempts INTEGER NOT NULL DEFAULT 0 CHECK(publish_attempts>=0),
    next_publish_at REAL,
    last_publish_error TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending_send'
        CHECK(review_status IN ('pending_send','pending_review','approved','rejected')),
    review_message_id TEXT UNIQUE,
    reviewed_by TEXT,
    review_note TEXT CHECK(review_note IS NULL OR length(review_note)<=10000),
    reviewed_at TEXT,
    deletion_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(deletion_status IN ('pending','deleting','succeeded','failed')),
    deletion_attempts INTEGER NOT NULL DEFAULT 0 CHECK(deletion_attempts>=0),
    next_deletion_at REAL,
    last_deletion_error TEXT,
    deletion_completed_at TEXT,
    review_processed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS deletion_reviews_active_document
    ON deletion_reviews(document_id) WHERE review_status != 'rejected';
CREATE INDEX IF NOT EXISTS deletion_reviews_status_created
    ON deletion_reviews(review_status, created_at, submission_id);
CREATE INDEX IF NOT EXISTS deletion_reviews_document
    ON deletion_reviews(document_id);
CREATE INDEX IF NOT EXISTS deletion_reviews_requested_by
    ON deletion_reviews(requested_by,created_at,submission_id);
CREATE INDEX IF NOT EXISTS deletion_reviews_uploader
    ON deletion_reviews(uploader,created_at,submission_id);
CREATE TABLE IF NOT EXISTS processed_deletion_review_receipts (
    submission_id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    review_message_id TEXT NOT NULL UNIQUE,
    review_payload_sha256 TEXT NOT NULL,
    review_processed_at TEXT NOT NULL,
    cleaned_at TEXT NOT NULL
);
'''


class SQLiteDeletionReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.database_path = self.root / 'deletions.db'

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA synchronous=FULL')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            tables = {row['name'] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'pending_deletions' in tables and 'deletion_reviews' in tables:
                raise ValueError('删除审核库同时存在新旧申请表，拒绝覆盖，请先核对数据')
            migration = 'ALTER TABLE pending_deletions RENAME TO deletion_reviews;\n' if 'pending_deletions' in tables else ''
            for name in ('active_document', 'status_created', 'document'):
                migration += f'DROP INDEX IF EXISTS pending_deletions_{name};\n'
            connection.executescript('BEGIN IMMEDIATE;\n' + migration + _SCHEMA)
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(deletion_reviews)')}
            additions = {
                'delivery_status': "TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_status IN ('pending','publishing','published','uncertain','blocked'))",
                'publish_attempts': 'INTEGER NOT NULL DEFAULT 0 CHECK(publish_attempts>=0)',
                'next_publish_at': 'REAL', 'last_publish_error': 'TEXT',
                'deletion_status': "TEXT NOT NULL DEFAULT 'pending' CHECK(deletion_status IN ('pending','deleting','succeeded','failed'))",
                'deletion_attempts': 'INTEGER NOT NULL DEFAULT 0 CHECK(deletion_attempts>=0)',
                'next_deletion_at': 'REAL', 'last_deletion_error': 'TEXT',
                'deletion_completed_at': 'TEXT',
                'review_processed_at': 'TEXT',
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f'ALTER TABLE deletion_reviews ADD COLUMN {name} {definition}')
            # 已绑定的旧回执意味着已确认送审，升级后不能再次发送。
            if 'delivery_status' not in columns:
                connection.execute("UPDATE deletion_reviews SET delivery_status='published' WHERE message_id IS NOT NULL")
            connection.execute('CREATE INDEX IF NOT EXISTS deletion_reviews_delivery ON deletion_reviews(delivery_status,next_publish_at,created_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS deletion_reviews_execution ON deletion_reviews(review_status,deletion_status,next_deletion_at,created_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS deletion_reviews_cleanup ON deletion_reviews(review_processed_at,review_status,deletion_status)')
            # 旧版正式删除完成时间是可信本地事实；拒绝记录必须等待标志协调，不能用远端审核时间代替。
            connection.execute('''UPDATE deletion_reviews SET review_processed_at=deletion_completed_at
                WHERE review_processed_at IS NULL AND review_status='approved'
                AND deletion_status='succeeded' AND review_message_id IS NOT NULL
                AND deletion_completed_at IS NOT NULL''')

    @staticmethod
    def _record(row):
        if row is None:
            raise LookupError('删除待审申请不存在')
        return DeletionReviewRecord.model_validate(dict(row))

    @staticmethod
    def _message_id(value):
        if not isinstance(value, str) or not value or len(value)>128 or value != value.strip():
            raise ValueError('中间件消息 ID 必须为 1–128 字符且无首尾空白')
        return value

    @staticmethod
    def _feedback_hash(feedback: DeletionReviewFeedback):
        payload = feedback.model_dump(mode='json')
        payload['reviewed_at'] = feedback.reviewed_at.astimezone(UTC).isoformat()
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def _processed_receipt(self, connection, message_id, feedback):
        row = connection.execute('''SELECT * FROM processed_deletion_review_receipts
            WHERE request_id=? OR review_message_id=?''', (message_id, feedback.review_message_id)).fetchone()
        if row is None:
            return None
        if (row['request_id'] != message_id or row['review_message_id'] != feedback.review_message_id
                or row['review_payload_sha256'] != self._feedback_hash(feedback)):
            raise DeletionReviewConflictError('已清理的删除申请收到冲突反馈')
        return ProcessedDeletionReviewReceipt(submission_id=row['submission_id'],
            review_message_id=row['review_message_id'], review_processed_at=row['review_processed_at'])

    def get_processed_receipt(self, *, message_id: str, feedback: DeletionReviewFeedback):
        message_id = self._message_id(message_id)
        feedback = DeletionReviewFeedback.model_validate(feedback.model_dump(mode='json'))
        with self._connection() as connection:
            return self._processed_receipt(connection, message_id, feedback)

    def save(self, submission_id: UUID, snapshot: DeletionReviewSnapshot, *, note: str = ''):
        sid = str(UUID(str(submission_id)))
        snapshot = DeletionReviewSnapshot.model_validate(snapshot.model_dump(mode='json'))
        now = datetime.now(UTC)
        record = DeletionReviewRecord(**snapshot.model_dump(), submission_id=sid, note=note,
            review_status='pending_send', created_at=now, updated_at=now)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM processed_deletion_review_receipts WHERE submission_id=?', (sid,)).fetchone():
                raise DeletionReviewConflictError('删除申请已清理，不能重新创建')
            old = connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone()
            if old is not None:
                saved = self._record(old)
                # 相同申请可以安全重放，审核字段及时间绝不被初始数据覆盖。
                if any(getattr(saved, key) != getattr(record, key)
                       for key in (*DeletionReviewSnapshot.model_fields, 'note')):
                    raise DeletionReviewConflictError('删除申请已存在，禁止覆盖原合同快照或操作人')
                return saved
            fields = (*DeletionReviewSnapshot.model_fields, 'submission_id', 'note', 'created_at', 'updated_at')
            data = record.model_dump(mode='json')
            try:
                connection.execute(
                    f"INSERT INTO deletion_reviews ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                    tuple(data[key] for key in fields))
            except sqlite3.IntegrityError as exc:
                raise DeletionReviewConflictError('该合同已存在未拒绝的删除申请') from exc
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())

    def get(self, submission_id: UUID):
        with self._connection() as connection:
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?',
                (str(UUID(str(submission_id))),)).fetchone())

    def list_ids(self) -> tuple[UUID, ...]:
        with self._connection() as connection:
            return tuple(UUID(row['submission_id']) for row in connection.execute(
                'SELECT submission_id FROM deletion_reviews ORDER BY created_at DESC,submission_id ASC'))

    def list_ids_for_user(self, user_name: str) -> tuple[UUID, ...]:
        """人员范围按 OR 计算，同一人兼任两个角色也只返回一条申请。"""
        with self._connection() as connection:
            return tuple(UUID(row['submission_id']) for row in connection.execute(
                '''SELECT submission_id FROM deletion_reviews
                   WHERE requested_by=? OR uploader=?
                   ORDER BY created_at DESC,submission_id ASC''', (user_name, user_name)))

    def get_for_user(self, submission_id: UUID, user_name: str):
        # 详情与列表使用相同条件，不能仅凭 UUID 绕过人员范围。
        with self._connection() as connection:
            return self._record(connection.execute(
                '''SELECT * FROM deletion_reviews WHERE submission_id=?
                   AND (requested_by=? OR uploader=?)''',
                (str(UUID(str(submission_id))), user_name, user_name)).fetchone())

    def get_latest_for_document(self, document_id: str):
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM deletion_reviews WHERE document_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                (document_id,)).fetchone()
            return None if row is None else self._record(row)

    def list_latest_records(self):
        """每份合同只取最近申请；旧拒绝记录不能覆盖后来的申请标志。"""
        with self._connection() as connection:
            rows = connection.execute('''SELECT * FROM deletion_reviews AS current
                WHERE rowid=(SELECT rowid FROM deletion_reviews AS latest
                    WHERE latest.document_id=current.document_id
                    ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1)''')
            return tuple(self._record(row) for row in rows)

    def list_unprocessed_rejections(self):
        with self._connection() as connection:
            return tuple(self._record(row) for row in connection.execute('''SELECT * FROM deletion_reviews
                WHERE review_status='rejected' AND review_message_id IS NOT NULL AND review_processed_at IS NULL'''))

    def get_by_message_id(self, message_id: str):
        with self._connection() as connection:
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE message_id=?',
                (self._message_id(message_id),)).fetchone())

    def bind_message_id(self, submission_id: UUID, message_id: str):
        sid = str(UUID(str(submission_id)))
        message_id = self._message_id(message_id)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM processed_deletion_review_receipts WHERE request_id=?', (message_id,)).fetchone():
                raise DeletionReviewConflictError('该中间件消息已绑定到已清理申请')
            record = self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())
            if record.message_id is not None:
                if record.message_id != message_id:
                    raise DeletionReviewConflictError('删除申请已绑定其他中间件消息')
                return record
            if record.review_status != 'pending_send':
                raise DeletionReviewConflictError('只有待发送申请可以首次绑定消息')
            try:
                connection.execute("UPDATE deletion_reviews SET message_id=?, review_status='pending_review', delivery_status='published', next_publish_at=NULL, last_publish_error=NULL, updated_at=? WHERE submission_id=?",
                    (message_id, datetime.now(UTC).isoformat(), sid))
            except sqlite3.IntegrityError as exc:
                raise DeletionReviewConflictError('该中间件消息已绑定其他删除申请') from exc
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())

    def recover_interrupted_publications(self, *, retry_seconds: float = 60):
        """利用删除申请流的去重保护，恢复中断和旧挂起记录的延迟重试。"""
        if not math.isfinite(retry_seconds) or retry_seconds<=0:
            raise ValueError('重试间隔必须为有限正数')
        with self._connection() as connection:
            connection.execute("""UPDATE deletion_reviews SET delivery_status='uncertain',
                last_publish_error='process_interrupted',next_publish_at=?,updated_at=?
                WHERE delivery_status='publishing' AND review_status='pending_send'""",
                (time.time()+retry_seconds,datetime.now(UTC).isoformat()))
            connection.execute("""UPDATE deletion_reviews SET next_publish_at=?
                WHERE delivery_status='uncertain' AND review_status='pending_send' AND next_publish_at IS NULL""",
                (time.time()+retry_seconds,))

    def claim_next_publication(self):
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("""SELECT * FROM deletion_reviews
                WHERE review_status='pending_send' AND delivery_status IN ('pending','uncertain')
                AND (next_publish_at IS NULL OR next_publish_at<=?)
                ORDER BY created_at,submission_id LIMIT 1""", (time.time(),)).fetchone()
            if row is None:
                return None
            connection.execute("""UPDATE deletion_reviews SET delivery_status='publishing',
                publish_attempts=publish_attempts+1,updated_at=? WHERE submission_id=?""",
                (datetime.now(UTC).isoformat(), row['submission_id']))
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?',
                (row['submission_id'],)).fetchone())

    def finish_publication(self, submission_id: UUID, *, state: str, message_id: str | None,
                           error_code: str | None, retry_seconds: float):
        if state not in {'published', 'retry', 'blocked', 'uncertain'} or not math.isfinite(retry_seconds) or retry_seconds<=0:
            raise ValueError('发送结果与重试间隔无效')
        if state == 'published' and message_id is None:
            raise ValueError('发送成功必须提供消息 ID')
        if message_id is not None:
            self._message_id(message_id)
            if state in {'retry', 'blocked'}:
                raise ValueError('明确未发布的结果不能携带消息 ID')
        sid = str(UUID(str(submission_id)))
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            record = self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())
            # 审核反馈可能先于本轮发送回执到达，禁止回退终态或替换已审核的请求身份。
            if record.review_message_id is not None:
                if message_id is not None and message_id != record.message_id:
                    raise DeletionReviewConflictError('发送回执与已审核的请求身份冲突')
                return record
            message_id = message_id or record.message_id
            target = 'pending' if state == 'retry' else state
            if state == 'retry' and message_id is not None:
                target = 'uncertain'
            if (record.delivery_status == target and record.message_id == message_id
                    and record.last_publish_error == error_code):
                return record
            if record.delivery_status != 'publishing' or record.review_status != 'pending_send':
                raise DeletionReviewConflictError('申请不处于当前发送状态')
            if message_id is not None and connection.execute(
                'SELECT 1 FROM processed_deletion_review_receipts WHERE request_id=?', (message_id,)).fetchone():
                raise DeletionReviewConflictError('消息 ID 已关联已清理申请')
            try:
                connection.execute("""UPDATE deletion_reviews SET delivery_status=?,message_id=?,review_status=?,
                    next_publish_at=?,last_publish_error=?,updated_at=? WHERE submission_id=?""",
                    (target, message_id, 'pending_review' if state == 'published' else 'pending_send',
                    time.time()+retry_seconds if state in {'retry','uncertain'} else None,
                    error_code, datetime.now(UTC).isoformat(), sid))
            except sqlite3.IntegrityError as exc:
                raise DeletionReviewConflictError('消息 ID 已关联其他申请') from exc
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())

    def save_review_result(self, *, message_id: str, feedback: DeletionReviewFeedback):
        message_id = self._message_id(message_id)
        feedback = DeletionReviewFeedback.model_validate(feedback.model_dump(mode='json'))
        status = 'approved' if feedback.approved else 'rejected'
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._processed_receipt(connection, message_id, feedback)
            if receipt is not None:
                return receipt
            record = self._record(connection.execute('SELECT * FROM deletion_reviews WHERE message_id=?', (message_id,)).fetchone())
            if record.review_message_id is not None:
                if record.review_status != status or any(getattr(record, key) != getattr(feedback, key)
                        for key in ('review_message_id', 'reviewed_by', 'review_note', 'reviewed_at')):
                    raise DeletionReviewConflictError('审核反馈与已保存结果不一致，禁止覆盖')
                return record
            if record.review_status not in {'pending_review', 'pending_send'}:
                raise DeletionReviewConflictError('只有已送审申请可以保存审核结果')
            try:
                connection.execute('''UPDATE deletion_reviews SET review_status=?, review_message_id=?,
                    reviewed_by=?,review_note=?,reviewed_at=?,delivery_status='published',next_publish_at=NULL,
                    updated_at=? WHERE submission_id=?''',
                    (status, feedback.review_message_id, feedback.reviewed_by, feedback.review_note,
                     feedback.reviewed_at.isoformat(), datetime.now(UTC).isoformat(), str(record.submission_id)))
            except sqlite3.IntegrityError as exc:
                raise DeletionReviewConflictError('该反馈消息已用于其他删除申请') from exc
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?',
                (str(record.submission_id),)).fetchone())

    def recover_interrupted_deletions(self):
        """单 worker 启动时恢复残留领取；实际删除以正式库的 deleting 状态继续。"""
        with self._connection() as connection:
            connection.execute("""UPDATE deletion_reviews SET deletion_status='failed', next_deletion_at=0,
                last_deletion_error='deletion_interrupted',updated_at=?
                WHERE review_status='approved' AND deletion_status='deleting'""", (datetime.now(UTC).isoformat(),))

    def claim_next_deletion(self, *, now: float | None = None):
        """只领取明确批准且未完成的申请；领取和尝试计数在同一短事务中。"""
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("""SELECT * FROM deletion_reviews WHERE review_status='approved'
                AND review_message_id IS NOT NULL AND deletion_status IN ('pending','failed')
                AND (next_deletion_at IS NULL OR next_deletion_at<=?)
                ORDER BY created_at,submission_id LIMIT 1""", (time.time() if now is None else now,)).fetchone()
            if row is None:
                return None
            connection.execute("""UPDATE deletion_reviews SET deletion_status='deleting',
                deletion_attempts=deletion_attempts+1,updated_at=? WHERE submission_id=?""",
                (datetime.now(UTC).isoformat(),row['submission_id']))
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?',
                (row['submission_id'],)).fetchone())

    def finish_deletion(self, submission_id: UUID, *, succeeded: bool, error_code: str | None = None,
                        retry_seconds: float = 60):
        if type(succeeded) is not bool or retry_seconds<=0:
            raise ValueError('删除结果须为布尔值，重试间隔须为正数')
        if not succeeded and (not error_code or len(error_code)>128):
            raise ValueError('删除失败须提供脱敏错误代码')
        sid = str(UUID(str(submission_id)))
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            record = self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())
            if record.deletion_status == 'succeeded' and succeeded:
                return record
            if record.review_status != 'approved' or record.deletion_status != 'deleting':
                raise DeletionReviewConflictError('只有已领取的批准申请可以更新删除结果')
            now = datetime.now(UTC).isoformat()
            connection.execute('''UPDATE deletion_reviews SET deletion_status=?,next_deletion_at=?,
                last_deletion_error=?,deletion_completed_at=?,review_processed_at=?,updated_at=? WHERE submission_id=?''',
                ('succeeded' if succeeded else 'failed', None if succeeded else time.time()+retry_seconds,
                 None if succeeded else error_code, now if succeeded else None, now if succeeded else None, now, sid))
            return self._record(connection.execute('SELECT * FROM deletion_reviews WHERE submission_id=?', (sid,)).fetchone())

    def mark_rejection_processed(self, submission_id: UUID):
        """由申请服务在恢复标志之后调用；不根据远端时间提前开始清理计时。"""
        with self._connection() as connection:
            now = datetime.now(UTC).isoformat()
            connection.execute('''UPDATE deletion_reviews SET review_processed_at=?,updated_at=?
                WHERE submission_id=? AND review_status='rejected'
                AND review_message_id IS NOT NULL AND review_processed_at IS NULL''',
                (now, now, str(UUID(str(submission_id)))))

    def list_cleanup_candidates(self, *, cutoff: datetime, limit: int):
        if cutoff.utcoffset() is None or type(limit) is not int or limit <= 0:
            raise ValueError('清理截止时间必须带时区，批量大小必须为正整数')
        with self._connection() as connection:
            rows = connection.execute('''SELECT submission_id FROM deletion_reviews
                WHERE review_processed_at IS NOT NULL AND review_message_id IS NOT NULL
                AND julianday(review_processed_at)<=julianday(?)
                AND ((review_status='approved' AND deletion_status='succeeded') OR review_status='rejected')
                ORDER BY review_processed_at,submission_id LIMIT ?''',
                (cutoff.astimezone(UTC).isoformat(), limit))
            return tuple(UUID(row['submission_id']) for row in rows)

    def cleanup_completed(self, submission_id: UUID, *, cutoff: datetime) -> bool:
        """同一事务保存最小回执和删除审核行；绝不操作引用的正式 PDF。"""
        if cutoff.utcoffset() is None:
            raise ValueError('清理截止时间必须带时区')
        sid = str(UUID(str(submission_id)))
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT * FROM deletion_reviews WHERE submission_id=?
                AND review_processed_at IS NOT NULL AND review_message_id IS NOT NULL
                AND julianday(review_processed_at)<=julianday(?)
                AND ((review_status='approved' AND deletion_status='succeeded') OR review_status='rejected')''',
                (sid, cutoff.astimezone(UTC).isoformat())).fetchone()
            if row is None:
                return False
            if not row['message_id']:
                raise DeletionReviewConflictError('已完成删除申请缺少消息身份，不能安全清理')
            feedback = DeletionReviewFeedback(review_message_id=row['review_message_id'],
                approved=row['review_status']=='approved', reviewed_by=row['reviewed_by'],
                review_note=row['review_note'], reviewed_at=row['reviewed_at'])
            connection.execute('''INSERT INTO processed_deletion_review_receipts
                (submission_id,request_id,review_message_id,review_payload_sha256,review_processed_at,cleaned_at)
                VALUES (?,?,?,?,?,?)''', (sid, row['message_id'], row['review_message_id'],
                    self._feedback_hash(feedback), row['review_processed_at'], datetime.now(UTC).isoformat()))
            connection.execute('DELETE FROM deletion_reviews WHERE submission_id=?', (sid,))
            return True
