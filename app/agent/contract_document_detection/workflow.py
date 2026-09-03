"""合同文档识别 Agent 的单节点工作流。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_document_detection.node import detect_contract_document
from app.agent.contract_document_detection.state import (
    ContractDocumentDetectionState,
)


def build_contract_document_detection_graph():
    """装配“处理版 PDF → 合同文档识别结果”工作流。"""
    graph = StateGraph(ContractDocumentDetectionState)
    graph.add_node("detect_contract_document", detect_contract_document)
    graph.add_edge(START, "detect_contract_document")
    graph.add_edge("detect_contract_document", END)
    return graph.compile()


__all__ = ["build_contract_document_detection_graph"]
