"""把 Agent 私有审计结果投影为面向审核端的精简草稿。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)
from app.agent.contract_extraction.subgraph.clause_extraction.state import (
    ClauseExtractionResult,
)
from app.agent.contract_extraction.subgraph.field_extraction.core.state import (
    CoreExtractionResult,
)
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
)
from app.service.contract_extraction.executor import RetrievalViewOutput
from app.service.contract_extraction.model import (
    ClauseDraftData,
    ContractCategoryView,
    ContractClassificationView,
    CoreDraftData,
    ResultStatus,
    RetrievalViewDraftData,
)


class UnusableBranchResultError(RuntimeError):
    """分支没有形成任何可提交到草稿的权威结果。"""


@dataclass(frozen=True, slots=True)
class ProjectedSection:
    """一次分支结果的用户投影及其完整程度。"""

    result_status: ResultStatus
    data: CoreDraftData | ClauseDraftData | RetrievalViewDraftData


def project_classification(
    result: ContractClassificationResult,
) -> ContractClassificationView:
    """仅保留分类身份和实际场景，不泄漏分类工具轨迹。"""
    categories = tuple(
        ContractCategoryView(
            code=item.decision.category_code,
            name=item.decision.category_name,
            scenario=item.decision.scenario,
        )
        for item in result.matches
    )
    return ContractClassificationView(
        status=result.status,
        categories=categories,
        unmapped_type_description=result.unmapped_type_description,
    )


def _project_core_objects(field_result: BaseModel) -> list[dict[str, Any]]:
    """把中文属性名值转换为 Elasticsearch 使用的稳定属性 code。"""
    source_objects = (
        field_result.objects
        if field_result.status == "extracted"
        else field_result.partial_objects
        if field_result.status == "failed"
        else ()
    )
    objects: list[dict[str, Any]] = []
    for extracted in source_objects:
        objects.append(
            {
                field_result.property_codes[property_name]: extracted.value[
                    property_name
                ]
                for property_name in field_result.property_names
                if property_name in extracted.value
            }
        )
    return objects


def _collapse_core_value(
    field_result: BaseModel,
    objects: list[dict[str, Any]],
) -> Any | None:
    """按 Core 基数生成 ES 标量、严格对象或 nested 数组形状。"""
    if not objects:
        return None
    if field_result.cardinality is FieldCardinality.MULTIPLE:
        return tuple(objects)
    first = objects[0]
    if len(field_result.property_names) == 1:
        property_name = field_result.property_names[0]
        return first.get(field_result.property_codes[property_name])
    return first


def project_core(result: BaseModel) -> ProjectedSection:
    """按 ES Core 形状投影；未提取定义以 null 保留给审核端。"""
    if not isinstance(result, CoreExtractionResult):
        raise TypeError("Core 分支返回了未知结果类型")
    if result.status == "failed":
        raise UnusableBranchResultError("Core 字段没有形成可用结果")

    fields: dict[str, Any | None] = {}
    for field_result in result.fields:
        if field_result.code in fields:
            raise ValueError(f"Core 结果包含重复 code：{field_result.code}")
        fields[field_result.code] = _collapse_core_value(
            field_result,
            _project_core_objects(field_result),
        )

    return ProjectedSection(
        result_status=(
            ResultStatus.COMPLETED
            if result.status == "completed"
            else ResultStatus.PARTIAL
        ),
        data=CoreDraftData(root=fields),
    )


def project_clause(result: ClauseExtractionResult) -> ProjectedSection:
    """按 ES clauses 形状投影成功条款，去除候选、证据与审计。"""
    if result.status == "failed":
        raise UnusableBranchResultError("条款分支没有形成可用结果")

    clauses: list[dict[str, Any]] = []
    for clause in result.clauses:
        if clause.status != "extracted":
            continue
        candidate = clause.candidate
        item: dict[str, Any] = {
            "clause_id": candidate.candidate_id,
            "order": candidate.order,
            "identifier": candidate.identifier,
            "path": [
                " ".join(
                    filter(None, (segment.identifier, segment.title_hint))
                )
                for segment in candidate.document_path
            ],
            "level": candidate.level,
            "start_page": candidate.evidence.start.page_number,
            "end_page": candidate.evidence.end.page_number,
            "content": clause.content,
        }
        if candidate.title_hint:
            item["title"] = candidate.title_hint
        if candidate.parent_candidate_id:
            item["parent_clause_id"] = candidate.parent_candidate_id
        clauses.append(item)

    return ProjectedSection(
        result_status=(
            ResultStatus.COMPLETED
            if result.status == "completed"
            else ResultStatus.PARTIAL
        ),
        data=ClauseDraftData(root=tuple(clauses)),
    )


def project_retrieval_view(result: RetrievalViewOutput) -> ProjectedSection:
    """保留问题文本与向量就绪信息，不通过审核接口传输高维向量。"""
    if result.questions.status == "failed" or result.vector.vector is None:
        raise UnusableBranchResultError("检索准备没有形成可用合同向量")

    questions = [
        {
            "question_id": item.question_id,
            "order": item.order,
            "question": item.question,
        }
        for item in result.questions.questions
    ]
    completed = (
        result.questions.status == "completed"
        and result.embeddings.status == "completed"
        and result.vector.status == "completed"
    )
    return ProjectedSection(
        result_status=(
            ResultStatus.COMPLETED if completed else ResultStatus.PARTIAL
        ),
        data=RetrievalViewDraftData(
            questions=tuple(questions),
            vector_ready=True,
            vector_dimensions=result.vector.dimensions,
            source_question_count=result.vector.source_embedding_count,
        ),
    )


__all__ = [
    "ProjectedSection",
    "UnusableBranchResultError",
    "project_classification",
    "project_clause",
    "project_core",
    "project_retrieval_view",
]
