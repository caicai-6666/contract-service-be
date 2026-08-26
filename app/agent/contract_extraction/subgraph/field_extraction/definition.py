"""模型可读动态提取对象的机器契约。"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)


class FieldValueType(StrEnum):
    """对象属性允许使用的 JSON 基本类型。"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class FieldCardinality(StrEnum):
    """提取对象在单份合同中允许的实例数。"""

    SINGLE = "single"
    MULTIPLE = "multiple"


class FieldDefinitionModel(BaseModel):
    """字段定义的共享严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldPropertyDefinition(FieldDefinitionModel):
    """提取对象中的一个扁平基本类型属性。"""

    name: str
    aliases: tuple[str, ...]
    type: FieldValueType
    required: bool
    meaning: str
    excludes: str

    @field_validator("name", "meaning", "excludes")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """拒绝无法提供语义约束的空文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("内容不能为空")
        return normalized

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """属性别名必须是一维且不重复的字符串序列。"""
        normalized = tuple(alias.strip() for alias in value)
        if any(not alias for alias in normalized):
            raise ValueError("属性别名不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("属性别名不能重复")
        return normalized


class FieldDefinition(FieldDefinitionModel):
    """一个 YAML 文件对应一种可单次或多次提取的扁平对象。"""

    name: str
    aliases: tuple[str, ...]
    meaning: str
    excludes: str
    cardinality: FieldCardinality
    properties: tuple[FieldPropertyDefinition, ...]

    @field_validator("name", "meaning", "excludes")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """拒绝无法提供语义约束的空文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("内容不能为空")
        return normalized

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """对象别名必须是一维且不重复的字符串序列。"""
        normalized = tuple(alias.strip() for alias in value)
        if any(not alias for alias in normalized):
            raise ValueError("对象别名不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("对象别名不能重复")
        return normalized

    @field_validator("properties")
    @classmethod
    def validate_properties(
        cls,
        value: tuple[FieldPropertyDefinition, ...],
    ) -> tuple[FieldPropertyDefinition, ...]:
        """对象至少有一个属性，且属性名称不得重复。"""
        if not value:
            raise ValueError("提取对象至少需要一个属性")
        names = [property_definition.name for property_definition in value]
        if len(names) != len(set(names)):
            raise ValueError("对象属性名称不能重复")
        return value

    @model_validator(mode="after")
    def validate_required_property(self) -> "FieldDefinition":
        """防止定义出没有任何必填事实的空对象。"""
        if not any(property_definition.required for property_definition in self.properties):
            raise ValueError("提取对象至少需要一个必填属性")
        return self


class FieldDefinitionCollection(FieldDefinitionModel):
    """同一职责目录中按文件名稳定排列的字段定义快照。"""

    kind: Literal["core", "attribute"]
    definitions: tuple[FieldDefinition, ...]
    content_sha256: str

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        """目录内容指纹必须是小写 SHA-256。"""
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("字段定义目录指纹必须是 64 位小写 SHA-256")
        return value

    @model_validator(mode="after")
    def validate_unique_names(self) -> "FieldDefinitionCollection":
        """同一职责目录内的对象名称必须唯一。"""
        names = [definition.name for definition in self.definitions]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.kind} 字段定义名称不能重复")
        return self

    def get(self, name: str) -> FieldDefinition:
        """按稳定名称返回一个字段定义，不存在时明确失败。"""
        for definition in self.definitions:
            if definition.name == name:
                return definition
        raise KeyError(f"未知 {self.kind} 字段定义：{name}")


class FieldDefinitionCatalog(FieldDefinitionModel):
    """应用启动时加载的完整 Core 与 Attribute 定义快照。"""

    root: Path
    core: FieldDefinitionCollection
    attribute: FieldDefinitionCollection
    content_sha256: str

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        """全目录内容指纹必须是小写 SHA-256。"""
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("字段定义总目录指纹必须是 64 位小写 SHA-256")
        return value

    @model_validator(mode="after")
    def validate_catalog(self) -> "FieldDefinitionCatalog":
        """Core 必须可用，且两类定义不得出现身份冲突。"""
        if self.core.kind != "core" or self.attribute.kind != "attribute":
            raise ValueError("字段定义集合的 kind 与目录职责不一致")
        if not self.core.definitions:
            raise ValueError("Core 字段定义不能为空")
        names = [
            definition.name
            for definition in (
                *self.core.definitions,
                *self.attribute.definitions,
            )
        ]
        if len(names) != len(set(names)):
            raise ValueError("Core 与 Attribute 字段定义名称不能重复")
        return self

    @property
    def definition_count(self) -> int:
        """返回当前内存快照中的字段定义总数。"""
        return len(self.core.definitions) + len(self.attribute.definitions)


__all__ = [
    "FieldCardinality",
    "FieldDefinition",
    "FieldDefinitionCatalog",
    "FieldDefinitionCollection",
    "FieldPropertyDefinition",
    "FieldValueType",
]
