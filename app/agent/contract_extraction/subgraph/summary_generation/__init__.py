"""合同摘要生成子图及其私有节点。"""

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from typing_extensions import TypedDict

from app.agent.contract_extraction.node import generate_summary_placeholder
from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    PreparedPDF,
    WorkflowPlaceholder,
)


class SummaryGenerationSubgraphState(TypedDict, total=False):
    """摘要生成子图的私有状态。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    summary_generation: WorkflowPlaceholder


def build_summary_generation_subgraph():
    """自行装配摘要生成子图。"""
    graph = StateGraph(SummaryGenerationSubgraphState)
    graph.add_node("generate_summary", generate_summary_placeholder)
    graph.add_edge(START, "generate_summary")
    graph.add_edge("generate_summary", END)
    return graph.compile()
