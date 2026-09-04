"""正式类别全部未命中时的一次性交易类型描述任务。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

UNMAPPED_TYPE_DESCRIPTION_PROMPT_VERSION = "unmapped-type-description-v4"

UNMAPPED_TYPE_DESCRIPTION_TASK = """程序已确认：当前合同不符合权威类别目录中的任何已定义类别。

当前任务是重新核查合同页面图像，并用一段简洁中文描述这份合同实际约定的交易类型。描述应直接说明一方主要提供、完成或让渡什么，另一方主要支付、返还或承担什么；不要只改写合同标题，不要创造类别 code，不要把结果命名为“其他”或“未分类”。

本次只调用 describe_unmapped_type 一次，按证据、简洁推理摘要、最终类型描述的顺序提交。"""


def append_unmapped_type_description_task(
    common_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用独立 user 消息追加未映射合同描述任务。"""
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("合同分类公共前缀不能为空")
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": UNMAPPED_TYPE_DESCRIPTION_TASK,
                }
            ],
        }
    )
    return messages


__all__ = [
    "UNMAPPED_TYPE_DESCRIPTION_PROMPT_VERSION",
    "append_unmapped_type_description_task",
]
