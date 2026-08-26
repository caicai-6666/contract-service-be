"""合同分类并行判别共享的公共提示词。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

CLASSIFICATION_COMMON_PROMPT_VERSION = "classification-common-v4"

CLASSIFICATION_COMMON_HEADER = """以下规则供所有单类别合同判别任务共同使用。后续会另行提供当前唯一目标类别的权威定义及专家正反例；在看到这些类别专属资料后再作判断。"""

CLASSIFICATION_COMMON_TASK = """任务目标：
判断当前合同是否具备目标类别定义要求的核心权利义务结构。每个请求只判断一个目标类别；一份合同可以在不同请求中命中多个类别，同一复合交易在分别完整满足多个类别的核心结构时也允许多标签。

事实与规则优先级：
1. 原始 PDF 是唯一事实来源，预处理文档结构只用于定位，不得替代页面证据。
2. 只使用预处理文档结构中的单元页码、文字锚点和摘要辅助导航；忽略 unit_locations 坐标，不按坐标裁剪页面，也不输出视觉位置。导航信息不足时必须核查完整相关页面，不能据此作出否定分类。
3. 当前类别的 definition.yaml 是类别语义和边界的唯一权威定义。
4. positive 与 negative 专家卡片只用于校准典型情形和相邻边界，不得覆盖、扩展或修改权威定义，也不得把示例事实复制到当前合同。
5. 合同标题、类别别名、关键词、主体经营范围和普通附随条款只能用于发现候选，不能单独证明类别成立。

判别方法：
1. 先从合同证据识别双方实际形成的核心权利义务交换，不以合同标题代替交易结构。
2. 比较该交换是否满足 meaning、core_exchange、includes 和 evidence_hints.strong。
3. 检查是否落入 excludes，并使用 distinguish_from 判断目标类别自身是否成立及是否允许与相邻类别共同命中。相邻类别可能成立不能自动否定当前类别；只有当前类别的必要结构缺失，或 definition.yaml 明确规定该情形不成立时才能否定。
4. 将当前合同与正反例比较时，只使用决定类别边界的相同点和差异点。
5. 当前请求看不到其他类别的全部判断结果，不得决定主类别、次类别、并列关系或 unmapped 状态。

工具与输出协议：
1. 每轮必须且只能调用本轮提供的一个工具，禁止输出普通文本。
2. think 用于围绕上述判别方法比较合同证据、权威定义和正反例；它只进入当前类别的短期记忆，不提交正式决定，也不输出冗长思维草稿。
3. 合同中不存在满足当前定义的核心权利义务结构时，调用 not_belong_to_category。即使作否定决定，也要引用合同实际交易内容或相关页面事实，并解释为何不满足当前类别，不能只声称“未发现”。
4. 合同中存在满足当前定义的核心权利义务结构时，调用 belong_to_category，并在最后概括该合同中实际存在的交易场景。不得因为同一合同可能同时满足相邻类别，就拒绝一个已经被充分证据支持的目标类别。
5. 两个终止工具互斥。参数必须先提供可定位页面证据，再提供简洁推理摘要，最终由工具名称及 belong_to_category 的 decision 表达决定；最终决定不得引入证据和推理摘要没有支持的事实。
6. 页面证据只保留物理页码和足以核对判断的短内容，不输出坐标或其他视觉位置。
7. 工具返回 ok=false 时，按照反馈指出的参数位置、问题和修正方向重新调用。"""


def build_classification_common_messages(
    base_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """复制合同基础前缀，并在尾部追加字节稳定的分类公共规则。"""
    messages = deepcopy(list(base_messages))
    if not messages:
        raise ValueError("合同分类基础前缀不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("合同基础 user 消息必须使用内容块列表")
    content.append(
        {
            "type": "text",
            "text": (
                f"{CLASSIFICATION_COMMON_HEADER}\n\n"
                f"{CLASSIFICATION_COMMON_TASK}"
            ),
        }
    )
    return messages


__all__ = [
    "CLASSIFICATION_COMMON_PROMPT_VERSION",
    "build_classification_common_messages",
]
