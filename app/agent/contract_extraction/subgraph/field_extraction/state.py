"""字段提取父子图的私有状态。"""

from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    FieldExtractionResult,
    PreparedPDF,
    WorkflowPlaceholder,
)


class FieldExtractionSubgraphState(TypedDict, total=False):
    """在核心字段、特殊字段和结果汇总之间传递的状态。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    core_field: BaseModel
    special_field: WorkflowPlaceholder
    field_extraction: FieldExtractionResult
