"""合同信息抽取工作流的节点占位实现。"""

from app.agent.contract_extraction.context import (
    CONTRACT_BASE_CONTEXT_VERSION,
    CONTRACT_PREFILL_CONTEXT_VERSION,
    build_contract_base_messages,
    build_contract_prefill_messages,
    context_sha256,
)
from app.agent.contract_extraction.state import (
    ContractBaseContext,
    ContractExtractionResult,
    ContractExtractionState,
    ContractPrefillContext,
)
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)


def assemble_base_context(
    state: ContractExtractionState,
) -> ContractExtractionState:
    """组装供分类读取的“页面图像 + 权威文档结构”稳定基础前缀。"""
    prepared_pdf = state["prepared_pdf"]
    structure = state["document_structure"]
    if structure.document_id != prepared_pdf.document_id:
        raise ValueError("文档结构与 PreparedPDF 的 document_id 不一致")

    messages = build_contract_base_messages(
        prepared_pdf.pages,
        state["prompt_context"].pages,
        structure,
    )
    return {
        "base_context": ContractBaseContext(
            document_id=prepared_pdf.document_id,
            prompt_version=CONTRACT_BASE_CONTEXT_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


def assemble_prefill_context(
    state: ContractExtractionState,
) -> ContractExtractionState:
    """在基础前缀末尾追加分类结果，形成三个下游共享的最终前缀。"""
    base_context = state["base_context"]
    classification = state["classification"]
    if not isinstance(classification, ContractClassificationResult):
        raise TypeError(
            "classification 必须是 ContractClassificationResult，"
            "不能将占位对象或分类运行审计写入最终公共前缀"
        )
    if classification.document_id != base_context.document_id:
        raise ValueError("分类结果与基础前缀的 document_id 不一致")

    messages = build_contract_prefill_messages(
        base_context.messages,
        classification,
    )
    return {
        "prefill_context": ContractPrefillContext(
            document_id=base_context.document_id,
            prompt_version=CONTRACT_PREFILL_CONTEXT_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


def merge_extraction_results(
    state: ContractExtractionState,
) -> ContractExtractionState:
    """合并结构理解与三个业务子图的结果，形成后续 OCR 结果包络。"""
    prepared_pdf = state["prepared_pdf"]
    return {
        "result": ContractExtractionResult(
            pdf_path=prepared_pdf.source_path,
            classification=state["classification"],
            suggested_file_name=state["suggested_file_name"],
            document_structure=state["document_structure"],
            field_extraction=state["field_extraction"],
            clause_extraction=state["clause_extraction"],
            retrieval_questions=state["retrieval_questions"],
            retrieval_question_embeddings=state["retrieval_question_embeddings"],
            contract_retrieval_vector=state["contract_retrieval_vector"],
        )
    }
