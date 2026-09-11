"""从内存检查 PDF 能否打开；不加载页面内容、不渲染、不持久化。"""

from typing import Literal

import pymupdf

from app.tool.pdf_page import serialized_pdf_operation


PDFOpenErrorCode = Literal['empty_file', 'invalid_pdf', 'not_pdf', 'password_required', 'no_pages']


class PDFOpenError(ValueError):
    """可归因于上传文件的打开失败；不向用户泄露底层解析异常。"""

    def __init__(self, code: PDFOpenErrorCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@serialized_pdf_operation
def inspect_pdf_openable(content: bytes) -> int:
    """校验真实格式、密码要求和页数，返回页数；调用方负责逐份顺序检查。"""
    if not isinstance(content, bytes):
        raise TypeError('PDF 检查只接受内存 bytes')
    if not content:
        raise PDFOpenError('empty_file', '文件内容为空')
    try:
        # 自动识别真实内容，不以扩展名或强制 PDF 类型掩盖其他可打开的格式。
        with pymupdf.open(stream=content) as document:
            if not document.is_pdf:
                raise PDFOpenError('not_pdf', '文件实际格式不是 PDF')
            # 仅检查打开密码；不因为限制复制、打印就拒绝可正常打开的文件。
            if document.needs_pass:
                raise PDFOpenError('password_required', 'PDF 需要打开密码，当前未提供密码')
            page_count = document.page_count
            if page_count <= 0:
                raise PDFOpenError('no_pages', 'PDF 不包含任何页面')
            return page_count
    except PDFOpenError:
        raise
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise PDFOpenError('invalid_pdf', 'PDF 无法解析，文件可能损坏或格式无效') from exc


__all__ = ['PDFOpenError', 'PDFOpenErrorCode', 'inspect_pdf_openable']
