"""单类别判别任务的权威定义与专家样例尾部。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Literal

import yaml

from app.agent.contract_extraction.subgraph.classification.definition import (
    ContractCategory,
    ContractCategoryDefinition,
    ExpertExampleCard,
)

CLASSIFICATION_CATEGORY_PROMPT_VERSION = "classification-category-v3"

CATEGORY_DEFINITION_BEGIN = "===== 当前目标类别权威定义：开始 ====="
CATEGORY_DEFINITION_END = "===== 当前目标类别权威定义：结束 ====="
POSITIVE_EXAMPLES_BEGIN = "===== 当前目标类别专家正例：开始 ====="
POSITIVE_EXAMPLES_END = "===== 当前目标类别专家正例：结束 ====="
NEGATIVE_EXAMPLES_BEGIN = "===== 当前目标类别专家反例：开始 ====="
NEGATIVE_EXAMPLES_END = "===== 当前目标类别专家反例：结束 ====="

_DEFINITION_COMMENTS = """# definition.yaml 字段注释：
# code：程序持有的稳定类别身份，不是判断证据。
# name：当前类别的标准中文名称。
# aliases：只用于识别常见叫法，单独命中别名不能证明类别成立。
# meaning：当前类别的权威正向语义。
# core_exchange：类别成立时双方必须形成的核心权利义务交换。
# core_exchange.provider_obligation：提供方的核心义务。
# core_exchange.counterparty_obligation：相对方的核心义务。
# includes：应纳入当前类别的典型交易结构。
# excludes：当前类别必要结构不成立的情形；其他类别也成立不能单独作为排除理由。
# distinguish_from：与相邻类别的边界列表，同时说明仅命中或允许共同命中的条件。
# distinguish_from.category：被比较的相邻类别 code。
# distinguish_from.rule：当前类别自身成立、与相邻类别区分或共同命中的规则。
# evidence_hints.strong：支持类别成立的强证据提示。
# evidence_hints.insufficient：不能单独支持类别成立的弱信号。"""

_EXAMPLE_COMMENTS = """# 专家示例卡片字段注释：
# scenario：经过脱敏的典型交易场景，只用于边界对比。
# evidence：支持该示例判断的简短事实，不能复制为当前合同证据。
# reasoning_summary：示例事实如何落到当前类别边界的简洁说明。"""


class _IndentedSafeDumper(yaml.SafeDumper):
    """让块序列与所属 YAML 字段保持两空格缩进。"""

    def increase_indent(
        self,
        flow: bool = False,
        indentless: bool = False,
    ) -> None:
        return super().increase_indent(flow, False)


def _dump_yaml(value: ContractCategoryDefinition | ExpertExampleCard) -> str:
    """按 Pydantic 字段顺序生成稳定、无截断的 YAML。"""
    return yaml.dump(
        value.model_dump(mode="json"),
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=100_000,
    ).strip()


def _render_definition(definition: ContractCategoryDefinition) -> str:
    """渲染带逐字段注释的完整权威类别定义。"""
    return "\n".join(
        (
            CATEGORY_DEFINITION_BEGIN,
            "```yaml",
            _DEFINITION_COMMENTS,
            _dump_yaml(definition),
            "```",
            CATEGORY_DEFINITION_END,
        )
    )


def _render_example(
    card: ExpertExampleCard,
    *,
    label: Literal["正例", "反例"],
    index: int,
    total: int,
) -> str:
    """渲染一张带字段注释和显式区块标签的专家卡片。"""
    return "\n".join(
        (
            f"{label}卡片 {index}/{total}",
            "```yaml",
            f"# 当前区块标签：{label}；卡片本身不重复保存 label。",
            _EXAMPLE_COMMENTS,
            _dump_yaml(card),
            "```",
        )
    )


def _render_examples(
    cards: tuple[ExpertExampleCard, ...],
    *,
    label: Literal["正例", "反例"],
    begin: str,
    end: str,
) -> str:
    """在成对边界标记之间按目录顺序渲染全部卡片。"""
    rendered_cards = [
        _render_example(
            card,
            label=label,
            index=index,
            total=len(cards),
        )
        for index, card in enumerate(cards, start=1)
    ]
    return "\n\n".join((begin, *rendered_cards, end))


def render_category_judgment_task(category: ContractCategory) -> str:
    """生成“完整定义 → 全部正例 → 全部反例”的单类别任务尾部。"""
    return "\n\n".join(
        (
            "以下资料只定义当前唯一目标类别。权威定义优先于专家示例；正反例只校准边界，不得替代当前合同取证。读取全部资料后，请判断当前合同是否属于该类别。",
            _render_definition(category.definition),
            _render_examples(
                category.positive_examples,
                label="正例",
                begin=POSITIVE_EXAMPLES_BEGIN,
                end=POSITIVE_EXAMPLES_END,
            ),
            _render_examples(
                category.negative_examples,
                label="反例",
                begin=NEGATIVE_EXAMPLES_BEGIN,
                end=NEGATIVE_EXAMPLES_END,
            ),
        )
    )


def build_category_judgment_messages(
    common_messages: Iterable[dict[str, Any]],
    category: ContractCategory,
) -> list[dict[str, Any]]:
    """复制公共前缀，并用独立 user 消息追加当前类别任务变量。"""
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("合同分类公共前缀不能为空")
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": render_category_judgment_task(category),
                }
            ],
        }
    )
    return messages


__all__ = [
    "CATEGORY_DEFINITION_BEGIN",
    "CATEGORY_DEFINITION_END",
    "CLASSIFICATION_CATEGORY_PROMPT_VERSION",
    "NEGATIVE_EXAMPLES_BEGIN",
    "NEGATIVE_EXAMPLES_END",
    "POSITIVE_EXAMPLES_BEGIN",
    "POSITIVE_EXAMPLES_END",
    "build_category_judgment_messages",
    "render_category_judgment_task",
]
