"""基于已准备 PDF 的文档结构理解子图状态。"""

from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    PDFPromptContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.state import (
    DocumentStructureMetadata,
    UnitDiscoveryResult,
)
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.visual_grounding.state import (
    UnitVisualGroundingResult,
)


class DocumentUnderstandingState(TypedDict, total=False):
    """页面上下文、结构发现、视觉定位及其对外结果的共享状态。"""

    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    unit_discovery: UnitDiscoveryResult
    unit_grounding: UnitVisualGroundingResult
    document_structure: DocumentStructureMetadata
