"""合同检索问题提出工具的本地严格契约。"""

from __future__ import annotations

import json
from typing import Any, Final, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictQuestionToolModel(BaseModel):
    """禁止额外参数和宽松类型转换的问题提出工具模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


QUESTION_GENERATION_TOOL_VERSION: Final = "retrieval-question-tool-v2"
QUESTION_GENERATION_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO


class QuestionEvidence(StrictQuestionToolModel):
    """支持提出某个问题的一条简短合同证据。"""

    page_number: int = Field(
        ge=1,
        description="证据所在的 PDF 物理页码，从 1 开始；不是合同印刷页码。",
    )
    content: str = Field(
        max_length=300,
        description=(
            "支持该事项适用于当前合同或可能存在重要缺失的可核对原文短片段，"
            "最多 300 个字符；不得复制整页或整段合同正文。"
        ),
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("问题证据不能为空")
        return normalized


class ProposeQuestionArguments(StrictQuestionToolModel):
    """按证据、推理摘要、最终问题的顺序提交一个问题。"""

    evidence: list[QuestionEvidence] = Field(
        min_length=1,
        max_length=10,
        description=(
            "支持当前问题适用于本合同或对应事项可能存在重要缺失的合同证据，按物理页码"
            "和阅读顺序排列；至少一条，不能使用合同外知识。"
        ),
    )
    reasoning_summary: str = Field(
        max_length=2000,
        description=(
            "说明前述证据如何对应提问指南、该事项为何适用于当前合同且具有检索价值的"
            "简洁推理摘要；不得引入证据未支持的新合同事实。"
        ),
    )
    question: str = Field(
        max_length=240,
        description=(
            "本次唯一提交、可脱离当前对话理解的自然中文用户问题。应模拟采购、销售、项目、"
            "财务、法务或管理人员真实查找合同的说法；原文能够确认时带上相关方简称、具体"
            "产品、服务或项目，优先询问谁做什么、何时做、满足什么条件或涉及多少。允许用"
            "一至两个紧密问句问完整同一主要意图，不得写成审查清单、字段标签、僵硬的"
            "“合同约定的……是什么”模板，不得合并不同事项或要求合同外法律判断。"
        ),
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence_order(
        cls,
        value: list[QuestionEvidence],
    ) -> list[QuestionEvidence]:
        page_numbers = [item.page_number for item in value]
        if page_numbers != sorted(page_numbers):
            raise ValueError("问题证据必须按物理页码升序排列")
        return value

    @field_validator("reasoning_summary", "question")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("推理摘要和正式问题均不能为空")
        return normalized


class GeneratedQuestion(StrictQuestionToolModel):
    """程序为已接受问题补充规划关联、身份和顺序后的正式对象。"""

    question_id: str
    order: int
    focus_id: str | None = None
    attention_codes: tuple[str, ...] = ()
    evidence: tuple[QuestionEvidence, ...]
    reasoning_summary: str
    question: str


class QuestionGenerationToolFeedback(StrictQuestionToolModel):
    """工具动作的最小反馈。"""

    ok: bool
    message: str


QuestionGenerationToolArguments: TypeAlias = ProposeQuestionArguments


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictQuestionToolModel],
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


PROPOSE_QUESTION_TOOL: Final[dict[str, Any]] = _function_tool(
    name="propose_question",
    description=(
        "一次提交一个适用于当前合同且具有检索价值的问题。先给可核对合同证据，再说明"
        "选择理由，最后给出唯一正式问题；问题身份和顺序由程序生成。"
    ),
    arguments_model=ProposeQuestionArguments,
)

_ARGUMENT_MODELS: Final[dict[str, type[StrictQuestionToolModel]]] = {
    "propose_question": ProposeQuestionArguments,
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


def parse_question_generation_tool_arguments(
    name: str,
    raw_arguments: str,
) -> QuestionGenerationToolArguments:
    """在接受动作前解析并严格校验模型参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的问题提出工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def build_generated_question(
    arguments: ProposeQuestionArguments,
    *,
    order: int,
    focus_id: str | None = None,
    attention_codes: tuple[str, ...] = (),
) -> GeneratedQuestion:
    """用程序持有的规划关联与顺序生成稳定问题身份。"""
    if order < 1:
        raise ValueError("问题顺序必须大于等于 1")
    return GeneratedQuestion(
        question_id=f"generated-question-{order:04d}",
        order=order,
        focus_id=focus_id,
        attention_codes=attention_codes,
        evidence=tuple(arguments.evidence),
        reasoning_summary=arguments.reasoning_summary,
        question=arguments.question,
    )


def validation_error_feedback(error: Exception) -> QuestionGenerationToolFeedback:
    """把参数错误转换为包含位置、问题与修正方向的简短反馈。"""
    if not isinstance(error, ValidationError):
        return QuestionGenerationToolFeedback(
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
            correction = "填写大于等于 1 的真实 PDF 物理页码"
        elif "evidence" in path:
            correction = "按物理页码升序提供至少一条简短、可核对的合同证据"
        elif path == "question":
            correction = "只填写一个非空、可独立理解且不要求合同外判断的问题"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(errors) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return QuestionGenerationToolFeedback(ok=False, message="\n".join(messages))


__all__ = [
    "PROPOSE_QUESTION_TOOL",
    "QUESTION_GENERATION_TOOL_CHOICE",
    "QUESTION_GENERATION_TOOL_VERSION",
    "GeneratedQuestion",
    "ProposeQuestionArguments",
    "QuestionEvidence",
    "QuestionGenerationToolArguments",
    "QuestionGenerationToolFeedback",
    "build_generated_question",
    "parse_question_generation_tool_arguments",
    "validation_error_feedback",
]
