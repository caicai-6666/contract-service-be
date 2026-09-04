"""条款候选顺序发现节点的严格函数工具契约。"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Final, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictClauseToolModel(BaseModel):
    """禁止额外参数和宽松类型转换的条款工具模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


CLAUSE_DISCOVERY_TOOL_VERSION: Final = "clause-discovery-tool-v10"
CLAUSE_DISCOVERY_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO
CLAUSE_CONTENT_TOOL_VERSION: Final = "clause-content-tool-v10"
# 条款正文是长文本并发任务。保持 auto，配合非 strict 工具，避开 vLLM
# XGrammar 的逐 token Schema 约束；程序仍在调用后严格解析并校验正文边界。
CLAUSE_CONTENT_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO


class ClauseBoundaryAnchor(StrictClauseToolModel):
    """一个不承载完整正文的精简条款边界锚点。"""

    page_number: int = Field(
        ge=1,
        description="该边界锚点所在合同页面的物理页码，从 1 开始；不是页面中印刷的页码。",
    )
    anchor: str = Field(
        max_length=160,
        description=(
            "足以定位边界的页面原文短片段，最多 160 个字符；"
            "应包含必要的编号、标题或相邻文字，不得复制完整条款正文。"
        ),
    )

    @field_validator("anchor")
    @classmethod
    def validate_anchor(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("条款边界锚点不能为空")
        return normalized


class ClauseBoundaryEvidence(StrictClauseToolModel):
    """供后续正文提取复用的一组条款起止边界证据。"""

    start: ClauseBoundaryAnchor = Field(
        description=(
            "当前条款的包含式起始锚点，应定位到编号、标题或首句；该锚点始终属于当前条款。"
        )
    )
    end: ClauseBoundaryAnchor = Field(
        description=(
            "当前条款自身最后一段原文的必填包含式锚点；必须属于当前条款，"
            "不得使用下一条款、签署区、附件或其他非当前条款内容作为结束锚点。"
        )
    )

    @model_validator(mode="after")
    def validate_page_order(self) -> ClauseBoundaryEvidence:
        if self.start.page_number > self.end.page_number:
            raise ValueError("条款起始锚点页码不能晚于结束锚点页码")
        return self


class ClauseCompletionEvidence(StrictClauseToolModel):
    """支持“条款区域已经检查完毕”的末尾页面证据。"""

    page_number: int = Field(
        ge=1,
        description="结束检查证据所在合同页面的物理页码，从 1 开始。",
    )
    content: str = Field(
        max_length=200,
        description=(
            "条款区域末尾或随后非条款区域的简短可核对原文，例如附件标题或签署标题；"
            "最多 200 个字符，不得复制完整页面或条款正文。"
        ),
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("结束检查证据不能为空")
        return normalized


class ClauseHierarchyObservation(StrictClauseToolModel):
    """首轮层级分析使用的一条页面结构观察。"""

    page_numbers: list[int] = Field(
        min_length=1,
        max_length=20,
        description=(
            "支持当前结构观察的合同页面物理页码，按升序填写且不得重复；"
            "页码从 1 开始，不使用合同印刷页码。"
        ),
    )
    observation: str = Field(
        max_length=500,
        description=(
            "从所列页面直接观察到的编号体系、标题、缩进、项目符号、表格、"
            "条款区域或非条款区域；只写可见事实，不在此给出层级结论或复制完整正文。"
        ),
    )

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: list[int]) -> list[int]:
        if any(page_number < 1 for page_number in value):
            raise ValueError("层级观察页码必须大于等于 1")
        if value != sorted(value):
            raise ValueError("层级观察页码必须按升序填写")
        if len(value) != len(set(value)):
            raise ValueError("层级观察页码不能重复")
        return value

    @field_validator("observation")
    @classmethod
    def validate_observation(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("页面结构观察不能为空")
        return normalized


class ClauseHierarchyGuidance(StrictClauseToolModel):
    """写入工作区并约束后续候选发现的最终层级指导。"""

    structure_summary: str = Field(
        max_length=2000,
        description=(
            "整份合同条款组织结构的简明结论，包括主要条款区域、局部编号体系、"
            "预计最大层级和跨页延续；不得列出尚未逐条确认的完整候选目录。"
        ),
    )
    extraction_guidance: list[str] = Field(
        min_length=1,
        max_length=20,
        description=(
            "后续逐条发现必须遵循的合同专属指导原则，按重要性排序；应明确同级模式、"
            "父子模式、编号重置、特殊视觉列表、没有自身直接正文的纯结构标题，"
            "以及需要排除的非条款区域，不重复通用提示词。"
        ),
    )

    @field_validator("structure_summary")
    @classmethod
    def validate_structure_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("合同层级结构摘要不能为空")
        return normalized

    @field_validator("extraction_guidance")
    @classmethod
    def validate_extraction_guidance(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("层级提取指导不能包含空字符串")
        if len(normalized) != len(set(normalized)):
            raise ValueError("层级提取指导不能包含重复条目")
        return normalized


class AnalyzeClauseHierarchyArguments(StrictClauseToolModel):
    """首轮详细分析整份合同条款组织层级的证据优先参数。"""

    evidence: list[ClauseHierarchyObservation] = Field(
        min_length=1,
        max_length=20,
        description=(
            "覆盖整份合同主要版式和编号变化的页面结构观察；证据应足以支持后续层级分析。"
        ),
    )
    reasoning_summary: str = Field(
        max_length=4000,
        description=(
            "基于页面观察详细分析局部编号体系、同级序列、真实父级、编号重置、"
            "跨页延续、视觉列表及非条款区域；比较可能冲突的结构信号并说明取舍，"
            "不得逐条提取完整条款正文。"
        ),
    )
    decision: ClauseHierarchyGuidance = Field(
        description=(
            "由前述证据与分析形成的合同专属层级摘要和后续提取指导；"
            "该对象会写入工作区并在每轮候选发现时持续提供给模型。"
        ),
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("合同层级分析推理摘要不能为空")
        return normalized


class ThinkArguments(StrictClauseToolModel):
    """think 只记录下一条款发现动作的自然语言推理。"""

    reasoning: str = Field(
        description=(
            "关于下一个尚未记录条款的简洁自然语言思考，先判断移除下级正文后是否仍有"
            "当前条款自己的直接正文，再判断起止边界、原合同绝对层级、完整文档路径和"
            "最近的已记录正文祖先；纯结构标题不生成候选但必须保留在下级路径中，"
            "不在此写入工作区或提取完整正文。"
        )
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("条款发现思考不能为空")
        return normalized


class ClauseDocumentPathSegment(StrictClauseToolModel):
    """原合同层级路径中的一个可见结构段，包括可被跳过的纯标题。"""

    identifier: str = Field(
        max_length=120,
        description=(
            "该层在合同页面中可见的原始编号或稳定标识；纯标题没有编号时使用其简短原文标题，"
            "不得改写成推测编号或复制正文。"
        ),
    )
    title_hint: str | None = Field(
        max_length=120,
        description=(
            "该层在合同页面中明确可见或可可靠概括的简短主题；无法确认时传 null。"
            "纯结构标题即使不进入正文候选，也必须在路径中保留。"
        ),
    )

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("文档层级路径标识不能为空")
        return normalized

    @field_validator("title_hint")
    @classmethod
    def normalize_title_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("未知的路径主题应传 null，不应传空字符串")
        return normalized


class ClauseCandidateDecision(StrictClauseToolModel):
    """模型提交的一个正文候选及其原合同绝对层级，不包含程序身份。"""

    identifier: str = Field(
        max_length=120,
        description=(
            "用于稳定识别并关联当前条款的标识：优先保留原始编号；无编号时使用简短稳定描述，"
            "不得包含完整条款正文。"
        ),
    )
    title_hint: str | None = Field(
        max_length=120,
        description=(
            "对条款主题的简短提示；优先保留明确原文标题，无标题且无法可靠概括时传 null。"
        ),
    )
    document_path: list[ClauseDocumentPathSegment] = Field(
        min_length=1,
        max_length=12,
        description=(
            "从原合同最外层条款到当前候选自身的完整结构路径，按层级由浅到深排列；"
            "最后一项必须就是当前 identifier/title_hint。路径必须保留没有自身直接正文、"
            "因而未进入候选目录的纯编号或标题，父标题是否提取不得改变路径或 level。"
        ),
    )
    parent_candidate_id: str | None = Field(
        description=(
            "当前候选在已记录正文候选中的最近祖先 candidate_id，用于正文去重；"
            "只能引用工作区中 document_path 是当前路径真前缀且层级最深的候选。"
            "若原合同父级均为未记录的纯结构标题，则传 null；该值为空不表示 level 为 1。"
        )
    )
    level: int = Field(
        ge=1,
        description=(
            "当前候选在原合同中的绝对结构深度，最外层为 1，并且必须等于 document_path"
            " 的项目数；纯结构父级是否进入正文候选都不得使当前候选升级或降级。"
        ),
    )

    @field_validator("identifier")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("条款标识不能为空")
        if normalized.casefold() in {"null", "none", "n/a", "na"} or normalized in {
            "不记录",
            "跳过",
        }:
            raise ValueError(
                "条款标识不能使用空值或排除项占位文本；非条款内容不得调用记录工具"
            )
        return normalized

    @field_validator("title_hint", "parent_candidate_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("未知的可选文本应传 null，不应传空字符串")
        return normalized

    @model_validator(mode="after")
    def validate_document_path(self) -> ClauseCandidateDecision:
        if self.level != len(self.document_path):
            raise ValueError("level 必须等于 document_path 的项目数")
        current = self.document_path[-1]
        if current.identifier != self.identifier:
            raise ValueError("document_path 最后一项 identifier 必须等于当前 identifier")
        if current.title_hint != self.title_hint:
            raise ValueError("document_path 最后一项 title_hint 必须等于当前 title_hint")
        return self


class CandidateDecisionArguments(StrictClauseToolModel):
    """记录和修正工具共享的证据优先参数。"""

    evidence: ClauseBoundaryEvidence = Field(
        description=(
            "当前条款唯一的一组精简起止锚点，既用于证明候选边界，也会原样保存到工作区"
            "并传给后续细节提取；不得承载完整条款正文。"
        ),
    )
    reasoning_summary: str = Field(
        description=(
            "先说明移除全部下级正文后仍有哪些文字直接属于当前候选，并据此确认它不是"
            "纯编号或标题；再简洁说明起止锚点、父级和层级。不得重复或补写完整正文。"
        )
    )
    decision: ClauseCandidateDecision = Field(
        description=(
            "基于前述边界证据和推理形成的条款标识、主题提示、父级与层级决定；"
            "程序身份和顺序不由模型提交。"
        )
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("推理摘要不能为空")
        return normalized


class RecordClauseCandidateArguments(CandidateDecisionArguments):
    """按阅读顺序记录一个新条款候选。"""


class ReviseLastClauseCandidateArguments(CandidateDecisionArguments):
    """只替换工作区最后一个条款候选。"""


class ClauseDiscoveryCompletion(StrictClauseToolModel):
    """模型检查合同条款区域末尾后提交的完成位置。"""

    last_checked_page: int = Field(
        ge=1,
        description=(
            "实际完成条款候选检查的最后合同页面物理页码，不得早于工作区最后一个候选的起始页。"
        ),
    )
    last_checked_anchor: str = Field(
        max_length=200,
        description=(
            "证明条款区域已经结束的短原文或视觉位置描述，例如附件或签署区域标题；"
            "不得复制完整页面。"
        ),
    )

    @field_validator("last_checked_anchor")
    @classmethod
    def validate_last_checked_anchor(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("最后检查位置的锚点不能为空")
        return normalized


class FinishClauseDiscoveryArguments(StrictClauseToolModel):
    """完成条款候选发现的证据优先参数。"""

    evidence: list[ClauseCompletionEvidence] = Field(
        min_length=1,
        max_length=3,
        description=(
            "支持条款区域已经检查完毕的末尾证据；至少一条必须来自 last_checked_page。"
        ),
    )
    reasoning_summary: str = Field(
        description=(
            "简洁说明为什么工作区已经覆盖全部主条款和子层级条款，以及为何后续区域不再包含条款。"
        )
    )
    decision: ClauseDiscoveryCompletion = Field(
        description="基于末尾证据和覆盖检查形成的最后检查页与结束锚点最终决定。"
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("完成判断的推理摘要不能为空")
        return normalized


class ClauseCandidateWorkspaceItem(StrictClauseToolModel):
    """程序追加到长记忆工作区尾部的精简条款候选。"""

    candidate_id: str
    order: int = Field(ge=1)
    identifier: str
    title_hint: str | None
    document_path: tuple[ClauseDocumentPathSegment, ...]
    parent_candidate_id: str | None
    level: int = Field(ge=1)
    evidence: ClauseBoundaryEvidence

    @field_validator("document_path", mode="before")
    @classmethod
    def normalize_document_path(
        cls,
        value: Any,
    ) -> tuple[Any, ...]:
        """JSON/YAML 数组进入不可变工作区时统一冻结为元组。"""
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_document_path(self) -> ClauseCandidateWorkspaceItem:
        if self.level != len(self.document_path):
            raise ValueError("候选 level 必须等于 document_path 的项目数")
        if not self.document_path:
            raise ValueError("候选 document_path 不能为空")
        current = self.document_path[-1]
        if current.identifier != self.identifier or current.title_hint != self.title_hint:
            raise ValueError("候选 document_path 最后一项必须表示候选自身")
        return self


class ClauseDiscoveryToolFeedback(StrictClauseToolModel):
    """写回当前工具会话的最小反馈。"""

    ok: bool
    message: str


ClauseDiscoveryToolArguments: TypeAlias = (
    AnalyzeClauseHierarchyArguments
    | ThinkArguments
    | RecordClauseCandidateArguments
    | ReviseLastClauseCandidateArguments
    | FinishClauseDiscoveryArguments
)


class ClauseDiscoveryToolError(ValueError):
    """包含错误位置、问题与修正方向的工作区校验错误。"""

    def __init__(self, path: str, problem: str, correction: str) -> None:
        super().__init__(problem)
        self.path = path
        self.problem = problem
        self.correction = correction


class ExtractClauseContentArguments(StrictClauseToolModel):
    """根据程序提供的候选证据提交完整直接内容。"""

    reasoning_summary: str = Field(
        max_length=500,
        description=(
            "简洁说明如何使用程序提供的候选起止页码和包含式锚点覆盖完整直接内容，"
            "如何处理跨页延续、表格和子条款排除，以及是否存在遮挡或无法辨认文字；"
            "最多 500 个字符，不重复候选证据、完整正文或隐式思维草稿。"
        ),
    )
    content: str = Field(
        description=(
            "由程序提供的候选证据和前述边界判断支持的当前候选完整直接原文；"
            "使用 Markdown 兼容纯文本保留编号、标题、段落顺序、换行、项目符号和"
            "必要表格结构，不得摘要、改写、翻译、规范化、补全缺失文字或加入解释；"
            "父候选不得重复任何已单独建模的子孙条款正文。"
        ),
    )

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("条款内容提取的推理摘要不能为空")
        return normalized

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("条款完整内容不能为空")
        if normalized.casefold() in {"null", "none", "n/a", "not found"} or normalized in {
            "未找到",
            "无法提取",
            "无",
        }:
            raise ValueError("条款完整内容不能使用缺失或放弃占位值")
        return normalized


class ExtractedClauseContent(StrictClauseToolModel):
    """程序补充候选身份后形成的不可变条款内容对象。"""

    candidate_id: str
    reasoning_summary: str
    content: str


class ClauseContentToolFeedback(StrictClauseToolModel):
    """写回单条款详情会话的最小反馈。"""

    ok: bool
    message: str


class ClauseContentToolError(ValueError):
    """包含错误位置、问题与修正方向的条款内容校验错误。"""

    def __init__(self, path: str, problem: str, correction: str) -> None:
        super().__init__(problem)
        self.path = path
        self.problem = problem
        self.correction = correction


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictClauseToolModel],
) -> dict[str, Any]:
    """生成 non-strict 工具 Schema；参数仍由本地契约严格校验。"""
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
        "记录条款候选发现的一段简洁自然语言推理，重点判断下一个尚未记录的"
        "条款是否具有自身直接正文，以及其标号、原合同绝对层级、完整文档路径、"
        "正文祖先和边界；纯结构标题只在此确认后跳过正文候选，但仍保留在路径中。"
    ),
    arguments_model=ThinkArguments,
)

ANALYZE_CLAUSE_HIERARCHY_TOOL: Final[dict[str, Any]] = _function_tool(
    name="analyze_clause_hierarchy",
    description=(
        "首轮扫描整份合同并详细分析条款组织层级。先提交页面结构证据，再分析编号、"
        "父子关系、同级序列、编号重置、特殊版式及纯结构标题，最后形成写入工作区的"
        "合同专属指导；"
        "本工具只能在首轮成功调用一次，不逐条记录候选或提取完整正文。"
    ),
    arguments_model=AnalyzeClauseHierarchyArguments,
)

RECORD_CLAUSE_CANDIDATE_TOOL: Final[dict[str, Any]] = _function_tool(
    name="record_clause_candidate",
    description=(
        "按合同原始阅读顺序记录一个具有自身直接规范正文的新条款候选，包括符合该条件的"
        "子层级条款。只有编号、标题或分组名而没有直接正文时不得调用本工具；先提交可复用"
        "的精简起止锚点，再给出推理摘要、原合同完整路径和候选决定；未提取的纯标题必须"
        "保留在路径中，候选 ID 与顺序由程序生成。"
    ),
    arguments_model=RecordClauseCandidateArguments,
)

REVISE_LAST_CLAUSE_CANDIDATE_TOOL: Final[dict[str, Any]] = _function_tool(
    name="revise_last_clause_candidate",
    description=(
        "仅在刚记录的最后一个条款候选存在错误时，用新的完整决定替换它。"
        "不能修改更早候选，也不能改变程序生成的候选 ID 与顺序。"
    ),
    arguments_model=ReviseLastClauseCandidateArguments,
)

FINISH_CLAUSE_DISCOVERY_TOOL: Final[dict[str, Any]] = _function_tool(
    name="finish_clause_discovery",
    description=(
        "确认合同条款区域已按原始顺序检查完毕且没有遗漏主条款或子层级条款时"
        "调用此工具完成发现。先提交末尾证据和完成判断的简洁推理摘要，最后提交实际检查到的"
        "物理页码与结束锚点；程序仍会校验工作区和覆盖位置。"
    ),
    arguments_model=FinishClauseDiscoveryArguments,
)

EXTRACT_CLAUSE_CONTENT_TOOL: Final[dict[str, Any]] = _function_tool(
    name="extract_clause_content",
    description=(
        "依据程序已经提供的当前候选起止证据，提交该候选自己的完整直接原文。"
        "先简洁说明边界、跨页连续性及子条款排除，再原样提交完整内容；叶子条款覆盖"
        "整条，父条款只包含自身编号、标题、引导语和独立正文，不重复子孙条款。"
    ),
    arguments_model=ExtractClauseContentArguments,
)

INITIAL_CLAUSE_DISCOVERY_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    ANALYZE_CLAUSE_HIERARCHY_TOOL,
)
CANDIDATE_START_CLAUSE_DISCOVERY_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    RECORD_CLAUSE_CANDIDATE_TOOL,
)
CLAUSE_DISCOVERY_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    RECORD_CLAUSE_CANDIDATE_TOOL,
    REVISE_LAST_CLAUSE_CANDIDATE_TOOL,
    FINISH_CLAUSE_DISCOVERY_TOOL,
)
CLAUSE_CONTENT_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    EXTRACT_CLAUSE_CONTENT_TOOL,
)

_ARGUMENT_MODELS: Final[dict[str, type[StrictClauseToolModel]]] = {
    "analyze_clause_hierarchy": AnalyzeClauseHierarchyArguments,
    "think": ThinkArguments,
    "record_clause_candidate": RecordClauseCandidateArguments,
    "revise_last_clause_candidate": ReviseLastClauseCandidateArguments,
    "finish_clause_discovery": FinishClauseDiscoveryArguments,
}


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


def parse_clause_discovery_tool_arguments(
    name: str,
    raw_arguments: str,
) -> ClauseDiscoveryToolArguments:
    """在执行工具前再次解析并严格校验模型参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的条款候选发现工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def parse_clause_content_tool_arguments(
    name: str,
    raw_arguments: str,
) -> ExtractClauseContentArguments:
    """解析并严格校验单条款内容工具参数。"""
    if name != "extract_clause_content":
        raise ValueError(f"未知的条款内容提取工具：{name}")
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return ExtractClauseContentArguments.model_validate(
        _decode_embedded_json(payload)
    )


def _validate_page_number(
    *,
    path: str,
    page_number: int,
    page_count: int,
) -> None:
    if page_count < 1:
        raise ValueError("合同物理页数必须大于等于 1")
    if page_number > page_count:
        raise ClauseDiscoveryToolError(
            path,
            f"第 {page_number} 页超出合同物理页数 {page_count}",
            f"改用 1～{page_count} 范围内的真实物理页码",
        )


def validate_clause_hierarchy_analysis(
    arguments: AnalyzeClauseHierarchyArguments,
    *,
    page_count: int,
) -> None:
    """校验首轮层级分析引用的页面均属于当前 PDF。"""
    for evidence_index, observation in enumerate(arguments.evidence):
        for page_index, page_number in enumerate(observation.page_numbers):
            _validate_page_number(
                path=f"evidence.{evidence_index}.page_numbers.{page_index}",
                page_number=page_number,
                page_count=page_count,
            )


def _validate_workspace(
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
) -> None:
    """拒绝无法支持确定性追加和修正的损坏工作区。"""
    expected_orders = tuple(range(1, len(workspace) + 1))
    if tuple(item.order for item in workspace) != expected_orders:
        raise ValueError("条款工作区 order 必须从 1 开始连续递增")
    expected_ids = tuple(f"clause-{order:04d}" for order in expected_orders)
    if tuple(item.candidate_id for item in workspace) != expected_ids:
        raise ValueError("条款工作区 candidate_id 与程序顺序不一致")
    previous_items: tuple[ClauseCandidateWorkspaceItem, ...] = ()
    for item in workspace:
        _validate_candidate_parent_reference(
            parent_candidate_id=item.parent_candidate_id,
            document_path=item.document_path,
            previous_items=previous_items,
        )
        previous_items = (*previous_items, item)


def _is_document_ancestor(
    ancestor_path: tuple[ClauseDocumentPathSegment, ...],
    descendant_path: tuple[ClauseDocumentPathSegment, ...],
) -> bool:
    """判断一个完整文档路径是否是另一条路径的真前缀。"""
    return len(ancestor_path) < len(descendant_path) and (
        descendant_path[: len(ancestor_path)] == ancestor_path
    )


def _validate_candidate_parent_reference(
    *,
    parent_candidate_id: str | None,
    document_path: tuple[ClauseDocumentPathSegment, ...],
    previous_items: tuple[ClauseCandidateWorkspaceItem, ...],
) -> None:
    """让候选父引用指向路径上最近的已记录正文祖先。"""
    ancestors = tuple(
        item
        for item in previous_items
        if _is_document_ancestor(item.document_path, document_path)
    )
    nearest = max(ancestors, key=lambda item: item.level, default=None)
    if nearest is None:
        if parent_candidate_id is not None:
            raise ClauseDiscoveryToolError(
                "decision.parent_candidate_id",
                f"工作区中没有 {parent_candidate_id} 可作为当前文档路径的正文祖先",
                "传 null；未记录的纯结构父级只保留在 document_path 中",
            )
        return
    if parent_candidate_id != nearest.candidate_id:
        raise ClauseDiscoveryToolError(
            "decision.parent_candidate_id",
            f"当前路径最近的已记录正文祖先是 {nearest.candidate_id}",
            f"把 parent_candidate_id 改为 {nearest.candidate_id}",
        )


def _validate_candidate_decision(
    decision: ClauseCandidateDecision,
    *,
    evidence: ClauseBoundaryEvidence,
    previous_items: tuple[ClauseCandidateWorkspaceItem, ...],
    page_count: int,
) -> None:
    """校验页面、顺序、重复项、绝对文档路径和候选父引用。"""
    _validate_page_number(
        path="evidence.start.page_number",
        page_number=evidence.start.page_number,
        page_count=page_count,
    )
    _validate_page_number(
        path="evidence.end.page_number",
        page_number=evidence.end.page_number,
        page_count=page_count,
    )

    if (
        previous_items
        and evidence.start.page_number < previous_items[-1].evidence.start.page_number
    ):
        raise ClauseDiscoveryToolError(
            "evidence.start.page_number",
            "新候选页码早于工作区最后一个候选，违反原始阅读顺序",
            f"从第 {previous_items[-1].evidence.start.page_number} 页或其后继续查找",
        )

    duplicate = next(
        (
            item
            for item in previous_items
            if item.evidence.start.page_number == evidence.start.page_number
            and item.evidence.start.anchor == evidence.start.anchor
        ),
        None,
    )
    if duplicate is not None:
        raise ClauseDiscoveryToolError(
            "evidence.start.anchor",
            f"该起始位置已由 {duplicate.candidate_id} 记录",
            "跳过重复条款并从其后继续，或修正为下一条款的真实开头",
        )

    if (
        previous_items
        and evidence.start.page_number == previous_items[-1].evidence.end.page_number
        and evidence.start.anchor == previous_items[-1].evidence.end.anchor
    ):
        raise ClauseDiscoveryToolError(
            "evidence.start.anchor",
            (
                "新候选起始锚点与工作区最后候选的结束锚点相同；"
                "结束锚点必须属于前一候选自身，不能使用下一候选开头"
            ),
            (
                "先调用 revise_last_clause_candidate，把最后候选的 end 修正为"
                "其自身最后一段原文，再重新记录当前候选"
            ),
        )

    _validate_candidate_parent_reference(
        parent_candidate_id=decision.parent_candidate_id,
        document_path=tuple(decision.document_path),
        previous_items=previous_items,
    )


def record_clause_candidate(
    arguments: RecordClauseCandidateArguments,
    *,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    page_count: int,
) -> ClauseCandidateWorkspaceItem:
    """校验新候选并生成由程序持有身份的精简工作区条目。"""
    _validate_workspace(workspace)
    _validate_candidate_decision(
        arguments.decision,
        evidence=arguments.evidence,
        previous_items=workspace,
        page_count=page_count,
    )
    order = len(workspace) + 1
    return ClauseCandidateWorkspaceItem(
        candidate_id=f"clause-{order:04d}",
        order=order,
        **arguments.decision.model_dump(),
        evidence=arguments.evidence,
    )


def revise_last_clause_candidate(
    arguments: ReviseLastClauseCandidateArguments,
    *,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    page_count: int,
) -> ClauseCandidateWorkspaceItem:
    """校验最后候选的完整替换，并保留其程序身份与顺序。"""
    _validate_workspace(workspace)
    if not workspace:
        raise ClauseDiscoveryToolError(
            "revise_last_clause_candidate",
            "工作区尚无条款候选，不能执行修正",
            "先调用 record_clause_candidate 记录第一条候选",
        )
    _validate_candidate_decision(
        arguments.decision,
        evidence=arguments.evidence,
        previous_items=workspace[:-1],
        page_count=page_count,
    )
    previous = workspace[-1]
    return ClauseCandidateWorkspaceItem(
        candidate_id=previous.candidate_id,
        order=previous.order,
        **arguments.decision.model_dump(),
        evidence=arguments.evidence,
    )


def validate_finish_clause_discovery(
    arguments: FinishClauseDiscoveryArguments,
    *,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    page_count: int,
) -> None:
    """结束前校验候选非空、检查位置和末尾证据。"""
    _validate_workspace(workspace)
    if not workspace:
        raise ClauseDiscoveryToolError(
            "finish_clause_discovery",
            "工作区尚未记录任何条款候选",
            "继续检查合同并至少成功调用一次 record_clause_candidate",
        )
    checked_page = arguments.decision.last_checked_page
    _validate_page_number(
        path="decision.last_checked_page",
        page_number=checked_page,
        page_count=page_count,
    )
    for index, item in enumerate(arguments.evidence):
        _validate_page_number(
            path=f"evidence.{index}.page_number",
            page_number=item.page_number,
            page_count=page_count,
        )
    last_boundary_page = workspace[-1].evidence.end.page_number
    if checked_page < last_boundary_page:
        raise ClauseDiscoveryToolError(
            "decision.last_checked_page",
            "最后检查页早于工作区最后一个条款候选的结束锚点",
            f"至少继续检查到第 {last_boundary_page} 页",
        )
    if not any(item.page_number == checked_page for item in arguments.evidence):
        raise ClauseDiscoveryToolError(
            "evidence",
            f"没有证据来自最后检查页 {checked_page}",
            "提供该页条款区域结束位置或后续非条款区域的可核对原文",
        )


def _normalized_clause_text(value: str) -> str:
    """忽略空白差异比较模型正文与候选短锚点。"""
    return "".join(value.split()).casefold()


def _normalized_anchor_coverage_text(value: str) -> str:
    """为位置匹配移除不影响条款定位的空白、填写线和标点。"""
    normalized = _normalized_clause_text(value)
    without_fill_lines = normalized.translate(str.maketrans("", "", "_＿﹍﹎﹏"))
    without_punctuation = "".join(
        character
        for character in without_fill_lines
        if not unicodedata.category(character).startswith("P")
    )
    # 极短锚点可能只包含项目符号。此时保留旧表示，避免空串天然匹配任何正文。
    return without_punctuation or without_fill_lines or normalized


_MINIMUM_ANCHOR_LOCATION_SIMILARITY: Final = 0.85


def _minimum_substring_edit_distance(pattern: str, text: str) -> int:
    """计算模式串与正文任意连续片段之间的最小 Levenshtein 距离。"""
    if not pattern:
        return 0
    if not text:
        return len(pattern)

    # 首行全为零，表示匹配可以从正文任意位置开始；最终行最小值即最佳结束位置。
    previous = [0] * (len(text) + 1)
    for pattern_index, pattern_character in enumerate(pattern, start=1):
        current = [pattern_index]
        for text_index, text_character in enumerate(text, start=1):
            substitution_cost = int(pattern_character != text_character)
            current.append(
                min(
                    previous[text_index] + 1,
                    current[text_index - 1] + 1,
                    previous[text_index - 1] + substitution_cost,
                )
            )
        previous = current
    return min(previous)


def _anchor_is_located(anchor: str, content: str) -> bool:
    """判断边界锚点是否足以在正文中定位，不承担原文字符保真校验。"""
    normalized_anchor = _normalized_anchor_coverage_text(anchor)
    normalized_content = _normalized_anchor_coverage_text(content)
    if normalized_anchor in normalized_content:
        return True
    distance = _minimum_substring_edit_distance(
        normalized_anchor,
        normalized_content,
    )
    similarity = 1 - distance / len(normalized_anchor)
    return similarity >= _MINIMUM_ANCHOR_LOCATION_SIMILARITY


_CURRENCY_FILL_LINE_PATTERN: Final = re.compile(
    r"(人民币|RMB|[￥¥])\s*[_＿﹍﹎﹏]+\s*(?=\d)",
    flags=re.IGNORECASE,
)


def _normalize_visual_fill_lines(value: str) -> str:
    """清除货币标签与金额之间被误转写为字符的表单填写线。"""
    return _CURRENCY_FILL_LINE_PATTERN.sub(lambda match: f"{match.group(1)} ", value)


def _descendant_candidates(
    candidate: ClauseCandidateWorkspaceItem,
    *,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
) -> tuple[ClauseCandidateWorkspaceItem, ...]:
    """按扁平父级引用找出当前候选的全部子孙候选。"""
    items_by_id = {item.candidate_id: item for item in workspace}
    descendants: list[ClauseCandidateWorkspaceItem] = []
    for item in workspace:
        parent_id = item.parent_candidate_id
        while parent_id is not None:
            if parent_id == candidate.candidate_id:
                descendants.append(item)
                break
            parent = items_by_id.get(parent_id)
            if parent is None:
                break
            parent_id = parent.parent_candidate_id
    return tuple(descendants)


def extract_clause_content(
    arguments: ExtractClauseContentArguments,
    *,
    candidate: ClauseCandidateWorkspaceItem,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
) -> ExtractedClauseContent:
    """校验候选边界、正文覆盖和子条款排除后形成内容结果。"""
    _validate_workspace(workspace)
    items_by_id = {item.candidate_id: item for item in workspace}
    if items_by_id.get(candidate.candidate_id) != candidate:
        raise ClauseContentToolError(
            "candidate",
            f"候选 {candidate.candidate_id} 不在当前条款目录中或内容不一致",
            "只使用程序指定的当前候选，不能修改候选身份、层级或边界",
        )

    # 视觉模型有时只把连续金额填写线的空白段转写为 `_`，却正确忽略文字下方
    # 的同一横线。这里只修正货币标签到数字金额之间的确定性版式模式；其他
    # 下划线保持原样，避免破坏合同编号、账号或技术标识。
    content = _normalize_visual_fill_lines(arguments.content)
    normalized_content = _normalized_clause_text(content)
    start_anchor = candidate.evidence.start.anchor
    if not _anchor_is_located(start_anchor, content):
        raise ClauseContentToolError(
            "content",
            "最终 content 未覆盖程序指定的候选起始锚点",
            f"从候选起始原文“{start_anchor}”开始完整提取",
        )

    end_anchor = candidate.evidence.end.anchor
    if not _anchor_is_located(end_anchor, content):
        raise ClauseContentToolError(
            "content",
            "最终 content 未覆盖程序指定的候选结束锚点",
            f"继续提取并包含属于当前条款的结束原文“{end_anchor}”",
        )

    for descendant in _descendant_candidates(candidate, workspace=workspace):
        descendant_anchor = descendant.evidence.start.anchor
        if _normalized_clause_text(descendant_anchor) in normalized_content:
            raise ClauseContentToolError(
                "content",
                f"最终 content 重复包含子孙候选 {descendant.candidate_id} 的起始锚点",
                "保留当前父候选自己的编号、标题、引导语和独立正文，删除子孙条款正文",
            )

    return ExtractedClauseContent(
        candidate_id=candidate.candidate_id,
        reasoning_summary=arguments.reasoning_summary,
        content=content,
    )


def successful_clause_content_feedback(
    result: ExtractedClauseContent,
) -> ClauseContentToolFeedback:
    """返回不重复正文的最小成功反馈。"""
    return ClauseContentToolFeedback(
        ok=True,
        message=f"已提取 {result.candidate_id} 的完整直接内容。",
    )


def clause_content_validation_error_feedback(
    error: Exception,
) -> ClauseContentToolFeedback:
    """把内容参数或边界错误转换成短而可执行的反馈。"""
    if isinstance(error, ClauseContentToolError):
        return ClauseContentToolFeedback(
            ok=False,
            message=f"{error.path}：{error.problem}；请{error.correction}。",
        )
    if not isinstance(error, ValidationError):
        return ClauseContentToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按条款内容工具 Schema 修正后重新调用。",
        )

    messages: list[str] = []
    for item in error.errors(include_url=False)[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        if item["type"] == "missing":
            correction = "补充该必填参数"
        elif item["type"] == "extra_forbidden":
            correction = "删除该未定义参数"
        elif path == "content":
            correction = "提交非空的当前候选完整直接原文，不能使用放弃占位值"
        elif path == "reasoning_summary":
            correction = "简洁说明边界覆盖、跨页处理和子条款排除判断"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(error.errors(include_url=False)) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return ClauseContentToolFeedback(ok=False, message="\n".join(messages))


def successful_tool_feedback(
    name: str,
    *,
    workspace_item: ClauseCandidateWorkspaceItem | None = None,
) -> ClauseDiscoveryToolFeedback:
    """返回不复制工作区内容的最小成功反馈。"""
    if name == "analyze_clause_hierarchy":
        return ClauseDiscoveryToolFeedback(
            ok=True,
            message="合同层级分析已写入工作区；请按该指导发现第一个条款候选。",
        )
    if name == "think":
        return ClauseDiscoveryToolFeedback(
            ok=True,
            message="思考已记录，请继续定位下一条款。",
        )
    if name == "finish_clause_discovery":
        return ClauseDiscoveryToolFeedback(
            ok=True,
            message="条款候选发现已经完成。",
        )
    if workspace_item is None:
        raise ValueError(f"工具 {name} 的成功反馈必须提供 workspace_item")
    if name == "record_clause_candidate":
        return ClauseDiscoveryToolFeedback(
            ok=True,
            message=f"已记录 {workspace_item.candidate_id}；请从该条款之后继续。",
        )
    if name == "revise_last_clause_candidate":
        return ClauseDiscoveryToolFeedback(
            ok=True,
            message=f"已修正 {workspace_item.candidate_id}；请从修正后的条款之后继续。",
        )
    raise ValueError(f"未知的条款候选发现工具：{name}")


def validation_error_feedback(error: Exception) -> ClauseDiscoveryToolFeedback:
    """把参数或状态错误转换为包含改进方向的简短反馈。"""
    if isinstance(error, ClauseDiscoveryToolError):
        return ClauseDiscoveryToolFeedback(
            ok=False,
            message=f"{error.path}：{error.problem}；请{error.correction}。",
        )
    if not isinstance(error, ValidationError):
        return ClauseDiscoveryToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按当前工具参数定义修正后重新调用。",
        )

    messages: list[str] = []
    for item in error.errors(include_url=False)[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        if item["type"] == "missing":
            correction = "补充该必填参数"
        elif item["type"] == "extra_forbidden":
            correction = "删除该未定义参数"
        elif "page_number" in path or path.endswith("level"):
            correction = "提交大于等于 1 且符合合同实际结构的整数"
        elif "document_path" in path:
            correction = "按原合同由外到内补全路径，并让最后一项表示当前候选"
        elif "title_hint" in path or "parent_candidate_id" in path:
            correction = "未知时传 null，已知时传非空字符串"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(error.errors(include_url=False)) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return ClauseDiscoveryToolFeedback(ok=False, message="\n".join(messages))


__all__ = [
    "ANALYZE_CLAUSE_HIERARCHY_TOOL",
    "CANDIDATE_START_CLAUSE_DISCOVERY_TOOLS",
    "CLAUSE_CONTENT_TOOLS",
    "CLAUSE_CONTENT_TOOL_CHOICE",
    "CLAUSE_CONTENT_TOOL_VERSION",
    "CLAUSE_DISCOVERY_TOOLS",
    "CLAUSE_DISCOVERY_TOOL_CHOICE",
    "CLAUSE_DISCOVERY_TOOL_VERSION",
    "EXTRACT_CLAUSE_CONTENT_TOOL",
    "FINISH_CLAUSE_DISCOVERY_TOOL",
    "INITIAL_CLAUSE_DISCOVERY_TOOLS",
    "RECORD_CLAUSE_CANDIDATE_TOOL",
    "REVISE_LAST_CLAUSE_CANDIDATE_TOOL",
    "THINK_TOOL",
    "AnalyzeClauseHierarchyArguments",
    "ClauseBoundaryAnchor",
    "ClauseBoundaryEvidence",
    "ClauseCandidateDecision",
    "ClauseCandidateWorkspaceItem",
    "ClauseCompletionEvidence",
    "ClauseContentToolError",
    "ClauseContentToolFeedback",
    "ClauseDiscoveryCompletion",
    "ClauseDiscoveryToolArguments",
    "ClauseDiscoveryToolError",
    "ClauseDiscoveryToolFeedback",
    "ClauseDocumentPathSegment",
    "ClauseHierarchyGuidance",
    "ClauseHierarchyObservation",
    "ExtractClauseContentArguments",
    "ExtractedClauseContent",
    "FinishClauseDiscoveryArguments",
    "RecordClauseCandidateArguments",
    "ReviseLastClauseCandidateArguments",
    "ThinkArguments",
    "clause_content_validation_error_feedback",
    "extract_clause_content",
    "parse_clause_content_tool_arguments",
    "parse_clause_discovery_tool_arguments",
    "record_clause_candidate",
    "revise_last_clause_candidate",
    "successful_clause_content_feedback",
    "successful_tool_feedback",
    "validate_clause_hierarchy_analysis",
    "validate_finish_clause_discovery",
    "validation_error_feedback",
]
