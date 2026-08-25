"""核心字段提取子图的私有状态与结果契约。"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import ContractPrefillContext, PreparedPDF
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinition,
)
from app.agent.contract_extraction.subgraph.field_extraction.tool import (
    FieldEvidence,
    FieldObjectValue,
)


class CoreFieldModel(BaseModel):
    """核心字段目录、结果和审计记录的不可变基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CoreFieldCatalog(CoreFieldModel):
    """节点一按稳定文件顺序加载的核心字段目录。"""

    directory: str
    sha256: str
    definitions: tuple[FieldDefinition, ...]


class FieldToolFeedback(CoreFieldModel):
    """工具执行后写回单字段短期上下文的最小反馈。"""

    ok: bool
    message: str


class FieldToolCallAudit(CoreFieldModel):
    """单字段一次工具调用的审计记录。"""

    round_number: int
    call_id: str
    name: str
    raw_arguments: str
    feedback: FieldToolFeedback
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class FieldOutcomeBase(CoreFieldModel):
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


class ExtractedFieldObject(CoreFieldModel):
    """一次通过证据、理由和扁平 Schema 校验的对象。"""

    evidence: tuple[FieldEvidence, ...]
    reasoning: str
    value: FieldObjectValue


class ExtractedCoreField(FieldOutcomeBase):
    """已成功提交一个或多个扁平对象的定义。"""

    status: Literal["extracted"] = "extracted"
    objects: tuple[ExtractedFieldObject, ...]
    finish_reasoning: str | None


class AbandonedCoreField(FieldOutcomeBase):
    """模型确认当前合同不能可靠提取任何对象。"""

    status: Literal["abandoned"] = "abandoned"
    reasoning: str


class FailedCoreField(FieldOutcomeBase):
    """协议、请求或最大轮次导致的运行失败。"""

    status: Literal["failed"] = "failed"
    partial_objects: tuple[ExtractedFieldObject, ...]
    error: str


CoreFieldOutcome: TypeAlias = (
    ExtractedCoreField | AbandonedCoreField | FailedCoreField
)


class CoreFieldExtractionResult(CoreFieldModel):
    """节点二并行处理全部核心字段后的稳定结果。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    prompt_version: str
    catalog_sha256: str
    fields: tuple[CoreFieldOutcome, ...]
    elapsed_ms: float


class CoreFieldSubgraphState(TypedDict, total=False):
    """核心字段子图只读公共前缀，并拥有目录与提取结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    core_field_catalog: CoreFieldCatalog
    core_field: CoreFieldExtractionResult


__all__ = [
    "AbandonedCoreField",
    "CoreFieldCatalog",
    "CoreFieldExtractionResult",
    "CoreFieldOutcome",
    "CoreFieldSubgraphState",
    "ExtractedCoreField",
    "ExtractedFieldObject",
    "FailedCoreField",
    "FieldToolCallAudit",
    "FieldToolFeedback",
]
