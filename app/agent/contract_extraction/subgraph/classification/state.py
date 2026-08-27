"""合同分类子图状态。"""

from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import ContractBaseContext
from app.agent.contract_extraction.subgraph.classification.definition import (
    ContractCategoryCatalog,
)
from app.agent.contract_extraction.subgraph.classification.tool import (
    CategoryMatchCard,
    ClassificationEvidence,
    ClassificationToolFeedback,
)


class ClassificationModel(BaseModel):
    """分类上下文、运行审计和结果使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ClassificationContext(ClassificationModel):
    """供全部单类别判别复用的不可变公共前缀。"""

    document_id: str
    prompt_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class ClassificationPreheatResult(ClassificationModel):
    """携带分类工具预热公共前缀的可观测结果。"""

    status: Literal["warmed", "degraded"]
    document_id: str
    prompt_version: str
    model: str
    completed_at: datetime
    prefix_sha256: str
    elapsed_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    error: str | None = None


class ClassificationToolCallAudit(ClassificationModel):
    """单类别会话的一次工具调用与模型用量审计。"""

    round_number: int
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None = None
    feedback: ClassificationToolFeedback
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class CategoryJudgmentBase(ClassificationModel):
    """命中、未命中与失败终态共享的运行字段。"""

    category_code: str
    category_name: str
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[ClassificationToolCallAudit, ...]


class MatchedCategory(CategoryJudgmentBase):
    """当前合同命中目标类别。"""

    status: Literal["matched"] = "matched"
    match: CategoryMatchCard


class NotMatchedCategory(CategoryJudgmentBase):
    """当前合同不命中目标类别。"""

    status: Literal["not_matched"] = "not_matched"
    evidence: tuple[ClassificationEvidence, ...]
    reasoning_summary: str


class FailedCategory(CategoryJudgmentBase):
    """请求或工具协议未能形成有效终止决定。"""

    status: Literal["failed"] = "failed"
    error: str


CategoryJudgmentOutcome: TypeAlias = (
    MatchedCategory | NotMatchedCategory | FailedCategory
)


class UnmappedTypeDescription(ClassificationModel):
    """零类别命中时保留在分类审计中的证据化类型描述。"""

    evidence: tuple[ClassificationEvidence, ...]
    reasoning_summary: str
    description: str


class ContractClassificationRun(ClassificationModel):
    """分类子图私有的逐类别完整审计结果。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    common_prompt_version: str
    category_prompt_version: str
    catalog_sha256: str
    categories: tuple[CategoryJudgmentOutcome, ...]
    unmapped_type_description: UnmappedTypeDescription | None = None
    unmapped_type_description_error: str | None = None
    unmapped_type_tool_calls: tuple[ClassificationToolCallAudit, ...] = ()
    elapsed_ms: float


class ContractClassificationResult(ClassificationModel):
    """追加到最终公共前缀的紧凑分类结果。"""

    status: Literal["classified", "unmapped", "partial", "failed"]
    document_id: str
    model: str
    common_prompt_version: str
    category_prompt_version: str
    catalog_sha256: str
    matches: tuple[CategoryMatchCard, ...]
    failed_category_codes: tuple[str, ...]
    unmapped_type_description: str | None = None


class ClassificationSubgraphState(TypedDict, total=False):
    """分类公共前缀、后续判别过程及结果的私有共享状态。"""

    base_context: ContractBaseContext
    category_catalog: ContractCategoryCatalog
    page_count: int
    classification_context: ClassificationContext
    classification_preheat: ClassificationPreheatResult
    classification_run: ContractClassificationRun
    classification: ContractClassificationResult


__all__ = [
    "CategoryJudgmentOutcome",
    "ClassificationContext",
    "ClassificationModel",
    "ClassificationPreheatResult",
    "ClassificationSubgraphState",
    "ClassificationToolCallAudit",
    "ContractClassificationResult",
    "ContractClassificationRun",
    "FailedCategory",
    "MatchedCategory",
    "NotMatchedCategory",
    "UnmappedTypeDescription",
]
