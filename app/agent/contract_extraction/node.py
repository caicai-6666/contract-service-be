"""合同信息抽取工作流的节点占位实现。"""

from app.agent.contract_extraction.state import (
    ContractExtractionResult,
    ContractExtractionState,
    WorkflowPlaceholder,
)


def extract_clause_placeholder(
    state: ContractExtractionState,
) -> ContractExtractionState:
    """预留合同条款抽取子图的首个节点。"""
    return {
        "clause_extraction": WorkflowPlaceholder(
            node="extract_clause",
            message="待接入条款边界识别、排序和证据提取。",
        )
    }


def generate_summary_placeholder(
    state: ContractExtractionState,
) -> ContractExtractionState:
    """预留合同摘要生成子图的首个节点。"""
    return {
        "summary_generation": WorkflowPlaceholder(
            node="generate_summary",
            message="待接入格式化摘要生成与向量检索文本约束。",
        )
    }


def merge_extraction_results(
    state: ContractExtractionState,
) -> ContractExtractionState:
    """合并结构理解与三个业务子图的结果，形成后续 OCR 结果包络。"""
    request = state["request"]
    return {
        "result": ContractExtractionResult(
            pdf_path=request.pdf_path,
            preheat=state["preheat"],
            document_structure=state["document_structure"],
            field_extraction=state["field_extraction"],
            clause_extraction=state["clause_extraction"],
            summary_generation=state["summary_generation"],
        )
    }
