"""合同建议文件名生成节点使用的 Pydantic function tools。"""

from __future__ import annotations

import json
from typing import Any, Final, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictFileNameGenerationToolModel(BaseModel):
    """拒绝额外参数和宽松类型转换的建议文件名工具基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


FILE_NAME_GENERATION_TOOL_VERSION: Final = "file-name-generation-tool-v1"
FILE_NAME_GENERATION_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO
FILE_NAME_GENERATION_TOOL_PLACEMENT: Final = "after_task"

_KNOWN_FILE_EXTENSIONS: Final = (
    ".pdf",
    ".doc",
    ".docx",
    ".wps",
    ".rtf",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
)
_GENERIC_FILE_NAMES: Final = frozenset(
    {
        "合同",
        "合同书",
        "协议",
        "协议书",
        "买卖合同",
        "采购合同",
        "销售合同",
        "服务合同",
        "技术合同",
        "合作协议",
        "租赁合同",
    }
)


class SuggestedFileNameEvidence(StrictFileNameGenerationToolModel):
    """支持建议名称的一条可核对页面证据。"""

    page_number: int = Field(
        ge=1,
        description=(
            "该证据所在合同页面的物理页码，从 1 开始；不是页面中印刷的页码。"
        ),
    )
    content: str = Field(
        max_length=300,
        description=(
            "页面中直接支持正式标题、核心标的、具体项目、实际服务内容或合同类型的"
            "简短可核对原文，最多 300 个字符；不得填写分类摘要、推断或整页正文。"
        ),
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """拒绝只有空白的命名证据。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("建议文件名证据不能为空")
        return normalized


class ThinkArguments(StrictFileNameGenerationToolModel):
    """单轮 think 的过程性推理工作空间。"""

    reasoning: str = Field(
        max_length=2000,
        description=(
            "围绕当前命名任务进行的简洁分析：核对原始标题是否泛化，比较核心标的、"
            "具体项目、实际服务内容与合同类型，并检查候选名称是否准确、友好且具有"
            "辨识度；这里不提交最终建议文件名。"
        ),
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        """think 必须包含实际命名分析。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("think 推理不能为空")
        return normalized


class SubmitSuggestedFileNameArguments(StrictFileNameGenerationToolModel):
    """按证据、理由和最终名称提交唯一建议文件名。"""

    evidence: list[SuggestedFileNameEvidence] = Field(
        min_length=1,
        max_length=10,
        description=(
            "支持建议名称的页面证据，按物理页码升序排列；至少包含一条。原始标题"
            "泛化或缺失时，证据必须覆盖建议名称采用的核心标的、具体项目或实际服务"
            "内容，不能只引用通用合同标题。"
        ),
    )
    reasoning: str = Field(
        max_length=2000,
        description=(
            "简洁说明原始标题是否具有辨识度，以及前述页面证据和分类摘要如何支持"
            "最终名称中的核心内容与合同类型；不得引入证据未支持的合同事实。"
        ),
    )
    file_name: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "当前合同唯一的建议展示文件名，只包含名称主体，不包含文件扩展名或存储"
            "路径；不得包含 /、\\、:、*、?、\"、<、>、|、换行或控制字符，也不得"
            "以句点开头或结尾；不得只提交“买卖合同”“服务合同”等缺少具体交易"
            "内容的泛化标题。"
        ),
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence_order(
        cls,
        value: list[SuggestedFileNameEvidence],
    ) -> list[SuggestedFileNameEvidence]:
        """证据顺序必须与合同页面阅读顺序一致。"""
        page_numbers = [item.page_number for item in value]
        if page_numbers != sorted(page_numbers):
            raise ValueError("建议文件名证据必须按物理页码升序排列")
        return value

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        """正式建议必须包含非空命名理由。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("建议文件名理由不能为空")
        return normalized

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        """执行与最终入库兼容的文件名字符校验，并排除常见扩展名。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("建议文件名不能为空")
        if normalized[0] == "." or normalized[-1] == ".":
            raise ValueError("建议文件名不能以句点开头或结尾")
        invalid_characters = set('/\\:*?"<>|\r\n')
        if any(
            character in invalid_characters or ord(character) < 32
            for character in normalized
        ):
            raise ValueError("建议文件名包含非法字符")
        if normalized.casefold().endswith(_KNOWN_FILE_EXTENSIONS):
            raise ValueError("建议文件名只需名称主体，不得包含文件扩展名")
        if normalized in _GENERIC_FILE_NAMES:
            raise ValueError("建议文件名不能只使用缺少具体交易内容的泛化标题")
        return normalized


FileNameGenerationToolArguments: TypeAlias = (
    ThinkArguments | SubmitSuggestedFileNameArguments
)


class FileNameGenerationToolFeedback(StrictFileNameGenerationToolModel):
    """写回当前命名会话短期纠错记忆的最小反馈。"""

    ok: bool
    message: str


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictFileNameGenerationToolModel],
) -> dict[str, Any]:
    """从唯一 Pydantic 参数契约生成 OpenAI 兼容函数工具。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            # vLLM/Qwen 使用 XML 工具协议；本地 Pydantic 继续执行严格校验。
            "strict": False,
        },
    }


THINK_TOOL: Final[dict[str, Any]] = _function_tool(
    name="think",
    description=(
        "提供一次过程性推理空间，用于判断标题是否泛化、比较页面中的核心交易内容"
        "并检查候选名称的准确性和辨识度；该动作不提交最终建议文件名。"
    ),
    arguments_model=ThinkArguments,
)

SUBMIT_SUGGESTED_FILE_NAME_TOOL: Final[dict[str, Any]] = _function_tool(
    name="submit_suggested_file_name",
    description=(
        "命名依据充分时，依次提交可核对页面证据、简洁命名理由和唯一的建议展示"
        "文件名；这是当前命名任务的终止动作。"
    ),
    arguments_model=SubmitSuggestedFileNameArguments,
)

FILE_NAME_GENERATION_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    SUBMIT_SUGGESTED_FILE_NAME_TOOL,
)

_ARGUMENT_MODELS: Final[dict[str, type[StrictFileNameGenerationToolModel]]] = {
    "think": ThinkArguments,
    "submit_suggested_file_name": SubmitSuggestedFileNameArguments,
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


def parse_file_name_generation_tool_arguments(
    name: str,
    raw_arguments: str,
) -> FileNameGenerationToolArguments:
    """执行工具前解析参数，并用本地 Pydantic 契约严格校验。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的建议文件名生成工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def validation_error_feedback(error: Exception) -> FileNameGenerationToolFeedback:
    """把参数错误转换为包含位置、问题和修正方向的最小反馈。"""
    if not isinstance(error, ValidationError):
        return FileNameGenerationToolFeedback(
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
        elif path.startswith("evidence"):
            correction = "按物理页码升序提供简短、可直接核对的页面证据"
        elif path == "reasoning":
            correction = "提供证据如何支持核心内容和合同类型的简洁命名理由"
        elif path == "file_name":
            correction = (
                "提交不超过 255 个字符、没有扩展名和非法字符的名称主体"
            )
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(errors) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return FileNameGenerationToolFeedback(
        ok=False,
        message="\n".join(messages),
    )


__all__ = [
    "FILE_NAME_GENERATION_TOOLS",
    "FILE_NAME_GENERATION_TOOL_CHOICE",
    "FILE_NAME_GENERATION_TOOL_PLACEMENT",
    "FILE_NAME_GENERATION_TOOL_VERSION",
    "SUBMIT_SUGGESTED_FILE_NAME_TOOL",
    "THINK_TOOL",
    "FileNameGenerationToolArguments",
    "FileNameGenerationToolFeedback",
    "StrictFileNameGenerationToolModel",
    "SubmitSuggestedFileNameArguments",
    "SuggestedFileNameEvidence",
    "ThinkArguments",
    "parse_file_name_generation_tool_arguments",
    "validation_error_feedback",
]
