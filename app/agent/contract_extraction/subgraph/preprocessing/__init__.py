"""PDF 预处理子图及其私有节点。"""

from hashlib import sha256
from pathlib import Path

import pymupdf
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractExtractionRequest,
    PDFPromptContext,
    PDFPromptPage,
    PreparedPDF,
    PreparedPDFPage,
)
from app.agent.contract_extraction.subgraph.preprocessing.prompt import (
    PDF_READING_PROMPT_VERSION,
    build_pdf_page_descriptor,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.node import (
    discover_document_units,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.state import (
    DocumentStructureMetadata,
    UnitDiscoveryResult,
)
from app.core.config import get_settings
from app.tool.pdf_page import (
    PDFPageRenderConfig,
    compress_pdf_pages,
)


class PreprocessingSubgraphState(TypedDict, total=False):
    """PDF 预处理、结构发现及其对外结果的共享状态。"""

    request: ContractExtractionRequest
    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    unit_discovery: UnitDiscoveryResult
    document_structure: DocumentStructureMetadata


def _file_sha256(path: Path) -> str:
    """流式计算原始 PDF 的稳定文档标识，避免一次性读入大文件。"""
    digest = sha256()
    with path.open("rb") as pdf_file:
        for chunk in iter(lambda: pdf_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_pdf(path: Path) -> int:
    """校验 PDF 可读取、未加密且至少包含一页，并返回页数。"""
    if not path.is_file():
        raise FileNotFoundError(f"PDF 文件不存在：{path}")
    if path.stat().st_size <= 0:
        raise ValueError(f"PDF 文件为空：{path}")

    try:
        with pymupdf.open(path) as document:
            if not document.is_pdf:
                raise ValueError(f"输入文件不是 PDF：{path}")
            if document.needs_pass:
                raise ValueError(f"PDF 已加密且未提供密码：{path}")
            if document.page_count <= 0:
                raise ValueError(f"PDF 不包含可处理页面：{path}")
            return document.page_count
    except pymupdf.FileDataError as exc:
        raise ValueError(f"PDF 文件损坏或格式无效：{path}") from exc


def prepare_pdf(state: PreprocessingSubgraphState) -> PreprocessingSubgraphState:
    """检查 PDF，并按动态视觉预算完成整份文档的逐页等比渲染。"""
    path = state["request"].pdf_path
    page_count = _validate_pdf(path)
    mllm = get_settings().mllm
    vision = mllm.vision
    visual_tokens_per_page = mllm.visual_token_budget_per_page(page_count)
    visual_tokens_per_request = mllm.visual_token_budget(page_count)
    render_config = PDFPageRenderConfig(
        max_render_scale=vision.max_render_scale,
        visual_token_patch_size=vision.visual_token_patch_size,
        max_visual_tokens_per_page=visual_tokens_per_page,
    )

    compressed_pages = compress_pdf_pages(path, config=render_config)
    if len(compressed_pages) != page_count:
        raise RuntimeError("PDF 渲染页数与检查结果不一致")

    total_visual_tokens = sum(page.visual_tokens for page in compressed_pages)
    if total_visual_tokens > visual_tokens_per_request:
        raise RuntimeError("PDF 渲染结果超出动态视觉 token 预算")
    pages = tuple(
        PreparedPDFPage(
            page_number=page.page_number,
            png_bytes=page.png_bytes,
            width_pixels=page.width_pixels,
            height_pixels=page.height_pixels,
            render_scale=page.render_scale,
            visual_tokens=page.visual_tokens,
            content_sha256=sha256(page.png_bytes).hexdigest(),
            was_scaled=page.render_scale < vision.max_render_scale,
        )
        for page in compressed_pages
    )
    return {
        "prepared_pdf": PreparedPDF(
            document_id=_file_sha256(path),
            source_path=path,
            file_size_bytes=path.stat().st_size,
            page_count=page_count,
            total_visual_tokens=total_visual_tokens,
            visual_tokens_per_page_budget=visual_tokens_per_page,
            visual_tokens_per_request_budget=visual_tokens_per_request,
            pages=pages,
        )
    }


def build_pdf_prompt_context(
    state: PreprocessingSubgraphState,
) -> PreprocessingSubgraphState:
    """把每页图像尺寸转换为可复用的确定性提示词计划。"""
    prepared_pdf = state["prepared_pdf"]
    prompt_pages = tuple(
        PDFPromptPage(
            page_number=page.page_number,
            width_pixels=page.width_pixels,
            height_pixels=page.height_pixels,
            descriptor=build_pdf_page_descriptor(page),
        )
        for page in prepared_pdf.pages
    )
    return {
        "prompt_context": PDFPromptContext(
            prompt_version=PDF_READING_PROMPT_VERSION,
            pages=prompt_pages,
        )
    }


def build_preprocessing_subgraph():
    """装配“PDF 标准化 → 提示词上下文 → 结构发现”的预处理子图。"""
    graph = StateGraph(PreprocessingSubgraphState)
    graph.add_node("prepare_pdf", prepare_pdf)
    graph.add_node("build_pdf_prompt_context", build_pdf_prompt_context)
    graph.add_node("discover_document_units", discover_document_units)
    graph.add_edge(START, "prepare_pdf")
    graph.add_edge("prepare_pdf", "build_pdf_prompt_context")
    graph.add_edge("build_pdf_prompt_context", "discover_document_units")
    graph.add_edge("discover_document_units", END)
    return graph.compile()
