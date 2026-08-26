"""合同类别定义与专家示例的严格对象契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
CategoryCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    ),
]


class ContractCategoryModel(BaseModel):
    """类别目录使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CoreExchange(ContractCategoryModel):
    """一个类别成立时双方必须形成的核心义务交换。"""

    provider_obligation: NonEmptyText
    counterparty_obligation: NonEmptyText


class CategoryDistinction(ContractCategoryModel):
    """当前类别与一个相邻类别的决定性边界。"""

    category: CategoryCode
    rule: NonEmptyText


class CategoryEvidenceHints(ContractCategoryModel):
    """类别判断的强证据与不足证据提示。"""

    strong: tuple[NonEmptyText, ...] = Field(min_length=1)
    insufficient: tuple[NonEmptyText, ...] = Field(min_length=1)

    @field_validator("strong", "insufficient")
    @classmethod
    def validate_unique_hints(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """同一证据列表不允许重复内容。"""
        if len(value) != len(set(value)):
            raise ValueError("证据提示不能重复")
        return value


class ContractCategoryDefinition(ContractCategoryModel):
    """一个 `definition.yaml` 对应的权威合同类别定义。"""

    code: CategoryCode
    name: NonEmptyText
    aliases: tuple[NonEmptyText, ...] = Field(min_length=1)
    meaning: NonEmptyText
    core_exchange: CoreExchange
    includes: tuple[NonEmptyText, ...] = Field(min_length=1)
    excludes: tuple[NonEmptyText, ...] = Field(min_length=1)
    distinguish_from: tuple[CategoryDistinction, ...]
    evidence_hints: CategoryEvidenceHints

    @field_validator("aliases", "includes", "excludes")
    @classmethod
    def validate_unique_items(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """别名和边界条目在各自列表内必须唯一。"""
        if len(value) != len(set(value)):
            raise ValueError("列表内容不能重复")
        return value

    @model_validator(mode="after")
    def validate_distinctions(self) -> ContractCategoryDefinition:
        """相邻类别引用不得自引用或在同一定义内重复。"""
        categories = [item.category for item in self.distinguish_from]
        if self.code in categories:
            raise ValueError("distinguish_from 不能引用当前类别自身")
        if len(categories) != len(set(categories)):
            raise ValueError("distinguish_from.category 不能重复")
        return self


class ExpertExampleCard(ContractCategoryModel):
    """一个类别目录下的单张正例或反例专家卡片。"""

    scenario: NonEmptyText
    evidence: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=3)
    reasoning_summary: NonEmptyText

    @field_validator("evidence")
    @classmethod
    def validate_unique_evidence(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """同一卡片不重复注入相同证据。"""
        if len(value) != len(set(value)):
            raise ValueError("卡片证据不能重复")
        return value


class ContractCategory(ContractCategoryModel):
    """一个类别的权威定义和按文件名排序的正反例集合。"""

    definition: ContractCategoryDefinition
    positive_examples: tuple[ExpertExampleCard, ...] = Field(min_length=3)
    negative_examples: tuple[ExpertExampleCard, ...] = Field(min_length=3)


class ContractCategoryCatalog(ContractCategoryModel):
    """应用启动时加载的完整、确定性合同类别快照。"""

    root: Path
    categories: tuple[ContractCategory, ...] = Field(min_length=1)
    content_sha256: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]

    @model_validator(mode="after")
    def validate_unique_codes(self) -> ContractCategoryCatalog:
        """内存目录中的类别身份必须全局唯一。"""
        codes = [category.definition.code for category in self.categories]
        if len(codes) != len(set(codes)):
            raise ValueError("合同类别 code 不能重复")
        return self

    def get(self, code: str) -> ContractCategory:
        """按稳定 code 返回类别对象，不存在时明确失败。"""
        for category in self.categories:
            if category.definition.code == code:
                return category
        raise KeyError(f"未知合同类别：{code}")

    @property
    def positive_example_count(self) -> int:
        """返回当前快照的正例总数。"""
        return sum(len(category.positive_examples) for category in self.categories)

    @property
    def negative_example_count(self) -> int:
        """返回当前快照的反例总数。"""
        return sum(len(category.negative_examples) for category in self.categories)


__all__ = [
    "CategoryDistinction",
    "CategoryEvidenceHints",
    "ContractCategory",
    "ContractCategoryCatalog",
    "ContractCategoryDefinition",
    "CoreExchange",
    "ExpertExampleCard",
]
