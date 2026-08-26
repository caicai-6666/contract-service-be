"""最终公共前缀的预热任务提示词。"""

from __future__ import annotations

from typing import Any, Iterable

from app.agent.contract_extraction.context import append_contract_task

CONTRACT_PREFILL_TASK = """这是最终下游公共前缀预热请求。请仅确认已读取 PDF、文档结构与分类结果；不要提取字段、条款或生成摘要。"""


def append_prefill_task(
    common_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在完整公共前缀之后追加预热专属任务。"""
    return append_contract_task(
        common_messages,
        task_suffix=CONTRACT_PREFILL_TASK,
    )
