"""下游公共前缀预热子图状态。"""

from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    ContractPreheatResult,
    PDFPromptContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.state import (
    DocumentStructureMetadata,
)


class PreheatSubgraphState(TypedDict, total=False):
    """两个预热节点之间传递的私有状态。"""

    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    document_structure: DocumentStructureMetadata
    prefill_context: ContractPrefillContext
    preheat: ContractPreheatResult
