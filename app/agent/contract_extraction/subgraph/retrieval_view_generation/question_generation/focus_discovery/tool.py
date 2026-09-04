"""合同检索问题关注点发现工具的本地严格契约。"""

from __future__ import annotations

import json
import re
from typing import Any, Final, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictQuestionFocusToolModel(BaseModel):
    """禁止额外参数和宽松类型转换的关注点工具模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


QUESTION_FOCUS_DISCOVERY_TOOL_VERSION: Final = "retrieval-question-focus-tool-v2"
QUESTION_FOCUS_DISCOVERY_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

_ATTENTION_CODE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$")


class QuestionFocusEvidence(StrictQuestionFocusToolModel):
    """支持某个问题关注点适用于当前合同的一条简短证据。"""

    page_number: int = Field(
        ge=1,
        description="证据所在合同页面的物理页码，从 1 开始；不是页面中印刷的页码。",
    )
    content: str = Field(
        max_length=300,
        description=(
            "支持当前关注点适用于本合同，或对应事项可能存在重要缺失的可核对原文"
            "短片段，最多 300 个字符；不得复制整页、整段正文或使用合同外知识。"
        ),
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("关注点证据不能为空")
        return normalized


class ThinkQuestionFocusArguments(StrictQuestionFocusToolModel):
    """记录发现下一个问题关注点所需的自然语言推理。"""

    reasoning: str = Field(
        max_length=2000,
        description=(
            "结合当前合同事实、提问指南和已经成功记录的工具轨迹，比较尚未覆盖事项的"
            "适用性、独立性和用户检索价值，并判断相关事项是否应合并为一个关注点的"
            "自然语言推理；只记录思考，不在此提交关注点或正式问题。"
        ),
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("问题关注点思考不能为空")
        return normalized


class GenerateQuestionFocusArguments(StrictQuestionFocusToolModel):
    """按证据、推理摘要和最终关注点要求提交一个问题规划项。"""

    evidence: list[QuestionFocusEvidence] = Field(
        min_length=1,
        max_length=10,
        description=(
            "支持当前问题关注点适用于本合同或对应事项可能存在重要缺失的合同证据，"
            "按合同页面物理页码和页内阅读顺序排列；至少一条，不得使用合同外知识。"
        ),
    )
    reasoning_summary: str = Field(
        max_length=2000,
        description=(
            "说明前述证据如何支持当前关注点、所组合事项为何属于同一真实用户意图，"
            "以及该意图为何具有独立检索价值的简洁推理摘要；不得引入证据未支持的"
            "合同事实。"
        ),
    )
    attention_codes: list[str] = Field(
        min_length=1,
        max_length=12,
        description=(
            "当前问题关注点采用的一个或多个指南稳定标识，按其对最终问题的重要性"
            "排列，例如 common.price_tax_and_settlement 或 sale.quantity_price_and_"
            "payment_linkage；允许组合多个相关标识，但不得虚构指南目录外标识、重复"
            "标识或把无法由同一连贯问题回答的事项强行合并。"
        ),
    )
    focus: str = Field(
        max_length=500,
        description=(
            "交给正式问题生成任务的一份问题规划要求，使用自然语言写清应围绕当前合同"
            "询问的对象、范围以及必须共同覆盖的条件、时间、金额、责任或例外；可以"
            "融合多个紧密相关的指南关注点，但不得直接写成最终用户问题，不得包含答案，"
            "也不得加入与前述证据和推理无关的新事项。"
        ),
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence_order(
        cls,
        value: list[QuestionFocusEvidence],
    ) -> list[QuestionFocusEvidence]:
        page_numbers = [item.page_number for item in value]
        if page_numbers != sorted(page_numbers):
            raise ValueError("关注点证据必须按物理页码升序排列")
        return value

    @field_validator("reasoning_summary", "focus")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("关注点推理摘要和最终关注点要求均不能为空")
        return normalized

    @field_validator("attention_codes")
    @classmethod
    def validate_attention_codes(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("指南关注点标识不能为空")
        invalid = [
            item
            for item in normalized
            if _ATTENTION_CODE_PATTERN.fullmatch(item) is None
        ]
        if invalid:
            raise ValueError(
                "指南关注点标识必须使用 namespace.code 格式：" + ", ".join(invalid)
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError("同一问题关注点不能重复引用指南关注点标识")
        return normalized


class FinishQuestionFocusDiscoveryArguments(StrictQuestionFocusToolModel):
    """确认无需继续发现新的问题关注点。"""

    reasoning_summary: str = Field(
        max_length=2000,
        description=(
            "说明为何当前合同已经没有尚未记录、同时具备适用性、独立性和真实用户检索"
            "价值的问题关注点；只说明自然结束依据，不得在此追加关注点或正式问题。"
        ),
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("结束关注点发现的推理摘要不能为空")
        return normalized


class GeneratedQuestionFocus(StrictQuestionFocusToolModel):
    """程序为已接受关注点补充身份和顺序后的正式规划对象。"""

    focus_id: str
    order: int
    evidence: tuple[QuestionFocusEvidence, ...]
    reasoning_summary: str
    attention_codes: tuple[str, ...]
    focus: str


class QuestionFocusToolFeedback(StrictQuestionFocusToolModel):
    """关注点工具动作的最小反馈。"""

    ok: bool
    message: str


QuestionFocusToolArguments: TypeAlias = (
    ThinkQuestionFocusArguments
    | GenerateQuestionFocusArguments
    | FinishQuestionFocusDiscoveryArguments
)


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictQuestionFocusToolModel],
) -> dict[str, Any]:
    """生成 non-strict 工具 Schema，实际参数仍由本地严格校验。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            "strict": False,
        },
    }


THINK_QUESTION_FOCUS_TOOL: Final[dict[str, Any]] = _function_tool(
    name="think",
    description=(
        "思考当前合同中尚未记录的高价值用户查询方向，并比较相关事项应合并还是分开；"
        "本工具只记录自然语言推理，不提交关注点或正式问题。"
    ),
    arguments_model=ThinkQuestionFocusArguments,
)

GENERATE_QUESTION_FOCUS_TOOL: Final[dict[str, Any]] = _function_tool(
    name="generate_question_focus",
    description=(
        "提交一个供后续生成单个正式问题的关注点要求。先给合同证据，再说明组合与入选"
        "理由，最后提交可融合一个或多个相关指南关注点的一份问题规划；不得直接生成问题。"
    ),
    arguments_model=GenerateQuestionFocusArguments,
)

FINISH_QUESTION_FOCUS_DISCOVERY_TOOL: Final[dict[str, Any]] = _function_tool(
    name="finish_question_focus_discovery",
    description=(
        "确认当前合同中已经没有尚未记录且值得真实用户独立检索的问题关注点时自然结束；"
        "本工具不提交新关注点或正式问题。"
    ),
    arguments_model=FinishQuestionFocusDiscoveryArguments,
)

QUESTION_FOCUS_DISCOVERY_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_QUESTION_FOCUS_TOOL,
    GENERATE_QUESTION_FOCUS_TOOL,
    FINISH_QUESTION_FOCUS_DISCOVERY_TOOL,
)

_ARGUMENT_MODELS: Final[dict[str, type[StrictQuestionFocusToolModel]]] = {
    "think": ThinkQuestionFocusArguments,
    "generate_question_focus": GenerateQuestionFocusArguments,
    "finish_question_focus_discovery": FinishQuestionFocusDiscoveryArguments,
}


def _decode_embedded_json(value: Any) -> Any:
    """兼容模型工具解析器把嵌套参数编码成 JSON 字符串的情况。"""
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


def parse_question_focus_tool_arguments(
    name: str,
    raw_arguments: str,
) -> QuestionFocusToolArguments:
    """在接受关注点动作前解析并严格校验模型参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的问题关注点工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def build_generated_question_focus(
    arguments: GenerateQuestionFocusArguments,
    *,
    order: int,
) -> GeneratedQuestionFocus:
    """使用程序持有的顺序为关注点生成稳定身份。"""
    if order < 1:
        raise ValueError("问题关注点顺序必须大于等于 1")
    return GeneratedQuestionFocus(
        focus_id=f"question-focus-{order:04d}",
        order=order,
        evidence=tuple(arguments.evidence),
        reasoning_summary=arguments.reasoning_summary,
        attention_codes=tuple(arguments.attention_codes),
        focus=arguments.focus,
    )


def successful_question_focus_tool_feedback(
    name: str,
    *,
    generated_focus: GeneratedQuestionFocus | None = None,
) -> QuestionFocusToolFeedback:
    """返回不暴露数量、不重复业务内容的最小成功反馈。"""
    if name == "think":
        return QuestionFocusToolFeedback(
            ok=True,
            message="思考已记录，请执行当前最合适的下一动作。",
        )
    if name == "finish_question_focus_discovery":
        return QuestionFocusToolFeedback(ok=True, message="问题关注点发现已经完成。")
    if name == "generate_question_focus":
        if generated_focus is None:
            raise ValueError(
                "generate_question_focus 的成功反馈必须提供 generated_focus"
            )
        return QuestionFocusToolFeedback(
            ok=True,
            message=(
                f"已记录 {generated_focus.focus_id}；请判断是否仍有值得记录的独立"
                "关注点，或结束发现。"
            ),
        )
    raise ValueError(f"未知的问题关注点工具：{name}")


def question_focus_validation_error_feedback(
    error: Exception,
) -> QuestionFocusToolFeedback:
    """把关注点参数错误转换为含位置、问题和修正方向的短反馈。"""
    if not isinstance(error, ValidationError):
        return QuestionFocusToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按当前工具参数定义修正后重新调用。",
        )

    messages: list[str] = []
    errors = error.errors(include_url=False)
    for item in errors[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        error_type = item["type"]
        if error_type == "missing":
            correction = "补充该必填参数"
        elif error_type == "extra_forbidden":
            correction = "删除该未定义参数"
        elif "page_number" in path:
            correction = "填写大于等于 1 的真实合同页面物理页码"
        elif "evidence" in path:
            correction = "按物理页码升序提供至少一条简短、可核对的合同证据"
        elif "attention_codes" in path:
            correction = "使用指南目录中真实、互不重复的 namespace.code 稳定标识"
        elif path == "focus":
            correction = "填写一个非空、连贯且不直接写成正式问题的关注点要求"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(errors) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return QuestionFocusToolFeedback(ok=False, message="\n".join(messages))


__all__ = [
    "FINISH_QUESTION_FOCUS_DISCOVERY_TOOL",
    "GENERATE_QUESTION_FOCUS_TOOL",
    "QUESTION_FOCUS_DISCOVERY_TOOLS",
    "QUESTION_FOCUS_DISCOVERY_TOOL_CHOICE",
    "QUESTION_FOCUS_DISCOVERY_TOOL_VERSION",
    "THINK_QUESTION_FOCUS_TOOL",
    "FinishQuestionFocusDiscoveryArguments",
    "GenerateQuestionFocusArguments",
    "GeneratedQuestionFocus",
    "QuestionFocusEvidence",
    "QuestionFocusToolArguments",
    "QuestionFocusToolFeedback",
    "ThinkQuestionFocusArguments",
    "build_generated_question_focus",
    "parse_question_focus_tool_arguments",
    "question_focus_validation_error_feedback",
    "successful_question_focus_tool_feedback",
]
