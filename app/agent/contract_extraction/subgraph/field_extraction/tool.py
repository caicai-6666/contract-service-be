"""Core 使用的严格扁平对象提取工具。"""

from __future__ import annotations

import json
from math import isfinite
from typing import Any, Final, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinition,
    FieldPropertyDefinition,
    FieldValueType,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO

ABANDON_REASON_SUFFIX: Final[str] = "因此，当前对象无法从该合同中可靠提取。"
FINISH_REASON_SUFFIX: Final[str] = "因此，当前对象定义已提取完毕。"
FIELD_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

FieldValue: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool
FieldObjectValue: TypeAlias = dict[str, FieldValue]


class StrictFieldToolModel(BaseModel):
    """禁止额外参数的不可变字段工具模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FieldEvidence(StrictFieldToolModel):
    """支持单个扁平对象的最小页面证据。"""

    page_number: int = Field(
        description="该证据所在合同页面的物理页码，从 1 开始；不是页面中印刷的页码。"
    )
    content: str = Field(
        description=(
            "从该页直接观察到、足以支持当前对象属性值的简短原文；"
            "不得填写推断、规范化后的改写值或无关的整页文本。"
        )
    )

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        if value < 1:
            raise ValueError("物理页码必须大于等于 1")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("证据内容不能为空")
        return normalized


class ThinkArguments(StrictFieldToolModel):
    """think 只记录当前对象定义的一段自然语言推理。"""

    reasoning: str = Field(
        description=(
            "针对当前对象定义的简洁自然语言思考，用于比较页面证据、已提取对象和剩余候选；"
            "不在此提交正式对象。"
        )
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("思考理由不能为空")
        return normalized


class AbandonExtractionArguments(StrictFieldToolModel):
    """没有任何可靠对象时提交的终止决定。"""

    reasoning: str = Field(
        description=(
            f"说明当前合同为何没有可可靠提取的完整对象，并必须以“{ABANDON_REASON_SUFFIX}”结束；"
            "不得用该工具代替仍可提取的不完整检查。"
        )
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("放弃理由不能为空")
        if not normalized.endswith(ABANDON_REASON_SUFFIX):
            raise ValueError(f"放弃理由必须以“{ABANDON_REASON_SUFFIX}”结束")
        return normalized


class FinishExtractionArguments(StrictFieldToolModel):
    """multiple 已穷尽全部对象时提交的显式终止决定。"""

    reasoning: str = Field(
        description=(
            f"说明 multiple 对象已经全部提取完毕，并必须以“{FINISH_REASON_SUFFIX}”结束；"
            "single 定义不得调用此工具。"
        )
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("结束理由不能为空")
        if not normalized.endswith(FINISH_REASON_SUFFIX):
            raise ValueError(f"结束理由必须以“{FINISH_REASON_SUFFIX}”结束")
        return normalized


class ExtractObjectArguments(StrictFieldToolModel):
    """一次对象提交的固定外层参数；value 的内部 Schema 动态生成。"""

    evidence: list[FieldEvidence] = Field(
        description=(
            "支持当前单个扁平对象全部必填属性的页面原文证据，按合同阅读顺序排列；"
            "至少提供一条。"
        )
    )
    reasoning: str = Field(
        description=(
            "简洁说明证据如何满足当前对象定义、属性含义及排除边界；"
            "不得重复输出 value 的 JSON，也不得引入证据之外的新事实。"
        )
    )
    value: dict[str, Any] = Field(
        description=(
            "当前定义的唯一正式扁平对象决定；只允许提交动态 Schema 声明的基本类型属性，"
            "不得嵌套对象或添加未定义属性。"
        )
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[FieldEvidence]) -> list[FieldEvidence]:
        if not value:
            raise ValueError("提取对象至少需要一条证据")
        return value

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("提取理由不能为空")
        return normalized


FieldToolArguments: TypeAlias = (
    ThinkArguments
    | AbandonExtractionArguments
    | FinishExtractionArguments
    | ExtractObjectArguments
)


class FieldObjectValidationError(ValueError):
    """带明确参数位置和修正方向的扁平对象校验错误。"""

    def __init__(self, path: str, problem: str, correction: str) -> None:
        super().__init__(problem)
        self.path = path
        self.problem = problem
        self.correction = correction


def canonical_field_value(value: FieldObjectValue) -> str:
    """使用紧凑 JSON 表达一个扁平对象，供反馈和理由结尾复用。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _property_schema(property_definition: FieldPropertyDefinition) -> dict[str, Any]:
    description = (
        f"{property_definition.meaning} 排除边界：{property_definition.excludes}"
    )
    return {
        "type": property_definition.type.value,
        "description": description,
    }


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictFieldToolModel],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            "strict": False,
        },
    }


THINK_TOOL: Final[dict[str, Any]] = _function_tool(
    name="think",
    description=(
        "记录当前对象定义的一段简洁自然语言推理，用于比较证据、已提取对象"
        "和剩余候选；不提交正式对象。"
    ),
    arguments_model=ThinkArguments,
)

ABANDON_EXTRACTION_TOOL: Final[dict[str, Any]] = _function_tool(
    name="abandon_extraction",
    description=(
        "当前合同没有出现该对象、对象不适用或证据不足以支持任何一个完整对象时，"
        "提交零对象终止决定。"
    ),
    arguments_model=AbandonExtractionArguments,
)

FINISH_EXTRACTION_TOOL: Final[dict[str, Any]] = _function_tool(
    name="finish_extraction",
    description=(
        "仅用于 multiple 定义；确认已提取对象之外不存在更多可靠对象时，"
        "结束当前对象定义。"
    ),
    arguments_model=FinishExtractionArguments,
)


def build_extract_object_tool(definition: FieldDefinition) -> dict[str, Any]:
    """根据 properties 构造禁止额外属性的扁平对象 Schema。"""
    tool = _function_tool(
        name="extract_object",
        description=(
            f"为当前定义“{definition.name}”提交一个完整、独立的扁平对象。"
            "参数依次提供页面证据、推理摘要和对象值；value 是唯一正式决定，"
            "reasoning 不重复 value 的 JSON。"
        ),
        arguments_model=ExtractObjectArguments,
    )
    value_schema = {
        "type": "object",
        "description": (
            f"当前定义“{definition.name}”的唯一正式扁平对象决定。"
            f"对象含义：{definition.meaning} 排除边界：{definition.excludes} "
            "只提交下列已定义属性，不得嵌套或增加额外属性。"
        ),
        "properties": {
            item.name: _property_schema(item) for item in definition.properties
        },
        "required": [item.name for item in definition.properties if item.required],
        "additionalProperties": False,
    }
    tool["function"]["parameters"]["properties"]["value"] = value_schema
    return tool


def build_field_tools(
    definition: FieldDefinition,
    *,
    has_extracted_objects: bool,
) -> tuple[dict[str, Any], ...]:
    """根据短期记忆状态返回当前轮真正允许调用的工具。"""
    base = (THINK_TOOL, build_extract_object_tool(definition))
    if has_extracted_objects:
        if definition.cardinality is FieldCardinality.MULTIPLE:
            return base + (FINISH_EXTRACTION_TOOL,)
        return base
    return base + (ABANDON_EXTRACTION_TOOL,)


def _decode_embedded_json(value: Any) -> Any:
    """兼容 Qwen 工具解析器把嵌套参数编码成 JSON 字符串的情况。"""
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return _decode_embedded_json(decoded)
    if isinstance(value, list):
        return [_decode_embedded_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_embedded_json(item) for key, item in value.items()}
    return value


def _type_correction(property_definition: FieldPropertyDefinition) -> str:
    return {
        FieldValueType.STRING: "传入非空 JSON 字符串，并保留必要的原文字符",
        FieldValueType.INTEGER: "仅传入不带单位和小数点的 JSON 整数",
        FieldValueType.NUMBER: "仅传入不带币种、单位或百分号的 JSON 数值",
        FieldValueType.BOOLEAN: "仅传入 JSON 布尔值 true 或 false",
    }[property_definition.type]


def _validate_property_value(
    property_definition: FieldPropertyDefinition,
    value: Any,
) -> FieldValue:
    expected_type = property_definition.type
    valid = {
        FieldValueType.STRING: type(value) is str,
        FieldValueType.INTEGER: type(value) is int,
        FieldValueType.NUMBER: type(value) in {int, float},
        FieldValueType.BOOLEAN: type(value) is bool,
    }[expected_type]
    if not valid:
        raise FieldObjectValidationError(
            f"value.{property_definition.name}",
            f"要求 {expected_type.value}，但收到 {value!r}",
            _type_correction(property_definition),
        )
    if expected_type is FieldValueType.STRING and not value.strip():
        raise FieldObjectValidationError(
            f"value.{property_definition.name}",
            "字符串不能为空",
            "传入包含合同原文事实的非空字符串",
        )
    if expected_type is FieldValueType.NUMBER and not isfinite(value):
        raise FieldObjectValidationError(
            f"value.{property_definition.name}",
            "数值不能是 NaN 或无穷大",
            "传入有限 JSON 数值",
        )
    return value


def validate_object_value(
    definition: FieldDefinition,
    value: dict[str, Any],
) -> FieldObjectValue:
    """按定义顺序验证必填、额外属性和每个基本类型。"""
    definitions = {item.name: item for item in definition.properties}
    extra = [name for name in value if name not in definitions]
    if extra:
        raise FieldObjectValidationError(
            "value",
            f"包含未定义属性 {extra}",
            f"只提交已定义属性 {list(definitions)}",
        )
    missing = [
        item.name
        for item in definition.properties
        if item.required and item.name not in value
    ]
    if missing:
        raise FieldObjectValidationError(
            "value",
            f"缺少必填属性 {missing}",
            "补充每个必填属性及其直接证据",
        )
    normalized: FieldObjectValue = {}
    for item in definition.properties:
        if item.name in value:
            normalized[item.name] = _validate_property_value(item, value[item.name])
    return normalized


def parse_field_tool_arguments(
    definition: FieldDefinition,
    name: str,
    raw_arguments: str,
) -> FieldToolArguments:
    """解析工具参数并再次执行本地 strict 扁平对象校验。"""
    models: dict[str, type[StrictFieldToolModel]] = {
        "think": ThinkArguments,
        "extract_object": ExtractObjectArguments,
        "abandon_extraction": AbandonExtractionArguments,
        "finish_extraction": FinishExtractionArguments,
    }
    try:
        arguments_model = models[name]
    except KeyError as exc:
        raise ValueError(f"未知的对象提取工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    arguments = arguments_model.model_validate(_decode_embedded_json(payload))
    if not isinstance(arguments, ExtractObjectArguments):
        return arguments
    value = validate_object_value(definition, arguments.value)
    return arguments.model_copy(update={"value": value})


__all__ = [
    "ABANDON_EXTRACTION_TOOL",
    "ABANDON_REASON_SUFFIX",
    "FIELD_TOOL_CHOICE",
    "FINISH_EXTRACTION_TOOL",
    "FINISH_REASON_SUFFIX",
    "THINK_TOOL",
    "AbandonExtractionArguments",
    "ExtractObjectArguments",
    "FieldEvidence",
    "FieldObjectValidationError",
    "FieldObjectValue",
    "FieldToolArguments",
    "FieldValue",
    "FinishExtractionArguments",
    "ThinkArguments",
    "build_extract_object_tool",
    "build_field_tools",
    "canonical_field_value",
    "parse_field_tool_arguments",
    "validate_object_value",
]
