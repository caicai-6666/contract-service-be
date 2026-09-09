"""会话、终态任务/摘要与工作区持久化；不消费 SSE 或构造模型消息。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from pydantic import SecretStr

from app.infrastructure.sqlite_vector import connect_vector_database
from app.schema.communication import TERMINAL_STATUSES


_RECORD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS conversation_records (
    record_id TEXT PRIMARY KEY NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    kind TEXT NOT NULL CHECK(kind IN ('task', 'summary')),
    turn_id TEXT UNIQUE,
    status TEXT CHECK(status IN ('completed', 'cancelled', 'superseded', 'rejected', 'failed', 'expired')),
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload) = 'object'),
    created_at INTEGER NOT NULL,
    activated_at INTEGER,
    processing_duration_ms INTEGER CHECK(processing_duration_ms IS NULL OR (kind = 'task' AND typeof(processing_duration_ms) = 'integer' AND processing_duration_ms >= 0)),
    retrieval_text TEXT,
    embedding BLOB,
    memory_processed_at INTEGER CHECK(memory_processed_at IS NULL OR (kind = 'task' AND typeof(memory_processed_at) = 'integer' AND memory_processed_at >= 0)),
    UNIQUE(conversation_id, sequence),
    CHECK((kind = 'task' AND turn_id IS NOT NULL AND length(turn_id) > 0 AND status IS NOT NULL)
       OR (kind = 'summary' AND turn_id IS NULL AND status IS NULL)),
    CHECK((embedding IS NULL AND retrieval_text IS NULL)
       OR (typeof(embedding) = 'blob' AND length(embedding) > 0 AND length(embedding) % 4 = 0
           AND retrieval_text IS NOT NULL AND length(trim(retrieval_text)) > 0))
);
"""


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY NOT NULL CHECK(length(conversation_id) > 0),
    secret_key TEXT NOT NULL CHECK(length(trim(secret_key)) > 0),
    created_at INTEGER NOT NULL,
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200)
);
CREATE INDEX IF NOT EXISTS conversations_owner_time
    ON conversations(secret_key, created_at, conversation_id);

{_RECORD_TABLE_SQL}
CREATE INDEX IF NOT EXISTS records_summary_boundary
    ON conversation_records(conversation_id, sequence) WHERE kind = 'summary';
CREATE INDEX IF NOT EXISTS records_time
    ON conversation_records(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS conversation_workspaces (
    conversation_id TEXT PRIMARY KEY NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload) = 'object'),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    updated_at INTEGER NOT NULL
);
"""


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _default_name(created_at: int) -> str:
    # 使用同一个创建时间与固定 UTC+8，避免容器默认 UTC 导致展示名称偏移。
    return datetime.fromtimestamp(created_at / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _json(value: dict) -> str:
    if not isinstance(value, dict):
        raise ValueError("内容必须是 JSON 对象")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class CommunicationStoreConflict(ValueError):
    """摘要边界或工作区版本过期，需要重新读取后提交。"""


class SQLiteCommunicationStore:
    """同步短事务存储接口；调用方须先认证，传入服务端取得的 SecretStr。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # 新库在 SQLite 打开前以仅当前用户可读写的权限创建；不改写现有库权限。
        try:
            descriptor = os.open(self.database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._connection() as connection:
            connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            # 老记录没有可靠计时来源，保持 NULL，不用落库时间推算任务耗时。
            record_columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_records)")}
            if "activated_at" not in record_columns:
                connection.execute("ALTER TABLE conversation_records ADD COLUMN activated_at INTEGER")
            if "processing_duration_ms" not in record_columns:
                connection.execute(
                    "ALTER TABLE conversation_records ADD COLUMN processing_duration_ms INTEGER "
                    "CHECK(processing_duration_ms IS NULL OR (kind = 'task' "
                    "AND typeof(processing_duration_ms) = 'integer' AND processing_duration_ms >= 0))"
                )
            self._remove_embedding_model(connection)
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(conversation_records)')}
            if 'memory_processed_at' not in columns:
                connection.execute("ALTER TABLE conversation_records ADD COLUMN memory_processed_at INTEGER "
                                   "CHECK(memory_processed_at IS NULL OR (kind = 'task' "
                                   "AND typeof(memory_processed_at) = 'integer' AND memory_processed_at >= 0))")
            # 旧库中已有有效检索对的任务已加工；空向量不能代表已筛选跳过。
            connection.execute("UPDATE conversation_records SET memory_processed_at = ? "
                               "WHERE kind = 'task' AND memory_processed_at IS NULL AND embedding IS NOT NULL", (_now(),))
            connection.execute("CREATE INDEX IF NOT EXISTS records_memory_pending "
                               "ON conversation_records(conversation_id, sequence) "
                               "WHERE kind = 'task' AND memory_processed_at IS NULL")
            # 兼容名称字段确定前已初始化的库，不重建会话或改变所有权。
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversations)")}
            if "name" not in columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN name TEXT NOT NULL DEFAULT '新会话' "
                    "CHECK(length(trim(name)) BETWEEN 1 AND 200)"
                )
                for row in connection.execute("SELECT conversation_id, created_at FROM conversations").fetchall():
                    connection.execute("UPDATE conversations SET name = ? WHERE conversation_id = ?",
                                       (_default_name(row["created_at"]), row["conversation_id"]))

    @staticmethod
    def _remove_embedding_model(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_records)")}
        if "embedding_model" not in columns:
            return
        # 旧 CHECK 引用了待删除列，不能直接 DROP COLUMN；同一事务重建并保留其余字段。
        connection.execute(_RECORD_TABLE_SQL.replace("IF NOT EXISTS conversation_records", "conversation_records_new"))
        retained = "record_id, conversation_id, sequence, kind, turn_id, status, payload, created_at, activated_at, processing_duration_ms, retrieval_text, embedding"
        if 'memory_processed_at' in columns:
            retained += ', memory_processed_at'
        connection.execute(
            f"INSERT INTO conversation_records_new ({retained}) SELECT {retained} FROM conversation_records"
        )
        connection.execute("DROP TABLE conversation_records")
        connection.execute("ALTER TABLE conversation_records_new RENAME TO conversation_records")
        connection.execute(
            "CREATE INDEX records_summary_boundary ON conversation_records(conversation_id, sequence) WHERE kind = 'summary'"
        )
        connection.execute("CREATE INDEX records_time ON conversation_records(conversation_id, created_at)")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = connect_vector_database(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            # 分配会话序号、验证摘要边界和写入必须位于同一写事务。
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _owner(connection: sqlite3.Connection, conversation_id: str, secret_key: SecretStr) -> None:
        if not isinstance(secret_key, SecretStr) or not secret_key.get_secret_value().strip():
            raise ValueError("必须提供已认证用户的非空密钥")
        row = connection.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ? AND secret_key = ?",
            (conversation_id, secret_key.get_secret_value()),
        ).fetchone()
        if row is None:
            # 不向调用方区分不存在和属于其他用户，不回显密钥。
            raise LookupError("会话不存在")

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
            raise ValueError("会话名称必须为 1 至 200 个字符")
        return value.strip()

    def create_conversation(self, conversation_id: str, *, secret_key: SecretStr,
                            name: str | None = None) -> None:
        if not conversation_id.strip():
            raise ValueError("会话标识不能为空")
        if not isinstance(secret_key, SecretStr) or not secret_key.get_secret_value().strip():
            raise ValueError("必须提供已认证用户的非空密钥")
        now = _now()
        # 默认名称是创建时生成的固定文本，不随读取、重启或改名刷新。
        name = _default_name(now) if name is None else self._name(name)
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO conversations (conversation_id, secret_key, created_at, name) VALUES (?, ?, ?, ?)",
                (conversation_id, secret_key.get_secret_value(), now, name),
            )
            connection.execute(
                "INSERT INTO conversation_workspaces VALUES (?, ?, 0, ?)",
                (conversation_id, _json({"achieved_goals": [], "known_information": [], "next_tasks": []}), now),
            )

    def rename_conversation(self, conversation_id: str, *, secret_key: SecretStr, name: str) -> dict:
        """只修改本人会话的展示名称，不改变创建时间、归属或任务顺序。"""
        name = self._name(name)
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            connection.execute("UPDATE conversations SET name = ? WHERE conversation_id = ?", (name, conversation_id))
            return dict(connection.execute(
                "SELECT conversation_id, name, created_at FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone())

    def rollback_empty_conversation(self, conversation_id: str, *, secret_key: SecretStr) -> None:
        """仅补偿首轮注册失败的新会话；已有轨迹或更新过的工作区禁止删除。"""
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            result = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM conversation_records WHERE conversation_id = ?) "
                "AND EXISTS (SELECT 1 FROM conversation_workspaces WHERE conversation_id = ? AND revision = 0)",
                (conversation_id, conversation_id, conversation_id),
            )
            if result.rowcount != 1:
                raise CommunicationStoreConflict("会话已经产生内容，不能补偿删除")

    def read_conversation(self, conversation_id: str, *, secret_key: SecretStr) -> dict:
        with self._connection() as connection:
            self._owner(connection, conversation_id, secret_key)
            return dict(connection.execute(
                "SELECT conversation_id, name, created_at FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone())

    def list_conversations(self, *, secret_key: SecretStr) -> list[dict]:
        """返回当前密钥的全部会话；不读取轨迹，也不向外返回凭据。"""
        if not isinstance(secret_key, SecretStr) or not secret_key.get_secret_value().strip():
            raise ValueError("必须提供已认证用户的非空密钥")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT conversation_id, name, created_at FROM conversations "
                "WHERE secret_key = ? ORDER BY created_at DESC, conversation_id DESC",
                (secret_key.get_secret_value(),),
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_conversation(self, conversation_id: str, *, secret_key: SecretStr) -> None:
        """仅删除本人会话；外键在同一事务级联删除任务、摘要和工作区。"""
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            connection.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))

    def append_task(self, conversation_id: str, *, secret_key: SecretStr,
                    turn_id: str, status: str, payload: dict,
                    processing_duration_ms: int | None = None) -> int:
        """追加完整终态轮次；活动轮次继续由事件运行时管理，不保存为已完成历史。"""
        if status not in TERMINAL_STATUSES or not turn_id.strip():
            raise ValueError("历史任务必须具有轮次标识和有效终态")
        if processing_duration_ms is not None and (
            type(processing_duration_ms) is not int or processing_duration_ms < 0
        ):
            raise ValueError("处理时间必须为非负整数毫秒或空值")
        return self._append(conversation_id, secret_key=secret_key, kind="task",
                            turn_id=turn_id, status=status, payload=payload,
                            processing_duration_ms=processing_duration_ms)

    def append_summary(self, conversation_id: str, *, secret_key: SecretStr,
                       text: str, expected_sequence: int) -> int:
        """摘要须覆盖此前摘要和其后全部任务；拒绝对过期尾部追加摘要。"""
        if not text.strip() or expected_sequence < 1:
            raise ValueError("摘要和覆盖边界不能为空")
        return self._append(conversation_id, secret_key=secret_key, kind="summary",
                            payload={"text": text, "covers_through_sequence": expected_sequence},
                            expected_sequence=expected_sequence)

    def _append(self, conversation_id: str, *, secret_key: SecretStr, kind: str,
                payload: dict, turn_id: str | None = None, status: str | None = None,
                expected_sequence: int | None = None,
                processing_duration_ms: int | None = None) -> int:
        content = _json(payload)
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            last = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM conversation_records WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            if expected_sequence is not None and expected_sequence != last:
                raise CommunicationStoreConflict("摘要覆盖边界已过期")
            sequence = last + 1
            connection.execute(
                "INSERT INTO conversation_records "
                "(record_id, conversation_id, sequence, kind, turn_id, status, payload, created_at, processing_duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), conversation_id, sequence, kind, turn_id, status, content, _now(), processing_duration_ms),
            )
            return sequence

    def read_context_records(self, conversation_id: str, *, secret_key: SecretStr) -> list[dict]:
        """读取最新摘要（包含）及其后任务并正序返回；不是最终模型上下文。"""
        with self._connection() as connection:
            self._owner(connection, conversation_id, secret_key)
            rows = connection.execute(
                "SELECT record_id, sequence, kind, turn_id, status, payload, created_at, activated_at, processing_duration_ms "
                "FROM conversation_records WHERE conversation_id = ? AND sequence >= "
                "(SELECT COALESCE(MAX(sequence), 1) FROM conversation_records "
                "WHERE conversation_id = ? AND kind = 'summary') ORDER BY sequence",
                (conversation_id, conversation_id),
            ).fetchall()
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def read_history_window(
        self, conversation_id: str, *, secret_key: SecretStr,
        start_sequence: int | None = None, extend: bool = False, include_workspace: bool = False,
    ) -> dict:
        """首次从最新摘要读取；刷新向前跨一个摘要边界，查询在同一读事务内完成。"""
        with self._connection() as connection:
            connection.execute("BEGIN")
            self._owner(connection, conversation_id, secret_key)
            conversation = dict(connection.execute(
                "SELECT conversation_id, name, created_at FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone())
            if start_sequence is None:
                boundary = connection.execute(
                    "SELECT MAX(sequence) FROM conversation_records WHERE conversation_id = ? AND kind = 'summary'",
                    (conversation_id,),
                ).fetchone()[0] or 1
            elif extend:
                boundary = connection.execute(
                    "SELECT MAX(sequence) FROM conversation_records "
                    "WHERE conversation_id = ? AND kind = 'summary' AND sequence < ?",
                    (conversation_id, start_sequence),
                ).fetchone()[0] or 1
            else:
                boundary = start_sequence
            rows = connection.execute(
                "SELECT record_id, sequence, kind, turn_id, status, payload, created_at, activated_at, processing_duration_ms "
                "FROM conversation_records WHERE conversation_id = ? AND sequence >= ? ORDER BY sequence",
                (conversation_id, boundary),
            ).fetchall()
            earliest = rows[0]["sequence"] if rows else boundary
            has_more = bool(connection.execute(
                "SELECT EXISTS(SELECT 1 FROM conversation_records WHERE conversation_id = ? AND sequence < ?)",
                (conversation_id, earliest),
            ).fetchone()[0])
            result = conversation | {"records": [dict(row) | {"payload": json.loads(row["payload"])} for row in rows],
                                     "has_more": has_more}
            if include_workspace:
                # 首次驻留时与轨迹共用读事务，避免加载到不同提交时刻的数据。
                result['workspace'] = self._read_workspace(connection, conversation_id)
            return result

    def backup_task(self, conversation_id: str, *, secret_key: SecretStr, record: dict) -> None:
        """按注册时分配的身份与序号复制冻结任务；重试只接受完全相同的内容。"""
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            self._backup_task(connection, conversation_id, record)

    @staticmethod
    def _backup_task(connection: sqlite3.Connection, conversation_id: str, record: dict) -> None:
        from app.schema.communication import ConversationHistoryRecord

        item = ConversationHistoryRecord.model_validate(record)
        if item.kind != 'task' or item.status not in TERMINAL_STATUSES or not item.turn_id:
            raise ValueError('只能备份冻结的终态任务')
        columns = 'record_id, sequence, kind, turn_id, status, payload, created_at, activated_at, processing_duration_ms'
        existing = connection.execute(
            f'SELECT {columns} FROM conversation_records WHERE conversation_id = ? '
            'AND (sequence = ? OR turn_id = ? OR record_id = ?)',
            (conversation_id, item.sequence, item.turn_id, item.record_id),
        ).fetchall()
        if existing:
            if len(existing) != 1 or ConversationHistoryRecord.model_validate(
                dict(existing[0]) | {'payload': json.loads(existing[0]['payload'])}
            ) != item:
                raise CommunicationStoreConflict('备份与已保存记录冲突，拒绝覆盖')
            return
        connection.execute(
            f'INSERT INTO conversation_records (conversation_id, {columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (conversation_id, item.record_id, item.sequence, item.kind, item.turn_id, item.status,
             _json(item.payload), item.created_at, item.activated_at, item.processing_duration_ms),
        )

    def backup_tasks_with_workspace(
        self, conversation_id: str, *, secret_key: SecretStr, records: list[dict],
        workspace: dict, expected_workspace_revision: int,
    ) -> None:
        """原子复制同会话冻结轨迹与工作区；任一冲突则整批回滚。"""
        from app.schema.communication_workspace import WorkspaceSnapshot

        snapshot = WorkspaceSnapshot.model_validate(workspace)
        if type(expected_workspace_revision) is not int or not 0 <= expected_workspace_revision <= snapshot.revision:
            raise ValueError('工作区备份版本无效')
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            for record in records:
                self._backup_task(connection, conversation_id, record)
            self._backup_workspace(connection, conversation_id, snapshot, expected_workspace_revision)

    @classmethod
    def _backup_workspace(cls, connection, conversation_id, snapshot, expected_revision):
        saved = cls._read_workspace(connection, conversation_id)
        # 提交成功但回执丢失时只确认相同快照，不回写旧内容。
        if saved == snapshot.model_dump():
            return
        if saved['revision'] != expected_revision or snapshot.revision <= saved['revision']:
            raise CommunicationStoreConflict('工作区版本已过期，拒绝覆盖')
        connection.execute(
            'UPDATE conversation_workspaces SET payload = ?, revision = ?, updated_at = ? WHERE conversation_id = ?',
            (_json(snapshot.payload.model_dump()), snapshot.revision, snapshot.updated_at, conversation_id),
        )

    def read_memory_backlog(self, conversation_id: str, *, secret_key: SecretStr) -> dict:
        """归档专用读取：补充摘要边界之前尚未加工的任务，不读取已有检索正文/向量。"""
        with self._connection() as connection:
            connection.execute('BEGIN')
            self._owner(connection, conversation_id, secret_key)
            rows = connection.execute(
                'SELECT record_id, sequence, kind, turn_id, status, payload, created_at, activated_at, processing_duration_ms '
                "FROM conversation_records WHERE conversation_id = ? AND kind = 'task' AND memory_processed_at IS NULL ORDER BY sequence",
                (conversation_id,),
            ).fetchall()
            processed = connection.execute(
                'SELECT record_id FROM conversation_records WHERE conversation_id = ? AND memory_processed_at IS NOT NULL',
                (conversation_id,),
            ).fetchall()
            return {'records': [dict(row) | {'payload': json.loads(row['payload'])} for row in rows],
                    'processed_ids': {row['record_id'] for row in processed}}

    def archive_tasks_with_workspace(
        self, conversation_id: str, *, secret_key: SecretStr, records: list[dict],
        memories: list[dict], workspace: dict, expected_workspace_revision: int,
    ) -> None:
        """原轨迹、检索对、加工完成标记与工作区在同一事务提交，不修改原轨迹。"""
        from app.agent.conversation_memory.state import MemoryPendingRecord
        from app.schema.communication import ConversationHistoryRecord
        from app.schema.communication_workspace import WorkspaceSnapshot
        from sqlite_vec import serialize_float32

        snapshot = WorkspaceSnapshot.model_validate(workspace)
        if type(expected_workspace_revision) is not int or not 0 <= expected_workspace_revision <= snapshot.revision:
            raise ValueError('工作区备份版本无效')
        if not records or len(records) != len(memories):
            raise ValueError('归档任务与加工结果必须完整配对')
        pairs = []
        for raw, memory in zip(records, memories, strict=True):
            record = ConversationHistoryRecord.model_validate(raw)
            memory = MemoryPendingRecord.model_validate(memory)
            original = dict(task_id=record.record_id, status=record.status, payload=record.payload,
                            created_at=record.created_at, activated_at=record.activated_at,
                            processing_duration_ms=record.processing_duration_ms)
            if memory.model_dump(exclude={'retrieval_text', 'embedding'}) != original:
                raise ValueError('加工结果改变了原任务身份、轨迹或计时')
            pairs.append((record, memory.retrieval_text,
                          serialize_float32(memory.embedding) if memory.embedding is not None else None))
        if len({r.record_id for r, _, _ in pairs}) != len(pairs):
            raise ValueError('归档任务重复')
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            for record, text, vector in pairs:
                self._backup_task(connection, conversation_id, record.model_dump())
                saved = connection.execute(
                    'SELECT retrieval_text, embedding, memory_processed_at FROM conversation_records WHERE record_id = ?',
                    (record.record_id,),
                ).fetchone()
                if saved['memory_processed_at'] is not None:
                    if (saved['retrieval_text'], saved['embedding']) != (text, vector):
                        raise CommunicationStoreConflict('已加工任务禁止被不同结果覆盖')
                    continue
                connection.execute(
                    'UPDATE conversation_records SET retrieval_text = ?, embedding = ?, memory_processed_at = ? WHERE record_id = ?',
                    (text, vector, _now(), record.record_id),
                )
            self._backup_workspace(connection, conversation_id, snapshot, expected_workspace_revision)

    @staticmethod
    def _read_workspace(connection: sqlite3.Connection, conversation_id: str) -> dict:
        row = connection.execute(
            'SELECT payload, revision, updated_at FROM conversation_workspaces WHERE conversation_id = ?',
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise LookupError('会话工作区不存在')
        return dict(row) | {'payload': json.loads(row['payload'])}

    def read_workspace(self, conversation_id: str, *, secret_key: SecretStr) -> dict:
        with self._connection() as connection:
            self._owner(connection, conversation_id, secret_key)
            return self._read_workspace(connection, conversation_id)

    def update_workspace(self, conversation_id: str, *, secret_key: SecretStr,
                         payload: dict, expected_revision: int) -> int:
        """写入已校验的工作区；版本比较避免并发任务覆盖较新的结果。"""
        from app.schema.communication_workspace import WorkspacePayload

        content = _json(WorkspacePayload.model_validate(payload).model_dump())
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('工作区版本必须为非负整数')
        with self._connection(write=True) as connection:
            self._owner(connection, conversation_id, secret_key)
            result = connection.execute(
                "UPDATE conversation_workspaces SET payload = ?, revision = revision + 1, updated_at = ? "
                "WHERE conversation_id = ? AND revision = ?",
                (content, _now(), conversation_id, expected_revision),
            )
            if result.rowcount != 1:
                raise CommunicationStoreConflict("工作区版本已过期")
            return expected_revision + 1
