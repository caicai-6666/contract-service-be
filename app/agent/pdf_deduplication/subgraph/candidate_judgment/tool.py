"""全量双 PDF 关系判断使用的 Pydantic function tools。"""

from __future__ import annotations

import json
from typing import Any, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictCandidateJudgmentToolModel(BaseModel):
    """拒绝额外参数与宽松类型转换的候选判断工具参数基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


StrictFullDocumentToolModel = StrictCandidateJudgmentToolModel


FULL_DOCUMENT_JUDGMENT_TOOL_VERSION: Final = "full-document-relation-tool-v3"


class ContractRelationEvidence(StrictCandidateJudgmentToolModel):
    """能够在两份合同页面中直接复核的一项跨文档证据。"""

    uploaded_page_number: int = Field(
        ge=1,
        description="该证据所在上传合同 A 页面的物理页码，从 1 开始。",
    )
    candidate_page_number: int = Field(
        ge=1,
        description="该证据所在候选合同 B 页面的物理页码，从 1 开始。",
    )
    observation: str = Field(
        max_length=500,
        description=(
            "在 A、B 两个所列页面中可直接核对的简短原文、数字、签章或版式对比；"
            "必须同时说明两侧观察，不得填写推断、关系结论或复制整页内容。"
        ),
    )

    @field_validator("observation")
    @classmethod
    def validate_observation(cls, value: str) -> str:
        """跨文档证据必须是非空的精简观察。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("跨文档证据观察不能为空")
        return normalized


class ThinkArguments(StrictCandidateJudgmentToolModel):
    """单轮 think 的真实推理工作空间。"""

    reasoning: str = Field(
        description=(
            "实际分析与推理；用于比较双侧页面证据、建立或排除 duplicate、similar、"
            "different 假设、分析版本连续性和冲突并选择下一步动作。应保持简洁，"
            "使包含工具结构在内的整轮响应不超过 1024 completion tokens；"
            "这里不提交正式关系决定。"
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


ContractRelation: TypeAlias = Literal["duplicate", "similar", "different"]


class SubmitContractRelationArguments(StrictCandidateJudgmentToolModel):
    """证据充分时提交唯一的三分类合同关系。"""

    evidence: list[ContractRelationEvidence] = Field(
        min_length=1,
        max_length=20,
        description=(
            "支持最终关系的跨文档页面证据，按重要性排列；每项必须同时引用 A、B 的物理页码。"
        ),
    )
    reasoning_summary: str = Field(
        max_length=2000,
        description=(
            "简洁说明前述证据如何满足共同关系标准，并保留会影响结论的差异、冲突或不确定性；"
            "不得引入页面中没有的新事实。"
        ),
    )
    decision: ContractRelation = Field(
        description=(
            "最终合同关系：duplicate 表示同一合同或同一版本链且不应并存；"
            "similar 表示明确相关或高度相似但应独立保存；different 表示合同身份和交易事项独立。"
        ),
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        """正式决定必须携带非空、可审计的推理摘要。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("关系判断推理摘要不能为空")
        return normalized


UnableToDetermineReason: TypeAlias = Literal[
    "unreadable_pages",
    "missing_critical_evidence",
    "conflicting_evidence",
]


class ReportUnableToDetermineRelationArguments(StrictCandidateJudgmentToolModel):
    """材料本身不足以可靠三分类时提交失败出口。"""

    evidence: list[ContractRelationEvidence] = Field(
        min_length=1,
        max_length=20,
        description=(
            "能够直接证明页面不可读、关键内容缺失或证据冲突的跨文档观察；"
            "每项必须同时引用 A、B 的物理页码，不能只写抽象原因。"
        ),
    )
    reasoning_summary: str = Field(
        max_length=2000,
        description=(
            "说明已经核对哪些关键事实、为何现有材料仍无法充分支持任一三分类关系，"
            "以及无法由其他可用页面消解的具体不确定性。"
        ),
    )
    reason: UnableToDetermineReason = Field(
        description=(
            "无法判断的主要原因：unreadable_pages 表示关键页面不可读；"
            "missing_critical_evidence 表示当前文件缺少区分关系所需的关键证据；"
            "conflicting_evidence 表示关键证据相互冲突且无法消解。"
        ),
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        """失败出口也必须说明完整的证据核对过程。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("无法判断的推理摘要不能为空")
        return normalized


FullDocumentToolArguments: TypeAlias = (
    ThinkArguments
    | SubmitContractRelationArguments
    | ReportUnableToDetermineRelationArguments
)


def build_candidate_judgment_function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictCandidateJudgmentToolModel],
) -> dict[str, Any]:
    """从唯一的 Pydantic 参数契约生成 OpenAI 兼容函数工具。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            # 本地仍执行 strict Pydantic 校验；关闭服务端 strict Schema 解码，
            # 避免 vLLM/XGrammar 与 Qwen XML 工具协议互相干扰。
            "strict": False,
        },
    }


THINK_TOOL: Final[dict[str, Any]] = build_candidate_judgment_function_tool(
    name="think",
    description=(
        "提供一次真实推理空间，用于比较双侧证据与关系假设；应保持简洁，"
        "使包含工具结构在内的整轮响应不超过 1024 completion tokens。"
        "该动作不提交正式结果。"
    ),
    arguments_model=ThinkArguments,
)

SUBMIT_CONTRACT_RELATION_TOOL: Final[dict[str, Any]] = (
    build_candidate_judgment_function_tool(
        name="submit_contract_relation",
        description=(
            "证据充分时，依次提交跨文档页面证据、简洁推理摘要和唯一的三分类合同关系。"
        ),
        arguments_model=SubmitContractRelationArguments,
    )
)

REPORT_UNABLE_TO_DETERMINE_RELATION_TOOL: Final[dict[str, Any]] = (
    build_candidate_judgment_function_tool(
        name="report_unable_to_determine_relation",
        description=(
            "仅在至少一次 think 后，因页面不可读、关键证据缺失或证据冲突而无法可靠三分类时退出。"
        ),
        arguments_model=ReportUnableToDetermineRelationArguments,
    )
)

FULL_DOCUMENT_JUDGMENT_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    SUBMIT_CONTRACT_RELATION_TOOL,
    REPORT_UNABLE_TO_DETERMINE_RELATION_TOOL,
)
FULL_DOCUMENT_JUDGMENT_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

_ARGUMENT_MODELS: Final[dict[str, type[StrictCandidateJudgmentToolModel]]] = {
    "think": ThinkArguments,
    "submit_contract_relation": SubmitContractRelationArguments,
    "report_unable_to_determine_relation": ReportUnableToDetermineRelationArguments,
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


def parse_full_document_tool_arguments(
    name: str,
    raw_arguments: str,
) -> FullDocumentToolArguments:
    """执行工具前用本地 Pydantic 契约重新解析并严格校验参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的全量合同关系判断工具：{name}") from exc
    return parse_candidate_judgment_arguments(
        name=name,
        raw_arguments=raw_arguments,
        arguments_model=arguments_model,
    )


def parse_candidate_judgment_arguments(
    *,
    name: str,
    raw_arguments: str,
    arguments_model: type[StrictCandidateJudgmentToolModel],
) -> StrictCandidateJudgmentToolModel:
    """解析任一候选判断工具参数，并兼容嵌套 JSON 字符串。"""
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


__all__ = [
    "ContractRelation",
    "ContractRelationEvidence",
    "StrictCandidateJudgmentToolModel",
    "StrictFullDocumentToolModel",
    "FULL_DOCUMENT_JUDGMENT_TOOLS",
    "FULL_DOCUMENT_JUDGMENT_TOOL_CHOICE",
    "FULL_DOCUMENT_JUDGMENT_TOOL_VERSION",
    "FullDocumentToolArguments",
    "REPORT_UNABLE_TO_DETERMINE_RELATION_TOOL",
    "ReportUnableToDetermineRelationArguments",
    "SUBMIT_CONTRACT_RELATION_TOOL",
    "SubmitContractRelationArguments",
    "THINK_TOOL",
    "ThinkArguments",
    "UnableToDetermineReason",
    "build_candidate_judgment_function_tool",
    "parse_candidate_judgment_arguments",
    "parse_full_document_tool_arguments",
]
