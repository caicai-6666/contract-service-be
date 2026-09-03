"""从本地合同文件根目录加载 PDF 查重候选。"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import pymupdf

from app.agent.contract_extraction.state import PreparedPDF, PreparedPDFPage
from app.agent.pdf_deduplication.state import PDFDuplicateCandidate
from app.core.config import MLLMSettings
from app.tool.pdf_page import PDFPageRenderConfig, compress_pdf_pages

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT_FILE_ROOT = _PROJECT_ROOT / "data/contract"


class LocalPDFDuplicateCandidateLoadError(ValueError):
    """候选 URI、文件身份或处理版 PDF 内容不可信。"""


class LocalPDFDuplicateCandidateLoader:
    """把根相对 `file_uri` 解析到 `data/contract` 并恢复页面缓存。"""

    def __init__(
        self,
        settings: MLLMSettings,
        *,
        root: Path = DEFAULT_CONTRACT_FILE_ROOT,
    ) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"合同 PDF 根目录不存在：{resolved_root}")
        self._settings = settings
        self._root = resolved_root

    @property
    def root(self) -> Path:
        """返回加载器固定使用的本地合同文件根目录。"""
        return self._root

    async def load(self, candidate: PDFDuplicateCandidate) -> PreparedPDF:
        """在线程中读取并渲染候选处理版 PDF，不阻塞事件循环。"""
        return await asyncio.to_thread(self._load_sync, candidate)

    async def read_pdf_bytes(self, candidate: PDFDuplicateCandidate) -> bytes:
        """在线程中读取并校验候选 PDF，但不执行昂贵的页面渲染。"""
        return await asyncio.to_thread(self._read_pdf_bytes_sync, candidate)

    def _load_sync(self, candidate: PDFDuplicateCandidate) -> PreparedPDF:
        """校验 URI、哈希和页数后，从原文件字节恢复 PreparedPDF。"""
        path = self._resolve_candidate_path(candidate)
        pdf_bytes = self._read_validated_pdf_bytes(candidate, path)
        page_count = self._read_page_count(pdf_bytes, path.name)
        if page_count != candidate.page_count:
            raise LocalPDFDuplicateCandidateLoadError(
                "候选处理版 PDF 页数与 ES page_count 不一致："
                f"file={page_count}, es={candidate.page_count}"
            )

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
        try:
            rendered_pages = compress_pdf_pages(
                pdf_bytes,
                config=render_config,
            )
        except (ValueError, RuntimeError, pymupdf.FileDataError) as exc:
            raise LocalPDFDuplicateCandidateLoadError(
                f"候选处理版 PDF 页面渲染失败：{path.name}"
            ) from exc

        total_visual_tokens = sum(
            page.visual_tokens for page in rendered_pages
        )
        if total_visual_tokens > visual_tokens_per_request:
            raise LocalPDFDuplicateCandidateLoadError(
                "候选处理版 PDF 渲染结果超出当前 MLLM 视觉 token 预算"
            )
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
            for page in rendered_pages
        )
        return PreparedPDF(
            document_id=candidate.document_id,
            source_path=path,
            # 处理版字节原样进入状态；加载过程只恢复页面图像，不重新封装 PDF。
            processed_pdf_bytes=pdf_bytes,
            source_file_size_bytes=len(pdf_bytes),
            processed_file_size_bytes=len(pdf_bytes),
            page_count=page_count,
            total_visual_tokens=total_visual_tokens,
            visual_tokens_per_page_budget=visual_tokens_per_page,
            visual_tokens_per_request_budget=visual_tokens_per_request,
            pages=pages,
        )

    def _read_pdf_bytes_sync(
        self,
        candidate: PDFDuplicateCandidate,
    ) -> bytes:
        """读取文件并校验内容哈希，避免 URL 被替换为其他 PDF。"""
        path = self._resolve_candidate_path(candidate)
        return self._read_validated_pdf_bytes(candidate, path)

    @staticmethod
    def _read_validated_pdf_bytes(
        candidate: PDFDuplicateCandidate,
        path: Path,
    ) -> bytes:
        """读取已解析路径，并验证字节仍与召回身份一致。"""
        try:
            pdf_bytes = path.read_bytes()
        except OSError as exc:
            raise LocalPDFDuplicateCandidateLoadError(
                f"候选处理版 PDF 读取失败：{path.name}"
            ) from exc
        if not pdf_bytes:
            raise LocalPDFDuplicateCandidateLoadError(
                f"候选处理版 PDF 为空：{path.name}"
            )

        document_id = sha256(pdf_bytes).hexdigest()
        if document_id != candidate.document_id:
            raise LocalPDFDuplicateCandidateLoadError(
                "候选处理版 PDF 内容哈希与 ES document_id 不一致"
            )
        return pdf_bytes

    def _resolve_candidate_path(self, candidate: PDFDuplicateCandidate) -> Path:
        """只接受 `/<document_id>.pdf`，再安全拼接到合同根目录。"""
        parsed = urlsplit(candidate.file_uri)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise LocalPDFDuplicateCandidateLoadError(
                "本地候选 file_uri 不能包含协议、主机、查询参数或片段"
            )
        decoded_path = unquote(parsed.path)
        uri_path = PurePosixPath(decoded_path)
        expected_name = f"{candidate.document_id}.pdf"
        if (
            not decoded_path.startswith("/")
            or uri_path.parent != PurePosixPath("/")
            or uri_path.name != expected_name
        ):
            raise LocalPDFDuplicateCandidateLoadError(
                f"候选 file_uri 必须严格使用 /{expected_name}"
            )

        resolved_path = (self._root / uri_path.name).resolve()
        # resolve 会跟随符号链接；父目录不再等于合同根目录时必须拒绝。
        if resolved_path.parent != self._root:
            raise LocalPDFDuplicateCandidateLoadError(
                "候选 file_uri 解析结果超出合同 PDF 根目录"
            )
        if not resolved_path.is_file():
            raise LocalPDFDuplicateCandidateLoadError(
                f"候选处理版 PDF 不存在：{uri_path.name}"
            )
        return resolved_path

    @staticmethod
    def _read_page_count(pdf_bytes: bytes, file_name: str) -> int:
        """检查处理版 PDF 可读、未加密且至少包含一页。"""
        try:
            document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            with document:
                if not document.is_pdf:
                    raise LocalPDFDuplicateCandidateLoadError(
                        f"候选文件不是 PDF：{file_name}"
                    )
                if document.needs_pass:
                    raise LocalPDFDuplicateCandidateLoadError(
                        f"候选 PDF 已加密：{file_name}"
                    )
                if document.page_count <= 0:
                    raise LocalPDFDuplicateCandidateLoadError(
                        f"候选 PDF 不包含页面：{file_name}"
                    )
                return document.page_count
        except pymupdf.FileDataError as exc:
            raise LocalPDFDuplicateCandidateLoadError(
                f"候选处理版 PDF 损坏或格式无效：{file_name}"
            ) from exc


__all__ = [
    "DEFAULT_CONTRACT_FILE_ROOT",
    "LocalPDFDuplicateCandidateLoadError",
    "LocalPDFDuplicateCandidateLoader",
]
