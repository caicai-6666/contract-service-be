"""在进入 Agent 工作流前异步检查并渲染合同 PDF。"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import pymupdf

from app.agent.contract_extraction.state import (
    ContractExtractionRequest,
    PreparedPDF,
    PreparedPDFPage,
)
from app.core.config import MLLMSettings
from app.tool.pdf_page import (
    PDFPageRenderConfig,
    assemble_pdf_pages,
    compress_pdf,
    serialized_pdf_operation,
)

PDFSource = Path | bytes


class PDFPreparationError(ValueError):
    """上传的 PDF 无法形成可供工作流读取的标准页面。"""


class PDFPreparationService(Protocol):
    """创建合同任务前必须完成的异步 PDF 准备端口。"""

    async def prepare(self, request: ContractExtractionRequest) -> PreparedPDF:
        """检查并渲染 PDF，返回可直接传入工作流的不可变页面。"""


class AsyncPDFPreparationService:
    """在线程中执行 PyMuPDF，同步算法不会阻塞 API 事件循环。"""

    def __init__(self, settings: MLLMSettings) -> None:
        self._settings = settings

    async def prepare(self, request: ContractExtractionRequest) -> PreparedPDF:
        """异步准备 PDF，并把可归因于输入文件的错误统一为服务异常。"""
        try:
            return await asyncio.to_thread(self._prepare_sync, request)
        except PDFPreparationError:
            raise
        except (FileNotFoundError, ValueError) as exc:
            raise PDFPreparationError(str(exc)) from exc

    @serialized_pdf_operation
    def _prepare_sync(self, request: ContractExtractionRequest) -> PreparedPDF:
        """按 MLLM 视觉预算完成确定性的同步 PDF 检查与渲染。"""
        source = request.pdf_source
        page_count = _validate_pdf(source, request.source_name)
        vision = self._settings.vision
        visual_tokens_per_page = self._settings.visual_token_budget_per_page(
            page_count
        )
        visual_tokens_per_request = self._settings.visual_token_budget(page_count)
        render_config = PDFPageRenderConfig(
            max_render_scale=vision.max_render_scale,
            visual_token_patch_size=vision.visual_token_patch_size,
            max_visual_tokens_per_page=visual_tokens_per_page,
        )

        compressed_pdf = compress_pdf(source, config=render_config)
        compressed_pages = compressed_pdf.pages
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
                width_points=page.width_points,
                height_points=page.height_points,
                render_scale=page.render_scale,
                visual_tokens=page.visual_tokens,
                content_sha256=sha256(page.png_bytes).hexdigest(),
                was_scaled=page.render_scale < vision.max_render_scale,
            )
            for page in compressed_pages
        )
        return PreparedPDF(
            document_id=sha256(compressed_pdf.pdf_bytes).hexdigest(),
            source_path=request.source_path,
            source_file_size_bytes=_source_size(source),
            processed_file_size_bytes=len(compressed_pdf.pdf_bytes),
            page_count=page_count,
            total_visual_tokens=total_visual_tokens,
            visual_tokens_per_page_budget=visual_tokens_per_page,
            visual_tokens_per_request_budget=visual_tokens_per_request,
            pages=pages,
        )


async def assemble_processed_pdf(prepared: PreparedPDF) -> bytes:
    """按需组装上传任务的 PDF，并在交付预览/入库前验证身份未发生变化。

    候选恢复的页面可能按新预算重新渲染，不能用此接口替代磁盘候选读取。
    """
    content = await asyncio.to_thread(assemble_pdf_pages, prepared.pages)
    if (
        len(content) != prepared.processed_file_size_bytes
        or sha256(content).hexdigest() != prepared.document_id
    ):
        raise RuntimeError("按需组装的处理版 PDF 与创建时的文件身份不一致")
    return content


def _source_size(source: PDFSource) -> int:
    """返回 PDF 来源字节数，并明确拒绝缺失路径。"""
    if isinstance(source, bytes):
        return len(source)
    if not source.is_file():
        raise FileNotFoundError(f"PDF 文件不存在：{source}")
    return source.stat().st_size


def _validate_pdf(source: PDFSource, source_name: str) -> int:
    """校验 PDF 可读取、未加密且至少包含一页，并返回页数。"""
    if _source_size(source) <= 0:
        raise ValueError(f"PDF 文件为空：{source_name}")

    try:
        document = (
            pymupdf.open(stream=source, filetype="pdf")
            if isinstance(source, bytes)
            else pymupdf.open(source)
        )
        with document:
            if not document.is_pdf:
                raise ValueError(f"输入文件不是 PDF：{source_name}")
            if document.needs_pass:
                raise ValueError(f"PDF 已加密且未提供密码：{source_name}")
            if document.page_count <= 0:
                raise ValueError(f"PDF 不包含可处理页面：{source_name}")
            return document.page_count
    except pymupdf.FileDataError as exc:
        raise ValueError(f"PDF 文件损坏或格式无效：{source_name}") from exc


__all__ = [
    "AsyncPDFPreparationService",
    "PDFPreparationError",
    "PDFPreparationService",
    "assemble_processed_pdf",
]
