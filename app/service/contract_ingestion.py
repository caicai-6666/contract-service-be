"""将人工复核结果一致地写入 SQLite、PDF 文件与正式合同索引。"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from elasticsearch import AsyncElasticsearch, NotFoundError
from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinition,
    FieldDefinitionCatalog,
    FieldPropertyDefinition,
    FieldValueType,
)
from app.infrastructure.contract_file_store import LocalContractFileStore
from app.infrastructure.contract_metadata_store import (
    ContractMetadata,
    ContractMetadataStatus,
    SQLiteContractMetadataStore,
)

if TYPE_CHECKING:
    from app.service.contract_extraction.model import (
        ClauseDraftData,
        ClauseView,
        ContractClassificationView,
        CoreDraftData,
    )


class ContractReviewValidationError(ValueError):
    """人工复核值不符合启动期字段目录或最终条款契约。"""


class ContractPersistenceError(RuntimeError):
    """SQLite、处理版 PDF 或 ES 未能形成一致的正式合同。"""


class _ContractIndexModel(BaseModel):
    """正式索引投影使用的严格不可变基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _ContractIndexIngestion(_ContractIndexModel):
    reviewer: str = Field(min_length=1)
    ingested_at: datetime


class _ContractIndexVectors(_ContractIndexModel):
    question_fusion: tuple[float, ...]
    page_fusion: tuple[float, ...]


class _ContractIndexCategory(_ContractIndexModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scenario: str = Field(min_length=1)


class _ContractIndexClassification(_ContractIndexModel):
    categories: tuple[_ContractIndexCategory, ...]
    unmapped_type_description: str | None = Field(default=None, min_length=1)


class _ContractIndexClause(_ContractIndexModel):
    clause_id: str = Field(min_length=1)
    order: int = Field(ge=1)
    identifier: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    path: tuple[str, ...] = Field(min_length=1)
    parent_clause_id: str | None = Field(default=None, min_length=1)
    level: int = Field(ge=1)
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    content: str = Field(min_length=1)


class _ContractIndexDocument(_ContractIndexModel):
    """不携带 Agent 审计或草稿状态的最终 ES 主文档。"""

    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name: str = Field(min_length=1, max_length=255)
    file_uri: str = Field(pattern=r"^/[0-9a-f]{64}\.pdf$")
    page_count: int = Field(gt=0)
    ingestion: _ContractIndexIngestion
    classification: _ContractIndexClassification
    core: dict[str, Any]
    clauses: tuple[_ContractIndexClause, ...] = Field(min_length=1)
    vectors: _ContractIndexVectors


@dataclass(frozen=True, slots=True)
class ContractIngestionResult:
    """一次正式入库成功后可以安全返回给调用方的结果。"""

    document_id: str
    file_name: str
    file_uri: str
    page_count: int
    reviewer: str
    ingested_at: datetime


class ContractIngestionService:
    """校验审核值，并协调 SQLite、处理版 PDF 与正式 ES。"""

    def __init__(
        self,
        *,
        elasticsearch: AsyncElasticsearch,
        index_name: str,
        file_store: LocalContractFileStore,
        metadata_store: SQLiteContractMetadataStore,
        field_catalog: FieldDefinitionCatalog,
        vector_dimensions: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_index_name = index_name.strip()
        if not normalized_index_name:
            raise ValueError("合同索引名称不能为空")
        if vector_dimensions <= 0:
            raise ValueError("合同向量维度必须大于 0")
        self._elasticsearch = elasticsearch
        self._index_name = normalized_index_name
        self._file_store = file_store
        self._metadata_store = metadata_store
        self._field_catalog = field_catalog
        self._vector_dimensions = vector_dimensions
        self._clock = clock or (lambda: datetime.now(UTC))
        # 当前部署限定单进程；同内容合同必须串行，避免并发覆盖使
        # SQLite 元数据与最后到达的 ES 文档属于不同入库尝试。
        self._document_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """初始化 SQLite，并恢复上次进程中断留下的非就绪记录。"""
        try:
            await asyncio.to_thread(self._metadata_store.initialize)
            unfinished = await asyncio.to_thread(
                self._metadata_store.list_unfinished
            )
        except Exception as exc:
            raise ContractPersistenceError("合同 SQLite 元数据初始化失败") from exc

        for metadata in unfinished:
            await self._reconcile_unfinished(metadata)

    async def ingest(
        self,
        *,
        document_id: str,
        processed_pdf_bytes: bytes,
        page_count: int,
        file_name: str,
        reviewer: str,
        classification: ContractClassificationView,
        core: CoreDraftData,
        clauses: ClauseDraftData,
        question_fusion_vector: tuple[float, ...],
        page_fusion_vector: tuple[float, ...],
    ) -> ContractIngestionResult:
        """形成最终文档；任一持久化步骤失败时不伪造成功结果。"""
        normalized_file_name = self._validate_file_name(file_name)
        normalized_reviewer = reviewer.strip()
        if not normalized_reviewer:
            raise ContractReviewValidationError("入库审核人不能为空")
        if page_count <= 0:
            raise ContractReviewValidationError("处理版 PDF 页数必须大于 0")

        projected_core = self._project_core(core)
        projected_clauses = self._project_clauses(
            clauses,
            page_count=page_count,
        )
        question_vector = self._validate_vector(
            question_fusion_vector,
            field_name="question_fusion",
        )
        page_vector = self._validate_vector(
            page_fusion_vector,
            field_name="page_fusion",
        )
        classification_document: dict[str, Any] = {
            "categories": [
                category.model_dump()
                for category in classification.categories
            ]
        }
        if (
            not classification.categories
            and classification.unmapped_type_description is not None
        ):
            classification_document["unmapped_type_description"] = (
                classification.unmapped_type_description
            )

        ingested_at = self._clock()
        if ingested_at.tzinfo is None or ingested_at.utcoffset() is None:
            raise RuntimeError("入库时间必须包含时区")
        file_uri = f"/{document_id}.pdf"
        document = _ContractIndexDocument(
            document_id=document_id,
            file_name=normalized_file_name,
            file_uri=file_uri,
            page_count=page_count,
            ingestion=_ContractIndexIngestion(
                reviewer=normalized_reviewer,
                ingested_at=ingested_at,
            ),
            classification=_ContractIndexClassification.model_validate(
                classification_document
            ),
            core=projected_core,
            clauses=tuple(
                _ContractIndexClause.model_validate(clause)
                for clause in projected_clauses
            ),
            vectors=_ContractIndexVectors(
                question_fusion=tuple(question_vector),
                page_fusion=tuple(page_vector),
            ),
        )
        ingestion_id = str(uuid4())
        metadata = ContractMetadata(
            document_id=document_id,
            file_name=normalized_file_name,
            category=self._category_summary(classification),
            contract_time=self._contract_time(projected_core),
            file_uri=file_uri,
            reviewer=normalized_reviewer,
            ingested_at=ingested_at,
            status=ContractMetadataStatus.INGESTING,
            ingestion_id=ingestion_id,
        )

        document_lock = self._document_locks.setdefault(
            document_id,
            asyncio.Lock(),
        )
        async with document_lock:
            return await self._persist(
                metadata=metadata,
                document=document,
                processed_pdf_bytes=processed_pdf_bytes,
            )

    async def _persist(
        self,
        *,
        metadata: ContractMetadata,
        document: _ContractIndexDocument,
        processed_pdf_bytes: bytes,
    ) -> ContractIngestionResult:
        """按统一文档身份串行执行三处持久化与状态发布。"""
        # SQLite 事务只登记本次尝试，不跨越后续文件 I/O 和 ES 网络请求。
        # 普通文件管理只读取 ready，因此不会暴露半完成记录。
        try:
            await asyncio.to_thread(
                self._metadata_store.begin_ingestion,
                metadata,
            )
        except Exception as exc:
            raise ContractPersistenceError("合同 SQLite 元数据写入失败") from exc

        try:
            stored_file_uri = await asyncio.to_thread(
                self._file_store.store_processed_pdf,
                document_id=metadata.document_id,
                pdf_bytes=processed_pdf_bytes,
            )
            if stored_file_uri != metadata.file_uri:
                raise RuntimeError("合同文件存储返回了非预期地址")
        except Exception as exc:
            await self._record_failure(
                metadata,
                reason="处理版 PDF 保存失败",
                cause=exc,
            )

        # ES 是正式内容的最后一个外部写入；固定 document_id 使未知结果
        # 或重试都安全覆盖同一文档，而不是生成重复记录。
        try:
            await self._elasticsearch.index(
                index=self._index_name,
                id=metadata.document_id,
                document=document.model_dump(mode="json", exclude_none=True),
                refresh="wait_for",
            )
        except Exception as exc:
            # 超时或连接中断不代表 ES 一定没有接收写入；立即按实时 GET
            # 核对本次完整元数据，匹配时按成功收敛，避免错误回滚状态。
            if not await self._elasticsearch_matches(metadata):
                await self._record_failure(
                    metadata,
                    reason="合同写入 Elasticsearch 失败",
                    cause=exc,
                )

        try:
            await asyncio.to_thread(
                self._metadata_store.mark_ready,
                document_id=metadata.document_id,
                ingestion_id=metadata.ingestion_id,
                updated_at=self._clock(),
            )
        except Exception as exc:
            # 此时 ES 可能已经成功，不能伪造回滚；保留 ingesting 供启动
            # 对账或同一 run_id 的幂等重试修复。
            raise ContractPersistenceError(
                "Elasticsearch 已写入，但 SQLite 就绪状态提交失败"
            ) from exc

        return ContractIngestionResult(
            document_id=metadata.document_id,
            file_name=metadata.file_name,
            file_uri=metadata.file_uri,
            page_count=document.page_count,
            reviewer=metadata.reviewer,
            ingested_at=metadata.ingested_at,
        )

    async def _record_failure(
        self,
        metadata: ContractMetadata,
        *,
        reason: str,
        cause: Exception,
    ) -> None:
        """尽力持久化失败状态；状态落库失败时仍不返回成功。"""
        try:
            await asyncio.to_thread(
                self._metadata_store.mark_failed,
                document_id=metadata.document_id,
                ingestion_id=metadata.ingestion_id,
                failure_reason=reason,
                updated_at=self._clock(),
            )
        except Exception as metadata_error:
            raise ContractPersistenceError(
                f"{reason}，且 SQLite 失败状态记录失败"
            ) from metadata_error
        raise ContractPersistenceError(reason) from cause

    async def _reconcile_unfinished(self, metadata: ContractMetadata) -> None:
        """按文件身份与 ES 内容核验中断尝试，再原子发布或标记失败。"""
        failure_reason = await asyncio.to_thread(
            self._validate_persisted_file,
            metadata,
        )
        if failure_reason is None:
            try:
                response = await self._elasticsearch.get(
                    index=self._index_name,
                    id=metadata.document_id,
                )
            except NotFoundError:
                failure_reason = "启动恢复时 Elasticsearch 中不存在合同文档"
            except Exception as exc:
                raise ContractPersistenceError(
                    "启动恢复时无法核验 Elasticsearch 合同文档"
                ) from exc
            else:
                source = response.get("_source")
                if not isinstance(source, Mapping) or not self._matches_metadata(
                    source,
                    metadata,
                ):
                    failure_reason = "启动恢复时 Elasticsearch 文档与 SQLite 不一致"

        try:
            if failure_reason is None:
                await asyncio.to_thread(
                    self._metadata_store.mark_ready,
                    document_id=metadata.document_id,
                    ingestion_id=metadata.ingestion_id,
                    updated_at=self._clock(),
                )
            else:
                await asyncio.to_thread(
                    self._metadata_store.mark_failed,
                    document_id=metadata.document_id,
                    ingestion_id=metadata.ingestion_id,
                    failure_reason=failure_reason,
                    updated_at=self._clock(),
                )
        except Exception as exc:
            raise ContractPersistenceError("启动恢复状态写入 SQLite 失败") from exc

    async def _elasticsearch_matches(self, metadata: ContractMetadata) -> bool:
        """在写入结果不确定时，实时核验 ES 是否已经接收本次文档。"""
        try:
            response = await self._elasticsearch.get(
                index=self._index_name,
                id=metadata.document_id,
            )
        except Exception:
            return False
        source = response.get("_source")
        return isinstance(source, Mapping) and self._matches_metadata(
            source,
            metadata,
        )

    def _validate_persisted_file(
        self,
        metadata: ContractMetadata,
    ) -> str | None:
        try:
            path = self._file_store.resolve(metadata.file_uri)
            actual_document_id = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            return "启动恢复时合同 PDF 不存在或不可读"
        if actual_document_id != metadata.document_id:
            return "启动恢复时合同 PDF 内容身份不一致"
        return None

    @classmethod
    def _matches_metadata(
        cls,
        source: Mapping[str, Any],
        metadata: ContractMetadata,
    ) -> bool:
        ingestion = source.get("ingestion")
        classification = source.get("classification")
        core = source.get("core")
        if not all(
            isinstance(value, Mapping)
            for value in (ingestion, classification, core)
        ):
            return False
        assert isinstance(ingestion, Mapping)
        assert isinstance(classification, Mapping)
        assert isinstance(core, Mapping)
        return (
            source.get("document_id") == metadata.document_id
            and source.get("file_name") == metadata.file_name
            and source.get("file_uri") == metadata.file_uri
            and ingestion.get("reviewer") == metadata.reviewer
            and cls._same_datetime(
                ingestion.get("ingested_at"),
                metadata.ingested_at,
            )
            and cls._category_summary_from_mapping(classification)
            == metadata.category
            and core.get("signing_date") == metadata.contract_time
        )

    @staticmethod
    def _same_datetime(value: Any, expected: datetime) -> bool:
        if not isinstance(value, str):
            return False
        try:
            actual = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return actual == expected

    @staticmethod
    def _category_summary(classification: ContractClassificationView) -> str:
        names = [category.name.strip() for category in classification.categories]
        if names:
            return " / ".join(names)
        description = classification.unmapped_type_description
        return description.strip() if description else "未映射"

    @staticmethod
    def _category_summary_from_mapping(classification: Mapping[str, Any]) -> str:
        categories = classification.get("categories")
        if isinstance(categories, list):
            names = [
                item.get("name", "").strip()
                for item in categories
                if isinstance(item, Mapping)
                and isinstance(item.get("name"), str)
                and item.get("name", "").strip()
            ]
            if names:
                return " / ".join(names)
        description = classification.get("unmapped_type_description")
        return description.strip() if isinstance(description, str) else "未映射"

    @staticmethod
    def _contract_time(core: Mapping[str, Any]) -> str | None:
        value = core.get("signing_date")
        if value is None:
            return None
        if not isinstance(value, str):
            raise RuntimeError("Core signing_date 必须投影为字符串")
        return value

    def _project_core(self, core: CoreDraftData) -> dict[str, Any]:
        """按固定目录校验完整 Core，并过滤没有最终值的字段。"""
        values = core.root
        definitions = {
            definition.code: definition
            for definition in self._field_catalog.core.definitions
        }
        submitted_codes = set(values)
        expected_codes = set(definitions)
        unknown_codes = sorted(submitted_codes - expected_codes)
        if unknown_codes:
            raise ContractReviewValidationError(
                f"Core 包含未知字段：{', '.join(unknown_codes)}"
            )
        missing_codes = sorted(expected_codes - submitted_codes)
        if missing_codes:
            raise ContractReviewValidationError(
                f"Core 缺少审核字段：{', '.join(missing_codes)}"
            )

        projected: dict[str, Any] = {}
        for code, definition in definitions.items():
            value = values[code]
            if value is None:
                continue
            normalized = self._validate_core_value(definition, value)
            # 空多值数组和 null 均表示审核后没有最终值，不进入 ES。
            if normalized != []:
                projected[code] = normalized
        return projected

    def _validate_core_value(
        self,
        definition: FieldDefinition,
        value: Any,
    ) -> Any:
        """按字段基数把审核值校验为 ES 使用的稳定形状。"""
        if definition.cardinality is FieldCardinality.MULTIPLE:
            if not isinstance(value, (list, tuple)):
                raise ContractReviewValidationError(
                    f"Core 字段 {definition.code} 必须是对象数组"
                )
            return [
                self._validate_core_object(definition, item, item_index=index)
                for index, item in enumerate(value, start=1)
            ]

        if len(definition.properties) == 1:
            if isinstance(value, (dict, list, tuple)):
                raise ContractReviewValidationError(
                    f"Core 字段 {definition.code} 必须是标量"
                )
            return self._validate_property_value(
                definition.properties[0],
                value,
                path=definition.code,
            )

        if not isinstance(value, Mapping):
            raise ContractReviewValidationError(
                f"Core 字段 {definition.code} 必须是对象"
            )
        return self._validate_core_object(definition, value)

    def _validate_core_object(
        self,
        definition: FieldDefinition,
        value: Any,
        *,
        item_index: int | None = None,
    ) -> dict[str, Any]:
        label = (
            definition.code
            if item_index is None
            else f"{definition.code}[{item_index}]"
        )
        if not isinstance(value, Mapping):
            raise ContractReviewValidationError(f"Core 字段 {label} 必须是对象")
        properties = {
            property_definition.code: property_definition
            for property_definition in definition.properties
        }
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ContractReviewValidationError(
                f"Core 字段 {label} 包含未知属性：{', '.join(unknown)}"
            )
        missing = [
            code
            for code, property_definition in properties.items()
            if property_definition.required and code not in value
        ]
        if missing:
            raise ContractReviewValidationError(
                f"Core 字段 {label} 缺少必填属性：{', '.join(missing)}"
            )
        return {
            code: self._validate_property_value(
                properties[code],
                property_value,
                path=f"{label}.{code}",
            )
            for code, property_value in value.items()
        }

    @staticmethod
    def _validate_property_value(
        definition: FieldPropertyDefinition,
        value: Any,
        *,
        path: str,
    ) -> Any:
        expected = definition.type
        valid = False
        if expected is FieldValueType.STRING:
            valid = isinstance(value, str) and bool(value.strip())
        elif expected is FieldValueType.INTEGER:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif expected is FieldValueType.NUMBER:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        elif expected is FieldValueType.BOOLEAN:
            valid = isinstance(value, bool)
        if not valid:
            raise ContractReviewValidationError(
                f"Core 属性 {path} 必须是有效的 {expected.value}"
            )
        return value

    @staticmethod
    def _project_clauses(
        clauses: ClauseDraftData,
        *,
        page_count: int,
    ) -> list[dict[str, Any]]:
        """校验条款身份、顺序、父子引用和页码，并去除空可选字段。"""
        if not clauses.root:
            raise ContractReviewValidationError("入库合同至少需要一条最终条款")
        clause_ids = [clause.clause_id for clause in clauses.root]
        if any(not clause_id.strip() for clause_id in clause_ids):
            raise ContractReviewValidationError("条款 clause_id 不能为空")
        if len(clause_ids) != len(set(clause_ids)):
            raise ContractReviewValidationError("条款 clause_id 不能重复")
        orders = tuple(clause.order for clause in clauses.root)
        if orders != tuple(range(1, len(clauses.root) + 1)):
            raise ContractReviewValidationError(
                "条款必须按数组顺序使用从 1 连续增长的 order"
            )

        known_ids: set[str] = set()
        projected: list[dict[str, Any]] = []
        for clause in clauses.root:
            ContractIngestionService._validate_clause(
                clause,
                page_count=page_count,
                preceding_clause_ids=known_ids,
            )
            projected.append(clause.model_dump(exclude_none=True))
            known_ids.add(clause.clause_id)
        return projected

    @staticmethod
    def _validate_clause(
        clause: ClauseView,
        *,
        page_count: int,
        preceding_clause_ids: set[str],
    ) -> None:
        if not clause.identifier.strip():
            raise ContractReviewValidationError(
                f"条款 {clause.clause_id} 的 identifier 不能为空"
            )
        if clause.title is not None and not clause.title.strip():
            raise ContractReviewValidationError(
                f"条款 {clause.clause_id} 的 title 不能为空字符串"
            )
        if not clause.path or any(not item.strip() for item in clause.path):
            raise ContractReviewValidationError(
                f"条款 {clause.clause_id} 的 path 必须包含非空层级"
            )
        if not clause.content.strip():
            raise ContractReviewValidationError(
                f"条款 {clause.clause_id} 的 content 不能为空"
            )
        if clause.end_page > page_count:
            raise ContractReviewValidationError(
                f"条款 {clause.clause_id} 的页码超出合同总页数"
            )
        parent = clause.parent_clause_id
        if parent is not None and parent not in preceding_clause_ids:
            raise ContractReviewValidationError(
                f"条款 {clause.clause_id} 的父条款必须先于当前条款出现"
            )

    def _validate_vector(
        self,
        vector: tuple[float, ...],
        *,
        field_name: str,
    ) -> list[float]:
        if len(vector) != self._vector_dimensions:
            raise ContractReviewValidationError(
                f"{field_name} 向量维度必须为 {self._vector_dimensions}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise ContractReviewValidationError(
                f"{field_name} 向量包含非有限数值"
            )
        if not any(value != 0 for value in vector):
            raise ContractReviewValidationError(f"{field_name} 不能是零向量")
        return list(vector)

    @staticmethod
    def _validate_file_name(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ContractReviewValidationError("入库文件名不能为空")
        if len(normalized) > 255:
            raise ContractReviewValidationError("入库文件名不能超过 255 个字符")
        if normalized[0] == "." or normalized[-1] == ".":
            raise ContractReviewValidationError("入库文件名不能以句点开头或结尾")
        invalid_characters = set('/\\:*?"<>|\r\n')
        if any(
            character in invalid_characters or ord(character) < 32
            for character in normalized
        ):
            raise ContractReviewValidationError("入库文件名包含非法字符")
        return normalized


__all__ = [
    "ContractIngestionResult",
    "ContractIngestionService",
    "ContractPersistenceError",
    "ContractReviewValidationError",
]
