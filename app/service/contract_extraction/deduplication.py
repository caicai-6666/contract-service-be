"""将 PDF 查重 LangGraph 适配为合同提取运行时的应用端口。"""

from __future__ import annotations

from typing import Any, Protocol

from app.agent.contract_extraction.state import PreparedPDF
from app.agent.pdf_deduplication.state import PDFDeduplicationResult


class PDFDeduplicationGraph(Protocol):
    """合同提取运行时实际需要的 LangGraph 调用表面。"""

    async def ainvoke(self, input: dict[str, Any]) -> dict[str, Any]:
        """执行一份处理版 PDF 的完整查重图。"""


class PDFDeduplicationExecutor(Protocol):
    """合同提取服务依赖的查重执行端口。"""

    async def deduplicate(
        self,
        prepared_pdf: PreparedPDF,
    ) -> PDFDeduplicationResult:
        """返回页面融合向量、Top-3 候选及逐候选关系判断。"""


class AgentPDFDeduplicationExecutor:
    """调用已装配的 PDF 查重 LangGraph。"""

    def __init__(self, graph: PDFDeduplicationGraph) -> None:
        self._graph = graph

    async def deduplicate(
        self,
        prepared_pdf: PreparedPDF,
    ) -> PDFDeduplicationResult:
        output = await self._graph.ainvoke({"prepared_pdf": prepared_pdf})
        result = output.get("result")
        if not isinstance(result, PDFDeduplicationResult):
            raise RuntimeError("PDF 查重工作流没有返回有效 result")
        return result


__all__ = [
    "AgentPDFDeduplicationExecutor",
    "PDFDeduplicationExecutor",
    "PDFDeduplicationGraph",
]
