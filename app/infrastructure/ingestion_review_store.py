"""独立待审 SQLite 与 PDF 原子发布；不访问正式合同存储。"""
from contextlib import contextmanager
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import math
from uuid import UUID

import pymupdf

from app.schema.ingestion_review import IngestionReviewRecord, IngestionReviewSnapshot, ProcessedReviewReceipt


class IngestionReviewConflictError(ValueError):
    """同一提交或提取任务不能被另一份快照覆盖。"""


class IngestionReviewFileError(RuntimeError):
    """待审文件缺失或与保存的哈希不一致。"""


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS ingestion_reviews (
    submission_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    message_id TEXT,
    document_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
    snapshot_sha256 TEXT NOT NULL,
    pdf_relative_path TEXT NOT NULL UNIQUE,
    review_status TEXT NOT NULL DEFAULT 'pending_send'
        CHECK(review_status IN ('pending_send', 'pending_review', 'approved', 'rejected')),
    ingestion_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(ingestion_status IN ('pending', 'ingesting', 'succeeded', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ingestion_reviews_status_created
    ON ingestion_reviews(review_status, created_at, submission_id);
CREATE INDEX IF NOT EXISTS ingestion_reviews_document ON ingestion_reviews(document_id);
CREATE TABLE IF NOT EXISTS processed_review_receipts (
    submission_id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    review_message_id TEXT NOT NULL UNIQUE,
    review_payload_sha256 TEXT NOT NULL,
    review_processed_at TEXT NOT NULL,
    review_acked_at TEXT,
    cleaned_at TEXT NOT NULL
);
'''


class SQLiteIngestionReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.database_path = self.root / 'reviews.db'
        self.files_root = self.root / 'files'

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
        self.files_root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connection() as c:
            c.execute('PRAGMA journal_mode=WAL')
            tables = {row['name'] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'pending_reviews' in tables and 'ingestion_reviews' in tables:
                raise ValueError('入库审核库同时存在新旧申请表，拒绝覆盖，请先核对数据')
            # 原地迁移申请表，保留申请、消息 ID 和反馈回执，避免改名后重复送审。
            migration = 'ALTER TABLE pending_reviews RENAME TO ingestion_reviews;\n' if 'pending_reviews' in tables else ''
            legacy_indexes = ('status_created', 'document', 'cleanup', 'delivery', 'feedback', 'passport', 'message')
            migration += ''.join(f'DROP INDEX IF EXISTS pending_reviews_{name};\n' for name in legacy_indexes)
            c.executescript('BEGIN IMMEDIATE;\n' + migration + _SCHEMA)
            # 兼容本次功能早期创建的待审库；旧记录尚未发送，消息 ID 保持为空。
            columns = {row['name'] for row in c.execute('PRAGMA table_info(ingestion_reviews)')}
            if 'message_id' not in columns:
                c.execute('ALTER TABLE ingestion_reviews ADD COLUMN message_id TEXT')
            additions = {
                'review_message_id': 'TEXT',
                'review_offset': 'INTEGER',
                'review_payload_json': 'TEXT',
                'review_note': 'TEXT',
                'reviewed_by': 'TEXT',
                'reviewed_at': 'TEXT',
                'passport': 'TEXT',
                'review_processed_at': 'TEXT',
                'review_acked_at': 'TEXT',
                'last_review_error': 'TEXT',
                'note': "TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 10000)",
                'delivery_status': "TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_status IN ('pending','publishing','published','uncertain','blocked'))",
                'publish_attempts': 'INTEGER NOT NULL DEFAULT 0 CHECK(publish_attempts >= 0)',
                'next_publish_at': 'REAL',
                'last_publish_error': 'TEXT',
            }
            for name, definition in additions.items():
                if name not in columns:
                    c.execute(f'ALTER TABLE ingestion_reviews ADD COLUMN {name} {definition}')
            # 将早期快照中的备注移到申请列，同事务更新快照和哈希。
            for row in c.execute("SELECT submission_id,snapshot_json FROM ingestion_reviews WHERE json_type(snapshot_json,'$.note') IS NOT NULL").fetchall():
                payload = json.loads(row['snapshot_json'])
                note = payload.pop('note')
                if not isinstance(note, str) or len(note) > 10000:
                    raise ValueError('旧待审备注不符合字符串及长度约束')
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                c.execute('UPDATE ingestion_reviews SET note=?,snapshot_json=?,snapshot_sha256=? WHERE submission_id=?',
                    (note,content,hashlib.sha256(content.encode()).hexdigest(),row['submission_id']))
            # 已经绑定消息的旧记录不再扫描，避免升级后重复发送。
            if 'delivery_status' not in columns:
                c.execute("UPDATE ingestion_reviews SET delivery_status='published' WHERE message_id IS NOT NULL")
            c.execute('CREATE INDEX IF NOT EXISTS ingestion_reviews_cleanup ON ingestion_reviews(review_processed_at) WHERE review_processed_at IS NOT NULL')
            c.execute('CREATE INDEX IF NOT EXISTS ingestion_reviews_delivery ON ingestion_reviews(delivery_status,next_publish_at,created_at)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ingestion_reviews_feedback ON ingestion_reviews(review_message_id)')
            # 多份送审合同可共用通行证；幂等仍由申请与反馈消息标识保证。
            c.execute('DROP INDEX IF EXISTS ingestion_reviews_passport')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ingestion_reviews_message ON ingestion_reviews(message_id)')
            # 路径改为相对 files 目录；只迁移与申请身份严格匹配的旧值，不移动 PDF。
            c.execute("""UPDATE ingestion_reviews SET pdf_relative_path=submission_id || '.pdf'
                WHERE pdf_relative_path='files/' || submission_id || '.pdf'""")

    def _file_path(self, submission_id):
        # 路径仅由规范化 UUID 生成，不接受调用方提供文件路径。
        return self.files_root / f'{UUID(str(submission_id))}.pdf'

    @staticmethod
    def _record(row):
        if row is None:
            raise LookupError('待审申请不存在')
        return IngestionReviewRecord(submission_id=row['submission_id'],
            note=row['note'], message_id=row['message_id'], delivery_status=row['delivery_status'],
            publish_attempts=row['publish_attempts'], next_publish_at=row['next_publish_at'],
            last_publish_error=row['last_publish_error'],
            **{key: row[key] for key in ('review_message_id', 'review_offset', 'review_note',
                'reviewed_by', 'reviewed_at', 'passport', 'review_processed_at', 'review_acked_at', 'last_review_error')},
            snapshot=IngestionReviewSnapshot.model_validate_json(row['snapshot_json']),
            pdf_relative_path=row['pdf_relative_path'], review_status=row['review_status'],
            ingestion_status=row['ingestion_status'], created_at=row['created_at'], updated_at=row['updated_at'])

    @staticmethod
    def _validate_pdf(snapshot, pdf_bytes):
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
            raise ValueError('待审 PDF 必须为非空 bytes')
        if hashlib.sha256(pdf_bytes).hexdigest() != snapshot.document_id:
            raise ValueError('待审 PDF 哈希与 document_id 不一致')
        try:
            with pymupdf.open(stream=pdf_bytes, filetype='pdf') as document:
                if not document.is_pdf or document.needs_pass or document.page_count != snapshot.page_count:
                    raise ValueError('待审 PDF 类型、加密状态或页数不符合快照')
        except (RuntimeError, pymupdf.FileDataError) as exc:
            raise ValueError('待审文件不是可读取的 PDF') from exc

    def _publish_pdf(self, path, pdf_bytes, document_id):
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != document_id:
                raise IngestionReviewFileError('已存在的待审 PDF 与快照不一致')
            return
        fd, temporary = tempfile.mkstemp(prefix='.pending-', suffix='.tmp', dir=self.files_root)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(pdf_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            # 文件先持久化再提交数据库；断电恢复时不发布指向未写完 PDF 的记录。
            directory = os.open(self.files_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, submission_id: UUID, snapshot: IngestionReviewSnapshot, pdf_bytes: bytes, *, note: str = "") -> IngestionReviewRecord:
        submission_id = str(UUID(str(submission_id)))
        if not isinstance(note, str) or len(note) > 10000:
            raise ValueError('入库员备注必须为不超过 10000 字符的字符串')
        # 防止已构造模型的嵌套 dict 被调用方修改后绕过校验。
        snapshot = IngestionReviewSnapshot.model_validate(snapshot.model_dump(mode='json'))
        self._validate_pdf(snapshot, pdf_bytes)
        content = json.dumps(snapshot.model_dump(mode='json'), ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(content.encode()).hexdigest()
        path = self._file_path(submission_id)
        with self._connection() as c:
            # 跨进程提交串行，覆盖文件判断和记录提交，避免两个重试并发覆盖。
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM processed_review_receipts WHERE submission_id=?', (submission_id,)).fetchone():
                raise IngestionReviewConflictError('该申请已完成并清理，不能重新提交同一申请')
            old = c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=? OR run_id=?',
                            (submission_id, snapshot.run_id)).fetchone()
            if old is not None:
                if old['submission_id'] != submission_id or self._record(old).snapshot != snapshot or old['note'] != note:
                    raise IngestionReviewConflictError('该申请或提取任务已有不同的待审快照，禁止覆盖')
                # 上次请求可能已提交但未收到响应；同 ID 同内容返回原记录。
                self._publish_pdf(path, pdf_bytes, snapshot.document_id)
                return self._record(old)
            self._publish_pdf(path, pdf_bytes, snapshot.document_id)
            now = datetime.now(UTC).isoformat()
            c.execute('''INSERT INTO ingestion_reviews
                (submission_id,run_id,document_id,submitted_by,schema_version,snapshot_json,
                 snapshot_sha256,pdf_relative_path,created_at,updated_at,note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (submission_id,snapshot.run_id,snapshot.document_id,snapshot.submitted_by,
                 snapshot.schema_version,content,digest,f'{submission_id}.pdf',now,now,note))
            return self._record(c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?',
                                         (submission_id,)).fetchone())

    def get(self, submission_id: UUID) -> IngestionReviewRecord:
        with self._connection() as c:
            return self._record(c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?',
                                          (str(UUID(str(submission_id))),)).fetchone())

    def list_ids(self) -> tuple[UUID, ...]:
        """列表只读取申请 ID，按提交时间倒序、同时间按 ID 升序返回。"""
        with self._connection() as c:
            rows = c.execute('SELECT submission_id FROM ingestion_reviews ORDER BY created_at DESC,submission_id ASC').fetchall()
            return tuple(UUID(row['submission_id']) for row in rows)

    def get_basic(self, submission_id: UUID):
        """按申请身份读取轻量详情，避免加载大型快照及融合向量。"""
        with self._connection() as c:
            row = c.execute("""SELECT submission_id,run_id,document_id,submitted_by,note,
                review_note,reviewed_by,reviewed_at,passport,message_id,delivery_status,
                review_status,ingestion_status,created_at,updated_at,review_processed_at,
                pdf_relative_path,review_message_id,
                json_extract(snapshot_json,'$.file_name') AS file_name,
                json_extract(snapshot_json,'$.summary') AS summary,
                json_extract(snapshot_json,'$.page_count') AS page_count
                FROM ingestion_reviews WHERE submission_id=?""", (str(UUID(str(submission_id))),)).fetchone()
            if row is None:
                raise LookupError('待审申请不存在')
            return dict(row)

    def read_pdf(self, submission_id: UUID) -> bytes:
        record = self.get(submission_id)
        try:
            data = self._file_path(submission_id).read_bytes()
        except FileNotFoundError as exc:
            raise IngestionReviewFileError('待审 PDF 缺失') from exc
        if hashlib.sha256(data).hexdigest() != record.snapshot.document_id:
            raise IngestionReviewFileError('待审 PDF 哈希校验失败')
        return data

    @staticmethod
    def _message_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('中间件 message_id 必须为非空字符串')
        return value

    def bind_message_id(self, submission_id: UUID, message_id: str) -> IngestionReviewRecord:
        """仅在发送确认后调用：原子关联回执并标记待审核，同值重试不修改状态。"""
        submission_id = str(UUID(str(submission_id)))
        message_id = self._message_id(message_id)
        with self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?', (submission_id,)).fetchone()
            record = self._record(row)
            if record.message_id is not None:
                if record.message_id != message_id:
                    raise IngestionReviewConflictError('待审申请已绑定其他中间件消息，禁止覆盖')
                return record
            if record.review_status != 'pending_send':
                raise IngestionReviewConflictError('只有待发送申请可以首次绑定中间件消息')
            try:
                c.execute("""UPDATE ingestion_reviews SET message_id=?, review_status='pending_review', delivery_status='published', updated_at=?
                    WHERE submission_id=?""", (message_id, datetime.now(UTC).isoformat(), submission_id))
            except sqlite3.IntegrityError as exc:
                raise IngestionReviewConflictError('该中间件消息已绑定其他待审申请') from exc
            return self._record(c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?',
                                          (submission_id,)).fetchone())

    def get_by_message_id(self, message_id: str) -> IngestionReviewRecord:
        with self._connection() as c:
            return self._record(c.execute('SELECT * FROM ingestion_reviews WHERE message_id=?',
                                          (self._message_id(message_id),)).fetchone())

    def recover_interrupted_publications(self, *, retry_seconds: float = 60):
        """中间件按平台与 source_id 去重；中断及旧挂起记录恢复为延迟重试。"""
        if not math.isfinite(retry_seconds) or retry_seconds<=0:
            raise ValueError('重试间隔必须为有限正数')
        with self._connection() as c:
            c.execute("""UPDATE ingestion_reviews SET delivery_status='uncertain',
                last_publish_error='process_interrupted',next_publish_at=?,updated_at=?
                WHERE delivery_status='publishing' AND review_status='pending_send'""",
                (time.time()+retry_seconds,datetime.now(UTC).isoformat()))
            c.execute("""UPDATE ingestion_reviews SET next_publish_at=?
                WHERE delivery_status='uncertain' AND review_status='pending_send' AND next_publish_at IS NULL""",
                (time.time()+retry_seconds,))

    def claim_next_publication(self) -> IngestionReviewRecord | None:
        with self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("""SELECT * FROM ingestion_reviews WHERE review_status='pending_send'
                AND delivery_status IN ('pending','uncertain')
                AND (next_publish_at IS NULL OR next_publish_at<=?)
                ORDER BY created_at,submission_id LIMIT 1""", (time.time(),)).fetchone()
            if row is None:
                return None
            c.execute("""UPDATE ingestion_reviews SET delivery_status='publishing',
                publish_attempts=publish_attempts+1, updated_at=? WHERE submission_id=?""",
                (datetime.now(UTC).isoformat(),row['submission_id']))
            return self._record(c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?',
                                          (row['submission_id'],)).fetchone())

    def finish_publication(self, submission_id: UUID, *, state: str, message_id: str | None,
                           error_code: str | None, retry_seconds: float):
        if state not in {'published', 'retry', 'blocked', 'uncertain'} or not math.isfinite(retry_seconds) or retry_seconds<=0:
            raise ValueError('未知消息发送结果')
        if state == 'published' and message_id is None:
            raise ValueError('发布成功必须携带 message_id')
        if message_id is not None:
            self._message_id(message_id)
        sid = str(UUID(str(submission_id)))
        with self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            record = self._record(c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?',(sid,)).fetchone())
            # 重试期间可能先收到旧请求的审核反馈；迟到的发送回执不能回退审核终态。
            if record.review_message_id is not None:
                if message_id is not None and message_id != record.message_id:
                    raise IngestionReviewConflictError('发送回执与已审核的请求身份冲突')
                return
            # 新一轮超时或401没有 ID 时，保留此前不确定响应中的关联身份。
            message_id = message_id or record.message_id
            target = 'pending' if state == 'retry' else state
            if state == 'retry' and message_id is not None:
                target = 'uncertain'
            if (record.delivery_status == target and record.message_id == message_id
                    and record.last_publish_error == error_code):
                return
            if record.delivery_status != 'publishing' or record.review_status != 'pending_send':
                raise IngestionReviewConflictError('申请不处于当前发送状态')
            try:
                c.execute("""UPDATE ingestion_reviews SET delivery_status=?,message_id=?,review_status=?,
                    next_publish_at=?,last_publish_error=?,updated_at=? WHERE submission_id=?""",
                    (target, message_id,
                     'pending_review' if state == 'published' else 'pending_send',
                     time.time()+retry_seconds if state in {'retry','uncertain'} else None,
                     error_code,datetime.now(UTC).isoformat(),sid))
            except sqlite3.IntegrityError as exc:
                raise IngestionReviewConflictError('中间件消息 ID 已关联其他申请') from exc


    def receive_review(self, delivery):
        """先保存不可变反馈，再执行跨库入库；重投不得改写既有审核决定。"""
        from app.infrastructure.middleware import MiddlewareReviewDelivery
        delivery = MiddlewareReviewDelivery.model_validate(delivery)
        message = delivery.message
        payload = message.model_dump_json()
        now = datetime.now(UTC).isoformat()
        with self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            receipt = c.execute('SELECT * FROM processed_review_receipts WHERE request_id=? OR review_message_id=?',
                (message.request_id, message.message_id)).fetchone()
            if receipt is not None:
                if (receipt['request_id'] != message.request_id or receipt['review_message_id'] != message.message_id
                        or receipt['review_payload_sha256'] != hashlib.sha256(payload.encode()).hexdigest()):
                    raise IngestionReviewConflictError('已清理申请收到冲突反馈，不能确认消息')
                return ProcessedReviewReceipt(submission_id=receipt['submission_id'],
                    review_message_id=receipt['review_message_id'], review_processed_at=receipt['review_processed_at'])
            row = c.execute('SELECT * FROM ingestion_reviews WHERE message_id=?', (message.request_id,)).fetchone()
            if row is None:
                raise LookupError('审核反馈未匹配到本地入库请求，不确认消息')
            if row['review_message_id'] is not None:
                if row['review_message_id'] != message.message_id or row['review_payload_json'] != payload:
                    raise IngestionReviewConflictError('同一申请收到冲突的审核反馈')
                # 同一反馈可能因重投位于不同 offset；每次只确认当前 pull 的位置。
                c.execute('UPDATE ingestion_reviews SET review_offset=? WHERE submission_id=?',
                    (delivery.offset, row['submission_id']))
            else:
                try:
                    c.execute("""UPDATE ingestion_reviews SET review_message_id=?,review_offset=?,
                        review_payload_json=?,review_note=?,reviewed_by=?,reviewed_at=?,passport=?,
                        review_status=?,delivery_status='published',next_publish_at=NULL,updated_at=? WHERE submission_id=?""",
                        (message.message_id,delivery.offset,payload,message.note,message.reviewer,
                         message.created_at.isoformat(),message.passport,
                         'approved' if message.passport else 'rejected',now,row['submission_id']))
                except sqlite3.IntegrityError as exc:
                    raise IngestionReviewConflictError('反馈标识已绑定其他申请') from exc
            return self._record(c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?',
                (row['submission_id'],)).fetchone())

    def update_review_processing(self, submission_id, *, state, error_code=None):
        """本地完成标记是 ack 的前置条件；拒绝不会创建正式合同。"""
        if state not in ('ingesting', 'failed', 'completed', 'acknowledged'):
            raise ValueError('未知审核处理状态')
        now = datetime.now(UTC).isoformat()
        with self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM ingestion_reviews WHERE submission_id=?', (str(submission_id),)).fetchone()
            if row is None and state == 'acknowledged':
                # 清理与 ack 可以交错；快照删除后仍能记录确认，重投不再触发正式入库。
                cursor = c.execute('UPDATE processed_review_receipts SET review_acked_at=COALESCE(review_acked_at,?) WHERE submission_id=?',
                    (now, str(submission_id)))
                if cursor.rowcount == 1:
                    return
            record = self._record(row)
            if record.review_message_id is None:
                raise IngestionReviewConflictError('尚未保存审核反馈')
            if state == 'acknowledged':
                if record.review_processed_at is None:
                    raise IngestionReviewConflictError('本地处理尚未完成，不能确认反馈')
                c.execute('UPDATE ingestion_reviews SET review_acked_at=COALESCE(review_acked_at,?),updated_at=? WHERE submission_id=?',
                    (now,now,str(submission_id)))
            elif record.review_processed_at is None:
                approved = bool(record.passport)
                if state != 'completed' and not approved:
                    raise IngestionReviewConflictError('拒绝反馈不能进入正式入库')
                ingestion_status = ('succeeded' if approved else 'pending') if state == 'completed' else state
                c.execute("""UPDATE ingestion_reviews SET ingestion_status=?,review_processed_at=?,
                    last_review_error=?,updated_at=? WHERE submission_id=?""",
                    (ingestion_status,now if state == 'completed' else None,error_code,now,str(submission_id)))


    def list_cleanup_candidates(self, *, cutoff: datetime, limit: int) -> tuple[UUID, ...]:
        """仅按本地处理完成时间计龄，不按提交时间或远端审核时间计龄。"""
        if cutoff.utcoffset() is None or type(limit) is not int or limit <= 0:
            raise ValueError('清理截止时间必须带时区，批量大小必须为正整数')
        with self._connection() as c:
            rows = c.execute("""SELECT submission_id FROM ingestion_reviews
                WHERE review_processed_at IS NOT NULL AND review_message_id IS NOT NULL
                  AND julianday(review_processed_at) <= julianday(?)
                  AND ((review_status='approved' AND ingestion_status='succeeded') OR review_status='rejected')
                ORDER BY review_processed_at,submission_id LIMIT ?""",
                (cutoff.astimezone(UTC).isoformat(),limit)).fetchall()
            return tuple(UUID(row['submission_id']) for row in rows)

    def cleanup_completed(self, submission_id: UUID, *, cutoff: datetime) -> bool:
        """先建立回执，再删除 PDF 和快照；文件失败则回滚，缺失文件允许重试。"""
        if cutoff.utcoffset() is None:
            raise ValueError('清理截止时间必须带时区')
        sid = str(UUID(str(submission_id)))
        with self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("""SELECT * FROM ingestion_reviews WHERE submission_id=?
                AND review_processed_at IS NOT NULL AND review_message_id IS NOT NULL
                AND julianday(review_processed_at) <= julianday(?)
                AND ((review_status='approved' AND ingestion_status='succeeded') OR review_status='rejected')""",
                (sid,cutoff.astimezone(UTC).isoformat())).fetchone()
            if row is None:
                return False
            if not row['message_id'] or not row['review_payload_json']:
                raise IngestionReviewConflictError('已完成申请缺少消息身份，不能安全清理')
            c.execute("""INSERT INTO processed_review_receipts
                (submission_id,request_id,review_message_id,review_payload_sha256,
                 review_processed_at,review_acked_at,cleaned_at) VALUES (?,?,?,?,?,?,?)""",
                (sid,row['message_id'],row['review_message_id'],
                 hashlib.sha256(row['review_payload_json'].encode()).hexdigest(),
                 row['review_processed_at'],row['review_acked_at'],datetime.now(UTC).isoformat()))
            # 不使用数据库中的自由路径，只删除 files/<规范 UUID>.pdf；符号链接仅删除链接本身。
            # 持有短事务覆盖本地文件删除，避免并发保存重建文件。无网络调用。
            self._file_path(sid).unlink(missing_ok=True)
            directory_fd = os.open(self.files_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # 若文件已删但事务提交失败，快照仍在，下次缺失文件按成功继续清理。
            c.execute('DELETE FROM ingestion_reviews WHERE submission_id=?', (sid,))
            return True
