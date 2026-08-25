"""特殊字段提取子图的私有状态。"""

from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    PreparedPDF,
    WorkflowPlaceholder,
)


class SpecialFieldSubgraphState(TypedDict, total=False):
    """特殊字段子图可读取核心字段结果，但只拥有特殊字段结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    core_field: BaseModel
    special_field: WorkflowPlaceholder
