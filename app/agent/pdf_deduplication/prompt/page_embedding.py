"""Qwen3-VL-Embedding 合同 PDF 单页近重复检索输入契约。"""

from __future__ import annotations

from typing import Final, Literal

PDFPageEmbeddingInputVersion = Literal["contract-near-duplicate-v1"]

PDF_PAGE_EMBEDDING_INPUT_VERSION: Final[PDFPageEmbeddingInputVersion] = (
    "contract-near-duplicate-v1"
)

PDF_PAGE_EMBEDDING_SYSTEM_INSTRUCTION: Final = (
    "为合同 PDF 近重复检索表示此页面。重点保留可见文字、数字、表格、版式、"
    "页面结构、页眉页脚、印章与签名；忽略压缩、缩放、渲染差异和轻微扫描"
    "噪声，但保留合同主体、金额、日期、条款、页码及签章等实质差异。"
)


def build_pdf_page_embedding_messages(
    image_data_url: str,
) -> list[dict[str, object]]:
    """构造稳定的单页对称编码消息，不注入页面外文本或运行时元数据。"""
    if not image_data_url.startswith("data:image/png;base64,"):
        raise ValueError("PDF 页面向量化只接受 PNG data URL")
    if image_data_url == "data:image/png;base64,":
        raise ValueError("PDF 页面 PNG data URL 不能为空")
    return [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": PDF_PAGE_EMBEDDING_SYSTEM_INSTRUCTION,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url},
                },
                # 空文本保持 vLLM Qwen3-VL-Embedding 官方单图消息形状；
                # 不加入 OCR、文件名或物理页码，避免同源版本因外部元数据漂移。
                {"type": "text", "text": ""},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
        },
    ]


__all__ = [
    "PDFPageEmbeddingInputVersion",
    "PDF_PAGE_EMBEDDING_INPUT_VERSION",
    "PDF_PAGE_EMBEDDING_SYSTEM_INSTRUCTION",
    "build_pdf_page_embedding_messages",
]
