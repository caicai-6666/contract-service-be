"""字段提取父子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.state import FieldExtractionResult
from app.agent.contract_extraction.subgraph.field_extraction.core_field import (
    build_core_field_subgraph,
)
from app.agent.contract_extraction.subgraph.field_extraction.special_field import (
    build_special_field_subgraph,
)
from app.agent.contract_extraction.subgraph.field_extraction.state import (
    FieldExtractionSubgraphState,
)


def build_field_extraction_subgraph():
    """按“核心字段 → 特殊字段”装配字段提取父子图。"""
    core_field_subgraph = build_core_field_subgraph()
    special_field_subgraph = build_special_field_subgraph()

    async def run_core_field_subgraph(
        state: FieldExtractionSubgraphState,
    ) -> FieldExtractionSubgraphState:
        """调用核心字段子图，并只回写其私有结果。"""
        result = await core_field_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
            }
        )
        return {"core_field": result["core_field"]}

    async def run_special_field_subgraph(
        state: FieldExtractionSubgraphState,
    ) -> FieldExtractionSubgraphState:
        """将核心字段上下文传给特殊字段子图，并回写其私有结果。"""
        result = await special_field_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
                "core_field": state["core_field"],
            }
        )
        return {"special_field": result["special_field"]}

    def merge_field_results(
        state: FieldExtractionSubgraphState,
    ) -> FieldExtractionSubgraphState:
        """形成字段模块的统一占位输出契约。"""
        return {
            "field_extraction": FieldExtractionResult(
                core_field=state["core_field"],
                special_field=state["special_field"],
            )
        }

    graph = StateGraph(FieldExtractionSubgraphState)
    graph.add_node("core_field_extraction", run_core_field_subgraph)
    graph.add_node("special_field_extraction", run_special_field_subgraph)
    graph.add_node("merge_field_results", merge_field_results)
    graph.add_edge(START, "core_field_extraction")
    graph.add_edge("core_field_extraction", "special_field_extraction")
    graph.add_edge("special_field_extraction", "merge_field_results")
    graph.add_edge("merge_field_results", END)
    return graph.compile()


__all__ = ["build_field_extraction_subgraph"]
