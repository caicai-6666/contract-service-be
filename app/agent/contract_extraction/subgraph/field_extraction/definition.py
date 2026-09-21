"""Core 动态提取对象及索引元数据的机器契约。"""

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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


class FieldEnumOption(FieldDefinitionModel):
    """机器枚举值与审核界面的展示标签。"""

    value: str = Field(min_length=1, description="允许提交的标准字符串值。")
    label: str = Field(min_length=1, description="该标准值的中文展示名称。")


class FieldConstraints(FieldDefinitionModel):
    """模型、审核与检索共用的可执行约束。"""

    enum: tuple[FieldEnumOption, ...] | None = Field(default=None, description="允许的字符串枚举；null 表示不限制枚举。")
    minimum: float | None = Field(default=None, allow_inf_nan=False, description="允许的最小数值，包含边界。")
    maximum: float | None = Field(default=None, allow_inf_nan=False, description="允许的最大数值，包含边界。")
    multiple_of: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="数值必须为该值的整数倍；1 表示只允许整数值。")

    @model_validator(mode="after")
    def validate_constraints(self) -> Self:
        if self.enum is not None:
            values = [item.value for item in self.enum]
            if not values or len(values) != len(set(values)):
                raise ValueError("枚举必须非空且标准值不能重复")
            if any(not item.value.strip() or not item.label.strip() for item in self.enum):
                raise ValueError("枚举标准值和标签不能是空白")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum 不能大于 maximum")
        return self

    def json_schema_keywords(self) -> dict:
        result = {}
        if self.enum is not None:
            result["enum"] = [item.value for item in self.enum]
        for source, target in (("minimum", "minimum"), ("maximum", "maximum"), ("multiple_of", "multipleOf")):
            value = getattr(self, source)
            if value is not None:
                result[target] = value
        return result


class FieldPropertyDefinition(FieldDefinitionModel):
    """提取对象中的一个扁平基本类型属性。"""

    name: str
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    aliases: tuple[str, ...]
    type: FieldValueType
    tokenize: bool | None = None
    index_format: Literal["strict_date"] | None = None
    constraints: FieldConstraints
    extraction_rule: str
    required: bool
    meaning: str
    excludes: str

    @model_validator(mode="after")
    def validate_tokenize(self) -> Self:
        """ES 分词开关只能用于字符串属性。"""
        if self.tokenize is not None and self.type is not FieldValueType.STRING:
            raise ValueError(
                f"属性“{self.name}”仅在 type=string 时允许配置 tokenize"
            )
        if self.index_format is not None and (
            self.type is not FieldValueType.STRING or self.tokenize is True
        ):
            raise ValueError("index_format=strict_date 仅允许用于不分词的字符串属性")
        if self.constraints.enum is not None and self.type is not FieldValueType.STRING:
            raise ValueError("enum 仅允许用于 string 属性")
        if any(getattr(self.constraints, key) is not None for key in ("minimum", "maximum", "multiple_of")) and self.type not in (FieldValueType.INTEGER, FieldValueType.NUMBER):
            raise ValueError("数值范围和倍数约束仅允许用于 integer 或 number 属性")
        return self

    @field_validator("name", "meaning", "excludes", "extraction_rule")
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
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
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
        """对象至少有一个属性，且属性名称和索引代码不得重复。"""
        if not value:
            raise ValueError("提取对象至少需要一个属性")
        names = [property_definition.name for property_definition in value]
        if len(names) != len(set(names)):
            raise ValueError("对象属性名称不能重复")
        codes = [property_definition.code for property_definition in value]
        if len(codes) != len(set(codes)):
            raise ValueError("对象属性 code 不能重复")
        return value

    @model_validator(mode="after")
    def validate_required_property(self) -> "FieldDefinition":
        """防止定义出没有任何必填事实的空对象。"""
        if not any(
            property_definition.required
            for property_definition in self.properties
        ):
            raise ValueError("提取对象至少需要一个必填属性")
        return self


class FieldDefinitionCollection(FieldDefinitionModel):
    """Core 目录中按文件名稳定排列的字段定义快照。"""

    kind: Literal["core"]
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
        """同一职责目录内的对象名称与索引代码必须唯一。"""
        names = [definition.name for definition in self.definitions]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.kind} 字段定义名称不能重复")
        codes = [definition.code for definition in self.definitions]
        if len(codes) != len(set(codes)):
            raise ValueError(f"{self.kind} 字段定义 code 不能重复")
        return self

    def get(self, name: str) -> FieldDefinition:
        """按稳定名称返回一个字段定义，不存在时明确失败。"""
        for definition in self.definitions:
            if definition.name == name:
                return definition
        raise KeyError(f"未知 {self.kind} 字段定义：{name}")


class FieldDefinitionCatalog(FieldDefinitionModel):
    """应用启动时加载的完整 Core 定义快照。"""

    root: Path
    core: FieldDefinitionCollection
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
        """Core 集合的目录职责必须正确且至少包含一个定义。"""
        if self.core.kind != "core":
            raise ValueError("字段定义集合的 kind 必须为 core")
        if not self.core.definitions:
            raise ValueError("Core 字段定义不能为空")
        return self

    @property
    def definition_count(self) -> int:
        """返回当前内存快照中的字段定义总数。"""
        return len(self.core.definitions)


__all__ = [
    "FieldCardinality",
    "FieldDefinition",
    "FieldDefinitionCatalog",
    "FieldDefinitionCollection",
    "FieldPropertyDefinition",
    "FieldValueType",
]
