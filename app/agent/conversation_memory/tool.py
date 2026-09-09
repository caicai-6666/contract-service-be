"""记忆筛选工具契约及批次独立的动作校验；不负责模型循环或历史落盘。"""

from __future__ import annotations

import json
from typing import Any, Final, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from app.agent.conversation_memory.state import MemoryGenerationInput


MEMORY_PLANNING_TOOL_VERSION: Final = "conversation-memory-tools-v7"
MEMORY_PLANNING_TOOL_CHOICE: Final = "auto"


class MemoryFlowViolation(ValueError):
    """调用协议或执行顺序违规，由独立流程指引纠正；不用于参数错误。"""


class StrictMemoryToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def reject_blank_text(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("文本不能为空或仅包含空白")
        return value


class ThinkArguments(StrictMemoryToolModel):
    reasoning_summary: str = Field(
        min_length=1, max_length=2000,
        description="围绕当前记忆筛选任务进行相关分析与推理：梳理用户目标及任务关系、比较信息价值、识别冲突与不确定性、确定提取重点或下一步动作。以简洁、可复核的推理摘要表达，不局限于入选理由，不展开完整私有思维链，不在此提交任务选择。",
    )


class SelectTaskArguments(StrictMemoryToolModel):
    task_number: int = Field(
        ge=1,
        description="本批任务展示序号，从 1 开始；只能选择存在且大于上次成功选择序号的任务，不是任务 ID 或会话历史序号。",
    )
    evidence: str = Field(
        min_length=1, max_length=1500,
        description="来自所选任务自身的可定位依据，指出用户输入或轨迹序号并给出简短内容；不引用未读取的附件或网页，不以其他任务的结果补全本任务。",
    )
    reasoning_summary: str = Field(
        min_length=1, max_length=1500,
        description="说明前述依据为什么值得未来检索，区分用户要求、工具观察和助手结论，并保留冲突或未确认状态。",
    )
    extraction_requirements: str = Field(
        min_length=1, max_length=2000,
        description="专注当前单个任务，指导提取本轮新增、确认、延续或改变的信息及完成边界。明确用户意图可适度展开为完整陈述，但只完善表达，不新增对象、条件、动机、授权或结果，不把暂定写成已决定。前后变化须有本任务内依据，只有新条件时只写本轮调整为……。不从其他任务补全或合并内容，不只写总结全文。",
    )

class FinishSelectionArguments(StrictMemoryToolModel):
    reasoning_summary: str = Field(
        min_length=1, max_length=1500,
        description="确认已检查本批全部任务且所有值得提取的任务均已成功选择，简述结束依据；无选择时说明全部跳过的原因，不在此补交选择或提取要求。",
    )


_ARGUMENT_MODELS = {
    "think": ThinkArguments,
    "select_task": SelectTaskArguments,
    "finish_selection": FinishSelectionArguments,
}
_DESCRIPTIONS = {
    "think": "进行当前任务相关的分析与推理，可梳理任务关系、比较信息价值、分析冲突、规划提取重点及下一步动作；不提交正式选择，不改变业务状态。首个成功动作必须是 think，不得连续成功调用 think。",
    "select_task": "依据任务自身记录，提交一个值得提取的任务及其提取要求。必须先成功调用 think，选择序号严格递增；可以跳过但不能重复或回头补选。",
    "finish_selection": "确认全部任务已检查、所有选择均已提交后结束筛选。必须先成功调用 think；允许零选择，不修改已有选择，成功后不得继续调用工具。",
}
MEMORY_PLANNING_TOOLS: Final[tuple[dict[str, Any], ...]] = tuple(
    {
        "type": "function",
        "function": {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "parameters": model.model_json_schema(),
            "strict": False,
        },
    }
    for name, model in _ARGUMENT_MODELS.items()
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 属性，不能以最后一个值静默覆盖前一个值。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("工具参数含重复属性，请每个参数仅提交一次")
        result[key] = value
    return result


def parse_memory_planning_tool_arguments(
    name: str, raw_arguments: str,
) -> ThinkArguments | SelectTaskArguments | FinishSelectionArguments:
    """只解析服务端提供的 arguments JSON，不从普通文本猜测工具调用。"""
    if name not in _ARGUMENT_MODELS:
        raise ValueError("未知工具，请使用 think、select_task 或 finish_selection")
    if not isinstance(raw_arguments, str):
        raise ValueError("arguments 必须为 JSON 对象字符串")
    try:
        payload = json.loads(raw_arguments, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError("arguments 不是合法 JSON，请按工具定义提交 JSON 对象") from exc
    return _ARGUMENT_MODELS[name].model_validate(payload)


class SelectedMemoryTask(SelectTaskArguments):
    task_id: str = Field(description="程序根据展示序号映射的真实任务 ID，不由模型填写。")


class MemorySelectionResult(StrictMemoryToolModel):
    selected_tasks: tuple[SelectedMemoryTask, ...]
    skipped_task_ids: tuple[str, ...]
    reasoning_summary: str


class MemoryToolFeedback(StrictMemoryToolModel):
    ok: Literal[True] = True
    message: str


class MemoryPlanningTools:
    """每批创建一份动作状态；校验失败不修改选择或调用顺序。

    仅管理已接受结果，不持有模型 messages、失败轨迹或审计。
    调用方负责审计、有限恢复以及成功后只清理流程指引。
    """

    def __init__(self, request: MemoryGenerationInput):
        from app.agent.conversation_memory.state import MemoryGenerationInput

        validated = MemoryGenerationInput.model_validate(request.model_dump())
        self._task_ids = tuple(task.task_id for task in validated.tasks)
        self._last_action: str | None = None
        self._selected: tuple[SelectedMemoryTask, ...] = ()
        self._result: MemorySelectionResult | None = None

    @property
    def selected_tasks(self) -> tuple[SelectedMemoryTask, ...]:
        """只读的已接受选择，未 finish 时不能视作最终筛选结果。"""
        return self._selected

    @property
    def result(self) -> MemorySelectionResult | None:
        return self._result

    def execute(self, tool_calls: list[dict[str, Any]]) -> MemoryToolFeedback:
        """接受一轮 OpenAI 格式的工具调用；拒绝零调用、多调用及非法顺序。"""
        if self._result is not None:
            raise MemoryFlowViolation("筛选已完成，不能继续调用工具")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise MemoryFlowViolation("本轮必须且只能调用一个工具，请按当前允许的顺序重新调用")
        call = tool_calls[0]
        if not isinstance(call, dict) or call.get("type") != "function":
            raise MemoryFlowViolation("必须提交真实的 function 工具调用")
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise MemoryFlowViolation("工具调用缺少合法的 function.name")
        name = function["name"]
        arguments = parse_memory_planning_tool_arguments(name, function.get("arguments"))
        if self._last_action is None and name != "think":
            raise MemoryFlowViolation("首个成功动作必须为 think，请先调用 think")
        if name == "think":
            if self._last_action == "think":
                raise MemoryFlowViolation("不得连续调用 think，请选择任务或在满足完成条件时结束筛选")
            message = "任务相关推理已接受，请选择任务或在满足完成条件时结束筛选。"
        elif isinstance(arguments, SelectTaskArguments):
            number = arguments.task_number
            if number > len(self._task_ids):
                raise ValueError("task_number 超出本批范围，请使用展示列表中的真实序号")
            if self._selected and number <= self._selected[-1].task_number:
                raise MemoryFlowViolation("task_number 必须严格大于上次成功选择的序号，不得重复或回头补选")
            selection = SelectedMemoryTask(
                **arguments.model_dump(),
                task_id=self._task_ids[number - 1],
            )
            self._selected = (*self._selected, selection)
            message = f"任务 {number} 的选择已接受，请继续筛选或确认完成。"
        else:
            # 全部检查是模型的明确声明；程序能验证顺序与身份，不能证明语义判断正确。
            selected_ids = {item.task_id for item in self._selected}
            self._result = MemorySelectionResult(
                selected_tasks=self._selected,
                skipped_task_ids=tuple(t for t in self._task_ids if t not in selected_ids),
                reasoning_summary=arguments.reasoning_summary,
            )
            message = "筛选已完成，未选择的任务已标记为跳过。"
        self._last_action = name
        return MemoryToolFeedback(message=message)


# 单任务整理使用独立工具集合，避免将批量筛选的select_task暴露给该会话。
MEMORY_SUMMARIZING_TOOL_VERSION: Final = 'conversation-memory-summarizing-tools-v1'


class MemorySummaryThinkArguments(StrictMemoryToolModel):
    reasoning_summary: str = Field(
        min_length=1, max_length=2000,
        description='围绕当前单个任务核对原始依据与提取要求、信息来源、时间口径、未确认条件及记忆组织方式的简洁推理摘要。不提交正式记忆，不展开完整私有思维链。',
    )


class ExtractMemoryArguments(StrictMemoryToolModel):
    evidence: str = Field(
        min_length=1, max_length=2000,
        description='来自当前原始任务的可定位依据，指出用户输入或有效轨迹位置及关键内容，不把提取要求当作事实来源。若整个任务无可靠信息，说明已检查的内容及缺失的依据，不编造引用。',
    )
    reasoning_summary: str = Field(
        min_length=1, max_length=2000,
        description='简述如何依据原始任务组织记忆并保留来源、调整内容、适用条件和不确定性，不补充新事实。retrieval_text为null时，必须在此说明无法形成可靠记忆的原因。',
    )
    retrieval_text: str | None = Field(
        min_length=1, max_length=6000,
        description='适合未来检索的单任务中文记忆正文，保留有依据的事实及必要限定，不含工具流水账、内部标识、引导语或新增建议。仅在整个任务没有可靠整理依据时提交null；不得用空串或“无法整理”等提示句充当记忆。局部缺失时仍整理其余有效信息。',
    )


_SUMMARY_ARGUMENT_MODELS = {
    'think': MemorySummaryThinkArguments,
    'extract_memory': ExtractMemoryArguments,
}
_SUMMARY_DESCRIPTIONS = {
    'think': '分析当前任务及提取要求，核对原始事实和适用边界，不提交正式结果。首个成功动作必须是think，不得连续成功调用think。',
    'extract_memory': '提交当前任务的最终检索记忆，先给依据和整理理由，再给记忆正文。须先成功调用think；提交成功即结束本任务整理，不再另调完成工具。完全缺少可靠依据时以null正文和具体原因结束，不生成假记忆。',
}
MEMORY_SUMMARIZING_TOOLS: Final[tuple[dict[str, Any], ...]] = tuple(
    {'type': 'function', 'function': {
        'name': name, 'description': _SUMMARY_DESCRIPTIONS[name],
        'parameters': model.model_json_schema(), 'strict': False,
    }} for name, model in _SUMMARY_ARGUMENT_MODELS.items()
)


def parse_memory_summarizing_tool_arguments(
    name: str, raw_arguments: str,
) -> MemorySummaryThinkArguments | ExtractMemoryArguments:
    """只接受独立整理工具的真实JSON参数；错误由未来调用循环通过tool反馈。"""
    if name not in _SUMMARY_ARGUMENT_MODELS:
        raise ValueError('未知工具，请使用think或extract_memory')
    if not isinstance(raw_arguments, str):
        raise ValueError('arguments 必须为 JSON 对象字符串')
    try:
        payload = json.loads(raw_arguments, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError('arguments 不是合法 JSON，请按工具定义提交 JSON 对象') from exc
    return _SUMMARY_ARGUMENT_MODELS[name].model_validate(payload)


class MemorySummarizingTools:
    """每个任务独立的整理动作状态；只校验和提交结果，不调用模型或Embedding。"""

    def __init__(self):
        self._thought = False
        self._result: ExtractMemoryArguments | None = None

    @property
    def result(self) -> ExtractMemoryArguments | None:
        """None表示尚未提交；结果对象中的retrieval_text=None表示明确无可靠记忆。"""
        return self._result

    def execute(self, tool_calls: list[dict[str, Any]]) -> MemoryToolFeedback:
        if self._result is not None:
            raise MemoryFlowViolation('本任务整理已结束，不能继续调用工具')
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise MemoryFlowViolation('本轮必须且只能调用一个工具')
        call = tool_calls[0]
        if not isinstance(call, dict) or call.get('type') != 'function':
            raise MemoryFlowViolation('必须提交真实的 function 工具调用')
        function = call.get('function')
        if not isinstance(function, dict) or not isinstance(function.get('name'), str):
            raise MemoryFlowViolation('工具调用缺少合法的 function.name')
        arguments = parse_memory_summarizing_tool_arguments(function['name'], function.get('arguments'))
        if isinstance(arguments, MemorySummaryThinkArguments):
            if self._thought:
                raise MemoryFlowViolation('不得连续调用think，请调用extract_memory提交整理结果')
            self._thought = True
            return MemoryToolFeedback(message='分析已接受，请通过extract_memory提交整理结果。')
        if not self._thought:
            raise MemoryFlowViolation('首个成功动作必须为think，请先调用think')
        # 完整校验后才提交；null正文不是失败半成品，也不是可向量化的记忆文本。
        self._result = arguments
        return MemoryToolFeedback(message=(
            '记忆提取已完成。' if arguments.retrieval_text is not None
            else '已记录缺少可靠整理依据，本任务不生成记忆文本。'
        ))
