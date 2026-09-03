"""上传 PDF 与单份召回候选的自适应判重子图。"""

from langgraph.graph import END, START, StateGraph

from app.agent.pdf_deduplication.subgraph.candidate_judgment.node import (
    FULL_DOCUMENT_NODE,
    PAGE_NAVIGATION_AGENT_NODE,
    decide_candidate_judgment_route,
    judge_full_documents,
    judge_with_page_navigation_agent,
    route_candidate_judgment,
)
from app.agent.pdf_deduplication.subgraph.candidate_judgment.state import (
    PDFCandidateJudgmentState,
)


def build_candidate_judgment_subgraph():
    """装配“预算分流 → 全量输入/翻页 Agent”的逐候选子图。"""
    graph = StateGraph(PDFCandidateJudgmentState)
    graph.add_node("decide_candidate_judgment_route", decide_candidate_judgment_route)
    graph.add_node(FULL_DOCUMENT_NODE, judge_full_documents)
    graph.add_node(PAGE_NAVIGATION_AGENT_NODE, judge_with_page_navigation_agent)
    graph.add_edge(START, "decide_candidate_judgment_route")
    graph.add_conditional_edges(
        "decide_candidate_judgment_route",
        route_candidate_judgment,
        {
            FULL_DOCUMENT_NODE: FULL_DOCUMENT_NODE,
            PAGE_NAVIGATION_AGENT_NODE: PAGE_NAVIGATION_AGENT_NODE,
        },
    )
    graph.add_edge(FULL_DOCUMENT_NODE, END)
    graph.add_edge(PAGE_NAVIGATION_AGENT_NODE, END)
    return graph.compile()


__all__ = ["build_candidate_judgment_subgraph"]
