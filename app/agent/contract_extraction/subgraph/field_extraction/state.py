"""字段提取父子图的私有状态。"""

from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    FieldExtractionResult,
    PreparedPDF,
    WorkflowPlaceholder,
)
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldDefinitionCatalog,
)


class FieldExtractionSubgraphState(TypedDict, total=False):
    """在 Core、Attribute 和结果汇总之间传递的状态。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    field_definition_catalog: FieldDefinitionCatalog
    core: BaseModel
    attribute: WorkflowPlaceholder
    field_extraction: FieldExtractionResult
