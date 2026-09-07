"""使用 SQLite 保存正式合同的文件管理元数据与入库状态。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from app.core.contract_date import normalize_contract_date


class ContractMetadataStatus(StrEnum):
    """跨 SQLite、文件与 Elasticsearch 入库过程的有限状态。"""

    INGESTING = "ingesting"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class ContractCategoryMetadata:
    """从权威类别目录同步到 SQLite 的精简元数据。"""

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class StoredContractCategory:
    """SQLite 中已分配主键的类别元数据。"""

    category_id: int
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class ContractCategoryAssignment:
    """一份合同命中一个类别时的关联信息。"""

    category_code: str
    reasoning_summary: str | None


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
    category_assignments: tuple[ContractCategoryAssignment, ...] = ()

    @property
    def category_codes(self) -> tuple[str, ...]:
        """返回按分类结果顺序排列的稳定类别 code。"""
        return tuple(
            assignment.category_code
            for assignment in self.category_assignments
        )


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
                self._create_table_sql(
                    table_name="contracts",
                    if_not_exists=True,
                )
            )
            self._migrate_legacy_schema(connection)
            self._normalize_existing_contract_times(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contract_category_metadata (
                    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL UNIQUE,
                    CHECK (length(trim(code)) > 0),
                    CHECK (length(trim(name)) > 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contract_category_assignments (
                    document_id TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    position INTEGER NOT NULL CHECK (position > 0),
                    reasoning_summary TEXT,
                    PRIMARY KEY (document_id, category_id),
                    UNIQUE (document_id, position),
                    FOREIGN KEY (document_id)
                        REFERENCES contracts(document_id) ON DELETE CASCADE,
                    FOREIGN KEY (category_id)
                        REFERENCES contract_category_metadata(category_id)
                        ON DELETE RESTRICT
                )
                """
            )
            self._migrate_category_assignment_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS contracts_status_idx
                ON contracts(status)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS contract_category_filter_idx
                ON contract_category_assignments(category_id, document_id)
                """
            )
            connection.commit()

    def synchronize_categories(
        self,
        categories: tuple[ContractCategoryMetadata, ...],
    ) -> None:
        """以启动期权威目录同步类别元数据并回填旧合同关联。"""
        normalized = tuple(
            ContractCategoryMetadata(
                code=category.code.strip(),
                name=category.name.strip(),
            )
            for category in categories
        )
        if not normalized:
            raise ValueError("合同类别元数据不能为空")
        codes = [category.code for category in normalized]
        names = [category.name for category in normalized]
        if any(not value for value in (*codes, *names)):
            raise ValueError("合同类别 code 和 name 不能为空")
        if len(codes) != len(set(codes)):
            raise ValueError("合同类别 code 不能重复")
        if len(names) != len(set(names)):
            raise ValueError("合同类别 name 不能重复")

        with self._transaction() as connection:
            connection.executemany(
                """
                INSERT INTO contract_category_metadata (code, name)
                VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET name = excluded.name
                """,
                ((category.code, category.name) for category in normalized),
            )
            self._backfill_category_assignments(connection)
            self._normalize_category_summaries(connection)

    def normalize_category_summaries(self) -> int:
        """按现有类别关联幂等迁移摘要，返回实际更新的合同数。"""
        with self._transaction() as connection:
            self._backfill_category_assignments(connection)
            return self._normalize_category_summaries(connection)

    @staticmethod
    def _normalize_category_summaries(connection: sqlite3.Connection) -> int:
        # 关联表是类别身份与顺序的权威来源；无关联的未映射说明保持原样，不猜测 code。
        rows = connection.execute(
            """
            SELECT assignment.document_id, category.code
            FROM contract_category_assignments AS assignment
            JOIN contract_category_metadata AS category USING (category_id)
            ORDER BY assignment.document_id, assignment.position
            """
        ).fetchall()
        codes_by_document: dict[str, list[str]] = {}
        for row in rows:
            codes_by_document.setdefault(row["document_id"], []).append(row["code"])
        updated = 0
        for document_id, codes in codes_by_document.items():
            summary = " / ".join(codes)
            updated += connection.execute(
                "UPDATE contracts SET category = ? WHERE document_id = ? AND category <> ?",
                (summary, document_id, summary),
            ).rowcount
        return updated

    def list_categories(self) -> tuple[StoredContractCategory, ...]:
        """按主键顺序读取全部类别，不以是否已有合同关联作为过滤条件。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT category_id, code, name
                FROM contract_category_metadata
                ORDER BY category_id ASC
                """
            ).fetchall()
        return tuple(StoredContractCategory(**dict(row)) for row in rows)

    def begin_ingestion(self, metadata: ContractMetadata) -> None:
        """原子写入本次待入库元数据，并替换同文档的旧尝试。"""
        if metadata.status is not ContractMetadataStatus.INGESTING:
            raise ValueError("开始入库时 SQLite 状态必须为 ingesting")
        assignments = tuple(
            ContractCategoryAssignment(
                category_code=assignment.category_code.strip(),
                reasoning_summary=(
                    assignment.reasoning_summary.strip()
                    if assignment.reasoning_summary is not None
                    else None
                ),
            )
            for assignment in metadata.category_assignments
        )
        category_codes = tuple(
            assignment.category_code for assignment in assignments
        )
        if any(not code for code in category_codes):
            raise ValueError("合同类别 code 不能为空")
        if len(category_codes) != len(set(category_codes)):
            raise ValueError("同一合同不能重复关联类别")
        if any(
            not assignment.reasoning_summary
            for assignment in assignments
        ):
            raise ValueError("新入库的合同类别关联必须包含推理摘要")
        contract_time = (
            normalize_contract_date(metadata.contract_time)
            if metadata.contract_time is not None
            else None
        )
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
                    ingestion_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    category = excluded.category,
                    contract_time = excluded.contract_time,
                    file_uri = excluded.file_uri,
                    reviewer = excluded.reviewer,
                    ingested_at = excluded.ingested_at,
                    status = excluded.status,
                    ingestion_id = excluded.ingestion_id
                """,
                (
                    metadata.document_id,
                    metadata.file_name,
                    " / ".join(category_codes) if category_codes else metadata.category,
                    contract_time,
                    metadata.file_uri,
                    metadata.reviewer,
                    ingested_at,
                    metadata.status.value,
                    metadata.ingestion_id,
                ),
            )
            connection.execute(
                """
                DELETE FROM contract_category_assignments
                WHERE document_id = ?
                """,
                (metadata.document_id,),
            )
            for position, assignment in enumerate(
                assignments,
                start=1,
            ):
                cursor = connection.execute(
                    """
                    INSERT INTO contract_category_assignments (
                        document_id,
                        category_id,
                        position,
                        reasoning_summary
                    )
                    SELECT ?, category_id, ?, ?
                    FROM contract_category_metadata
                    WHERE code = ?
                    """,
                    (
                        metadata.document_id,
                        position,
                        assignment.reasoning_summary,
                        assignment.category_code,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"未知合同类别：{assignment.category_code}"
                    )

    def mark_ready(
        self,
        *,
        document_id: str,
        ingestion_id: str,
    ) -> None:
        """只把当前入库尝试原子发布为可供文件管理使用。"""
        self._update_status(
            document_id=document_id,
            ingestion_id=ingestion_id,
            status=ContractMetadataStatus.READY,
        )

    def delete_ingestion(
        self,
        *,
        document_id: str,
        ingestion_id: str,
    ) -> None:
        """只删除当前失败的入库尝试，不影响后续新尝试。"""
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM contracts
                WHERE document_id = ? AND ingestion_id = ?
                """,
                (document_id, ingestion_id),
            )
            if cursor.rowcount != 1:
                raise ContractMetadataStateError(
                    "合同入库记录已被其他尝试更新"
                )

    def list_unfinished(self) -> tuple[ContractMetadata, ...]:
        """返回启动时需要与文件和 ES 对账的非就绪记录。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM contracts
                WHERE status != 'ready'
                ORDER BY document_id
                """
            ).fetchall()
            return tuple(
                self._row_to_metadata(connection, row)
                for row in rows
            )

    def list_ready(
        self,
        *,
        category_code: str | None = None,
    ) -> tuple[ContractMetadata, ...]:
        """返回正式合同目录，可按权威类别 code 筛选。"""
        with closing(self._connect()) as connection:
            if category_code is None:
                rows = connection.execute(
                    """
                    SELECT * FROM contracts
                    WHERE status = 'ready'
                    ORDER BY ingested_at DESC, document_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT contracts.*
                    FROM contracts
                    JOIN contract_category_assignments AS assignment
                        ON assignment.document_id = contracts.document_id
                    JOIN contract_category_metadata AS category
                        ON category.category_id = assignment.category_id
                    WHERE contracts.status = 'ready' AND category.code = ?
                    ORDER BY contracts.ingested_at DESC, contracts.document_id
                    """,
                    (category_code.strip(),),
                ).fetchall()
            return tuple(
                self._row_to_metadata(connection, row)
                for row in rows
            )

    def get(self, document_id: str) -> ContractMetadata | None:
        """按统一文档身份读取一条元数据，主要供服务与测试核验。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM contracts WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._row_to_metadata(connection, row)
            )

    def _update_status(
        self,
        *,
        document_id: str,
        ingestion_id: str,
        status: ContractMetadataStatus,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE contracts
                SET status = ?
                WHERE document_id = ? AND ingestion_id = ?
                """,
                (
                    status.value,
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
        connection.execute("PRAGMA foreign_keys = ON")
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
    def _row_to_metadata(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ContractMetadata:
        category_assignments = tuple(
            ContractCategoryAssignment(
                category_code=category_row["code"],
                reasoning_summary=category_row["reasoning_summary"],
            )
            for category_row in connection.execute(
                """
                SELECT category.code, assignment.reasoning_summary
                FROM contract_category_assignments AS assignment
                JOIN contract_category_metadata AS category
                    ON category.category_id = assignment.category_id
                WHERE assignment.document_id = ?
                ORDER BY assignment.position
                """,
                (row["document_id"],),
            ).fetchall()
        )
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
            category_assignments=category_assignments,
        )

    @staticmethod
    def _backfill_category_assignments(
        connection: sqlite3.Connection,
    ) -> None:
        """兼容旧名称摘要和新版 code 摘要，恢复可完整识别的类别关联。"""
        category_ids = {
            key: row["category_id"]
            for row in connection.execute(
                "SELECT category_id, code, name FROM contract_category_metadata"
            ).fetchall()
            for key in (row["name"], row["code"])
        }
        rows = connection.execute(
            """
            SELECT contracts.document_id, contracts.category
            FROM contracts
            WHERE NOT EXISTS (
                SELECT 1
                FROM contract_category_assignments AS assignment
                WHERE assignment.document_id = contracts.document_id
            )
            """
        ).fetchall()
        for row in rows:
            names = tuple(
                name.strip()
                for name in row["category"].split(" / ")
                if name.strip()
            )
            if not names or any(name not in category_ids for name in names):
                continue
            if len({category_ids[name] for name in names}) != len(names):
                continue
            connection.executemany(
                """
                INSERT INTO contract_category_assignments (
                    document_id,
                    category_id,
                    position,
                    reasoning_summary
                ) VALUES (?, ?, ?, NULL)
                """,
                (
                    (row["document_id"], category_ids[name], position)
                    for position, name in enumerate(names, start=1)
                ),
            )

    @staticmethod
    def _migrate_category_assignment_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """为已创建的关联表补充可选推理摘要字段。"""
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(contract_category_assignments)"
            ).fetchall()
        }
        if "reasoning_summary" not in columns:
            connection.execute(
                """
                ALTER TABLE contract_category_assignments
                ADD COLUMN reasoning_summary TEXT
                """
            )

    @staticmethod
    def _normalize_existing_contract_times(
        connection: sqlite3.Connection,
    ) -> None:
        """将历史合同的完整签约日期幂等转换为 ISO 格式。"""
        rows = connection.execute(
            """
            SELECT document_id, contract_time
            FROM contracts
            WHERE contract_time IS NOT NULL
            """
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            try:
                normalized = normalize_contract_date(row["contract_time"])
            except ValueError as exc:
                raise ValueError(
                    "历史合同签约日期无法规范化："
                    f"document_id={row['document_id']}"
                ) from exc
            if normalized != row["contract_time"]:
                updates.append((normalized, row["document_id"]))
        if updates:
            connection.executemany(
                """
                UPDATE contracts
                SET contract_time = ?
                WHERE document_id = ?
                """,
                updates,
            )

    @staticmethod
    def _create_table_sql(
        *,
        table_name: str,
        if_not_exists: bool,
    ) -> str:
        clause = "IF NOT EXISTS " if if_not_exists else ""
        return f"""
            CREATE TABLE {clause}{table_name} (
                document_id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                category TEXT NOT NULL,
                contract_time TEXT,
                file_uri TEXT NOT NULL UNIQUE,
                reviewer TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('ingesting', 'ready')
                ),
                ingestion_id TEXT NOT NULL,
                CHECK (length(document_id) = 64),
                CHECK (length(trim(file_name)) BETWEEN 1 AND 255),
                CHECK (length(trim(category)) > 0),
                CHECK (length(trim(reviewer)) > 0)
            )
        """

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        """原子精简旧表，并清理历史失败记录。"""
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(contracts)").fetchall()
        }
        legacy_columns = {"failure_reason", "updated_at"}
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'contracts'"
        ).fetchone()
        table_sql = "" if table_row is None else table_row["sql"]
        if columns.isdisjoint(legacy_columns) and "'failed'" not in table_sql:
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                self._create_table_sql(
                    table_name="contracts_migrated",
                    if_not_exists=False,
                )
            )
            connection.execute(
                """
                INSERT INTO contracts_migrated (
                    document_id,
                    file_name,
                    category,
                    contract_time,
                    file_uri,
                    reviewer,
                    ingested_at,
                    status,
                    ingestion_id
                )
                SELECT
                    document_id,
                    file_name,
                    category,
                    contract_time,
                    file_uri,
                    reviewer,
                    ingested_at,
                    status,
                    ingestion_id
                FROM contracts
                WHERE status != 'failed'
                """
            )
            connection.execute("DROP TABLE contracts")
            connection.execute(
                "ALTER TABLE contracts_migrated RENAME TO contracts"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


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
    "ContractCategoryAssignment",
    "ContractCategoryMetadata",
    "ContractMetadata",
    "ContractMetadataStateError",
    "ContractMetadataStatus",
    "SQLiteContractMetadataStore",
]
