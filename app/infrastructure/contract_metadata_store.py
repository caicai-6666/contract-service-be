"""使用 SQLite 保存正式合同的文件管理元数据与入库状态。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class ContractMetadataStatus(StrEnum):
    """跨 SQLite、文件与 Elasticsearch 入库过程的有限状态。"""

    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ContractMetadata:
    """一份合同在 SQLite 中保存的轻量文件管理信息。"""

    document_id: str
    file_name: str
    category: str
    contract_time: str | None
    file_uri: str
    reviewer: str
    ingested_at: datetime
    status: ContractMetadataStatus
    ingestion_id: str
    failure_reason: str | None = None


class ContractMetadataStateError(RuntimeError):
    """入库尝试已被更新的尝试替代，不能再改变其状态。"""


class SQLiteContractMetadataStore:
    """以短事务维护合同目录，不在事务内调用文件系统或 ES。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.resolve()

    @property
    def database_path(self) -> Path:
        """返回 SQLite 数据库的绝对路径。"""
        return self._database_path

    def initialize(self) -> None:
        """创建数据库目录和固定表结构，并启用 WAL。"""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        if self._database_path.exists() and not self._database_path.is_file():
            raise ValueError(f"合同元数据数据库不是普通文件：{self._database_path}")

        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contracts (
                    document_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    contract_time TEXT,
                    file_uri TEXT NOT NULL UNIQUE,
                    reviewer TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('ingesting', 'ready', 'failed')
                    ),
                    ingestion_id TEXT NOT NULL,
                    failure_reason TEXT,
                    updated_at TEXT NOT NULL,
                    CHECK (length(document_id) = 64),
                    CHECK (length(trim(file_name)) BETWEEN 1 AND 255),
                    CHECK (length(trim(category)) > 0),
                    CHECK (length(trim(reviewer)) > 0)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS contracts_status_idx
                ON contracts(status)
                """
            )
            connection.commit()

    def begin_ingestion(self, metadata: ContractMetadata) -> None:
        """原子写入本次待入库元数据，并替换同文档的旧尝试。"""
        if metadata.status is not ContractMetadataStatus.INGESTING:
            raise ValueError("开始入库时 SQLite 状态必须为 ingesting")
        ingested_at = self._serialize_datetime(metadata.ingested_at)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO contracts (
                    document_id,
                    file_name,
                    category,
                    contract_time,
                    file_uri,
                    reviewer,
                    ingested_at,
                    status,
                    ingestion_id,
                    failure_reason,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    category = excluded.category,
                    contract_time = excluded.contract_time,
                    file_uri = excluded.file_uri,
                    reviewer = excluded.reviewer,
                    ingested_at = excluded.ingested_at,
                    status = excluded.status,
                    ingestion_id = excluded.ingestion_id,
                    failure_reason = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    metadata.document_id,
                    metadata.file_name,
                    metadata.category,
                    metadata.contract_time,
                    metadata.file_uri,
                    metadata.reviewer,
                    ingested_at,
                    metadata.status.value,
                    metadata.ingestion_id,
                    ingested_at,
                ),
            )

    def mark_ready(
        self,
        *,
        document_id: str,
        ingestion_id: str,
        updated_at: datetime,
    ) -> None:
        """只把当前入库尝试原子发布为可供文件管理使用。"""
        self._update_status(
            document_id=document_id,
            ingestion_id=ingestion_id,
            status=ContractMetadataStatus.READY,
            failure_reason=None,
            updated_at=updated_at,
        )

    def mark_failed(
        self,
        *,
        document_id: str,
        ingestion_id: str,
        failure_reason: str,
        updated_at: datetime,
    ) -> None:
        """记录当前尝试失败，保留可诊断、可对账的元数据。"""
        normalized_reason = failure_reason.strip()
        if not normalized_reason:
            raise ValueError("入库失败原因不能为空")
        self._update_status(
            document_id=document_id,
            ingestion_id=ingestion_id,
            status=ContractMetadataStatus.FAILED,
            failure_reason=normalized_reason,
            updated_at=updated_at,
        )

    def list_unfinished(self) -> tuple[ContractMetadata, ...]:
        """返回启动时需要与文件和 ES 对账的非就绪记录。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM contracts
                WHERE status != 'ready'
                ORDER BY updated_at, document_id
                """
            ).fetchall()
        return tuple(self._row_to_metadata(row) for row in rows)

    def list_ready(self) -> tuple[ContractMetadata, ...]:
        """返回可供日常选择和文件管理使用的正式合同目录。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM contracts
                WHERE status = 'ready'
                ORDER BY ingested_at DESC, document_id
                """
            ).fetchall()
        return tuple(self._row_to_metadata(row) for row in rows)

    def get(self, document_id: str) -> ContractMetadata | None:
        """按统一文档身份读取一条元数据，主要供服务与测试核验。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM contracts WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return None if row is None else self._row_to_metadata(row)

    def _update_status(
        self,
        *,
        document_id: str,
        ingestion_id: str,
        status: ContractMetadataStatus,
        failure_reason: str | None,
        updated_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE contracts
                SET status = ?, failure_reason = ?, updated_at = ?
                WHERE document_id = ? AND ingestion_id = ?
                """,
                (
                    status.value,
                    failure_reason,
                    self._serialize_datetime(updated_at),
                    document_id,
                    ingestion_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ContractMetadataStateError(
                    "合同入库状态已被其他尝试更新"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _transaction(self) -> _SQLiteTransaction:
        return _SQLiteTransaction(self._connect())

    @staticmethod
    def _serialize_datetime(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SQLite 入库时间必须包含时区")
        return value.isoformat(timespec="microseconds")

    @staticmethod
    def _row_to_metadata(row: sqlite3.Row) -> ContractMetadata:
        return ContractMetadata(
            document_id=row["document_id"],
            file_name=row["file_name"],
            category=row["category"],
            contract_time=row["contract_time"],
            file_uri=row["file_uri"],
            reviewer=row["reviewer"],
            ingested_at=datetime.fromisoformat(row["ingested_at"]),
            status=ContractMetadataStatus(row["status"]),
            ingestion_id=row["ingestion_id"],
            failure_reason=row["failure_reason"],
        )


class _SQLiteTransaction:
    """显式管理 BEGIN IMMEDIATE，确保异常路径总是回滚。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                try:
                    self._connection.commit()
                except Exception:
                    self._connection.rollback()
                    raise
            else:
                self._connection.rollback()
        finally:
            self._connection.close()


__all__ = [
    "ContractMetadata",
    "ContractMetadataStateError",
    "ContractMetadataStatus",
    "SQLiteContractMetadataStore",
]
