"""基于已准备 PDF 的文档结构理解子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.document_understanding.document_structure.node import (
    discover_document_units,
)
from app.agent.contract_extraction.subgraph.document_understanding.document_structure.visual_grounding import (
    locate_document_units,
)
from app.agent.contract_extraction.subgraph.document_understanding.node import (
    build_pdf_prompt_context,
)
from app.agent.contract_extraction.subgraph.document_understanding.state import (
    DocumentUnderstandingState,
)


def build_document_understanding_subgraph():
    """装配已准备 PDF 的提示词上下文、结构发现与并发视觉定位阶段。"""
    graph = StateGraph(DocumentUnderstandingState)
    graph.add_node("build_pdf_prompt_context", build_pdf_prompt_context)
    graph.add_node("discover_document_units", discover_document_units)
    graph.add_node("locate_document_units", locate_document_units)
    graph.add_edge(START, "build_pdf_prompt_context")
    graph.add_edge("build_pdf_prompt_context", "discover_document_units")
    graph.add_edge("discover_document_units", "locate_document_units")
    graph.add_edge("locate_document_units", END)
    return graph.compile()


__all__ = ["build_document_understanding_subgraph"]
