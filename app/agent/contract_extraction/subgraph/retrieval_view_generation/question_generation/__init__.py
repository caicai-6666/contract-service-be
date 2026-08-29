"""检索问题提出子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.node import (
    discover_question_focuses,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.node import (
    assemble_question_generation_context,
    embed_questions,
    fuse_question_embeddings,
    generate_questions_from_plans,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.state import (
    QuestionGenerationSubgraphState,
)


def build_question_generation_subgraph():
    """装配“指南渲染 → 问题规划 → 并发提问 → 向量化 → 融合”五节点子图。"""
    graph = StateGraph(QuestionGenerationSubgraphState)
    graph.add_node(
        "render_question_guides",
        assemble_question_generation_context,
    )
    graph.add_node("discover_question_focuses", discover_question_focuses)
    graph.add_node("generate_questions", generate_questions_from_plans)
    graph.add_node("embed_questions", embed_questions)
    graph.add_node("fuse_question_embeddings", fuse_question_embeddings)
    graph.add_edge(START, "render_question_guides")
    graph.add_edge("render_question_guides", "discover_question_focuses")
    graph.add_edge("discover_question_focuses", "generate_questions")
    graph.add_edge("generate_questions", "embed_questions")
    graph.add_edge("embed_questions", "fuse_question_embeddings")
    graph.add_edge("fuse_question_embeddings", END)
    return graph.compile()


def build_question_focus_discovery_subgraph():
    """装配可独立验证的“提问指南渲染 → 关注点发现”两节点子图。"""
    graph = StateGraph(QuestionGenerationSubgraphState)
    graph.add_node(
        "render_question_guides",
        assemble_question_generation_context,
    )
    graph.add_node("discover_question_focuses", discover_question_focuses)
    graph.add_edge(START, "render_question_guides")
    graph.add_edge("render_question_guides", "discover_question_focuses")
    graph.add_edge("discover_question_focuses", END)
    return graph.compile()


__all__ = [
    "build_question_focus_discovery_subgraph",
    "build_question_generation_subgraph",
]
