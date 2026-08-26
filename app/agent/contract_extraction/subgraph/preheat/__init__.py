"""最终公共前缀预热子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.preheat.node import (
    assemble_prefill_context,
    prefill_contract_context,
)
from app.agent.contract_extraction.subgraph.preheat.state import PreheatSubgraphState


def build_preheat_subgraph():
    """装配“追加分类结果 → vLLM 最终预热”的两节点子图。"""
    graph = StateGraph(PreheatSubgraphState)
    graph.add_node("assemble_prefill_context", assemble_prefill_context)
    graph.add_node("prefill_contract_context", prefill_contract_context)
    graph.add_edge(START, "assemble_prefill_context")
    graph.add_edge("assemble_prefill_context", "prefill_contract_context")
    graph.add_edge("prefill_contract_context", END)
    return graph.compile()


__all__ = ["build_preheat_subgraph"]
