"""PDF 预处理子图状态。"""

from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractExtractionRequest,
    PDFPromptContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.state import (
    DocumentStructureMetadata,
    UnitDiscoveryResult,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.visual_grounding.state import (
    UnitVisualGroundingResult,
)


class PreprocessingSubgraphState(TypedDict, total=False):
    """PDF 预处理、结构发现、视觉定位及其对外结果的共享状态。"""

    request: ContractExtractionRequest
    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    unit_discovery: UnitDiscoveryResult
    unit_grounding: UnitVisualGroundingResult
    document_structure: DocumentStructureMetadata
