"""PDF 查重三阶段工作流。"""

from elasticsearch import AsyncElasticsearch
from langgraph.graph import END, START, StateGraph

from app.agent.pdf_deduplication.node import (
    judge_duplicate_candidates,
    retrieve_duplicate_candidates,
    vectorize_processed_pdf,
)
from app.agent.pdf_deduplication.port import PDFDuplicateCandidateLoader
from app.agent.pdf_deduplication.state import PDFDeduplicationState


def build_pdf_deduplication_graph(
    client: AsyncElasticsearch,
    *,
    index_name: str,
    candidate_loader: PDFDuplicateCandidateLoader,
):
    """装配“页面向量融合 → Top 3 召回 → 并发判重”顺序图。"""
    graph = StateGraph(PDFDeduplicationState)
    graph.add_node("vectorize_processed_pdf", vectorize_processed_pdf)

    async def retrieve_node(state: PDFDeduplicationState) -> PDFDeduplicationState:
        return await retrieve_duplicate_candidates(
            state, client=client, index_name=index_name
        )

    async def judge_node(state: PDFDeduplicationState) -> PDFDeduplicationState:
        return await judge_duplicate_candidates(
            state,
            candidate_loader=candidate_loader,
        )

    graph.add_node("retrieve_duplicate_candidates", retrieve_node)
    graph.add_node(
        "judge_duplicate_candidates",
        judge_node,
    )
    graph.add_edge(START, "vectorize_processed_pdf")
    graph.add_edge("vectorize_processed_pdf", "retrieve_duplicate_candidates")
    graph.add_edge(
        "retrieve_duplicate_candidates",
        "judge_duplicate_candidates",
    )
    graph.add_edge("judge_duplicate_candidates", END)
    return graph.compile()


__all__ = ["build_pdf_deduplication_graph"]
