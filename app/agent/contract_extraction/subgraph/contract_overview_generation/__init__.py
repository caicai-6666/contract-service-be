"""合同概览生成子图装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.subgraph.contract_overview_generation.node import (
    assemble_contract_overview_context,
    generate_contract_overview,
)
from app.agent.contract_extraction.subgraph.contract_overview_generation.state import (
    ContractOverviewGenerationSubgraphState,
)
from app.agent.contract_extraction.subgraph.contract_overview_generation.tool import (
    CONTRACT_OVERVIEW_GENERATION_TOOLS,
    CONTRACT_OVERVIEW_GENERATION_TOOL_CHOICE,
    CONTRACT_OVERVIEW_GENERATION_TOOL_PLACEMENT,
    CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION,
    parse_contract_overview_generation_tool_arguments,
    validation_error_feedback,
)


def build_contract_overview_generation_subgraph():
    """装配“上下文组装 → 合同概览生成”的两节点子图。"""
    graph = StateGraph(ContractOverviewGenerationSubgraphState)
    graph.add_node("assemble_contract_overview_context", assemble_contract_overview_context)
    graph.add_node("generate_contract_overview", generate_contract_overview)
    graph.add_edge(START, "assemble_contract_overview_context")
    graph.add_edge("assemble_contract_overview_context", "generate_contract_overview")
    graph.add_edge("generate_contract_overview", END)
    return graph.compile()


__all__ = [
    "CONTRACT_OVERVIEW_GENERATION_TOOLS",
    "CONTRACT_OVERVIEW_GENERATION_TOOL_CHOICE",
    "CONTRACT_OVERVIEW_GENERATION_TOOL_PLACEMENT",
    "CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION",
    "build_contract_overview_generation_subgraph",
    "parse_contract_overview_generation_tool_arguments",
    "validation_error_feedback",
]
