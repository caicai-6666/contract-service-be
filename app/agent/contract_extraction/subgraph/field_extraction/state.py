"""字段提取父子图的私有状态。"""

from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    FieldExtractionResult,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldDefinitionCatalog,
)


class FieldExtractionSubgraphState(TypedDict, total=False):
    """字段父子图向 Core 子图传递输入并汇总正式结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    field_definition_catalog: FieldDefinitionCatalog
    core: BaseModel
    field_extraction: FieldExtractionResult
