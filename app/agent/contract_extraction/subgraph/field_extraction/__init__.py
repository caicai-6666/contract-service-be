"""字段提取父子图的装配入口。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.state import FieldExtractionResult
from app.agent.contract_extraction.subgraph.field_extraction.core import (
    build_core_subgraph,
)
from app.agent.contract_extraction.subgraph.field_extraction.attribute import (
    build_attribute_subgraph,
)
from app.agent.contract_extraction.subgraph.field_extraction.state import (
    FieldExtractionSubgraphState,
)


def build_field_extraction_subgraph():
    """按“Core → Attribute”装配字段提取父子图。"""
    core_subgraph = build_core_subgraph()
    attribute_subgraph = build_attribute_subgraph()

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
        return {"core": result["core"]}

    async def run_attribute_subgraph(
        state: FieldExtractionSubgraphState,
    ) -> FieldExtractionSubgraphState:
        """将 Core 上下文传给 Attribute 子图，并回写其私有结果。"""
        result = await attribute_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
                "core": state["core"],
            }
        )
        return {"attribute": result["attribute"]}

    def merge_field_results(
        state: FieldExtractionSubgraphState,
    ) -> FieldExtractionSubgraphState:
        """形成字段模块的统一占位输出契约。"""
        return {
            "field_extraction": FieldExtractionResult(
                core=state["core"],
                attribute=state["attribute"],
            )
        }

    graph = StateGraph(FieldExtractionSubgraphState)
    graph.add_node("core_extraction", run_core_subgraph)
    graph.add_node("attribute_extraction", run_attribute_subgraph)
    graph.add_node("merge_field_results", merge_field_results)
    graph.add_edge(START, "core_extraction")
    graph.add_edge("core_extraction", "attribute_extraction")
    graph.add_edge("attribute_extraction", "merge_field_results")
    graph.add_edge("merge_field_results", END)
    return graph.compile()


__all__ = ["build_field_extraction_subgraph"]
