"""合同信息抽取的主图装配。"""

from langgraph.graph import END, START, StateGraph

from app.agent.contract_extraction.node import (
    assemble_base_context,
    assemble_prefill_context,
    merge_extraction_results,
)
from app.agent.contract_extraction.state import ContractExtractionState
from app.agent.contract_extraction.subgraph import (
    build_classification_subgraph,
    build_clause_extraction_subgraph,
    build_document_understanding_subgraph,
    build_field_extraction_subgraph,
    build_retrieval_view_generation_subgraph,
)


def build_contract_extraction_graph():
    """组装“文档理解 → 分类 → 并行业务任务 → 结果合并”。"""
    document_understanding_subgraph = build_document_understanding_subgraph()
    classification_subgraph = build_classification_subgraph()
    field_subgraph = build_field_extraction_subgraph()
    clause_subgraph = build_clause_extraction_subgraph()
    retrieval_question_subgraph = build_retrieval_view_generation_subgraph()

    async def run_document_understanding_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """读取已准备页面，并写回提示词上下文与权威文档结构。"""
        result = await document_understanding_subgraph.ainvoke(
            {"prepared_pdf": state["prepared_pdf"]}
        )
        return {
            "prepared_pdf": result["prepared_pdf"],
            "prompt_context": result["prompt_context"],
            "document_structure": result["document_structure"],
        }

    async def run_classification_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """异步调用分类子图，并写回它拥有的分类结果。"""
        result = await classification_subgraph.ainvoke(
            {
                "base_context": state["base_context"],
                "category_catalog": state["category_catalog"],
                "page_count": state["prepared_pdf"].page_count,
            }
        )
        return {"classification": result["classification"]}

    async def run_field_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """调用字段子图，并只把它拥有的结果写回主图。"""
        result = await field_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
                "field_definition_catalog": state["field_definition_catalog"],
            }
        )
        return {"field_extraction": result["field_extraction"]}

    async def run_clause_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """异步调用条款子图，并只把最终条款结果写回主图。"""
        result = await clause_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
            }
        )
        return {"clause_extraction": result["clause_extraction"]}

    async def run_retrieval_question_subgraph(
        state: ContractExtractionState,
    ) -> ContractExtractionState:
        """异步生成检索问题及独立向量，并把两项权威结果写回主图。"""
        result = await retrieval_question_subgraph.ainvoke(
            {
                "prepared_pdf": state["prepared_pdf"],
                "document_structure": state["document_structure"],
                "prefill_context": state["prefill_context"],
                "retrieval_view_guide_catalog": state["retrieval_view_guide_catalog"],
            }
        )
        return {
            "retrieval_questions": result["retrieval_questions"],
            "retrieval_question_embeddings": result["retrieval_question_embeddings"],
            "contract_retrieval_vector": result["contract_retrieval_vector"],
        }

    graph = StateGraph(ContractExtractionState)
    graph.add_node(
        "document_understanding",
        run_document_understanding_subgraph,
    )
    graph.add_node("assemble_base_context", assemble_base_context)
    graph.add_node("classification", run_classification_subgraph)
    graph.add_node("assemble_prefill_context", assemble_prefill_context)
    graph.add_node("field_extraction", run_field_subgraph)
    graph.add_node("clause_extraction", run_clause_subgraph)
    graph.add_node(
        "retrieval_question_generation",
        run_retrieval_question_subgraph,
    )
    graph.add_node("merge_extraction_results", merge_extraction_results)

    graph.add_edge(START, "document_understanding")
    graph.add_edge("document_understanding", "assemble_base_context")
    graph.add_edge("assemble_base_context", "classification")
    graph.add_edge("classification", "assemble_prefill_context")
    graph.add_edge("assemble_prefill_context", "field_extraction")
    graph.add_edge("assemble_prefill_context", "clause_extraction")
    graph.add_edge("assemble_prefill_context", "retrieval_question_generation")
    graph.add_edge(
        [
            "field_extraction",
            "clause_extraction",
            "retrieval_question_generation",
        ],
        "merge_extraction_results",
    )
    graph.add_edge("merge_extraction_results", END)
    return graph.compile()
