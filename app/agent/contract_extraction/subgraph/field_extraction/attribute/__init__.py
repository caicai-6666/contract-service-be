"""Attribute 提取子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.field_extraction.attribute.node import (
    extract_attribute_placeholder,
)
from app.agent.contract_extraction.subgraph.field_extraction.attribute.state import (
    AttributeSubgraphState,
)


def build_attribute_subgraph():
    """装配 Attribute 提取子图；业务节点将在后续实现。"""
    graph = StateGraph(AttributeSubgraphState)
    graph.add_node("extract_attribute", extract_attribute_placeholder)
    graph.add_edge(START, "extract_attribute")
    graph.add_edge("extract_attribute", END)
    return graph.compile()


__all__ = ["build_attribute_subgraph"]
