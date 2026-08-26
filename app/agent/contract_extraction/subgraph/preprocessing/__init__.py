"""PDF 预处理子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.preprocessing.document_structure.node import (
    discover_document_units,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.visual_grounding import (
    locate_document_units,
)
from app.agent.contract_extraction.subgraph.preprocessing.node import (
    build_pdf_prompt_context,
    prepare_pdf,
)
from app.agent.contract_extraction.subgraph.preprocessing.state import (
    PreprocessingSubgraphState,
)


def build_preprocessing_subgraph():
    """装配 PDF 标准化、结构发现与并发视觉定位阶段。"""
    graph = StateGraph(PreprocessingSubgraphState)
    graph.add_node("prepare_pdf", prepare_pdf)
    graph.add_node("build_pdf_prompt_context", build_pdf_prompt_context)
    graph.add_node("discover_document_units", discover_document_units)
    graph.add_node("locate_document_units", locate_document_units)
    graph.add_edge(START, "prepare_pdf")
    graph.add_edge("prepare_pdf", "build_pdf_prompt_context")
    graph.add_edge("build_pdf_prompt_context", "discover_document_units")
    graph.add_edge("discover_document_units", "locate_document_units")
    graph.add_edge("locate_document_units", END)
    return graph.compile()


__all__ = ["build_preprocessing_subgraph"]
