"""特殊字段提取子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.field_extraction.special_field.node import (
    extract_special_field_placeholder,
)
from app.agent.contract_extraction.subgraph.field_extraction.special_field.state import (
    SpecialFieldSubgraphState,
)


def build_special_field_subgraph():
    """装配特殊字段提取子图；业务节点将在后续实现。"""
    graph = StateGraph(SpecialFieldSubgraphState)
    graph.add_node("extract_special_field", extract_special_field_placeholder)
    graph.add_edge(START, "extract_special_field")
    graph.add_edge("extract_special_field", END)
    return graph.compile()


__all__ = ["build_special_field_subgraph"]
