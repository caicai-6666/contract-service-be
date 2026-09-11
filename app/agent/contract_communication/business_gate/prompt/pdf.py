"""可读性与摘要复用同一文件页面前缀，节点差异只追加到末尾。"""

from app.agent.contract_extraction.state import PDFPromptPage
from app.agent.contract_extraction.subgraph.document_understanding.prompt import build_pdf_messages


def build_file_task_messages(file, *, task_suffix: str):
    """复用已渲染 PNG、物理页码和媒体 UUID，不重新渲染或编码页面。"""
    descriptors = tuple(PDFPromptPage(
        page_number=p.page_number,
        width_pixels=p.width_pixels,
        height_pixels=p.height_pixels,
        descriptor=f"第 {p.page_number} 页",
    ) for p in file.pages)
    # 原始文件名、文件 ID、可读性结论不参与公共前缀，也不充当文件事实。
    return build_pdf_messages(file.pages, descriptors, task_suffix=task_suffix)
