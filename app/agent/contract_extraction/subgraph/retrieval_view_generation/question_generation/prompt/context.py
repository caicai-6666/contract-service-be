"""问题提出指南的确定性上下文组装。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    RetrievalViewGuideCatalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.prompt import (
    render_question_guides,
)

QUESTION_GENERATION_CONTEXT_VERSION = "retrieval-question-context-v3"


def build_question_generation_messages(
    prefill_messages: Iterable[dict[str, Any]],
    *,
    guide_catalog: RetrievalViewGuideCatalog,
) -> list[dict[str, Any]]:
    """在合同公共前缀尾部追加稳定提问指南，不暴露后台数量限制。"""
    messages = deepcopy(list(prefill_messages))
    if not messages:
        raise ValueError("问题提出的合同公共前缀不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("问题提出的合同公共 user 消息必须使用内容块列表")

    content.append(
        {
            "type": "text",
            "text": render_question_guides(guide_catalog),
        }
    )
    return messages
