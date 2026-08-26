"""Attribute 提取子图的私有状态。"""

from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    PreparedPDF,
    WorkflowPlaceholder,
)


class AttributeSubgraphState(TypedDict, total=False):
    """Attribute 子图可读取 Core 结果，但只拥有 Attribute 结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    core: BaseModel
    attribute: WorkflowPlaceholder
