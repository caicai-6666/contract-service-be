"""检索问题指南的严格对象契约。"""

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
GuideCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    ),
]
ContentFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


def _require_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """拒绝同一语义列表中的重复条目。"""
    if len(values) != len(set(values)):
        raise ValueError(f"{label}不能重复")
    return values


class RetrievalViewGuideModel(BaseModel):
    """指南目录使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class QuestionAttentionPoint(RetrievalViewGuideModel):
    """用于发现检索问题的一个法律与行业关注点。"""

    code: GuideCode
    name: NonEmptyText
    legal_significance: NonEmptyText
    practice_significance: NonEmptyText
    applicable_when: tuple[NonEmptyText, ...] = Field(min_length=1)
    inspect_for: tuple[NonEmptyText, ...] = Field(min_length=1)
    material_if_missing: tuple[NonEmptyText, ...] = Field(min_length=1)
    excludes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @field_validator(
        "applicable_when",
        "inspect_for",
        "material_if_missing",
        "excludes",
    )
    @classmethod
    def validate_unique_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """同一语义列表不得包含重复规则。"""
        return _require_unique(value, label="提问关注点的列表内容")


class CommonQuestionGuide(RetrievalViewGuideModel):
    """所有合同共享的提问选题指南。"""

    name: NonEmptyText
    purpose: NonEmptyText
    selection_rules: tuple[NonEmptyText, ...] = Field(min_length=1)
    question_rules: tuple[NonEmptyText, ...] = Field(min_length=1)
    attention_points: tuple[QuestionAttentionPoint, ...] = Field(min_length=1)

    @field_validator("selection_rules", "question_rules")
    @classmethod
    def validate_unique_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """通用规则列表不得包含重复文本。"""
        return _require_unique(value, label="提问指南规则")

    @model_validator(mode="after")
    def validate_attention_codes(self) -> CommonQuestionGuide:
        """通用关注点 code 必须唯一。"""
        _require_unique(
            tuple(point.code for point in self.attention_points),
            label="通用提问关注点 code",
        )
        return self


class CategoryQuestionGuide(RetrievalViewGuideModel):
    """一个合同领域命名空间下的提问补充指南。"""

    category_code: GuideCode
    category_name: NonEmptyText
    purpose: NonEmptyText
    selection_rules: tuple[NonEmptyText, ...] = Field(min_length=1)
    attention_points: tuple[QuestionAttentionPoint, ...] = Field(min_length=1)

    @field_validator("selection_rules")
    @classmethod
    def validate_unique_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """领域选题规则不得包含重复文本。"""
        return _require_unique(value, label="领域提问指南规则")

    @model_validator(mode="after")
    def validate_attention_codes(self) -> CategoryQuestionGuide:
        """同一领域内的关注点 code 必须唯一。"""
        _require_unique(
            tuple(point.code for point in self.attention_points),
            label="领域提问关注点 code",
        )
        return self


class QuestionGuideCatalog(RetrievalViewGuideModel):
    """通用提问指南和按 code 稳定排序的全部领域指南快照。"""

    root: Path
    common: CommonQuestionGuide
    categories: tuple[CategoryQuestionGuide, ...]
    content_sha256: ContentFingerprint

    @model_validator(mode="after")
    def validate_category_codes(self) -> QuestionGuideCatalog:
        """提问领域指南的 category_code 必须唯一。"""
        _require_unique(
            tuple(guide.category_code for guide in self.categories),
            label="提问领域指南 category_code",
        )
        return self

    def get_category(self, code: str) -> CategoryQuestionGuide:
        """按领域 code 获取提问指南，不存在时明确失败。"""
        for guide in self.categories:
            if guide.category_code == code:
                return guide
        raise KeyError(f"领域 {code} 尚未定义提问指南")


class RetrievalViewGuideCatalog(RetrievalViewGuideModel):
    """应用启动时加载的完整检索问题指南快照。"""

    root: Path
    question: QuestionGuideCatalog
    content_sha256: ContentFingerprint


__all__ = [
    "CategoryQuestionGuide",
    "CommonQuestionGuide",
    "QuestionAttentionPoint",
    "QuestionGuideCatalog",
    "RetrievalViewGuideCatalog",
]
