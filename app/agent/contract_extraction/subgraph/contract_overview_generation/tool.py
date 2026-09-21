"""合同概览生成节点使用的 Pydantic function tools。"""

from __future__ import annotations

from app.infrastructure.model_json import load_model_json, validate_model_payload
import json
from typing import Any, Final, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictContractOverviewGenerationToolModel(BaseModel):
    """拒绝额外参数和宽松类型转换的建议文件名工具基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION: Final = "contract-overview-generation-tool-v2"
CONTRACT_OVERVIEW_GENERATION_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO
CONTRACT_OVERVIEW_GENERATION_TOOL_PLACEMENT: Final = "after_task"

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


class ContractOverviewEvidence(StrictContractOverviewGenerationToolModel):
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


class ThinkArguments(StrictContractOverviewGenerationToolModel):
    """单轮 think 的过程性推理工作空间。"""

    reasoning: str = Field(
        max_length=2000,
        description=(
            "围绕当前命名任务进行的简洁分析：核对原始标题是否泛化，比较核心标的、"
            "具体项目、实际服务内容与合同类型，并检查候选名称是否准确、友好且具有"
            "辨识度；同时核对摘要中的关键事实和条件；这里不提交正式结果。"
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


class SubmitContractOverviewArguments(StrictContractOverviewGenerationToolModel):
    """按证据、理由、名称和摘要提交合同概览。"""

    evidence: list[ContractOverviewEvidence] = Field(
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

    summary: str = Field(
        min_length=1,
        max_length=3000,
        description=(
            "合同内容概览，最后提交；用 1～2 个简洁自然段说明合同主题、项目或标的、"
            "核心交易或服务内容及各方主要分工，不加标题或列表，不逐项总结条款。"
            "不罗列付款、验收、违约、争议解决、金额、日期或技术明细；只有直接决定"
            "合同主题或核心范围的信息才简要保留。仅依据页面事实，不推断或作法律评价。"
            "最多 3000 字符是校验边界，不是建议长度，不以命名理由代替摘要。"
        ),
    )

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        """空白摘要不能使合同概览提交成功。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("合同摘要不能为空")
        return normalized

    @field_validator("evidence")
    @classmethod
    def validate_evidence_order(
        cls,
        value: list[ContractOverviewEvidence],
    ) -> list[ContractOverviewEvidence]:
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


ContractOverviewGenerationToolArguments: TypeAlias = (
    ThinkArguments | SubmitContractOverviewArguments
)


class ContractOverviewGenerationToolFeedback(StrictContractOverviewGenerationToolModel):
    """写回当前命名会话短期纠错记忆的最小反馈。"""

    ok: bool
    message: str


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictContractOverviewGenerationToolModel],
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
        "并检查候选名称的准确性、辨识度及摘要的事实边界；该动作不提交正式结果。"
    ),
    arguments_model=ThinkArguments,
)

SUBMIT_CONTRACT_OVERVIEW_TOOL: Final[dict[str, Any]] = _function_tool(
    name="submit_contract_overview",
    description=(
        "命名依据充分时，依次提交可核对页面证据、简洁命名理由和唯一的建议展示"
        "文件名，最后提交仅基于页面事实的合同摘要；这是合同概览任务的终止动作。"
    ),
    arguments_model=SubmitContractOverviewArguments,
)

CONTRACT_OVERVIEW_GENERATION_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    SUBMIT_CONTRACT_OVERVIEW_TOOL,
)

_ARGUMENT_MODELS: Final[dict[str, type[StrictContractOverviewGenerationToolModel]]] = {
    "think": ThinkArguments,
    "submit_contract_overview": SubmitContractOverviewArguments,
}


def parse_contract_overview_generation_tool_arguments(
    name: str,
    raw_arguments: str,
) -> ContractOverviewGenerationToolArguments:
    """执行工具前解析参数，并用本地 Pydantic 契约严格校验。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的合同概览生成工具：{name}") from exc
    try:
        payload = load_model_json(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return validate_model_payload(arguments_model, payload)


def validation_error_feedback(error: Exception) -> ContractOverviewGenerationToolFeedback:
    """把参数错误转换为包含位置、问题和修正方向的最小反馈。"""
    if not isinstance(error, ValidationError):
        return ContractOverviewGenerationToolFeedback(
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
        elif path == "summary":
            correction = "提交非空、最多 3000 个字符且仅基于合同页面事实的摘要"
        elif path == "file_name":
            correction = (
                "提交不超过 255 个字符、没有扩展名和非法字符的名称主体"
            )
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(errors) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return ContractOverviewGenerationToolFeedback(
        ok=False,
        message="\n".join(messages),
    )


__all__ = [
    "CONTRACT_OVERVIEW_GENERATION_TOOLS",
    "CONTRACT_OVERVIEW_GENERATION_TOOL_CHOICE",
    "CONTRACT_OVERVIEW_GENERATION_TOOL_PLACEMENT",
    "CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION",
    "SUBMIT_CONTRACT_OVERVIEW_TOOL",
    "THINK_TOOL",
    "ContractOverviewGenerationToolArguments",
    "ContractOverviewGenerationToolFeedback",
    "StrictContractOverviewGenerationToolModel",
    "SubmitContractOverviewArguments",
    "ContractOverviewEvidence",
    "ThinkArguments",
    "parse_contract_overview_generation_tool_arguments",
    "validation_error_feedback",
]
