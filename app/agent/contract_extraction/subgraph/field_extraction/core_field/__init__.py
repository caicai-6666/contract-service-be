"""核心字段提取子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.field_extraction.core_field.node import (
    extract_core_fields,
    load_core_field_definitions,
)
from app.agent.contract_extraction.subgraph.field_extraction.core_field.state import (
    CoreFieldSubgraphState,
)


def build_core_field_subgraph():
    """装配“加载对象定义 → 并行逐定义提取”两节点子图。"""
    graph = StateGraph(CoreFieldSubgraphState)
    graph.add_node("load_core_field_definitions", load_core_field_definitions)
    graph.add_node("extract_core_fields", extract_core_fields)
    graph.add_edge(START, "load_core_field_definitions")
    graph.add_edge("load_core_field_definitions", "extract_core_fields")
    graph.add_edge("extract_core_fields", END)
    return graph.compile()


__all__ = ["build_core_field_subgraph"]
