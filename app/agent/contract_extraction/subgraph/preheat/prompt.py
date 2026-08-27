"""最终公共前缀的预热任务提示词。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.agent.contract_extraction.context import append_contract_task

CONTRACT_PREFILL_TASK = """你已获得当前合同的完整 PDF、权威文档导航结构和已确认分类结果。当前没有指定字段、条款或摘要任务；请先读取这些材料，并准备在收到具体任务后严格依据原始 PDF 作答。现在不要提取字段、条款或生成摘要。"""


def append_prefill_task(
    common_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在完整公共前缀之后追加预热专属任务。"""
    return append_contract_task(
        common_messages,
        task_suffix=CONTRACT_PREFILL_TASK,
    )
