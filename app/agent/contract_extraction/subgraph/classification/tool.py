"""合同分类子图的严格函数工具契约。"""

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


class StrictClassificationToolModel(BaseModel):
    """禁止额外参数并禁止宽松类型转换的分类工具模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClassificationEvidence(StrictClassificationToolModel):
    """支持单类别判断的最小页码与文本证据。"""

    page_number: int = Field(
        description="该证据所在合同页面的物理页码，从 1 开始；不是页面中印刷的页码。"
    )
    content: str = Field(
        description=(
            "从该页直接观察到、足以支持当前类别判断的简短原文；"
            "不得填写推断、改写内容或无关的整页文本。"
        )
    )

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        if value < 1:
            raise ValueError("物理页码必须大于等于 1")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("证据内容不能为空")
        return normalized


class ThinkArguments(StrictClassificationToolModel):
    """think 只记录当前类别判别的一段自然语言推理。"""

    reasoning: str = Field(
        description=(
            "针对当前目标类别的简洁自然语言思考，比较合同证据、权威定义与正反例，"
            "并说明下一步需要核对的疑点；不在此提交类别决定。"
        )
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("思考理由不能为空")
        return normalized


class CategoryDecisionArguments(StrictClassificationToolModel):
    """两个终止工具共享的证据优先参数。"""

    evidence: list[ClassificationEvidence] = Field(
        description=(
            "支持当前属于或不属于判断的页面证据，按合同阅读顺序排列；"
            "至少提供一条可核对原文。"
        )
    )
    reasoning_summary: str = Field(
        description=(
            "简洁说明证据如何满足或不满足当前目标类别的核心权利义务定义，"
            "并指出冲突或不确定性；不得引入证据之外的新事实。"
        )
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence(
        cls,
        value: list[ClassificationEvidence],
    ) -> list[ClassificationEvidence]:
        if not value:
            raise ValueError("类别判断至少需要一条页面证据")
        return value

    @field_validator("reasoning_summary")
    @classmethod
    def validate_reasoning_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("推理摘要不能为空")
        return normalized


class NotBelongToCategoryArguments(CategoryDecisionArguments):
    """当前合同不属于目标类别时提交的终止决定。"""


class CategoryMatchDecision(StrictClassificationToolModel):
    """属于目标类别时记录当前合同实际交易场景的最小决定。"""

    scenario: str = Field(
        description=(
            "本合同实际发生的核心交易或权利义务场景概括，作为正式分类结果的语义说明；"
            "不填写类别代码、类别名称或泛化法律结论。"
        )
    )

    @field_validator("scenario")
    @classmethod
    def validate_scenario(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("交易场景概括不能为空")
        return normalized


class BelongToCategoryArguments(CategoryDecisionArguments):
    """当前合同属于目标类别时提交的终止决定。"""

    decision: CategoryMatchDecision = Field(
        description=(
            "确认命中当前目标类别后的最终决定，仅概括已由证据和推理支持的实际交易场景。"
        )
    )


class UnmappedTypeDescriptionArguments(CategoryDecisionArguments):
    """全部正式类别均未命中时生成的简短交易类型描述。"""

    description: str = Field(
        description=(
            "全部正式类别均未命中时，对合同实际核心交易和主要权利义务作出的简短中文描述；"
            "不得临时创造类别代码或类别名称。"
        )
    )

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("合同类型描述不能为空")
        return normalized


class CategoryIdentity(StrictClassificationToolModel):
    """由程序从当前权威类别定义注入的稳定身份。"""

    category_code: str
    category_name: str
    scenario: str

    @field_validator("category_code", "category_name", "scenario")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("类别身份和交易场景不能为空")
        return normalized


class CategoryMatchCard(StrictClassificationToolModel):
    """写入正式分类结果的证据优先命中卡片。"""

    evidence: tuple[ClassificationEvidence, ...]
    reasoning_summary: str
    decision: CategoryIdentity


class ClassificationToolFeedback(StrictClassificationToolModel):
    """写回单类别短期记忆的最小工具反馈。"""

    ok: bool
    message: str


ClassificationToolArguments: TypeAlias = (
    ThinkArguments | NotBelongToCategoryArguments | BelongToCategoryArguments
)


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictClassificationToolModel],
) -> dict[str, Any]:
    """生成 non-strict 工具 Schema；参数仍由本地 Pydantic 严格校验。"""
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
        "记录当前目标类别的一段简洁自然语言推理，用于比较合同证据、"
        "权威定义和正反例；不提交正式类别决定。"
    ),
    arguments_model=ThinkArguments,
)

NOT_BELONG_TO_CATEGORY_TOOL: Final[dict[str, Any]] = _function_tool(
    name="not_belong_to_category",
    description=(
        "合同中不存在满足当前目标类别定义的核心权利义务结构时调用。"
        "先提交页面证据，再给出不命中的简洁推理摘要，并终止当前类别判别。"
    ),
    arguments_model=NotBelongToCategoryArguments,
)

BELONG_TO_CATEGORY_TOOL: Final[dict[str, Any]] = _function_tool(
    name="belong_to_category",
    description=(
        "合同中存在满足当前目标类别定义的核心权利义务结构时调用；"
        "同一复合交易也可能同时满足其他类别。"
        "先提交页面证据和简洁推理摘要，最后概括当前合同的实际交易场景，"
        "并终止当前类别判别。"
    ),
    arguments_model=BelongToCategoryArguments,
)

DESCRIBE_UNMAPPED_TYPE_TOOL: Final[dict[str, Any]] = _function_tool(
    name="describe_unmapped_type",
    description=(
        "全部正式合同类别均未命中时调用。先提交合同页面证据和简洁推理摘要，"
        "最后用一段中文描述合同实际交易类型；不创建类别码。"
    ),
    arguments_model=UnmappedTypeDescriptionArguments,
)

CLASSIFICATION_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    NOT_BELONG_TO_CATEGORY_TOOL,
    BELONG_TO_CATEGORY_TOOL,
)
CLASSIFICATION_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

_ARGUMENT_MODELS: Final[dict[str, type[StrictClassificationToolModel]]] = {
    "think": ThinkArguments,
    "not_belong_to_category": NotBelongToCategoryArguments,
    "belong_to_category": BelongToCategoryArguments,
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


def parse_classification_tool_arguments(
    name: str,
    raw_arguments: str,
) -> ClassificationToolArguments:
    """在执行工具前再次解析并严格校验模型参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的合同分类工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def parse_unmapped_type_description_arguments(
    raw_arguments: str,
) -> UnmappedTypeDescriptionArguments:
    """解析并严格校验未映射合同的一次性描述工具参数。"""
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError("工具 describe_unmapped_type 的参数不是有效 JSON") from exc
    return UnmappedTypeDescriptionArguments.model_validate(
        _decode_embedded_json(payload)
    )


def build_category_match_card(
    arguments: BelongToCategoryArguments,
    *,
    category_code: str,
    category_name: str,
) -> CategoryMatchCard:
    """用程序持有的类别身份和已校验工具参数生成正式命中卡片。"""
    return CategoryMatchCard(
        evidence=tuple(arguments.evidence),
        reasoning_summary=arguments.reasoning_summary,
        decision=CategoryIdentity(
            category_code=category_code,
            category_name=category_name,
            scenario=arguments.decision.scenario,
        ),
    )


def successful_tool_feedback(name: str) -> ClassificationToolFeedback:
    """返回不携带冗余状态的最小成功反馈。"""
    messages = {
        "think": "思考已记录，请继续判断。",
        "not_belong_to_category": "不属于该类别的决定已接受。",
        "belong_to_category": "属于该类别的决定和命中卡片已接受。",
    }
    try:
        message = messages[name]
    except KeyError as exc:
        raise ValueError(f"未知的合同分类工具：{name}") from exc
    return ClassificationToolFeedback(ok=True, message=message)


def validation_error_feedback(error: Exception) -> ClassificationToolFeedback:
    """把参数错误转换为包含位置、问题与修正方向的简短反馈。"""
    if not isinstance(error, ValidationError):
        return ClassificationToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按当前工具参数定义修正后重新调用。",
        )

    messages: list[str] = []
    for item in error.errors(include_url=False)[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        error_type = item["type"]
        if error_type == "missing":
            correction = "补充该必填参数"
        elif error_type == "extra_forbidden":
            correction = "删除该未定义参数"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(error.errors(include_url=False)) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return ClassificationToolFeedback(ok=False, message="\n".join(messages))


__all__ = [
    "BELONG_TO_CATEGORY_TOOL",
    "CLASSIFICATION_TOOLS",
    "CLASSIFICATION_TOOL_CHOICE",
    "DESCRIBE_UNMAPPED_TYPE_TOOL",
    "NOT_BELONG_TO_CATEGORY_TOOL",
    "THINK_TOOL",
    "BelongToCategoryArguments",
    "CategoryIdentity",
    "CategoryMatchCard",
    "CategoryMatchDecision",
    "ClassificationEvidence",
    "ClassificationToolArguments",
    "ClassificationToolFeedback",
    "NotBelongToCategoryArguments",
    "ThinkArguments",
    "UnmappedTypeDescriptionArguments",
    "build_category_match_card",
    "parse_classification_tool_arguments",
    "parse_unmapped_type_description_arguments",
    "successful_tool_feedback",
    "validation_error_feedback",
]
