"""核心字段提取子图的私有状态与结果契约。"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import ContractPrefillContext, PreparedPDF
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinitionCatalog,
    FieldDefinitionCollection,
)
from app.agent.contract_extraction.subgraph.field_extraction.tool import (
    FieldEvidence,
    FieldObjectValue,
)


class CoreModel(BaseModel):
    """核心字段目录、结果和审计记录的不可变基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CoreContext(CoreModel):
    """供全部单字段提取复用的不可变 Core 公共任务前缀。"""

    document_id: str
    prompt_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class FieldToolFeedback(CoreModel):
    """工具执行后写回单字段短期上下文的最小反馈。"""

    ok: bool
    message: str


class FieldToolCallAudit(CoreModel):
    """单字段一次工具调用的审计记录。"""

    round_number: int
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None = None
    feedback: FieldToolFeedback
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class FieldOutcomeBase(CoreModel):
    """三种对象定义终态共享的运行信息。"""

    name: str
    cardinality: FieldCardinality
    property_names: tuple[str, ...]
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[FieldToolCallAudit, ...]


class ExtractedFieldObject(CoreModel):
    """一次通过证据、理由和扁平 Schema 校验的对象。"""

    evidence: tuple[FieldEvidence, ...]
    reasoning: str
    value: FieldObjectValue


class ExtractedCore(FieldOutcomeBase):
    """已成功提交一个或多个扁平对象的定义。"""

    status: Literal["extracted"] = "extracted"
    objects: tuple[ExtractedFieldObject, ...]
    finish_reasoning: str | None


class AbandonedCore(FieldOutcomeBase):
    """模型确认当前合同不能可靠提取任何对象。"""

    status: Literal["abandoned"] = "abandoned"
    reasoning: str


class FailedCore(FieldOutcomeBase):
    """协议、请求或最大轮次导致的运行失败。"""

    status: Literal["failed"] = "failed"
    partial_objects: tuple[ExtractedFieldObject, ...]
    error: str


CoreOutcome: TypeAlias = ExtractedCore | AbandonedCore | FailedCore


class CoreExtractionResult(CoreModel):
    """并行处理全部核心字段后的稳定结果。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    prompt_version: str
    catalog_sha256: str
    fields: tuple[CoreOutcome, ...]
    elapsed_ms: float


class CoreSubgraphState(TypedDict, total=False):
    """核心字段子图只读公共前缀，并拥有目录与提取结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    field_definition_catalog: FieldDefinitionCatalog
    core_definitions: FieldDefinitionCollection
    core_context: CoreContext
    core: CoreExtractionResult


__all__ = [
    "AbandonedCore",
    "CoreContext",
    "CoreExtractionResult",
    "CoreOutcome",
    "CoreSubgraphState",
    "ExtractedCore",
    "ExtractedFieldObject",
    "FailedCore",
    "FieldToolCallAudit",
    "FieldToolFeedback",
]
