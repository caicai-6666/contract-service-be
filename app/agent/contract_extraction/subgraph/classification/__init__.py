"""合同分类子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.classification.node import (
    assemble_classification_context,
    classify_contract,
    prefill_classification_context,
)
from app.agent.contract_extraction.subgraph.classification.state import (
    ClassificationSubgraphState,
)


def build_classification_subgraph():
    """装配“公共前缀组装 → 工具预热 → 并行分类”的分类子图。"""
    graph = StateGraph(ClassificationSubgraphState)
    graph.add_node("assemble_classification_context", assemble_classification_context)
    graph.add_node("prefill_classification_context", prefill_classification_context)
    graph.add_node("classify_contract", classify_contract)
    graph.add_edge(START, "assemble_classification_context")
    graph.add_edge(
        "assemble_classification_context",
        "prefill_classification_context",
    )
    graph.add_edge("prefill_classification_context", "classify_contract")
    graph.add_edge("classify_contract", END)
    return graph.compile()


__all__ = ["build_classification_subgraph"]
