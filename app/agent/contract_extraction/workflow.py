"""合同信息抽取的主图装配。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.node import merge_extraction_results
from app.agent.contract_extraction.state import ContractExtractionState
from app.agent.contract_extraction.subgraph import (
    build_clause_extraction_subgraph,
    build_field_extraction_subgraph,
    build_preheat_subgraph,
    build_preprocessing_subgraph,
    build_summary_generation_subgraph,
)


def build_contract_extraction_graph():
    """组装“预处理 → 预热 → 三个并行子图 → 合并”的主图。"""
    preprocessing_subgraph = build_preprocessing_subgraph()
    preheat_subgraph = build_preheat_subgraph()
    field_subgraph = build_field_extraction_subgraph()
    clause_subgraph = build_clause_extraction_subgraph()
    summary_subgraph = build_summary_generation_subgraph()

    async def run_preprocessing_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """调用预处理子图，并写回页面与权威文档结构。"""
        result = await preprocessing_subgraph.ainvoke(
            {"request": state["request"]}
        )
        return {
            "prepared_pdf": result["prepared_pdf"],
            "prompt_context": result["prompt_context"],
            "document_structure": result["document_structure"],
        }

    async def run_preheat_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """组装完整下游公共前缀，并向 vLLM 发起预热请求。"""
        result = await preheat_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "prompt_context": state["prompt_context"],
                "document_structure": state["document_structure"],
            }
        )
        return {
            "prefill_context": result["prefill_context"],
            "preheat": result["preheat"],
        }

    async def run_field_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """调用字段子图，并只把它拥有的结果写回主图。"""
        result = await field_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
            }
        )
        return {"field_extraction": result["field_extraction"]}

    def run_clause_subgraph(state: ContractExtractionState) -> ContractExtractionState:
        """调用条款子图，并只把它拥有的结果写回主图。"""
        result = clause_subgraph.invoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
            }
        )
        return {"clause_extraction": result["clause_extraction"]}

    def run_summary_subgraph(state: ContractExtractionState) -> ContractExtractionState:
        """调用摘要子图，并只把它拥有的结果写回主图。"""
        result = summary_subgraph.invoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
            }
        )
        return {"summary_generation": result["summary_generation"]}

    graph = StateGraph(ContractExtractionState)
    graph.add_node("pdf_preprocessing", run_preprocessing_subgraph)
    graph.add_node("preheat", run_preheat_subgraph)
    graph.add_node("field_extraction", run_field_subgraph)
    graph.add_node("clause_extraction", run_clause_subgraph)
    graph.add_node("summary_generation", run_summary_subgraph)
    graph.add_node("merge_extraction_results", merge_extraction_results)

    graph.add_edge(START, "pdf_preprocessing")
    graph.add_edge("pdf_preprocessing", "preheat")
    graph.add_edge("preheat", "field_extraction")
    graph.add_edge("preheat", "clause_extraction")
    graph.add_edge("preheat", "summary_generation")
    graph.add_edge(
        ["field_extraction", "clause_extraction", "summary_generation"],
        "merge_extraction_results",
    )
    graph.add_edge("merge_extraction_results", END)
    return graph.compile()
