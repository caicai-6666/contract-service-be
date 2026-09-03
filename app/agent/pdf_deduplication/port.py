"""PDF 查重工作流依赖的外部能力端口。"""

from __future__ import annotations

from typing import Protocol

from app.agent.contract_extraction.state import PreparedPDF
from app.agent.pdf_deduplication.state import PDFDuplicateCandidate


class PDFDuplicateCandidateLoader(Protocol):
    """按 ES 候选身份加载已经入库的处理版 PDF。"""

    async def load(self, candidate: PDFDuplicateCandidate) -> PreparedPDF:
        """返回保持候选 document_id 和逐页视觉 token 的 PreparedPDF。"""

    async def read_pdf_bytes(self, candidate: PDFDuplicateCandidate) -> bytes:
        """返回已校验身份的候选处理版 PDF 原始字节，供审核端预览。"""


__all__ = ["PDFDuplicateCandidateLoader"]
