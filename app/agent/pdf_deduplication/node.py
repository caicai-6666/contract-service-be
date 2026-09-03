"""PDF 查重工作流节点。"""

from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import Sequence
from time import monotonic

from elasticsearch import AsyncElasticsearch

from app.agent.pdf_deduplication.error import (
    PDFDeduplicationNodeNotImplementedError,
)
from app.agent.pdf_deduplication.prompt import (
    PDF_PAGE_EMBEDDING_INPUT_VERSION,
    build_pdf_page_embedding_messages,
)
from app.agent.pdf_deduplication.port import PDFDuplicateCandidateLoader
from app.agent.pdf_deduplication.state import (
    ExactDocumentDuplicateCandidate,
    FailedPDFCandidateJudgment,
    PDFCandidateJudgment,
    PDFDeduplicationResult,
    PDFDeduplicationState,
    PDFDuplicateCandidate,
    PDFDuplicateCandidateSet,
    PDFPageFusionVector,
)
from app.agent.pdf_deduplication.subgraph.candidate_judgment import (
    build_candidate_judgment_subgraph,
)
from app.core.config import get_settings
from app.infrastructure.embedding import EmbeddingClient

PDF_PAGE_FUSION_VERSION = "tail-weighted-1.5-l2-v1"
PDF_PAGE_FUSION_TAIL_WEIGHT = 1.5
PDF_PAGE_FUSION_DEFAULT_WEIGHT = 1.0


async def vectorize_processed_pdf(
    state: PDFDeduplicationState,
) -> PDFDeduplicationState:
    """逐页向量化处理版 PDF，并融合为可入库的页面向量。"""
    started = monotonic()
    prepared = state["prepared_pdf"]
    settings = get_settings().embedding
    pages = tuple(sorted(prepared.pages, key=lambda page: page.page_number))
    source_page_numbers = tuple(page.page_number for page in pages)
    if len(pages) != prepared.page_count:
        raise ValueError("PreparedPDF 页面数量与 page_count 不一致")
    if source_page_numbers != tuple(range(1, prepared.page_count + 1)):
        raise ValueError("PreparedPDF 页面必须按从 1 开始的连续物理页码排列")

    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    async with EmbeddingClient(settings) as client:
        async def embed_page(page) -> tuple[int, str, tuple[float, ...]]:
            data_url = "data:image/png;base64," + base64.b64encode(
                page.png_bytes
            ).decode("ascii")
            async with semaphore:
                completion = await client.create_multimodal_embedding(
                    messages=build_pdf_page_embedding_messages(data_url)
                )
            vector = _normalize_vector(
                completion.vectors[0],
                expected_dimensions=settings.dimensions,
            )
            return (
                page.page_number,
                completion.model or settings.model,
                vector,
            )

        page_embeddings = await asyncio.gather(
            *(embed_page(page) for page in pages)
        )

    response_models = {model for _, model, _ in page_embeddings}
    if len(response_models) != 1:
        raise ValueError(f"页面 Embedding 响应模型不一致：{sorted(response_models)}")
    fused = _fuse_tail_weighted(
        tuple((page_number, vector) for page_number, _, vector in page_embeddings),
        expected_dimensions=settings.dimensions,
    )
    page_fusion_vector = PDFPageFusionVector(
        document_id=prepared.document_id,
        embedding_model=next(iter(response_models)),
        embedding_input_version=PDF_PAGE_EMBEDDING_INPUT_VERSION,
        fusion_version=PDF_PAGE_FUSION_VERSION,
        fusion_method="weighted_mean_l2_normalized",
        dimensions=settings.dimensions,
        normalized=True,
        source_page_numbers=source_page_numbers,
        vector=fused,
        elapsed_ms=(monotonic() - started) * 1000,
    )
    return {**state, "page_fusion_vector": page_fusion_vector}


def _normalize_vector(
    vector: Sequence[float],
    *,
    expected_dimensions: int,
) -> tuple[float, ...]:
    """校验固定维度和有限数值，再执行 L2 归一化。"""
    values = tuple(float(value) for value in vector)
    if len(values) != expected_dimensions:
        raise ValueError(
            "页面向量维度不符："
            f"expected={expected_dimensions}, actual={len(values)}"
        )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("页面向量包含非有限数值")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm == 0:
        raise ValueError("页面向量不能是零向量")
    return tuple(value / norm for value in values)


def _fuse_tail_weighted(
    page_embeddings: tuple[tuple[int, tuple[float, ...]], ...],
    *,
    expected_dimensions: int,
) -> tuple[float, ...]:
    """按物理尾页 1.5 倍权重融合全部已归一化页面向量。"""
    if not page_embeddings:
        raise ValueError("页面融合至少需要一个页面向量")
    tail_page_number = max(page_number for page_number, _ in page_embeddings)
    weighted_sum = [0.0] * expected_dimensions
    total_weight = 0.0
    for page_number, vector in sorted(page_embeddings):
        weight = (
            PDF_PAGE_FUSION_TAIL_WEIGHT
            if page_number == tail_page_number
            else PDF_PAGE_FUSION_DEFAULT_WEIGHT
        )
        total_weight += weight
        for index, value in enumerate(vector):
            weighted_sum[index] += weight * value
    averaged = tuple(value / total_weight for value in weighted_sum)
    return _normalize_vector(
        averaged,
        expected_dimensions=expected_dimensions,
    )


async def retrieve_duplicate_candidates(
    state: PDFDeduplicationState,
    *,
    client: AsyncElasticsearch,
    index_name: str,
) -> PDFDeduplicationState:
    """使用页面融合向量从正式 ES 索引召回最多三份候选。"""
    started = monotonic()
    fusion = state["page_fusion_vector"]
    minimum_similarity = (
        get_settings().pdf_deduplication.minimum_recall_cosine_similarity
    )
    response = await client.search(
        index=index_name,
        knn={
            "field": "vectors.page_fusion",
            "query_vector": list(fusion.vector),
            "k": 3,
            "num_candidates": 20,
            # 该参数使用 mapping 定义的原始 cosine，而不是变换后的 _score。
            # ES 会在近邻探索后排除低于门槛的结果，因此最终可以少于 3 条。
            "similarity": minimum_similarity,
        },
        source=["document_id", "file_name", "file_uri", "page_count"],
    )
    candidates = []
    for rank, hit in enumerate(response["hits"]["hits"], start=1):
        source = hit.get("_source", {})
        candidates.append(PDFDuplicateCandidate(
            rank=rank,
            document_id=source["document_id"],
            file_name=source["file_name"],
            file_uri=source["file_uri"],
            page_count=source["page_count"],
            score=float(hit["_score"]),
        ))
    candidate_set = PDFDuplicateCandidateSet(
        document_id=fusion.document_id,
        candidates=tuple(candidates),
        elapsed_ms=(monotonic() - started) * 1000,
    )
    return {**state, "duplicate_candidates": candidate_set}


async def judge_duplicate_candidates(
    state: PDFDeduplicationState,
    *,
    candidate_loader: PDFDuplicateCandidateLoader,
) -> PDFDeduplicationState:
    """加载并并发调度 Top 3 候选判重子图，汇总最终查重结果。"""
    started = monotonic()
    uploaded_pdf = state["prepared_pdf"]
    fusion = state["page_fusion_vector"]
    candidate_set = state["duplicate_candidates"]
    if uploaded_pdf.document_id != fusion.document_id:
        raise ValueError("上传 PDF 与页面融合向量 document_id 不一致")
    if uploaded_pdf.document_id != candidate_set.document_id:
        raise ValueError("上传 PDF 与候选集合 document_id 不一致")

    candidates = candidate_set.candidates
    if not candidates:
        result = PDFDeduplicationResult(
            status="unique",
            document_id=uploaded_pdf.document_id,
            page_fusion_vector=fusion,
            candidate_set=candidate_set,
            judgments=(),
            duplicate_document_ids=(),
            elapsed_ms=(
                fusion.elapsed_ms
                + candidate_set.elapsed_ms
                + (monotonic() - started) * 1000
            ),
        )
        return {**state, "result": result}

    has_non_exact_candidate = any(
        candidate.document_id != uploaded_pdf.document_id
        for candidate in candidates
    )
    candidate_subgraph = (
        build_candidate_judgment_subgraph()
        if has_non_exact_candidate
        else None
    )

    async def judge_candidate(
        candidate: PDFDuplicateCandidate,
    ) -> PDFCandidateJudgment:
        candidate_started = monotonic()
        if candidate.document_id == uploaded_pdf.document_id:
            # document_id 是处理版 PDF 字节的 SHA-256。身份完全一致已经是
            # 确定性重复结论，无需加载候选文件或消耗 MLLM 视觉上下文。
            return ExactDocumentDuplicateCandidate(
                candidate_document_id=candidate.document_id,
                rank=candidate.rank,
                rounds=0,
                elapsed_ms=(monotonic() - candidate_started) * 1000,
                reasoning_summary=(
                    "上传处理版 PDF 与已入库合同的 SHA-256 document_id "
                    "完全一致，属于同一份文件。"
                ),
            )
        try:
            assert candidate_subgraph is not None
            candidate_pdf = await candidate_loader.load(candidate)
            output = await candidate_subgraph.ainvoke(
                {
                    "uploaded_pdf": uploaded_pdf,
                    "candidate_pdf": candidate_pdf,
                    "candidate": candidate,
                }
            )
            judgment = output.get("judgment")
            if judgment is None:
                raise RuntimeError("逐候选判重子图未返回 judgment")
            if judgment.candidate_document_id != candidate.document_id:
                raise ValueError("逐候选判重结果 document_id 与候选不一致")
            if judgment.rank != candidate.rank:
                raise ValueError("逐候选判重结果 rank 与候选不一致")
            return judgment
        except Exception as exc:
            # 单候选失败不能取消兄弟候选；未调用模型时不伪造模型或提示词版本。
            return FailedPDFCandidateJudgment(
                candidate_document_id=candidate.document_id,
                rank=candidate.rank,
                rounds=0,
                elapsed_ms=(monotonic() - candidate_started) * 1000,
                error=str(exc) or type(exc).__name__,
            )

    judgments = tuple(
        await asyncio.gather(*(judge_candidate(candidate) for candidate in candidates))
    )
    duplicate_document_ids = tuple(
        judgment.candidate_document_id
        for judgment in judgments
        if judgment.status == "duplicate"
    )
    failed_count = sum(judgment.status == "failed" for judgment in judgments)
    if duplicate_document_ids:
        status = "duplicate"
        error = None
    elif failed_count:
        status = "failed"
        error = f"{failed_count} 个候选未形成可靠重复判断"
    else:
        status = "unique"
        error = None

    result = PDFDeduplicationResult(
        status=status,
        document_id=uploaded_pdf.document_id,
        page_fusion_vector=fusion,
        candidate_set=candidate_set,
        judgments=judgments,
        duplicate_document_ids=duplicate_document_ids,
        elapsed_ms=(
            fusion.elapsed_ms
            + candidate_set.elapsed_ms
            + (monotonic() - started) * 1000
        ),
        error=error,
    )
    return {**state, "result": result}


__all__ = [
    "PDFDeduplicationNodeNotImplementedError",
    "PDF_PAGE_FUSION_DEFAULT_WEIGHT",
    "PDF_PAGE_FUSION_TAIL_WEIGHT",
    "PDF_PAGE_FUSION_VERSION",
    "judge_duplicate_candidates",
    "retrieve_duplicate_candidates",
    "vectorize_processed_pdf",
]
