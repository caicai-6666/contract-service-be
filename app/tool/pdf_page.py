"""PDF 页面按视觉 token 预算进行等比渲染与分组。"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt
from pathlib import Path
from typing import Iterable

import pymupdf


@dataclass(frozen=True, slots=True)
class PDFPageRenderConfig:
    """控制 PDF 页面渲染分辨率与视觉 token 预算。"""

    max_render_scale: float = 2.0
    visual_token_patch_size: int = 32
    max_visual_tokens_per_page: int = 2048

    def __post_init__(self) -> None:
        if self.max_render_scale <= 0:
            raise ValueError("max_render_scale 必须大于 0")
        if self.visual_token_patch_size <= 0:
            raise ValueError("visual_token_patch_size 必须大于 0")
        if self.max_visual_tokens_per_page <= 0:
            raise ValueError("max_visual_tokens_per_page 必须大于 0")


@dataclass(frozen=True, slots=True)
class CompressedPDFPage:
    """已按预算渲染的一页 PDF。页码从 1 开始。"""

    page_number: int
    png_bytes: bytes
    width_pixels: int
    height_pixels: int
    render_scale: float
    visual_tokens: int


def estimate_visual_tokens(
    width_pixels: int,
    height_pixels: int,
    *,
    patch_size: int = 32,
) -> int:
    """按模型视觉 patch 近似估算页面占用的 token 数。"""
    if width_pixels <= 0 or height_pixels <= 0:
        raise ValueError("页面像素宽高必须大于 0")
    if patch_size <= 0:
        raise ValueError("patch_size 必须大于 0")
    return ceil(width_pixels / patch_size) * ceil(height_pixels / patch_size)


def calculate_render_scale(
    page: pymupdf.Page,
    config: PDFPageRenderConfig = PDFPageRenderConfig(),
) -> float:
    """计算不超过单页 token 预算的最大等比渲染比例。"""
    rect = page.rect
    if rect.width <= 0 or rect.height <= 0:
        raise ValueError("PDF 页面尺寸必须大于 0")

    scale = config.max_render_scale
    for _ in range(8):
        estimated_tokens = estimate_visual_tokens(
            ceil(rect.width * scale),
            ceil(rect.height * scale),
            patch_size=config.visual_token_patch_size,
        )
        if estimated_tokens <= config.max_visual_tokens_per_page:
            return scale
        scale *= sqrt(config.max_visual_tokens_per_page / estimated_tokens)

    return scale


def compress_pdf_page(
    pdf_path: Path | str,
    page_number: int,
    config: PDFPageRenderConfig = PDFPageRenderConfig(),
) -> CompressedPDFPage:
    """将指定 PDF 页渲染为预算内的 PNG 图像。"""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF 文件不存在：{path}")
    if page_number < 1:
        raise ValueError("page_number 从 1 开始")

    with pymupdf.open(path) as document:
        if page_number > document.page_count:
            raise IndexError(f"页面 {page_number} 超出 PDF 页数 {document.page_count}")
        page = document[page_number - 1]
        return _compress_open_pdf_page(page, page_number, config)


def _compress_open_pdf_page(
    page: pymupdf.Page,
    page_number: int,
    config: PDFPageRenderConfig,
) -> CompressedPDFPage:
    """渲染已打开文档中的页面，供单页和整份 PDF 处理复用。"""
    scale = calculate_render_scale(page, config)
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        alpha=False,
    )
    visual_tokens = estimate_visual_tokens(
        pixmap.width,
        pixmap.height,
        patch_size=config.visual_token_patch_size,
    )
    if visual_tokens > config.max_visual_tokens_per_page:
        raise RuntimeError("页面渲染结果超出视觉 token 预算")

    return CompressedPDFPage(
        page_number=page_number,
        png_bytes=pixmap.tobytes("png"),
        width_pixels=pixmap.width,
        height_pixels=pixmap.height,
        render_scale=scale,
        visual_tokens=visual_tokens,
    )


def compress_pdf_pages(
    pdf_path: Path | str,
    *,
    config: PDFPageRenderConfig = PDFPageRenderConfig(),
    page_numbers: Iterable[int] | None = None,
) -> tuple[CompressedPDFPage, ...]:
    """按原始页序渲染指定页面；未指定时渲染整份 PDF。"""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF 文件不存在：{path}")

    with pymupdf.open(path) as document:
        ordered_page_numbers = (
            tuple(range(1, document.page_count + 1))
            if page_numbers is None
            else tuple(page_numbers)
        )
        if tuple(sorted(ordered_page_numbers)) != ordered_page_numbers:
            raise ValueError("page_numbers 必须按原始页码升序传入")
        if len(set(ordered_page_numbers)) != len(ordered_page_numbers):
            raise ValueError("page_numbers 不能包含重复页码")
        if ordered_page_numbers and ordered_page_numbers[0] < 1:
            raise ValueError("page_numbers 从 1 开始")
        if ordered_page_numbers and ordered_page_numbers[-1] > document.page_count:
            raise IndexError(
                f"页面 {ordered_page_numbers[-1]} 超出 PDF 页数 {document.page_count}"
            )

        return tuple(
            _compress_open_pdf_page(
                document[page_number - 1],
                page_number,
                config,
            )
            for page_number in ordered_page_numbers
        )
