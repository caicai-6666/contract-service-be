"""合同正式索引 mapping 的构造与启动期增量同步。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from elasticsearch import AsyncElasticsearch, ConflictError

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinition,
    FieldDefinitionCatalog,
    FieldPropertyDefinition,
    FieldValueType,
)
from app.core.config import Settings

ElasticsearchMapping = dict[str, Any]


class ContractIndexSchemaError(RuntimeError):
    """已有索引无法与当前合同 mapping 安全兼容。"""


@dataclass(frozen=True, slots=True)
class ContractIndexSyncResult:
    """应用本次启动对正式合同索引执行的同步结果。"""

    index_name: str
    created: bool
    added_core_fields: tuple[str, ...]


def _text_mapping(analyzer: str) -> ElasticsearchMapping:
    """构造索引与查询使用同一中文分析器的全文字段。"""
    return {
        "type": "text",
        "analyzer": analyzer,
        "search_analyzer": analyzer,
    }


def _core_property_mapping(
    definition: FieldPropertyDefinition,
    *,
    analyzer: str,
) -> ElasticsearchMapping:
    """把一个 Core 基本类型属性转换为 ES 标量 mapping。"""
    if definition.type is FieldValueType.STRING:
        if definition.tokenize:
            return _text_mapping(analyzer)
        return {"type": "keyword"}
    if definition.type is FieldValueType.INTEGER:
        return {"type": "integer"}
    if definition.type is FieldValueType.NUMBER:
        return {"type": "double"}
    if definition.type is FieldValueType.BOOLEAN:
        return {"type": "boolean"}
    raise AssertionError(f"未处理的 Core 属性类型：{definition.type}")


def _core_definition_mapping(
    definition: FieldDefinition,
    *,
    analyzer: str,
) -> ElasticsearchMapping:
    """按基数把 Core 定义投影为标量、严格对象或 nested。"""
    if definition.cardinality is FieldCardinality.SINGLE and len(
        definition.properties
    ) == 1:
        return _core_property_mapping(definition.properties[0], analyzer=analyzer)

    properties = {
        property_definition.code: _core_property_mapping(
            property_definition,
            analyzer=analyzer,
        )
        for property_definition in definition.properties
    }
    return {
        "type": (
            "nested"
            if definition.cardinality is FieldCardinality.MULTIPLE
            else "object"
        ),
        "dynamic": "strict",
        "properties": properties,
    }


def build_core_mapping_properties(
    catalog: FieldDefinitionCatalog,
    *,
    analyzer: str,
) -> dict[str, ElasticsearchMapping]:
    """从启动期 Core 快照生成稳定英文路径的 mapping 属性。"""
    return {
        definition.code: _core_definition_mapping(definition, analyzer=analyzer)
        for definition in catalog.core.definitions
    }


def build_contract_index_mapping(
    settings: Settings,
    catalog: FieldDefinitionCatalog,
) -> ElasticsearchMapping:
    """构造复核后合同正式索引的完整严格 mapping。"""
    analyzer = settings.elasticsearch_text_analyzer
    return {
        "dynamic": "strict",
        "properties": {
            "document_id": {"type": "keyword"},
            "file_name": {"type": "keyword", "index": False},
            "file_uri": {"type": "keyword", "index": False},
            "page_count": {"type": "integer"},
            "ingestion": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "reviewer": {"type": "keyword"},
                    "ingested_at": {"type": "date"},
                },
            },
            "classification": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "categories": {
                        "type": "nested",
                        "dynamic": "strict",
                        "properties": {
                            "code": {"type": "keyword"},
                            "name": {"type": "keyword"},
                            "scenario": _text_mapping(analyzer),
                        },
                    },
                    "unmapped_type_description": _text_mapping(analyzer),
                },
            },
            "core": {
                "type": "object",
                "dynamic": "strict",
                "properties": build_core_mapping_properties(
                    catalog,
                    analyzer=analyzer,
                ),
            },
            "clauses": {
                "type": "nested",
                "dynamic": "strict",
                "properties": {
                    "clause_id": {"type": "keyword"},
                    "order": {"type": "integer"},
                    "identifier": {"type": "keyword"},
                    "title": _text_mapping(analyzer),
                    "path": {"type": "keyword"},
                    "parent_clause_id": {"type": "keyword"},
                    "level": {"type": "integer"},
                    "start_page": {"type": "integer"},
                    "end_page": {"type": "integer"},
                    "content": _text_mapping(analyzer),
                },
            },
            "vectors": {
                "type": "object",
                "dynamic": "strict",
                "properties": {
                    "question_fusion": {
                        "type": "dense_vector",
                        "dims": settings.elasticsearch_vector_dimensions,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "page_fusion": {
                        "type": "dense_vector",
                        "dims": settings.elasticsearch_vector_dimensions,
                        "index": True,
                        "similarity": "cosine",
                    },
                },
            },
        },
    }


def _mapping_type(mapping: Mapping[str, Any]) -> Any:
    """ES 对 object 可能省略 type，比较时统一补全。"""
    if "type" in mapping:
        return mapping["type"]
    return "object" if "properties" in mapping else None


def _build_mapping_addition(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    path: str,
) -> tuple[ElasticsearchMapping | None, tuple[str, ...]]:
    """递归校验已有字段，并返回只包含缺失 Core 字段的安全补丁。"""
    expected_type = _mapping_type(expected)
    actual_type = _mapping_type(actual)
    if expected_type != actual_type:
        raise ContractIndexSchemaError(
            f"Elasticsearch 字段 {path} 类型不兼容："
            f"当前为 {actual_type!r}，配置要求 {expected_type!r}"
        )

    # 标量字段的类型与分析器无法原地修改；发现漂移时必须阻止启动。
    for key in ("analyzer", "search_analyzer"):
        expected_value = expected.get(key)
        actual_value = actual.get(key)
        if key == "search_analyzer" and actual_value is None:
            # ES 会省略与 analyzer 相同的 search_analyzer，读取时需按等价值比较。
            actual_value = actual.get("analyzer")
        if expected_value != actual_value:
            raise ContractIndexSchemaError(
                f"Elasticsearch 字段 {path} 的 {key} 不兼容："
                f"当前为 {actual_value!r}，配置要求 {expected_value!r}"
            )

    expected_properties = expected.get("properties")
    if not isinstance(expected_properties, Mapping):
        return None, ()
    actual_properties = actual.get("properties", {})
    if not isinstance(actual_properties, Mapping):
        raise ContractIndexSchemaError(
            f"Elasticsearch 字段 {path} 缺少对象 properties"
        )

    additions: ElasticsearchMapping = {}
    added_paths: list[str] = []
    for name, expected_child in expected_properties.items():
        child_path = f"{path}.{name}"
        actual_child = actual_properties.get(name)
        if actual_child is None:
            additions[name] = dict(expected_child)
            added_paths.append(child_path)
            continue
        if not isinstance(actual_child, Mapping):
            raise ContractIndexSchemaError(
                f"Elasticsearch 字段 {child_path} mapping 不是对象"
            )
        child_patch, child_paths = _build_mapping_addition(
            expected_child,
            actual_child,
            path=child_path,
        )
        if child_patch is not None:
            additions[name] = child_patch
            added_paths.extend(child_paths)

    expected_dynamic = expected.get("dynamic")
    actual_dynamic = actual.get("dynamic")
    dynamic_changed = (
        expected_dynamic is not None and expected_dynamic != actual_dynamic
    )
    if not additions and not dynamic_changed:
        return None, ()
    patch: ElasticsearchMapping = {
        "type": expected_type,
        "dynamic": expected_dynamic,
        "properties": additions,
    }
    return patch, tuple(added_paths)


def _response_body(response: Any) -> Any:
    """兼容 elastic-transport 响应对象和测试替身。"""
    return getattr(response, "body", response)


def _index_mappings(response: Any, index_name: str) -> Mapping[str, Any]:
    """从 get_mapping 响应中取得精确索引的 mapping。"""
    body = _response_body(response)
    if not isinstance(body, Mapping):
        raise ContractIndexSchemaError("Elasticsearch get_mapping 响应格式无效")
    index_payload = body.get(index_name)
    if index_payload is None and len(body) == 1:
        index_payload = next(iter(body.values()))
    if not isinstance(index_payload, Mapping):
        raise ContractIndexSchemaError(
            f"Elasticsearch 未返回索引 {index_name!r} 的 mapping"
        )
    mappings = index_payload.get("mappings", {})
    if not isinstance(mappings, Mapping):
        raise ContractIndexSchemaError(
            f"Elasticsearch 索引 {index_name!r} 的 mappings 格式无效"
        )
    return mappings


async def synchronize_contract_index(
    client: AsyncElasticsearch,
    settings: Settings,
    catalog: FieldDefinitionCatalog,
) -> ContractIndexSyncResult:
    """确保正式索引存在，并增量补齐当前配置新增的 Core mapping。"""
    index_name = settings.elasticsearch_index_name
    exists_response = await client.indices.exists(index=index_name)
    exists = bool(_response_body(exists_response))

    if not exists:
        try:
            response = await client.indices.create(
                index=index_name,
                # 启动同步只确认索引元数据；分片分配继续由 ES 磁盘与集群策略控制。
                wait_for_active_shards=0,
                settings={
                    "number_of_shards": settings.elasticsearch_number_of_shards,
                    "number_of_replicas": settings.elasticsearch_number_of_replicas,
                },
                mappings=build_contract_index_mapping(settings, catalog),
            )
        except ConflictError:
            # 多进程同时启动时允许另一个进程先完成创建，再进入兼容性检查。
            pass
        else:
            body = _response_body(response)
            if isinstance(body, Mapping) and not body.get("acknowledged", False):
                raise ContractIndexSchemaError(
                    f"Elasticsearch 未确认索引 {index_name!r} 的创建"
                )
            return ContractIndexSyncResult(
                index_name=index_name,
                created=True,
                added_core_fields=tuple(
                    definition.code for definition in catalog.core.definitions
                ),
            )

    mapping_response = await client.indices.get_mapping(index=index_name)
    current_mapping = _index_mappings(mapping_response, index_name)
    current_properties = current_mapping.get("properties", {})
    if not isinstance(current_properties, Mapping):
        raise ContractIndexSchemaError(
            f"Elasticsearch 索引 {index_name!r} 的根 properties 格式无效"
        )

    expected_core = build_contract_index_mapping(settings, catalog)["properties"][
        "core"
    ]
    current_core = current_properties.get("core")
    if current_core is None:
        core_patch = dict(expected_core)
        added_paths = tuple(
            f"core.{definition.code}" for definition in catalog.core.definitions
        )
    else:
        if not isinstance(current_core, Mapping):
            raise ContractIndexSchemaError("Elasticsearch 字段 core mapping 不是对象")
        core_patch, added_paths = _build_mapping_addition(
            expected_core,
            current_core,
            path="core",
        )

    if core_patch is not None:
        response = await client.indices.put_mapping(
            index=index_name,
            properties={"core": core_patch},
        )
        body = _response_body(response)
        if isinstance(body, Mapping) and not body.get("acknowledged", False):
            raise ContractIndexSchemaError(
                f"Elasticsearch 未确认索引 {index_name!r} 的 Core mapping 更新"
            )

    return ContractIndexSyncResult(
        index_name=index_name,
        created=False,
        added_core_fields=added_paths,
    )


__all__ = [
    "ContractIndexSchemaError",
    "ContractIndexSyncResult",
    "build_contract_index_mapping",
    "build_core_mapping_properties",
    "synchronize_contract_index",
]
