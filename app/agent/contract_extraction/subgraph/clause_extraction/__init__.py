"""条款提取子图及其私有节点。"""

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.node import extract_clause_placeholder
from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    PreparedPDF,
    WorkflowPlaceholder,
)


class ClauseExtractionSubgraphState(TypedDict, total=False):
    """条款提取子图的私有状态。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    clause_extraction: WorkflowPlaceholder


def build_clause_extraction_subgraph():
    """自行装配条款提取子图。"""
    graph = StateGraph(ClauseExtractionSubgraphState)
    graph.add_node("extract_clause", extract_clause_placeholder)
    graph.add_edge(START, "extract_clause")
    graph.add_edge("extract_clause", END)
    return graph.compile()
