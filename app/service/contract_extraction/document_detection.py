"""将合同文档识别 LangGraph 适配为 SSE 运行时执行端口。"""

from __future__ import annotations

from typing import Any, Protocol

from app.agent.contract_document_detection.state import (
    ContractDocumentDetectionResult,
)
from app.agent.contract_extraction.state import PreparedPDF


class ContractDocumentDetectionGraph(Protocol):
    """合同提取运行时实际需要的合同文档识别图调用表面。"""

    async def ainvoke(self, input: dict[str, Any]) -> dict[str, Any]:
        """执行一份处理版 PDF 的合同文档识别图。"""


class ContractDocumentDetectionExecutor(Protocol):
    """合同提取服务依赖的合同文档识别执行端口。"""

    async def detect(
        self,
        prepared_pdf: PreparedPDF,
    ) -> ContractDocumentDetectionResult:
        """返回合同、非合同或技术失败结果。"""


class AgentContractDocumentDetectionExecutor:
    """调用已装配的合同文档识别 LangGraph。"""

    def __init__(self, graph: ContractDocumentDetectionGraph) -> None:
        self._graph = graph

    async def detect(
        self,
        prepared_pdf: PreparedPDF,
    ) -> ContractDocumentDetectionResult:
        output = await self._graph.ainvoke({"prepared_pdf": prepared_pdf})
        result = output.get("result")
        if not isinstance(result, ContractDocumentDetectionResult):
            raise RuntimeError("合同文档识别工作流没有返回有效 result")
        if result.document_id != prepared_pdf.document_id:
            raise RuntimeError("合同文档识别结果与处理版 PDF 身份不一致")
        return result


__all__ = [
    "AgentContractDocumentDetectionExecutor",
    "ContractDocumentDetectionExecutor",
    "ContractDocumentDetectionGraph",
]
