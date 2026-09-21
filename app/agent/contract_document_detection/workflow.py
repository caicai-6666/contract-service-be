"""合同性质判断通过后执行文件准入与质量检查，统一收束结果。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_document_detection.node import (
    detect_contract_document,
    check_contract_file_quality,
    finalize_document_admission,
)
from app.agent.contract_document_detection.state import (
    ContractDocumentDetectionState,
)


def build_contract_document_detection_graph():
    """装配“处理版 PDF → 合同文档识别结果”工作流。"""
    graph = StateGraph(ContractDocumentDetectionState)
    graph.add_node("detect_contract_document", detect_contract_document)
    graph.add_node("check_contract_file_quality", check_contract_file_quality)
    graph.add_node("finalize_document_admission", finalize_document_admission)
    graph.add_edge(START, "detect_contract_document")
    graph.add_conditional_edges(
        "detect_contract_document",
        lambda state: "summary" if state["result"].status == "contract" else "end",
        {"summary": "check_contract_file_quality", "end": "finalize_document_admission"},
    )
    graph.add_edge("check_contract_file_quality", "finalize_document_admission")
    graph.add_edge("finalize_document_admission", END)
    return graph.compile()


__all__ = ["build_contract_document_detection_graph"]
