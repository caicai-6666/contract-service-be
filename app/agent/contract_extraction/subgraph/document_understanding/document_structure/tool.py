"""文档结构理解子图文档结构节点的本地函数工具契约。"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


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


class StructureEvidence(StrictToolModel):
    """支持合同主题、单元内容或边界判断的页面证据。"""

    page_number: int = Field(
        description="该证据所在的 PDF 物理页码，从 1 开始；不是合同印刷页码。"
    )
    kind: EvidenceKind = Field(
        description=(
            "证据形式：text 表示页面可读原文，visual 表示无法由短原文充分表达的版式或视觉事实。"
        )
    )
    content: str = Field(
        description=(
            "可直接核对的简短原文，或 visual 类型下的简短视觉事实描述；"
            "不得填写推断、总结或无关的整页内容。"
        )
    )

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

    title: str | None = Field(
        description=(
            "合同页面明确显示的正式标题，保持原文；没有可确认标题时传 null，不得自行拟题。"
        )
    )
    subject: str = Field(
        description=(
            "合同主要讨论的交易、合作或权利义务主题，用简短中文概括；不是合同标题的重复。"
        )
    )
    summary: str = Field(
        description=(
            "供后续结构导航使用的合同整体概览，概括主要参与关系、事项和文档组成；"
            "不得引入合同外事实或法律意见。"
        )
    )

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

    evidence: list[StructureEvidence] = Field(
        description=(
            "支持合同标题、主题和整体概览的页面证据，按合同阅读顺序排列；至少提供一条。"
        )
    )
    reasoning_summary: str = Field(
        description=(
            "简洁说明页面证据如何支持合同整体认识，并指出标题缺失、冲突或其他不确定性；"
            "不得引入证据之外的新事实。"
        )
    )
    decision: DocumentScopeDecision = Field(
        description="基于前述证据和推理形成的合同标题、主题与整体概览最终决定。"
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence(
        cls, value: list[StructureEvidence]
    ) -> list[StructureEvidence]:
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

    reason: str = Field(
        description=(
            "关于尚未覆盖内容、下一步动作或当前结构边界疑点的简洁自然语言思考；"
            "不在此提交内容单元。"
        )
    )

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """think 只接收一段非空自然语言理由。"""
        if not value.strip():
            raise ValueError("思考理由不能为空")
        return value


class UnitBoundary(StrictToolModel):
    """一个内容单元的开始或结束锚点。"""

    page_number: int = Field(description="该边界锚点所在的 PDF 物理页码，从 1 开始。")
    anchor_kind: EvidenceKind = Field(
        description=(
            "边界锚点形式：text 使用页面原文，visual 使用标题、印章或版式等可核对视觉特征。"
        )
    )
    anchor: str = Field(
        description=(
            "能够唯一或大致定位单元边界的简短原文或视觉特征；不得复制整个单元正文。"
        )
    )
    inclusion: BoundaryInclusion = Field(
        description=(
            "该锚点与当前单元的关系：inclusive 表示锚点属于当前单元，"
            "exclusive 表示锚点是相邻区域开头且不属于当前单元。"
        )
    )

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


class UnitNavigationAnchor(StrictToolModel):
    """位于单元起止边界之间、供后续视觉定位使用的有序锚点。"""

    page_number: int = Field(
        description="该中间导航锚点所在的 PDF 物理页码，从 1 开始。"
    )
    anchor_kind: EvidenceKind = Field(
        description=(
            "导航锚点形式：text 使用页面原文，visual 使用可核对的版式或视觉特征。"
        )
    )
    anchor: str = Field(
        description=(
            "跨页单元内部用于保持页面顺序和后续定位的简短原文或视觉特征；"
            "不得与起止锚点重复。"
        )
    )

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
        """导航锚点必须具备可复核的文本或视觉描述。"""
        if not value.strip():
            raise ValueError("导航锚点不能为空")
        return value


class UnitSpan(StrictToolModel):
    """一个连续内容单元的边界及其可选中间导航锚点。"""

    start: UnitBoundary = Field(
        description="当前连续内容单元的起始边界，通常包含属于该单元的标题或首句。"
    )
    navigation_anchors: list[UnitNavigationAnchor] = Field(
        description=(
            "位于起止边界之间、按物理页码和阅读顺序排列的中间锚点；"
            "单页或无需中间定位时传空列表。"
        )
    )
    end: UnitBoundary = Field(
        description=(
            "当前连续内容单元的结束边界；可使用当前单元末句，或使用 inclusion=exclusive "
            "表示下一单元开头。"
        )
    )

    @model_validator(mode="after")
    def validate_page_order(self) -> UnitSpan:
        """校验边界与中间锚点遵循连续文档阅读顺序。"""
        if self.start.page_number > self.end.page_number:
            raise ValueError("单元开始页不能晚于结束页")
        navigation_pages = [anchor.page_number for anchor in self.navigation_anchors]
        if any(
            page_number < self.start.page_number or page_number > self.end.page_number
            for page_number in navigation_pages
        ):
            raise ValueError("导航锚点必须位于单元起止页范围内")
        if navigation_pages != sorted(navigation_pages):
            raise ValueError("导航锚点必须按照物理页码和阅读顺序排列")

        boundary_keys = {
            (
                self.start.page_number,
                self.start.anchor_kind,
                self.start.anchor.strip(),
            ),
            (
                self.end.page_number,
                self.end.anchor_kind,
                self.end.anchor.strip(),
            ),
        }
        navigation_keys = [
            (anchor.page_number, anchor.anchor_kind, anchor.anchor.strip())
            for anchor in self.navigation_anchors
        ]
        if any(key in boundary_keys for key in navigation_keys):
            raise ValueError("导航锚点不能重复 start 或 end 边界锚点")
        if len(set(navigation_keys)) != len(navigation_keys):
            raise ValueError("navigation_anchors 中不能出现重复锚点")
        return self


class UnitDecision(StrictToolModel):
    """一个宏观内容单元的最终决定。"""

    label: str = Field(
        description=(
            "用于后续导航的简短单元名称，概括该连续区域的功能；"
            "不是逐条款编号或整段正文。"
        )
    )
    summary: str = Field(
        description=(
            "对该宏观连续区域所讨论内容的简短概括；不得逐条提取条款或引入合同外事实。"
        )
    )
    span: UnitSpan = Field(
        description="该单元从开始到结束的连续页面边界及必要的中间导航锚点。"
    )

    @field_validator("label", "summary")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """内容单元必须具备有效名称和概览。"""
        if not value.strip():
            raise ValueError("单元名称与概览不能为空")
        return value


class GenerateUnitArguments(StrictToolModel):
    """generate_unit 每次只能提交一个内容单元。"""

    evidence: list[StructureEvidence] = Field(
        description=(
            "支持当前宏观单元的主题和边界判断的页面证据，按阅读顺序排列；至少提供一条。"
        )
    )
    reasoning_summary: str = Field(
        description=(
            "简洁说明证据为何共同构成一个宏观连续单元，以及为何不应按条款、自然段或换页过细拆分。"
        )
    )
    decision: UnitDecision = Field(
        description="基于前述证据和推理形成的单元名称、内容概览与连续范围最终决定。"
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence(
        cls, value: list[StructureEvidence]
    ) -> list[StructureEvidence]:
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

    reason: str = Field(
        description=(
            "说明合同宏观内容为何已经全部覆盖，以及最后检查到的内容区域；"
            "提交后仍需通过程序覆盖校验。"
        )
    )

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
    SummaryArguments | ThinkArguments | GenerateUnitArguments | FinishArguments
)


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictToolModel],
) -> dict[str, Any]:
    """把 Pydantic 参数模型转换为 OpenAI 兼容的非 strict function tool。

    状态机通过具名首轮选择与后续 required 工具选择约束动作出口；
    参数结构与业务语义继续由本地 Pydantic 和节点校验统一兜底。
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments_model.model_json_schema(),
            "strict": False,
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
        "一次提交一个用于合同内容导航的宏观连续内容单元。不得仅因条款编号、自然段或换页而拆分。"
    ),
    arguments_model=GenerateUnitArguments,
)

FINISH_TOOL: Final[dict[str, Any]] = _function_tool(
    name="finish",
    description=(
        "认为合同宏观内容已经覆盖完成时调用此工具完成单元发现；程序仍会校验当前状态并决定是否接受。"
    ),
    arguments_model=FinishArguments,
)

FIRST_ROUND_TOOLS: Final[tuple[dict[str, Any], ...]] = (SUMMARY_TOOL,)
DISCOVERY_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    GENERATE_UNIT_TOOL,
    FINISH_TOOL,
)

# 所有工具请求均使用 auto + non-strict，绕过 vLLM 的 XGrammar 解码路径。
# 首轮必须调用 summary、后续必须调用一个发现工具的业务约束由提示词和本地
# 状态机执行；未形成合法工具调用时会给出短期记忆反馈并有限重试。
FIRST_ROUND_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO
DISCOVERY_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

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
    "DISCOVERY_TOOLS",
    "DISCOVERY_TOOL_CHOICE",
    "FINISH_TOOL",
    "FIRST_ROUND_TOOLS",
    "FIRST_ROUND_TOOL_CHOICE",
    "GENERATE_UNIT_TOOL",
    "SUMMARY_TOOL",
    "THINK_TOOL",
    "FinishArguments",
    "GenerateUnitArguments",
    "SummaryArguments",
    "ThinkArguments",
    "ToolFeedback",
    "UnitNavigationAnchor",
    "parse_tool_arguments",
]
