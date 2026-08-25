"""下游公共前缀的稳定组装与预热任务。"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Iterable

from app.agent.contract_extraction.state import PDFPromptPage, PreparedPDFPage
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.state import (
    DocumentStructureMetadata,
)
from app.agent.contract_extraction.subgraph.preprocessing.prompt import (
    build_pdf_common_messages,
)

CONTRACT_PREFILL_PROMPT_VERSION = "contract-prefill-v1"

DOCUMENT_STRUCTURE_HEADER = """以下是预处理阶段确认的权威文档导航结构。后续任务必须结合原始 PDF 使用；结构用于定位，不得替代页面事实，也不得静默改写。"""

CONTRACT_PREFILL_TASK = """这是下游公共前缀预热请求。请仅确认已读取 PDF 与文档结构；不要提取字段、条款或生成摘要。"""


def serialize_document_structure(structure: DocumentStructureMetadata) -> str:
    """按 Pydantic 字段顺序生成确定、紧凑的结构 JSON。"""
    return json.dumps(
        structure.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_contract_prefill_messages(
    pages: Iterable[PreparedPDFPage],
    prompt_pages: Iterable[PDFPromptPage],
    structure: DocumentStructureMetadata,
) -> list[dict[str, Any]]:
    """复用 PDF 公共前缀，并在其后稳定追加权威文档结构。"""
    messages = build_pdf_common_messages(pages, prompt_pages)
    content = messages[-1]["content"]
    if not isinstance(content, list):
        raise TypeError("PDF 公共 user 消息必须使用内容块列表")
    content.append(
        {
            "type": "text",
            "text": (
                f"{DOCUMENT_STRUCTURE_HEADER}\n"
                f"文档结构：\n{serialize_document_structure(structure)}"
            ),
        }
    )
    return messages


def append_contract_task(
    common_messages: Iterable[dict[str, Any]],
    *,
    task_suffix: str,
) -> list[dict[str, Any]]:
    """复制公共前缀后追加任务，供预热和下游请求统一复用。"""
    if not task_suffix.strip():
        raise ValueError("task_suffix 不能为空")
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("下游公共前缀不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("下游公共 user 消息必须使用内容块列表")
    content.append(
        {
            "type": "text",
            "text": f"任务：\n{task_suffix.strip()}",
        }
    )
    return messages


def append_prefill_task(
    common_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在完整公共前缀之后追加预热专属任务。"""
    return append_contract_task(
        common_messages,
        task_suffix=CONTRACT_PREFILL_TASK,
    )


def contract_prefill_sha256(messages: Iterable[dict[str, Any]]) -> str:
    """计算 PDF 与结构公共前缀的确定性指纹。"""
    serialized = json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()
