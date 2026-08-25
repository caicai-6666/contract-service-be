"""预处理子图文档结构节点的严格函数工具契约。"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class StrictToolModel(BaseModel):
    """禁止额外字段的不可变工具参数基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceKind(StrEnum):
    """结构证据的可核对形式。"""

    TEXT = "text"
    VISUAL = "visual"


class BoundaryInclusion(StrEnum):
    """边界锚点是否属于当前内容单元。"""

    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"


class NormalizedBoundingBox(StrictToolModel):
    """以单页 0～1000 坐标系表示的矩形区域。"""

    x_min: int
    y_min: int
    x_max: int
    y_max: int

    @model_validator(mode="after")
    def validate_order(self) -> "NormalizedBoundingBox":
        """拒绝越界、零面积或方向颠倒的坐标框。"""
        coordinates = (self.x_min, self.y_min, self.x_max, self.y_max)
        if any(coordinate < 0 or coordinate > 1000 for coordinate in coordinates):
            raise ValueError("坐标必须位于 0～1000")
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("坐标框必须满足 x_min < x_max 且 y_min < y_max")
        return self


class StructureEvidence(StrictToolModel):
    """支持合同主题、单元内容或边界判断的页面证据。"""

    page_number: int
    kind: EvidenceKind
    content: str
    bbox: NormalizedBoundingBox | None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """证据必须保留可供复核的非空内容。"""
        if not value.strip():
            raise ValueError("证据内容不能为空")
        return value

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        """物理页码从 1 开始。"""
        if value < 1:
            raise ValueError("物理页码必须大于等于 1")
        return value


class DocumentScopeDecision(StrictToolModel):
    """面向结构导航的合同整体认识。"""

    title: str | None
    subject: str
    summary: str

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        """未知标题使用 null，而不是空字符串。"""
        if value is not None and not value.strip():
            raise ValueError("未知标题应传 null")
        return value

    @field_validator("subject", "summary")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """主题决定必须包含有效文本。"""
        if not value.strip():
            raise ValueError("主题与概览不能为空")
        return value


class SummaryArguments(StrictToolModel):
    """首轮 summary 工具的严格参数。"""

    evidence: list[StructureEvidence]
    reasoning_summary: str
    decision: DocumentScopeDecision

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[StructureEvidence]) -> list[StructureEvidence]:
        """整体认识至少需要一条可核对证据。"""
        if not value:
            raise ValueError("summary 至少需要一条证据")
        return value

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        """保留简洁、可审计的推理摘要。"""
        if not value.strip():
            raise ValueError("推理摘要不能为空")
        return value


class ThinkArguments(StrictToolModel):
    """think 工具仅保存一段自然语言理由。"""

    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """think 只接收一段非空自然语言理由。"""
        if not value.strip():
            raise ValueError("思考理由不能为空")
        return value


class UnitBoundary(StrictToolModel):
    """一个内容单元的开始或结束锚点。"""

    page_number: int
    anchor_kind: EvidenceKind
    anchor: str
    anchor_bbox: NormalizedBoundingBox | None
    inclusion: BoundaryInclusion

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        """物理页码从 1 开始。"""
        if value < 1:
            raise ValueError("物理页码必须大于等于 1")
        return value

    @field_validator("anchor")
    @classmethod
    def validate_anchor(cls, value: str) -> str:
        """边界必须具备可复核的文本或视觉描述。"""
        if not value.strip():
            raise ValueError("边界锚点不能为空")
        return value


class UnitSpan(StrictToolModel):
    """一个连续内容单元的起止范围。"""

    start: UnitBoundary
    end: UnitBoundary

    @model_validator(mode="after")
    def validate_page_order(self) -> "UnitSpan":
        """页面顺序属于工具参数可以直接校验的最低边界。"""
        if self.start.page_number > self.end.page_number:
            raise ValueError("单元开始页不能晚于结束页")
        if (
            self.start.page_number == self.end.page_number
            and self.start.anchor_bbox is not None
            and self.end.anchor_bbox is not None
            and self.start.anchor_bbox.y_min > self.end.anchor_bbox.y_max
        ):
            raise ValueError("同页单元的开始位置不能晚于结束位置")
        return self


class UnitDecision(StrictToolModel):
    """一个宏观内容单元的最终决定。"""

    label: str
    summary: str
    span: UnitSpan

    @field_validator("label", "summary")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """内容单元必须具备有效名称和概览。"""
        if not value.strip():
            raise ValueError("单元名称与概览不能为空")
        return value


class GenerateUnitArguments(StrictToolModel):
    """generate_unit 每次只能提交一个内容单元。"""

    evidence: list[StructureEvidence]
    reasoning_summary: str
    decision: UnitDecision

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[StructureEvidence]) -> list[StructureEvidence]:
        """每个单元至少具有一条可核对证据。"""
        if not value:
            raise ValueError("generate_unit 至少需要一条证据")
        return value

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        """单元决定之前必须给出简洁推理摘要。"""
        if not value.strip():
            raise ValueError("推理摘要不能为空")
        return value


class FinishArguments(StrictToolModel):
    """finish 工具提交发现完成的自然语言理由。"""

    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """终止请求必须说明覆盖完成的理由。"""
        if not value.strip():
            raise ValueError("终止理由不能为空")
        return value


class ToolFeedback(StrictToolModel):
    """写回短期上下文的最小工具反馈。"""

    ok: bool
    message: str


ToolArguments = (
    SummaryArguments
    | ThinkArguments
    | GenerateUnitArguments
    | FinishArguments
)


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictToolModel],
) -> dict[str, Any]:
    """把 Pydantic 参数模型转换为 OpenAI 兼容 strict function tool。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            "strict": True,
        },
    }


SUMMARY_TOOL: Final[dict[str, Any]] = _function_tool(
    name="summary",
    description=(
        "仅在单元发现首轮调用。先提供页码证据和简洁推理摘要，再形成面向结构导航的合同标题、主题和概览。"
    ),
    arguments_model=SummaryArguments,
)

THINK_TOOL: Final[dict[str, Any]] = _function_tool(
    name="think",
    description=(
        "记录一段简洁自然语言理由，用于考虑尚未覆盖的内容、下一步行动或当前边界疑点；不提交内容单元。"
    ),
    arguments_model=ThinkArguments,
)

GENERATE_UNIT_TOOL: Final[dict[str, Any]] = _function_tool(
    name="generate_unit",
    description=(
        "一次提交一个用于下游导航的宏观连续内容单元。不得仅因条款编号、自然段或换页而拆分。"
    ),
    arguments_model=GenerateUnitArguments,
)

FINISH_TOOL: Final[dict[str, Any]] = _function_tool(
    name="finish",
    description=(
        "认为合同宏观内容已经覆盖完成时请求结束单元发现；程序仍会校验当前状态并决定是否接受。"
    ),
    arguments_model=FinishArguments,
)

FIRST_ROUND_TOOLS: Final[tuple[dict[str, Any], ...]] = (SUMMARY_TOOL,)
DISCOVERY_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    GENERATE_UNIT_TOOL,
    FINISH_TOOL,
)

FIRST_ROUND_TOOL_CHOICE: Final[dict[str, Any]] = {
    "type": "function",
    "function": {"name": "summary"},
}
DISCOVERY_TOOL_CHOICE: Final[Literal["required"]] = "required"

_ARGUMENT_MODELS: Final[dict[str, type[StrictToolModel]]] = {
    "summary": SummaryArguments,
    "think": ThinkArguments,
    "generate_unit": GenerateUnitArguments,
    "finish": FinishArguments,
}


def _decode_embedded_json(value: Any) -> Any:
    """兼容 Qwen3 XML parser 将嵌套对象作为 JSON 字符串返回的情况。"""
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


def parse_tool_arguments(name: str, raw_arguments: str) -> ToolArguments:
    """在执行工具前用本地 Pydantic 契约再次校验模型参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的文档结构工具：{name}") from exc

    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


__all__ = [
    "DISCOVERY_TOOL_CHOICE",
    "DISCOVERY_TOOLS",
    "FIRST_ROUND_TOOL_CHOICE",
    "FIRST_ROUND_TOOLS",
    "FINISH_TOOL",
    "GENERATE_UNIT_TOOL",
    "SUMMARY_TOOL",
    "THINK_TOOL",
    "FinishArguments",
    "GenerateUnitArguments",
    "SummaryArguments",
    "ThinkArguments",
    "ToolFeedback",
    "parse_tool_arguments",
]
