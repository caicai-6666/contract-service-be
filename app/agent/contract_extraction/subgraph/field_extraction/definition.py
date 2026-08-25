"""模型可读动态提取对象的机器契约。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


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


__all__ = [
    "FieldCardinality",
    "FieldDefinition",
    "FieldPropertyDefinition",
    "FieldValueType",
]
