"""候选 PDF 页面导航与观察记录使用的 Pydantic function tools。"""

from __future__ import annotations

from typing import Any, Final, TypeAlias

from pydantic import Field, field_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO
from app.agent.pdf_deduplication.subgraph.candidate_judgment.tool import (
    FULL_DOCUMENT_JUDGMENT_TOOLS,
    ContractRelationEvidence,
    ReportUnableToDetermineRelationArguments,
    StrictCandidateJudgmentToolModel,
    SubmitContractRelationArguments,
    ThinkArguments,
    build_candidate_judgment_function_tool,
    parse_candidate_judgment_arguments,
)

PAGE_NAVIGATION_JUDGMENT_TOOL_VERSION: Final = (
    "candidate-page-navigation-tool-v2"
)


class InspectCandidatePagesArguments(StrictCandidateJudgmentToolModel):
    """请求查看一批候选合同 B 页面。"""

    page_numbers: list[int] = Field(
        min_length=1,
        max_length=3,
        description=(
            "本轮要打开的候选合同 B 页面的物理页码，从 1 开始；必须升序、"
            "不能重复且不能超出当前候选指南给出的总页数，一次最多三页。"
        ),
    )
    purpose: str = Field(
        max_length=500,
        description=(
            "查看本批 B 页要解决的具体核对问题，例如合同身份、交易金额、"
            "版本说明、条款冲突或文件边界；不得只写“继续查看”或提前提交关系结论。"
        ),
    )
    revisit_reason: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "本批包含已查看且当前隐藏的 B 页面时，说明重新打开该页所要补充的"
            "视觉细节或待消解冲突；全部页面均为首次查看时必须为 null。"
        ),
    )

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: list[int]) -> list[int]:
        """页面必须使用严格递增的正整数，候选总页数由节点继续校验。"""
        if any(page_number <= 0 for page_number in value):
            raise ValueError("候选合同 B 页码必须从 1 开始")
        if value != sorted(set(value)):
            raise ValueError("候选合同 B 页码必须升序且不能重复")
        return value

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str) -> str:
        """查看页面必须携带非空且有行动意义的目的。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("候选页面查看目的不能为空")
        return normalized

    @field_validator("revisit_reason")
    @classmethod
    def normalize_revisit_reason(cls, value: str | None) -> str | None:
        """可选复查原因存在时必须包含实际内容。"""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("候选页面复查原因不能为空字符串")
        return normalized


class RecordCandidatePageObservationsArguments(
    StrictCandidateJudgmentToolModel
):
    """保存当前可见候选页的精简观察并结束当前视觉批次。"""

    observations: list[ContractRelationEvidence] = Field(
        default_factory=list,
        max_length=12,
        description=(
            "从当前可见 B 页面与完整上传合同 A 中直接核对出的跨文档观察，"
            "按重要性排列，最多十二项；本批没有可复用证据时提交空列表，"
            "不得引用未查看或当前已隐藏的 B 页面。"
        ),
    )
    next_focus: str = Field(
        max_length=500,
        description=(
            "隐藏当前 B 页面后下一步需要核对的唯一主要问题；本批 observations"
            " 为空时还必须简要说明本批为何没有可复用观察。不得在此提交最终关系。"
        ),
    )

    @field_validator("next_focus")
    @classmethod
    def validate_next_focus(cls, value: str) -> str:
        """工作区下一步必须是非空的有限导航方向。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("候选页面记录后的下一步关注点不能为空")
        return normalized

PageNavigationToolArguments: TypeAlias = (
    InspectCandidatePagesArguments
    | RecordCandidatePageObservationsArguments
    | ThinkArguments
    | SubmitContractRelationArguments
    | ReportUnableToDetermineRelationArguments
)

INSPECT_CANDIDATE_PAGES_TOOL: Final[dict[str, Any]] = (
    build_candidate_judgment_function_tool(
        name="inspect_candidate_pages",
        description=(
            "按具体核对目的打开最多三页候选合同 B 页面；也可在说明复查原因后"
            "重新打开当前已隐藏的页面。该动作只提供页面，不记录观察或提交关系。"
        ),
        arguments_model=InspectCandidatePagesArguments,
    )
)

RECORD_CANDIDATE_PAGE_OBSERVATIONS_TOOL: Final[dict[str, Any]] = (
    build_candidate_judgment_function_tool(
        name="record_candidate_page_observations",
        description=(
            "把当前可见候选页面产生的精简跨文档观察和下一步关注点写入工作区；"
            "动作被接受后当前候选页面将隐藏，但不会提交最终关系。"
        ),
        arguments_model=RecordCandidatePageObservationsArguments,
    )
)

PAGE_NAVIGATION_JUDGMENT_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    INSPECT_CANDIDATE_PAGES_TOOL,
    RECORD_CANDIDATE_PAGE_OBSERVATIONS_TOOL,
    *FULL_DOCUMENT_JUDGMENT_TOOLS,
)
PAGE_NAVIGATION_JUDGMENT_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

_PAGE_NAVIGATION_ARGUMENT_MODELS: Final[
    dict[str, type[StrictCandidateJudgmentToolModel]]
] = {
    "inspect_candidate_pages": InspectCandidatePagesArguments,
    "record_candidate_page_observations": (
        RecordCandidatePageObservationsArguments
    ),
    "think": ThinkArguments,
    "submit_contract_relation": SubmitContractRelationArguments,
    "report_unable_to_determine_relation": (
        ReportUnableToDetermineRelationArguments
    ),
}


def parse_page_navigation_tool_arguments(
    name: str,
    raw_arguments: str,
) -> PageNavigationToolArguments:
    """用五项导航工具的严格 Pydantic 契约解析模型参数。"""
    try:
        arguments_model = _PAGE_NAVIGATION_ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的候选页面导航工具：{name}") from exc
    return parse_candidate_judgment_arguments(
        name=name,
        raw_arguments=raw_arguments,
        arguments_model=arguments_model,
    )


__all__ = [
    "INSPECT_CANDIDATE_PAGES_TOOL",
    "PAGE_NAVIGATION_JUDGMENT_TOOLS",
    "PAGE_NAVIGATION_JUDGMENT_TOOL_CHOICE",
    "PAGE_NAVIGATION_JUDGMENT_TOOL_VERSION",
    "RECORD_CANDIDATE_PAGE_OBSERVATIONS_TOOL",
    "InspectCandidatePagesArguments",
    "PageNavigationToolArguments",
    "RecordCandidatePageObservationsArguments",
    "parse_page_navigation_tool_arguments",
]
