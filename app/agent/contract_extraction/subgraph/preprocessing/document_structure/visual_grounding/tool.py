"""单元视觉定位节点的本地函数工具与状态校验契约。"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import Annotated, Any, Final, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from app.agent.contract_extraction.tool_protocol import TOOL_CHOICE_AUTO


class StrictVisualGroundingModel(BaseModel):
    """禁止额外参数和宽松类型转换的视觉定位模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


NormalizedCoordinate = Annotated[int, Field(ge=0, le=1000)]
VISUAL_GROUNDING_TOOL_VERSION: Final = "unit-visual-grounding-tool-v2"


class ThinkArguments(StrictVisualGroundingModel):
    """think 只记录当前单元视觉定位的一段自然语言推理。"""

    reasoning: str = Field(
        description=(
            "针对当前单元视觉定位的简洁自然语言思考，重点检查最早未覆盖锚点、"
            "页面阅读顺序、单栏或双栏布局和下一个框；不在此提交坐标。"
        )
    )

    @field_validator("reasoning")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("视觉定位思考不能为空")
        return normalized


class DrawBoundingBoxArguments(StrictVisualGroundingModel):
    """一次绘制一个单页框，并消费一个或多个连续锚点。"""

    anchor_ids: list[str] = Field(
        min_length=1,
        description=(
            "本次定位框覆盖的程序锚点 ID，必须从最早未覆盖锚点开始，"
            "只包含同一页且在给定顺序中连续的一个或多个 ID。"
        ),
    )
    page_number: int = Field(
        ge=1,
        description=(
            "本次定位框所在的 PDF 物理页码，必须与全部 anchor_ids 对应页面一致。"
        ),
    )
    bbox_2d: list[NormalizedCoordinate] = Field(
        min_length=4,
        max_length=4,
        description=(
            "单页 0～1000 归一化坐标框，严格按 [x_min, y_min, x_max, y_max] 提交；"
            "左上坐标必须严格小于右下坐标，不得跨页。"
        ),
    )

    @field_validator("anchor_ids")
    @classmethod
    def validate_anchor_ids(cls, value: list[str]) -> list[str]:
        normalized = [anchor_id.strip() for anchor_id in value]
        if any(not anchor_id for anchor_id in normalized):
            raise ValueError("锚点标识不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("一次绘制不能重复提交同一锚点")
        return normalized

    @field_validator("bbox_2d")
    @classmethod
    def validate_bbox_order(
        cls,
        value: list[NormalizedCoordinate],
    ) -> list[NormalizedCoordinate]:
        x_min, y_min, x_max, y_max = value
        if x_min >= x_max or y_min >= y_max:
            raise ValueError(
                "坐标框必须按 [x_min, y_min, x_max, y_max] 提交，"
                "并满足 x_min < x_max 且 y_min < y_max"
            )
        return value


class FinishArguments(StrictVisualGroundingModel):
    """模型认为当前单元的全部定位锚点已经覆盖时提交。"""

    reason: str = Field(
        description=(
            "说明当前单元全部程序锚点为何均已按顺序覆盖；仍有未覆盖锚点时不得调用完成工具。"
        )
    )

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("结束理由不能为空")
        return normalized


class LocalizationAnchor(StrictVisualGroundingModel):
    """由程序整理并按阅读顺序交给单元会话的定位锚点。"""

    anchor_id: str
    order: int = Field(ge=1)
    page_number: int = Field(ge=1)
    source: Literal["start", "navigation", "page_body", "end"]
    anchor_kind: Literal["text", "visual", "page_body"]
    content: str

    @field_validator("anchor_id", "content")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("定位锚点标识与内容不能为空")
        return normalized


class LocatedBoundingBox(StrictVisualGroundingModel):
    """程序接受并写入当前单元定位结果的一个单页框。"""

    anchor_ids: tuple[str, ...]
    page_number: int
    bbox_2d: tuple[int, int, int, int]


class VisualGroundingToolFeedback(StrictVisualGroundingModel):
    """写回单元短期记忆的最小工具反馈。"""

    ok: bool
    message: str


VisualGroundingToolArguments: TypeAlias = (
    ThinkArguments | DrawBoundingBoxArguments | FinishArguments
)


class VisualGroundingToolError(ValueError):
    """包含错误位置、问题和修正方向的状态校验错误。"""

    def __init__(self, path: str, problem: str, correction: str) -> None:
        super().__init__(problem)
        self.path = path
        self.problem = problem
        self.correction = correction


def _function_tool(
    *,
    name: str,
    description: str,
    arguments_model: type[StrictVisualGroundingModel],
) -> dict[str, Any]:
    """构造 non-strict 工具；参数与坐标语义由本地校验负责。"""
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
        "记录当前单元视觉定位的一段简洁自然语言推理，重点检查最早未覆盖锚点、"
        "页面阅读顺序、单栏或双栏布局以及下一个定位框；不提交坐标。"
    ),
    arguments_model=ThinkArguments,
)

DRAW_BOUNDING_BOX_TOOL: Final[dict[str, Any]] = _function_tool(
    name="draw_bbox",
    description=(
        "按照程序给定的锚点顺序绘制一个单页 0～1000 归一化坐标框。"
        "一次必须从最早未覆盖锚点开始，可以覆盖同页一个或多个连续锚点；"
        "不得跳过、重复或跨页消费锚点。"
    ),
    arguments_model=DrawBoundingBoxArguments,
)

FINISH_TOOL: Final[dict[str, Any]] = _function_tool(
    name="finish",
    description=(
        "确认当前单元全部定位锚点都已由成功的 draw_bbox 调用覆盖后调用此工具完成定位；"
        "程序仍会检查遗漏锚点和成功调用上限。"
    ),
    arguments_model=FinishArguments,
)

VISUAL_GROUNDING_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    THINK_TOOL,
    DRAW_BOUNDING_BOX_TOOL,
    FINISH_TOOL,
)
# 只有 auto + 全部 non-strict 才能绕过 vLLM XGrammar；每轮必须有一个
# 合法动作的约束由提示词、短期记忆反馈和本地状态机执行。
VISUAL_GROUNDING_TOOL_CHOICE: Final = TOOL_CHOICE_AUTO

_ARGUMENT_MODELS: Final[dict[str, type[StrictVisualGroundingModel]]] = {
    "think": ThinkArguments,
    "draw_bbox": DrawBoundingBoxArguments,
    "finish": FinishArguments,
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


def parse_visual_grounding_tool_arguments(
    name: str,
    raw_arguments: str,
) -> VisualGroundingToolArguments:
    """在执行工具前再次解析并严格校验模型参数。"""
    try:
        arguments_model = _ARGUMENT_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知的单元视觉定位工具：{name}") from exc
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具 {name} 的参数不是有效 JSON") from exc
    return arguments_model.model_validate(_decode_embedded_json(payload))


def _validate_anchor_configuration(
    anchors: tuple[LocalizationAnchor, ...],
) -> None:
    """拒绝无法支持确定性顺序校验的程序锚点配置。"""
    if not anchors:
        raise ValueError("视觉定位至少需要一个程序锚点")
    if len({anchor.anchor_id for anchor in anchors}) != len(anchors):
        raise ValueError("程序定位锚点包含重复 anchor_id")
    if tuple(anchor.order for anchor in anchors) != tuple(range(1, len(anchors) + 1)):
        raise ValueError("程序定位锚点必须按从 1 开始的连续 order 传入")
    if any(
        current.page_number > following.page_number
        for current, following in pairwise(anchors)
    ):
        raise ValueError("程序定位锚点页码必须遵循文档阅读顺序")


def validate_draw_bounding_box(
    arguments: DrawBoundingBoxArguments,
    *,
    anchors: tuple[LocalizationAnchor, ...],
    located_boxes: tuple[LocatedBoundingBox, ...],
) -> LocatedBoundingBox:
    """校验锚点消费顺序、页码、调用上限和重复坐标，并生成接受结果。"""
    _validate_anchor_configuration(anchors)
    consumed_ids = tuple(
        anchor_id for box in located_boxes for anchor_id in box.anchor_ids
    )
    all_anchor_ids = tuple(anchor.anchor_id for anchor in anchors)
    if len(located_boxes) >= len(anchors):
        raise VisualGroundingToolError(
            "draw_bbox",
            f"成功调用次数已达到锚点数 {len(anchors)}",
            "停止绘制并在全部锚点覆盖后调用 finish",
        )
    if consumed_ids != all_anchor_ids[: len(consumed_ids)]:
        raise ValueError("已接受定位框没有按程序锚点顺序保存")
    if len(consumed_ids) == len(all_anchor_ids):
        raise VisualGroundingToolError(
            "draw_bbox",
            "全部定位锚点均已覆盖，不能继续绘制",
            "调用 finish 结束当前单元定位",
        )

    submitted_ids = tuple(arguments.anchor_ids)
    expected_ids = all_anchor_ids[
        len(consumed_ids) : len(consumed_ids) + len(submitted_ids)
    ]
    if submitted_ids != expected_ids:
        next_anchor_id = all_anchor_ids[len(consumed_ids)]
        raise VisualGroundingToolError(
            "anchor_ids",
            f"必须从最早未覆盖锚点 {next_anchor_id} 开始并连续提交",
            f"按顺序提交 {list(expected_ids) or [next_anchor_id]}，不得跳过或重复锚点",
        )

    anchors_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    submitted_anchors = tuple(anchors_by_id[anchor_id] for anchor_id in submitted_ids)
    submitted_pages = {anchor.page_number for anchor in submitted_anchors}
    if len(submitted_pages) != 1:
        raise VisualGroundingToolError(
            "anchor_ids",
            "一个定位框不能消费不同页面的锚点",
            "只提交当前页面的连续锚点，下一页使用新的 draw_bbox 调用",
        )
    expected_page = submitted_anchors[0].page_number
    if arguments.page_number != expected_page:
        raise VisualGroundingToolError(
            "page_number",
            f"所选锚点位于第 {expected_page} 页，但提交了第 {arguments.page_number} 页",
            f"把 page_number 改为 {expected_page}",
        )

    bbox = tuple(arguments.bbox_2d)
    if any(
        box.page_number == arguments.page_number and box.bbox_2d == bbox
        for box in located_boxes
    ):
        raise VisualGroundingToolError(
            "bbox_2d",
            "当前页面已经保存完全相同的坐标框",
            "检查是否重复定位；若仍有锚点未覆盖，请绘制其真实区域",
        )

    previous_box = located_boxes[-1] if located_boxes else None
    if previous_box is not None and previous_box.page_number == arguments.page_number:
        _, previous_y_min, previous_x_max, _ = previous_box.bbox_2d
        current_x_min, current_y_min, _, _ = bbox
        moved_upward = current_y_min < previous_y_min
        moved_to_disjoint_right_column = current_x_min >= previous_x_max
        if moved_upward and not moved_to_disjoint_right_column:
            raise VisualGroundingToolError(
                "bbox_2d",
                "当前框在同页阅读顺序中回到了前一个框上方，且没有进入右侧独立栏",
                "同栏按从上到下绘制；双栏换栏时让新框位于前框右侧，或合并连续锚点",
            )
    # 双栏阅读可以从左栏底部跳到右栏顶部，因此只允许在两个横向不相交、
    # 且新框位于右侧时发生 y 回跳，不使用全局 y 单调规则。
    return LocatedBoundingBox(
        anchor_ids=submitted_ids,
        page_number=arguments.page_number,
        bbox_2d=bbox,
    )


def validate_finish(
    *,
    anchors: tuple[LocalizationAnchor, ...],
    located_boxes: tuple[LocatedBoundingBox, ...],
) -> None:
    """只有全部锚点均按顺序被成功定位后才接受 finish。"""
    _validate_anchor_configuration(anchors)
    consumed_ids = tuple(
        anchor_id for box in located_boxes for anchor_id in box.anchor_ids
    )
    expected_ids = tuple(anchor.anchor_id for anchor in anchors)
    if consumed_ids != expected_ids:
        missing_ids = expected_ids[len(consumed_ids) :]
        raise VisualGroundingToolError(
            "finish",
            f"仍有未覆盖锚点 {list(missing_ids)}",
            "继续按顺序调用 draw_bbox，全部覆盖后再调用 finish",
        )


def successful_tool_feedback(
    name: str,
    *,
    accepted_box: LocatedBoundingBox | None = None,
) -> VisualGroundingToolFeedback:
    """返回不携带冗余状态的最小成功反馈。"""
    if name == "think":
        return VisualGroundingToolFeedback(
            ok=True,
            message="思考已记录，请继续定位最早未覆盖锚点。",
        )
    if name == "finish":
        return VisualGroundingToolFeedback(
            ok=True,
            message="当前单元的视觉定位已经完成。",
        )
    if name == "draw_bbox" and accepted_box is not None:
        return VisualGroundingToolFeedback(
            ok=True,
            message=(
                f"已保存第 {accepted_box.page_number} 页定位框，"
                f"覆盖锚点 {list(accepted_box.anchor_ids)}。"
            ),
        )
    raise ValueError("draw_bbox 的成功反馈必须提供 accepted_box")


def validation_error_feedback(error: Exception) -> VisualGroundingToolFeedback:
    """把参数或状态错误转换为包含改进方向的简短反馈。"""
    if isinstance(error, VisualGroundingToolError):
        return VisualGroundingToolFeedback(
            ok=False,
            message=f"{error.path}：{error.problem}；请{error.correction}。",
        )
    if not isinstance(error, ValidationError):
        return VisualGroundingToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按当前工具参数定义修正后重新调用。",
        )

    messages: list[str] = []
    for item in error.errors(include_url=False)[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        if "bbox_2d" in path:
            correction = "提交四个 0～1000 整数并保证左上坐标严格小于右下坐标"
        elif "anchor_ids" in path:
            correction = "提交至少一个不重复的有效锚点标识"
        elif "page_number" in path:
            correction = "提交大于等于 1 的物理页码"
        elif item["type"] == "extra_forbidden":
            correction = "删除该未定义参数"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    return VisualGroundingToolFeedback(ok=False, message="\n".join(messages))


__all__ = [
    "DRAW_BOUNDING_BOX_TOOL",
    "FINISH_TOOL",
    "THINK_TOOL",
    "VISUAL_GROUNDING_TOOLS",
    "VISUAL_GROUNDING_TOOL_CHOICE",
    "VISUAL_GROUNDING_TOOL_VERSION",
    "DrawBoundingBoxArguments",
    "FinishArguments",
    "LocalizationAnchor",
    "LocatedBoundingBox",
    "ThinkArguments",
    "VisualGroundingToolArguments",
    "VisualGroundingToolError",
    "VisualGroundingToolFeedback",
    "parse_visual_grounding_tool_arguments",
    "successful_tool_feedback",
    "validate_draw_bounding_box",
    "validate_finish",
    "validation_error_feedback",
]
