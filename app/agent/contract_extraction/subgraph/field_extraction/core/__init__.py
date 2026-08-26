"""核心字段提取子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.field_extraction.core.node import (
    assemble_core_context,
    extract_core,
    prefill_core_context,
    select_core_definitions,
)
from app.agent.contract_extraction.subgraph.field_extraction.core.state import (
    CoreSubgraphState,
)


def build_core_subgraph():
    """装配“选择定义 → 组装公共任务 → 预热 → 并行提取”。"""
    graph = StateGraph(CoreSubgraphState)
    graph.add_node("select_core_definitions", select_core_definitions)
    graph.add_node("assemble_core_context", assemble_core_context)
    graph.add_node("prefill_core_context", prefill_core_context)
    graph.add_node("extract_core", extract_core)
    graph.add_edge(START, "select_core_definitions")
    graph.add_edge("select_core_definitions", "assemble_core_context")
    graph.add_edge("assemble_core_context", "prefill_core_context")
    graph.add_edge("prefill_core_context", "extract_core")
    graph.add_edge("extract_core", END)
    return graph.compile()


__all__ = ["build_core_subgraph"]
