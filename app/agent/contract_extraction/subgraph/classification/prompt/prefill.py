"""合同分类公共前缀的预热任务提示词。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

CLASSIFICATION_PREFILL_TASK = """这是分类公共前缀预热请求，不对应任何具体目标类别。请仅确认已读取合同、文档结构与分类公共规则，不得形成类别判断。"""


def append_classification_prefill_task(
    common_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用独立 user 消息追加不会进入正式判定的预热后缀。"""
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("合同分类公共前缀不能为空")
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": CLASSIFICATION_PREFILL_TASK,
                }
            ],
        }
    )
    return messages


__all__ = ["append_classification_prefill_task"]
