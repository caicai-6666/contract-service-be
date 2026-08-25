"""预处理子图内部的文档结构发现节点。"""

from __future__ import annotations

from itertools import chain
import json
from time import perf_counter
from typing import Iterable

from pydantic import ValidationError

from app.agent.contract_extraction.subgraph.preprocessing.document_structure.prompt import (
    UNIT_DISCOVERY_PROMPT_VERSION,
    build_unit_discovery_messages,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.state import (
    DocumentScope,
    DocumentStructureMetadata,
    DocumentStructureState,
    DocumentUnit,
    ToolCallAudit,
    UnitDiscoveryResult,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.tool import (
    DISCOVERY_TOOL_CHOICE,
    DISCOVERY_TOOLS,
    FIRST_ROUND_TOOL_CHOICE,
    FIRST_ROUND_TOOLS,
    FinishArguments,
    GenerateUnitArguments,
    SummaryArguments,
    ThinkArguments,
    ToolFeedback,
    parse_tool_arguments,
)
from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient, MLLMToolCall


class UnitDiscoveryProtocolError(RuntimeError):
    """模型响应无法按照单工具状态机继续执行。"""

    def __init__(
        self,
        message: str,
        *,
        audits: tuple[ToolCallAudit, ...] = (),
        scope: DocumentScope | None = None,
        units: tuple[DocumentUnit, ...] = (),
    ) -> None:
        super().__init__(message)
        self.audits = audits
        self.scope = scope
        self.units = units


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """只在至少一个响应提供用量时返回总和。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _validation_suggestion(path: str) -> str:
    """根据字段路径提供可直接执行的最短修正方向。"""
    if "page_number" in path:
        return "改为 1 至合同总页数范围内的物理页码"
    if "bbox" in path:
        return "使用 0～1000 的有效矩形坐标，无法定位时传 null"
    if path.startswith("evidence"):
        return "提供至少一条带页码、类型和可核对内容的页面证据"
    if "span" in path:
        return "重新检查开始与结束锚点，并保证开始位置不晚于结束位置"
    return "按照当前工具的参数定义修正该字段"


def _validation_feedback(error: Exception) -> ToolFeedback:
    """把解析或 Pydantic 错误压缩为“位置、问题、改进方向”。"""
    if not isinstance(error, ValidationError):
        return ToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按工具参数定义修正后重新调用。",
        )

    messages: list[str] = []
    for item in error.errors(include_url=False)[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        messages.append(
            f"{path}：{problem}；请{_validation_suggestion(path)}。"
        )
    return ToolFeedback(ok=False, message="\n".join(messages))


def _page_ranges(page_numbers: set[int]) -> str:
    """把离散页码压缩为适合反馈模型的连续范围。"""
    if not page_numbers:
        return ""
    ordered = sorted(page_numbers)
    ranges: list[str] = []
    start = previous = ordered[0]
    for page_number in ordered[1:]:
        if page_number == previous + 1:
            previous = page_number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page_number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return "、".join(ranges)


def _validate_page_numbers(
    page_numbers: Iterable[int],
    *,
    page_count: int,
    path: str,
) -> ToolFeedback | None:
    """校验模型引用的物理页码没有越出合同。"""
    invalid = sorted({number for number in page_numbers if not 1 <= number <= page_count})
    if invalid:
        return ToolFeedback(
            ok=False,
            message=(
                f"{path}：引用了合同范围外的页码 {invalid}，本合同只有 1-{page_count} 页；"
                "请重新查看页面标签并改为有效物理页码。"
            ),
        )
    return None


def _accept_summary(
    arguments: SummaryArguments,
    *,
    page_count: int,
) -> tuple[ToolFeedback, DocumentScope | None]:
    """校验并保存首轮合同整体认识。"""
    page_error = _validate_page_numbers(
        (evidence.page_number for evidence in arguments.evidence),
        page_count=page_count,
        path="evidence.page_number",
    )
    if page_error:
        return page_error, None
    return (
        ToolFeedback(
            ok=True,
            message="合同整体认识已保存，请开始发现宏观内容单元。",
        ),
        DocumentScope.from_arguments(arguments),
    )


def _maximum_unit_count(page_count: int) -> int:
    """返回粗粒度单元的安全上限，供校验与状态机预算共同使用。"""
    return max(6, page_count * 3)


def _accept_unit(
    arguments: GenerateUnitArguments,
    *,
    page_count: int,
    units: list[DocumentUnit],
) -> tuple[ToolFeedback, DocumentUnit | None]:
    """执行页码、顺序、重复和粗粒度数量的程序校验。"""
    span = arguments.decision.span
    referenced_pages = chain(
        (evidence.page_number for evidence in arguments.evidence),
        (span.start.page_number, span.end.page_number),
    )
    page_error = _validate_page_numbers(
        referenced_pages,
        page_count=page_count,
        path="evidence/decision.span.page_number",
    )
    if page_error:
        return page_error, None

    outside_span = sorted(
        {
            evidence.page_number
            for evidence in arguments.evidence
            if not span.start.page_number
            <= evidence.page_number
            <= span.end.page_number
        }
    )
    if outside_span:
        return (
            ToolFeedback(
                ok=False,
                message=(
                    f"evidence.page_number：证据页 {outside_span} 不在当前单元的起止范围内；"
                    "请移动单元边界以包含证据，或改用该范围内的证据。"
                ),
            ),
            None,
        )

    if units:
        previous = units[-1]
        previous_span = previous.decision.span
        if span.start.page_number < previous_span.end.page_number:
            return (
                ToolFeedback(
                    ok=False,
                    message=(
                        "decision.span.start.page_number：当前单元从前一个单元结束页之前开始，"
                        f"与 {previous.unit_id} 发生跨页重叠；请合并两个同类单元，"
                        "或把当前开始边界移动到前一单元结束位置之后。"
                    ),
                ),
                None,
            )
        if any(
            unit.decision.label == arguments.decision.label
            and unit.decision.span == arguments.decision.span
            for unit in units
        ):
            return (
                ToolFeedback(
                    ok=False,
                    message=(
                        "decision：该名称和起止范围已经生成过，属于重复单元；"
                        "请检查尚未覆盖的内容，不要再次提交相同单元。"
                    ),
                ),
                None,
            )

    maximum_units = _maximum_unit_count(page_count)
    if len(units) >= maximum_units:
        return (
            ToolFeedback(
                ok=False,
                message=(
                    f"decision：当前已经生成 {len(units)} 个单元，明显存在过细风险；"
                    "请停止按条款或自然段拆分，合并具有相同整体功能的相邻内容。"
                ),
            ),
            None,
        )

    unit_id = f"unit-{len(units) + 1:03d}"
    unit = DocumentUnit.from_arguments(unit_id, arguments)
    return (
        ToolFeedback(
            ok=True,
            message=f"内容单元已保存为 {unit_id}，请继续检查剩余内容。",
        ),
        unit,
    )


def _accept_finish(
    arguments: FinishArguments,
    *,
    page_count: int,
    units: list[DocumentUnit],
) -> ToolFeedback:
    """只有全部物理页面已被单元跨度覆盖时才接受结束。"""
    if not units:
        return ToolFeedback(
            ok=False,
            message=(
                "units：尚未生成任何内容单元，无法结束；"
                "请先调用 generate_unit 覆盖合同中的宏观内容。"
            ),
        )
    covered = set(
        chain.from_iterable(
            range(
                unit.decision.span.start.page_number,
                unit.decision.span.end.page_number + 1,
            )
            for unit in units
        )
    )
    uncovered = set(range(1, page_count + 1)) - covered
    if uncovered:
        return ToolFeedback(
            ok=False,
            message=(
                f"units：第 {_page_ranges(uncovered)} 页尚未被任何内容单元覆盖；"
                "请检查这些页面并生成缺失单元，或调整相邻单元边界后再调用 finish。"
            ),
        )
    return ToolFeedback(ok=True, message="单元发现已经完成。")


def _tool_message(call: MLLMToolCall, feedback: ToolFeedback) -> dict[str, str]:
    """构造与 assistant tool_call_id 一一对应的短期记忆消息。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


async def discover_document_units(
    state: DocumentStructureState,
) -> DocumentStructureState:
    """通过异步 strict function calling 循环发现合同宏观内容单元。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    pages = prepared_pdf.pages
    messages = build_unit_discovery_messages(
        pages,
        state["prompt_context"].pages,
    )

    settings = get_settings().mllm
    generation = settings.generation
    scope: DocumentScope | None = None
    units: list[DocumentUnit] = []
    audits: list[ToolCallAudit] = []
    consecutive_thinks = 0
    maximum_units = _maximum_unit_count(prepared_pdf.page_count)
    # 每个单元通常需要一次 think 和一次 generate_unit；额外轮次供首轮
    # summary、最终 finish 以及少量参数纠错使用。单页合同也可能包含多个宏观区域，
    # 因此不能仅按页数把轮次压到 12。
    maximum_rounds = min(64, max(20, maximum_units * 2 + 6))

    async with MLLMClient(settings) as client:
        for round_number in range(1, maximum_rounds + 1):
            first_round = scope is None
            request_started_at = perf_counter()
            response = await client.create_tool_chat_completion(
                messages=messages,
                tools=list(FIRST_ROUND_TOOLS if first_round else DISCOVERY_TOOLS),
                tool_choice=(
                    FIRST_ROUND_TOOL_CHOICE
                    if first_round
                    else DISCOVERY_TOOL_CHOICE
                ),
                max_completion_tokens=generation.max_completion_tokens,
                temperature=generation.temperature,
                top_p=generation.top_p,
                top_k=generation.top_k,
                presence_penalty=generation.presence_penalty,
                repetition_penalty=generation.repetition_penalty,
                seed=generation.seed,
                enable_thinking=False,
            )
            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            if len(response.tool_calls) != 1:
                raise UnitDiscoveryProtocolError(
                    "模型每轮必须返回且只能返回一个函数工具调用；"
                    f"第 {round_number} 轮实际返回 {len(response.tool_calls)} 个。"
                )

            call = response.tool_calls[0]
            messages.append(response.assistant_message)
            accepted_finish: FinishArguments | None = None
            try:
                arguments = parse_tool_arguments(call.name, call.arguments)
            except (ValueError, ValidationError) as exc:
                feedback = _validation_feedback(exc)
            else:
                if first_round and isinstance(arguments, SummaryArguments):
                    feedback, accepted_scope = _accept_summary(
                        arguments,
                        page_count=prepared_pdf.page_count,
                    )
                    if accepted_scope is not None:
                        scope = accepted_scope
                    consecutive_thinks = 0
                elif not first_round and isinstance(arguments, ThinkArguments):
                    consecutive_thinks += 1
                    if consecutive_thinks > 2:
                        feedback = ToolFeedback(
                            ok=False,
                            message=(
                                "reason：已经连续调用 think 两次但没有形成结构结果；"
                                "请根据现有判断调用 generate_unit，或在覆盖完成时调用 finish。"
                            ),
                        )
                    else:
                        feedback = ToolFeedback(
                            ok=True,
                            message="理由已记录，请继续选择下一步操作。",
                        )
                elif not first_round and isinstance(arguments, GenerateUnitArguments):
                    feedback, accepted_unit = _accept_unit(
                        arguments,
                        page_count=prepared_pdf.page_count,
                        units=units,
                    )
                    if accepted_unit is not None:
                        units.append(accepted_unit)
                    consecutive_thinks = 0
                elif not first_round and isinstance(arguments, FinishArguments):
                    feedback = _accept_finish(
                        arguments,
                        page_count=prepared_pdf.page_count,
                        units=units,
                    )
                    if feedback.ok:
                        accepted_finish = arguments
                    consecutive_thinks = 0
                else:
                    expected = "summary" if first_round else "think、generate_unit 或 finish"
                    feedback = ToolFeedback(
                        ok=False,
                        message=(
                            f"tool：当前阶段不能调用 {call.name}；"
                            f"请改为调用 {expected}。"
                        ),
                    )

            messages.append(_tool_message(call, feedback))
            completion = response.completion
            audits.append(
                ToolCallAudit(
                    round_number=round_number,
                    call_id=call.call_id,
                    name=call.name,
                    raw_arguments=call.arguments,
                    feedback=feedback,
                    elapsed_ms=elapsed_ms,
                    response_id=completion.response_id,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cached_tokens=completion.cached_tokens,
                )
            )
            if accepted_finish is not None:
                assert scope is not None
                result = UnitDiscoveryResult(
                    document_id=prepared_pdf.document_id,
                    model=completion.model or settings.model,
                    prompt_version=UNIT_DISCOVERY_PROMPT_VERSION,
                    scope=scope,
                    units=tuple(units),
                    finish_reason=accepted_finish.reason,
                    rounds=round_number,
                    elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                    prompt_tokens=_sum_optional(
                        audit.prompt_tokens for audit in audits
                    ),
                    completion_tokens=_sum_optional(
                        audit.completion_tokens for audit in audits
                    ),
                    cached_tokens=_sum_optional(
                        audit.cached_tokens for audit in audits
                    ),
                    tool_calls=tuple(audits),
                )
                return {
                    "unit_discovery": result,
                    "document_structure": DocumentStructureMetadata(
                        document_id=result.document_id,
                        scope=result.scope,
                        units=result.units,
                    ),
                }

    raise UnitDiscoveryProtocolError(
        f"单元发现达到最大轮次 {maximum_rounds}，模型仍未完成有效 finish。",
        audits=tuple(audits),
        scope=scope,
        units=tuple(units),
    )


__all__ = [
    "UnitDiscoveryProtocolError",
    "discover_document_units",
]
