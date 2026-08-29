"""Core 字段提取子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.state import FieldExtractionResult
from app.agent.contract_extraction.subgraph.field_extraction.core import (
    build_core_subgraph,
)
from app.agent.contract_extraction.subgraph.field_extraction.state import (
    FieldExtractionSubgraphState,
)


def build_field_extraction_subgraph():
    """装配仅包含 Core 的字段提取子图。"""
    core_subgraph = build_core_subgraph()

    async def run_core_subgraph(
        state: FieldExtractionSubgraphState,
    ) -> FieldExtractionSubgraphState:
        """调用核心字段子图，并只回写其私有结果。"""
        result = await core_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
                "field_definition_catalog": state[
                    "field_definition_catalog"
                ],
            }
        )
        core = result["core"]
        return {
            "core": core,
            "field_extraction": FieldExtractionResult(core=core),
        }

    graph = StateGraph(FieldExtractionSubgraphState)
    graph.add_node("core_extraction", run_core_subgraph)
    graph.add_edge(START, "core_extraction")
    graph.add_edge("core_extraction", END)
    return graph.compile()


__all__ = ["build_field_extraction_subgraph"]
