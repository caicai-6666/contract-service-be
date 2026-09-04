"""合同文档识别 Agent 使用的 Pydantic function tools。"""

from __future__ import annotations

import json
from typing import Any, Final, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictContractDocumentDetectionToolModel(BaseModel):
    """拒绝额外参数与宽松类型转换的合同文档识别工具基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


CONTRACT_DOCUMENT_DETECTION_TOOL_VERSION: Final = (
    "contract-document-detection-tool-v2"
)


class ContractDocumentEvidence(StrictContractDocumentDetectionToolModel):
    """能够从上传文档页面直接复核的一项合同属性证据。"""

    page_number: int = Field(
        ge=1,
        description="该证据所在上传文档页面的物理页码，从 1 开始。",
    )
    observation: str = Field(
        max_length=500,
        description=(
            "该页可直接观察的简短文字、数字、表格、签章或版式事实；"
            "应说明它体现的相对方关系、权利义务、文档性质或缺失的协议结构，"
            "不得填写脱离页面的猜测或复制整页内容。"
        ),
    )

    @field_validator("observation")
    @classmethod
    def validate_observation(cls, value: str) -> str:
        """页面证据必须包含去除首尾空白后的实际内容。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("合同文档识别证据不能为空")
        return normalized


class ThinkArguments(StrictContractDocumentDetectionToolModel):
    """单轮 think 的真实推理工作空间。"""

    reasoning: str = Field(
        description=(
            "实际分析与推理；用于综合全部可用页面、核对相对方关系和实质性"
            "权利义务、排除仅有标题或签章等弱线索，并选择下一步动作。"
            "应保持简洁，使包含工具结构在内的整轮响应不超过 1024 completion "
            "tokens；这里不提交最终是否为合同的决定。"
        ),
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        """拒绝没有实际内容的 think 调用。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("think 推理不能为空")
        return normalized


class SubmitContractDocumentJudgmentArguments(
    StrictContractDocumentDetectionToolModel
):
    """证据充分时提交唯一的是或否合同判断。"""

    evidence: list[ContractDocumentEvidence] = Field(
        min_length=1,
        max_length=20,
        description=(
            "支持最终判断的页面证据，按重要性排列；判定为合同时至少应覆盖"
            "相对方关系和实质性权利义务，判定为非合同时应覆盖实际文档性质"
            "以及缺失的决定性协议结构。"
        ),
    )
    reasoning_summary: str = Field(
        max_length=2000,
        description=(
            "简洁说明前述证据如何满足或不满足合同文档标准，并保留会影响"
            "判断的冲突或限制；不得引入页面中没有的新事实。"
        ),
    )
    is_contract: bool = Field(
        description=(
            "最终二分类决定：true 表示上传文档属于合同类文档，false 表示"
            "它属于缺少相对方协议性权利义务结构的非合同文档。"
        ),
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        """正式判断必须携带非空的精简推理摘要。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("合同文档识别推理摘要不能为空")
        return normalized


ContractDocumentDetectionToolArguments: TypeAlias = (
    ThinkArguments | SubmitContractDocumentJudgmentArguments
)


class ContractDocumentDetectionToolFeedback(
    StrictContractDocumentDetectionToolModel
):
    """写回当前 Agent 短期纠错记忆的最小工具反馈。"""

    ok: bool
    message: str


def build_contract_document_detection_function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictContractDocumentDetectionToolModel],
) -> dict[str, Any]:
    """从唯一的 Pydantic 参数契约生成 OpenAI 兼容函数工具。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            # vLLM/Qwen 使用自身的 XML 工具协议；本地 Pydantic 仍执行严格校验。
            "strict": False,
        },
    }


THINK_TOOL: Final[dict[str, Any]] = (
    build_contract_document_detection_function_tool(
        name="think",
        description=(
            "提供一次真实推理空间，用于综合页面证据和检验合同判断假设；"
            "应保持简洁，使包含工具结构在内的整轮响应不超过 1024 completion "
            "tokens。该动作不提交正式结果。"
        ),
        arguments_model=ThinkArguments,
    )
)

SUBMIT_CONTRACT_DOCUMENT_JUDGMENT_TOOL: Final[dict[str, Any]] = (
    build_contract_document_detection_function_tool(
        name="submit_contract_document_judgment",
        description=(
            "证据充分时，依次提交页面证据、简洁推理摘要和唯一的是或否合同判断。"
        ),
        arguments_model=SubmitContractDocumentJudgmentArguments,
    )
)

CONTRACT_DOCUMENT_DETECTION_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    SUBMIT_CONTRACT_DOCUMENT_JUDGMENT_TOOL,
)
CONTRACT_DOCUMENT_DETECTION_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

_ARGUMENT_MODELS: Final[
    dict[str, type[StrictContractDocumentDetectionToolModel]]
] = {
    "think": ThinkArguments,
    "submit_contract_document_judgment": (
        SubmitContractDocumentJudgmentArguments
    ),
}


def _decode_embedded_json(value: Any) -> Any:
    """兼容 Qwen XML parser 把嵌套对象编码成 JSON 字符串。"""
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


def parse_contract_document_detection_tool_arguments(
    name: str,
    raw_arguments: str,
) -> ContractDocumentDetectionToolArguments:
    """执行工具前解析参数，并用本地 Pydantic 契约严格校验。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的合同文档识别工具：{name}") from exc

    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def validation_error_feedback(
    error: Exception,
) -> ContractDocumentDetectionToolFeedback:
    """把参数错误转换为包含字段位置和修正方向的最小反馈。"""
    if not isinstance(error, ValidationError):
        return ContractDocumentDetectionToolFeedback(
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
            correction = "填写大于等于 1 的真实文档页面物理页码"
        elif "evidence" in path:
            correction = "提供至少一条简短、可直接复核的页面证据"
        elif path == "is_contract":
            correction = "填写 JSON 布尔值 true 或 false"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(errors) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return ContractDocumentDetectionToolFeedback(
        ok=False,
        message="\n".join(messages),
    )


__all__ = [
    "CONTRACT_DOCUMENT_DETECTION_TOOLS",
    "CONTRACT_DOCUMENT_DETECTION_TOOL_CHOICE",
    "CONTRACT_DOCUMENT_DETECTION_TOOL_VERSION",
    "SUBMIT_CONTRACT_DOCUMENT_JUDGMENT_TOOL",
    "THINK_TOOL",
    "ContractDocumentDetectionToolArguments",
    "ContractDocumentDetectionToolFeedback",
    "ContractDocumentEvidence",
    "StrictContractDocumentDetectionToolModel",
    "SubmitContractDocumentJudgmentArguments",
    "ThinkArguments",
    "build_contract_document_detection_function_tool",
    "parse_contract_document_detection_tool_arguments",
    "validation_error_feedback",
]
