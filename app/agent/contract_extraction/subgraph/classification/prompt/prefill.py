"""合同分类公共前缀的预热任务提示词。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

CLASSIFICATION_PREFILL_TASK = """你已获得当前合同、文档导航结构和单类别判别通用规则，但尚未获得任何目标类别定义或专家示例。请先读取已有材料，并准备在收到唯一目标类别资料后进行判断；现在不得形成类别结论。"""


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
