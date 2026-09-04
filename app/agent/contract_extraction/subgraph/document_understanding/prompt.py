"""文档结构理解子图拥有的公共阅读前缀与消息构造器。"""

from __future__ import annotations

import json
from base64 import b64encode
from collections.abc import Iterable
from hashlib import sha256
from typing import Any

from app.agent.contract_extraction.state import PDFPromptPage, PreparedPDFPage

PDF_READING_PROMPT_VERSION = "contract-page-reading-v4"

PDF_READING_SYSTEM_PROMPT = """你是合同页面图像阅读基础层。输入页面图像是当前任务唯一的事实来源。

阅读规范：
1. 严格按照页码顺序阅读，不得跳页、重排或把不同页面的内容错误拼接。
2. 区分可直接观察的文字、表格、印章、签名与版式信息，不得使用合同外知识补全缺失内容。
3. 后续任务要求提取或判断时，先给出带页码的可核对证据，再给出简洁推理摘要，最后给出决定。
4. 遇到模糊、遮挡、缺页或冲突内容时必须保留不确定性，不得猜测。
5. 严格服从页面之后追加的任务与输出格式；任务后缀不得改变以上事实与证据规则。
"""

PDF_INPUT_HEADER = """以下是同一份合同按原始顺序排列的连续页面图像。每个物理页码标签后的图像只对应该页；请先完整阅读，再执行所有页面之后追加的任务。"""


def build_pdf_page_descriptor(page: PreparedPDFPage) -> str:
    """只向模型标记物理页码，不暴露内部渲染尺寸。"""
    return f"第 {page.page_number} 页"


def _image_content(page: PreparedPDFPage) -> dict[str, Any]:
    """将稳定 PNG 编码为带内容身份的 vLLM 多模态内容块。"""
    encoded = b64encode(page.png_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
        # UUID 只参与传输层多模态缓存，不会由 chat template 渲染给模型。
        "uuid": page.media_uuid,
    }


def build_pdf_content_blocks(
    pages: Iterable[PreparedPDFPage],
    prompt_pages: Iterable[PDFPromptPage],
    *,
    header: str = PDF_INPUT_HEADER,
) -> list[dict[str, Any]]:
    """构建一份文档的连续页面图像内容块，供公共前缀和附加页面复用。"""
    if not header.strip():
        raise ValueError("页面图像输入标题不能为空")

    ordered_pages = tuple(pages)
    ordered_prompt_pages = tuple(prompt_pages)
    if not ordered_pages:
        raise ValueError("页面图像内容至少需要一页")
    page_numbers = tuple(page.page_number for page in ordered_pages)
    if page_numbers != tuple(sorted(page_numbers)) or len(set(page_numbers)) != len(
        page_numbers
    ):
        raise ValueError("页面图像必须按原始物理页码严格升序传入")
    if len(ordered_pages) != len(ordered_prompt_pages):
        raise ValueError("页面图像与提示词页面数量不一致")

    for page, prompt_page in zip(ordered_pages, ordered_prompt_pages, strict=True):
        if (
            prompt_page.page_number != page.page_number
            or prompt_page.width_pixels != page.width_pixels
            or prompt_page.height_pixels != page.height_pixels
            or prompt_page.descriptor != build_pdf_page_descriptor(page)
        ):
            raise ValueError(f"第 {page.page_number} 页的提示词描述与页面事实不一致")

    content: list[dict[str, Any]] = [{"type": "text", "text": header.strip()}]
    for page, prompt_page in zip(ordered_pages, ordered_prompt_pages, strict=True):
        content.append({"type": "text", "text": prompt_page.descriptor})
        content.append(_image_content(page))
    return content


def build_pdf_common_messages(
    pages: Iterable[PreparedPDFPage],
    prompt_pages: Iterable[PDFPromptPage],
) -> list[dict[str, Any]]:
    """构建所有合同任务必须逐块复用的公共消息前缀。"""
    return [
        {"role": "system", "content": PDF_READING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_pdf_content_blocks(pages, prompt_pages),
        },
    ]


def build_pdf_messages(
    pages: Iterable[PreparedPDFPage],
    prompt_pages: Iterable[PDFPromptPage],
    *,
    task_suffix: str,
) -> list[dict[str, Any]]:
    """在稳定公共前缀之后追加唯一可变的节点任务。"""
    if not task_suffix.strip():
        raise ValueError("task_suffix 不能为空")
    messages = build_pdf_common_messages(pages, prompt_pages)
    messages[-1]["content"].append(
        {
            "type": "text",
            "text": f"任务：\n{task_suffix.strip()}",
        }
    )
    return messages


def pdf_common_prefix_sha256(
    pages: Iterable[PreparedPDFPage],
    prompt_pages: Iterable[PDFPromptPage],
) -> str:
    """计算应用层公共消息结构的确定性指纹。"""
    serialized = json.dumps(
        build_pdf_common_messages(pages, prompt_pages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()
