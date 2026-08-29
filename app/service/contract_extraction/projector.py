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


def _public_field_object(item: BaseModel) -> dict[str, Any]:
    """保留字段审核真正需要的证据、理由和扁平值。"""
    return {
        "evidence": [
            evidence.model_dump(mode="json") for evidence in item.evidence
        ],
        "reasoning": item.reasoning,
        "value": dict(item.value),
    }


def project_core(result: BaseModel) -> ProjectedSection:
    """投影 Core 结果；失败原因和模型用量只留在私有运行记录中。"""
    if not isinstance(result, CoreExtractionResult):
        raise TypeError("Core 分支返回了未知结果类型")
    if result.status == "failed":
        raise UnusableBranchResultError("Core 字段没有形成可用结果")

    fields: list[dict[str, Any]] = []
    for field_result in result.fields:
        item: dict[str, Any] = {
            "name": field_result.name,
            "cardinality": field_result.cardinality.value,
            "status": field_result.status,
            "property_names": list(field_result.property_names),
        }
        if field_result.status == "extracted":
            item["objects"] = [
                _public_field_object(value) for value in field_result.objects
            ]
        elif field_result.status == "abandoned":
            item["objects"] = []
            item["reasoning"] = field_result.reasoning
        else:
            item["objects"] = [
                _public_field_object(value)
                for value in field_result.partial_objects
            ]
            item["message"] = "该字段本次未能完成提取，可稍后重试。"
        fields.append(item)

    return ProjectedSection(
        result_status=(
            ResultStatus.COMPLETED
            if result.status == "completed"
            else ResultStatus.PARTIAL
        ),
        data=CoreDraftData.model_validate({"fields": fields}),
    )


def project_clause(result: ClauseExtractionResult) -> ProjectedSection:
    """投影条款顺序、边界和正文，去除每轮请求与工具审计。"""
    if result.status == "failed":
        raise UnusableBranchResultError("条款分支没有形成可用结果")

    clauses: list[dict[str, Any]] = []
    for clause in result.clauses:
        candidate = clause.candidate
        item: dict[str, Any] = {
            "candidate_id": candidate.candidate_id,
            "order": candidate.order,
            "identifier": candidate.identifier,
            "title_hint": candidate.title_hint,
            "document_path": [
                segment.model_dump(mode="json")
                for segment in candidate.document_path
            ],
            "parent_candidate_id": candidate.parent_candidate_id,
            "level": candidate.level,
            "evidence": candidate.evidence.model_dump(mode="json"),
            "status": clause.status,
        }
        if clause.status == "extracted":
            item["reasoning_summary"] = clause.reasoning_summary
            item["content"] = clause.content
        else:
            item["message"] = "该条款本次未能完成提取，可稍后重试。"
        clauses.append(item)

    return ProjectedSection(
        result_status=(
            ResultStatus.COMPLETED
            if result.status == "completed"
            else ResultStatus.PARTIAL
        ),
        data=ClauseDraftData.model_validate({"clauses": clauses}),
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
