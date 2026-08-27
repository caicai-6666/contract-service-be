"""条款提取子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.clause_extraction.node import (
    discover_clause_candidates,
    extract_clause_contents,
    preheat_clause_extraction_context,
)
from app.agent.contract_extraction.subgraph.clause_extraction.state import (
    ClauseExtractionSubgraphState,
)


def build_clause_extraction_subgraph():
    """装配“候选发现 → 详情上下文预热 → 并发内容提取”三节点子图。"""
    graph = StateGraph(ClauseExtractionSubgraphState)
    graph.add_node(
        "discover_clause_candidates",
        discover_clause_candidates,
    )
    graph.add_node(
        "preheat_clause_extraction_context",
        preheat_clause_extraction_context,
    )
    graph.add_node(
        "extract_clause_contents",
        extract_clause_contents,
    )
    graph.add_edge(START, "discover_clause_candidates")
    graph.add_edge(
        "discover_clause_candidates",
        "preheat_clause_extraction_context",
    )
    graph.add_edge(
        "preheat_clause_extraction_context",
        "extract_clause_contents",
    )
    graph.add_edge("extract_clause_contents", END)
    return graph.compile()


__all__ = ["build_clause_extraction_subgraph"]
