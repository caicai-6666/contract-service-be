"""检索问题生成子图的兼容装配入口。"""

from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation import (
    build_question_generation_subgraph,
)


def build_retrieval_view_generation_subgraph():
    """返回生成问题、独立向量化并融合合同向量的五节点子图。"""
    return build_question_generation_subgraph()


__all__ = ["build_retrieval_view_generation_subgraph"]
