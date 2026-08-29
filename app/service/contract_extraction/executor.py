"""将现有 Agent 子图适配为可独立重试的应用执行端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from app.agent.contract_extraction.node import (
    assemble_base_context,
    assemble_prefill_context,
)
from app.agent.contract_extraction.state import (
    ContractBaseContext,
    ContractExtractionRequest,
    ContractPrefillContext,
    FieldExtractionResult,
    PDFPromptContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph import (
    build_classification_subgraph,
    build_clause_extraction_subgraph,
    build_field_extraction_subgraph,
    build_preprocessing_subgraph,
    build_retrieval_view_generation_subgraph,
)
from app.agent.contract_extraction.subgraph.classification.definition import (
    ContractCategoryCatalog,
)
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)
from app.agent.contract_extraction.subgraph.clause_extraction.state import (
    ClauseExtractionResult,
)
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldDefinitionCatalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    RetrievalViewGuideCatalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.state import (
    ContractRetrievalVectorResult,
    RetrievalQuestionEmbeddingResult,
    RetrievalQuestionGenerationResult,
)

PreprocessingUpdateCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreprocessingOutput:
    """预处理完成后可被分类和全部业务分支复用的权威结果。"""

    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    document_structure: BaseModel
    unit_discovery_audit: BaseModel | None = None
    unit_grounding_audit: BaseModel | None = None


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    """分类完成后可被三个并行分支安全共享的不可变输入。"""

    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    document_structure: BaseModel
    base_context: ContractBaseContext
    classification: ContractClassificationResult
    prefill_context: ContractPrefillContext
    classification_audit: BaseModel | None = None
    unit_discovery_audit: BaseModel | None = None
    unit_grounding_audit: BaseModel | None = None


@dataclass(frozen=True, slots=True)
class RetrievalViewOutput:
    """检索问题、逐问题向量和合同融合向量的完整内部结果。"""

    questions: RetrievalQuestionGenerationResult
    embeddings: RetrievalQuestionEmbeddingResult
    vector: ContractRetrievalVectorResult


class ContractExtractionExecutor(Protocol):
    """供内存任务服务依赖的最小异步执行端口。"""

    async def preprocess(
        self,
        request: ContractExtractionRequest,
        on_update: PreprocessingUpdateCallback,
    ) -> PreprocessingOutput:
        """预处理内存 PDF，并在节点完成时报告内部节点名。"""

    async def classify(self, output: PreprocessingOutput) -> ExtractionContext:
        """分类合同并构造下游公共上下文。"""

    async def extract_core(self, context: ExtractionContext) -> BaseModel:
        """独立提取 Core。"""

    async def extract_clause(self, context: ExtractionContext) -> ClauseExtractionResult:
        """独立提取条款。"""

    async def prepare_retrieval_view(
        self,
        context: ExtractionContext,
    ) -> RetrievalViewOutput:
        """独立生成问题并形成合同检索向量。"""


class AgentContractExtractionExecutor:
    """基于现有 LangGraph 子图的生产执行器。"""

    def __init__(
        self,
        *,
        category_catalog: ContractCategoryCatalog,
        field_catalog: FieldDefinitionCatalog,
        retrieval_guide_catalog: RetrievalViewGuideCatalog,
    ) -> None:
        self._category_catalog = category_catalog
        self._field_catalog = field_catalog
        self._retrieval_guide_catalog = retrieval_guide_catalog
        self._preprocessing_graph = build_preprocessing_subgraph()
        self._classification_graph = build_classification_subgraph()
        self._field_graph = build_field_extraction_subgraph()
        self._clause_graph = build_clause_extraction_subgraph()
        self._retrieval_graph = build_retrieval_view_generation_subgraph()

    async def preprocess(
        self,
        request: ContractExtractionRequest,
        on_update: PreprocessingUpdateCallback,
    ) -> PreprocessingOutput:
        """流式执行预处理，以便把阅读和结构理解映射为两个用户阶段。"""
        state: dict[str, Any] = {"request": request}
        async for update in self._preprocessing_graph.astream(
            state,
            stream_mode="updates",
        ):
            for node_name, values in update.items():
                if values:
                    state.update(values)
                await on_update(node_name, values or {})

        return PreprocessingOutput(
            prepared_pdf=state["prepared_pdf"],
            prompt_context=state["prompt_context"],
            document_structure=state["document_structure"],
            unit_discovery_audit=state.get("unit_discovery"),
            unit_grounding_audit=state.get("unit_grounding"),
        )

    async def classify(self, output: PreprocessingOutput) -> ExtractionContext:
        """执行分类并组装业务分支共享的最终前缀。"""
        base_state = {
            "prepared_pdf": output.prepared_pdf,
            "prompt_context": output.prompt_context,
            "document_structure": output.document_structure,
        }
        base_context = assemble_base_context(base_state)["base_context"]
        result = await self._classification_graph.ainvoke(
            {
                "base_context": base_context,
                "category_catalog": self._category_catalog,
                "page_count": output.prepared_pdf.page_count,
            }
        )
        classification = result["classification"]
        if classification.status == "failed":
            raise RuntimeError("合同分类没有形成可供后续使用的结果")

        prefill_context = assemble_prefill_context(
            {
                "base_context": base_context,
                "classification": classification,
            }
        )["prefill_context"]
        return ExtractionContext(
            prepared_pdf=output.prepared_pdf,
            prompt_context=output.prompt_context,
            document_structure=output.document_structure,
            base_context=base_context,
            classification=classification,
            prefill_context=prefill_context,
            classification_audit=result.get("classification_run"),
            unit_discovery_audit=output.unit_discovery_audit,
            unit_grounding_audit=output.unit_grounding_audit,
        )

    async def extract_core(self, context: ExtractionContext) -> BaseModel:
        """运行 Core 字段子图并返回其正式结果。"""
        result = await self._field_graph.ainvoke(
            {
                "prepared_pdf": context.prepared_pdf,
                "document_structure": context.document_structure,
                "prefill_context": context.prefill_context,
                "field_definition_catalog": self._field_catalog,
            }
        )
        field_extraction: FieldExtractionResult = result["field_extraction"]
        return field_extraction.core

    async def extract_clause(
        self,
        context: ExtractionContext,
    ) -> ClauseExtractionResult:
        """运行条款发现与正文提取子图。"""
        result = await self._clause_graph.ainvoke(
            {
                "prepared_pdf": context.prepared_pdf,
                "document_structure": context.document_structure,
                "prefill_context": context.prefill_context,
            }
        )
        return result["clause_extraction"]

    async def prepare_retrieval_view(
        self,
        context: ExtractionContext,
    ) -> RetrievalViewOutput:
        """运行检索问题生成、向量化与融合子图。"""
        result = await self._retrieval_graph.ainvoke(
            {
                "prepared_pdf": context.prepared_pdf,
                "document_structure": context.document_structure,
                "prefill_context": context.prefill_context,
                "retrieval_view_guide_catalog": self._retrieval_guide_catalog,
            }
        )
        return RetrievalViewOutput(
            questions=result["retrieval_questions"],
            embeddings=result["retrieval_question_embeddings"],
            vector=result["contract_retrieval_vector"],
        )


__all__ = [
    "AgentContractExtractionExecutor",
    "ContractExtractionExecutor",
    "ExtractionContext",
    "PreprocessingOutput",
    "PreprocessingUpdateCallback",
    "RetrievalViewOutput",
]
