"""PDF 页面进入工作流后的确定性提示词上下文节点。"""

from app.agent.contract_extraction.state import (
    PDFPromptContext,
    PDFPromptPage,
)
from app.agent.contract_extraction.subgraph.document_understanding.prompt import (
    PDF_READING_PROMPT_VERSION,
    build_pdf_page_descriptor,
)
from app.agent.contract_extraction.subgraph.document_understanding.state import (
    DocumentUnderstandingState,
)


def build_pdf_prompt_context(
    state: DocumentUnderstandingState,
) -> DocumentUnderstandingState:
    """构造只向模型暴露页码、内部保留页面尺寸的提示词计划。"""
    prepared_pdf = state["prepared_pdf"]
    prompt_pages = tuple(
        PDFPromptPage(
            page_number=page.page_number,
            width_pixels=page.width_pixels,
            height_pixels=page.height_pixels,
            descriptor=build_pdf_page_descriptor(page),
        )
        for page in prepared_pdf.pages
    )
    return {
        "prompt_context": PDFPromptContext(
            prompt_version=PDF_READING_PROMPT_VERSION,
            pages=prompt_pages,
        )
    }
