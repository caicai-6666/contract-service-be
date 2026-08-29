"""文档单元视觉定位节点。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from app.agent.contract_extraction.state import PDFPromptPage, PreparedPDFPage
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.state import (
    DocumentStructureMetadata,
    DocumentUnit,
    UnitLocation,
    UnitLocationRegion,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.visual_grounding.prompt import (
    UNIT_VISUAL_GROUNDING_PROMPT_VERSION,
    build_unit_visual_grounding_messages,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.visual_grounding.state import (
    GroundingToolCallAudit,
    UnitGroundingSessionResult,
    UnitVisualGroundingResult,
    VisualGroundingState,
)
from app.agent.contract_extraction.subgraph.preprocessing.document_structure.visual_grounding.tool import (
    VISUAL_GROUNDING_TOOL_CHOICE,
    VISUAL_GROUNDING_TOOL_VERSION,
    VISUAL_GROUNDING_TOOLS,
    DrawBoundingBoxArguments,
    FinishArguments,
    LocalizationAnchor,
    LocatedBoundingBox,
    ThinkArguments,
    VisualGroundingToolFeedback,
    parse_visual_grounding_tool_arguments,
    successful_tool_feedback,
    validate_draw_bounding_box,
    validate_finish,
    validation_error_feedback,
)
from app.agent.contract_extraction.tool_protocol import (
    ToolProtocolRecovery,
    audited_assistant_content,
    build_protocol_recovery_message,
)
from app.core.config import MLLMSettings, get_settings
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

_MAXIMUM_CONSECUTIVE_THINKS = 2
_MAXIMUM_COMPLETION_TOKENS = 1024


def _sum_optional(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def build_localization_anchors(unit: DocumentUnit) -> tuple[LocalizationAnchor, ...]:
    """按页面阅读顺序整理边界、中间锚点，并补齐无锚点的中间页。"""
    span = unit.decision.span
    anchors_by_page: dict[int, list[tuple[str, str, str]]] = {
        page_number: []
        for page_number in range(span.start.page_number, span.end.page_number + 1)
    }
    anchors_by_page[span.start.page_number].append(
        ("start", span.start.anchor_kind.value, span.start.anchor)
    )
    for anchor in span.navigation_anchors:
        anchors_by_page[anchor.page_number].append(
            ("navigation", anchor.anchor_kind.value, anchor.anchor)
        )
    anchors_by_page[span.end.page_number].append(
        ("end", span.end.anchor_kind.value, span.end.anchor)
    )

    ordered: list[tuple[int, str, str, str]] = []
    for page_number, page_anchors in anchors_by_page.items():
        if not page_anchors:
            page_anchors.append(
                (
                    "page_body",
                    "page_body",
                    f"第 {page_number} 页属于当前单元的连续中间内容",
                )
            )
        ordered.extend(
            (page_number, source, anchor_kind, content)
            for source, anchor_kind, content in page_anchors
        )

    return tuple(
        LocalizationAnchor(
            anchor_id=f"{unit.unit_id}-anchor-{order:03d}",
            order=order,
            page_number=page_number,
            source=source,
            anchor_kind=anchor_kind,
            content=content,
        )
        for order, (page_number, source, anchor_kind, content) in enumerate(
            ordered,
            start=1,
        )
    )


def select_unit_pages(
    unit: DocumentUnit,
    *,
    pages: tuple[PreparedPDFPage, ...],
    prompt_pages: tuple[PDFPromptPage, ...],
) -> tuple[tuple[PreparedPDFPage, ...], tuple[PDFPromptPage, ...]]:
    """只选择单元连续跨度涉及的页面，并保留对应物理页码描述。"""
    page_by_number = {page.page_number: page for page in pages}
    prompt_by_number = {page.page_number: page for page in prompt_pages}
    span = unit.decision.span
    required_numbers = tuple(range(span.start.page_number, span.end.page_number + 1))
    missing_pages = [
        page_number
        for page_number in required_numbers
        if page_number not in page_by_number or page_number not in prompt_by_number
    ]
    if missing_pages:
        raise ValueError(
            f"单元 {unit.unit_id} 引用了不存在的预处理页面 {missing_pages}"
        )
    return (
        tuple(page_by_number[page_number] for page_number in required_numbers),
        tuple(prompt_by_number[page_number] for page_number in required_numbers),
    )


def _tool_message(
    call: MLLMToolCall,
    feedback: VisualGroundingToolFeedback,
) -> dict[str, str]:
    """把极简执行反馈写入当前单元自己的短期记忆。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _protocol_recovery_message(
    *,
    tool_call_count: int,
) -> dict[str, str]:
    """说明协议失败并给出当前聊天模板要求的合法工具调用骨架。"""
    return build_protocol_recovery_message(
        tool_call_count=tool_call_count,
        result_label="定位结果",
    )


def _audited_assistant_content(value: object) -> str | None:
    """保留有限普通文本用于审计，避免把长自由输出无限写入运行状态。"""
    return audited_assistant_content(value)


def _discard_protocol_recovery_memory(
    messages: list[dict[str, Any]],
    recovery_start: int | None,
) -> None:
    """纠正成功后删除恢复期间的临时对话，审计状态不受影响。"""
    recovery = ToolProtocolRecovery(memory_start=recovery_start)
    recovery.accept_correction(messages)


def _failed_session(
    unit: DocumentUnit,
    *,
    page_numbers: tuple[int, ...],
    anchors: tuple[LocalizationAnchor, ...],
    located_boxes: list[LocatedBoundingBox],
    audits: list[GroundingToolCallAudit],
    started_at: float,
    error: str,
) -> UnitGroundingSessionResult:
    """保留失败会话审计，但不把部分定位框提升为权威结果。"""
    return UnitGroundingSessionResult(
        unit_id=unit.unit_id,
        status="failed",
        page_numbers=page_numbers,
        anchors=anchors,
        located_boxes=tuple(located_boxes),
        finish_reason=None,
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        prompt_tokens=_sum_optional(audit.prompt_tokens for audit in audits),
        completion_tokens=_sum_optional(audit.completion_tokens for audit in audits),
        cached_tokens=_sum_optional(audit.cached_tokens for audit in audits),
        tool_calls=tuple(audits),
        error=error,
    )


async def _locate_one_unit(
    unit: DocumentUnit,
    *,
    pages: tuple[PreparedPDFPage, ...],
    prompt_pages: tuple[PDFPromptPage, ...],
    client: MLLMClient,
    semaphore: asyncio.Semaphore,
    settings: MLLMSettings,
) -> UnitGroundingSessionResult:
    """为一个单元维护隔离的工具调用历史并收集全部定位框。"""
    started_at = perf_counter()
    selected_pages, selected_prompt_pages = select_unit_pages(
        unit,
        pages=pages,
        prompt_pages=prompt_pages,
    )
    page_numbers = tuple(page.page_number for page in selected_pages)
    anchors = build_localization_anchors(unit)
    messages = build_unit_visual_grounding_messages(
        selected_pages,
        selected_prompt_pages,
        unit,
        anchors,
    )
    generation = settings.generation
    audits: list[GroundingToolCallAudit] = []
    located_boxes: list[LocatedBoundingBox] = []
    consecutive_thinks = 0
    protocol_recovery = ToolProtocolRecovery()
    maximum_rounds = min(64, max(8, len(anchors) * 3 + 3))

    for round_number in range(1, maximum_rounds + 1):
        request_started_at = perf_counter()
        try:
            async with semaphore:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(VISUAL_GROUNDING_TOOLS),
                    tool_choice=VISUAL_GROUNDING_TOOL_CHOICE,
                    max_completion_tokens=min(
                        generation.max_completion_tokens,
                        _MAXIMUM_COMPLETION_TOKENS,
                    ),
                    temperature=0,
                    top_p=generation.top_p,
                    top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,
                    enable_thinking=False,
                    # 三个工具对所有单元完全一致；单元专属目标位于工具之后，
                    # 使相同页面集合的并发会话尽量复用“页面 + 规则 + 工具”。
                    tool_placement="before_task",
                )
        except (MLLMRequestError, MLLMUnavailableError) as exc:
            return _failed_session(
                unit,
                page_numbers=page_numbers,
                anchors=anchors,
                located_boxes=located_boxes,
                audits=audits,
                started_at=started_at,
                error=str(exc),
            )

        elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
        if len(response.tool_calls) != 1:
            completion = response.completion
            exceeded = protocol_recovery.record_protocol_failure(
                messages,
                assistant_message=response.assistant_message,
                tool_call_count=len(response.tool_calls),
                result_label="定位结果",
            )
            feedback = VisualGroundingToolFeedback(
                ok=False,
                message=(
                    f"tool_calls：本轮收到 {len(response.tool_calls)} 个工具调用，"
                    "必须恰好一个；请只提交一个定位动作。"
                ),
            )
            audits.append(
                GroundingToolCallAudit(
                    round_number=round_number,
                    call_id=None,
                    name="protocol_recovery",
                    raw_arguments="",
                    assistant_content=audited_assistant_content(
                        response.assistant_message.get("content")
                    ),
                    feedback=feedback,
                    elapsed_ms=elapsed_ms,
                    response_id=completion.response_id,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cached_tokens=completion.cached_tokens,
                )
            )
            if exceeded:
                return _failed_session(
                    unit,
                    page_numbers=page_numbers,
                    anchors=anchors,
                    located_boxes=located_boxes,
                    audits=audits,
                    started_at=started_at,
                    error="模型连续未形成恰好一个工具调用，已超过可恢复重试上限。",
                )
            continue

        call = response.tool_calls[0]
        protocol_recovery.accept_protocol()
        accepted_box: LocatedBoundingBox | None = None
        accepted_finish: FinishArguments | None = None
        try:
            arguments = parse_visual_grounding_tool_arguments(
                call.name,
                call.arguments,
            )
        except (ValueError, ValidationError) as exc:
            feedback = validation_error_feedback(exc)
        else:
            if isinstance(arguments, ThinkArguments):
                consecutive_thinks += 1
                if consecutive_thinks > _MAXIMUM_CONSECUTIVE_THINKS:
                    feedback = VisualGroundingToolFeedback(
                        ok=False,
                        message=(
                            "reasoning：已经连续调用 think 两次但没有推进定位；"
                            "请调用 draw_bbox，或在全部锚点完成后调用 finish。"
                        ),
                    )
                else:
                    feedback = successful_tool_feedback("think")
            elif isinstance(arguments, DrawBoundingBoxArguments):
                consecutive_thinks = 0
                try:
                    accepted_box = validate_draw_bounding_box(
                        arguments,
                        anchors=anchors,
                        located_boxes=tuple(located_boxes),
                    )
                except (ValueError, ValidationError) as exc:
                    feedback = validation_error_feedback(exc)
                else:
                    feedback = successful_tool_feedback(
                        "draw_bbox",
                        accepted_box=accepted_box,
                    )
            elif isinstance(arguments, FinishArguments):
                consecutive_thinks = 0
                try:
                    validate_finish(
                        anchors=anchors,
                        located_boxes=tuple(located_boxes),
                    )
                except (ValueError, ValidationError) as exc:
                    feedback = validation_error_feedback(exc)
                else:
                    feedback = successful_tool_feedback("finish")
                    accepted_finish = arguments
            else:  # pragma: no cover - 联合类型已覆盖所有工具参数
                feedback = VisualGroundingToolFeedback(
                    ok=False,
                    message=f"tool：当前不能调用 {call.name}；请使用视觉定位工具。",
                )

        tool_message = _tool_message(call, feedback)
        if feedback.ok:
            protocol_recovery.accept_correction(messages)
            messages.append(response.assistant_message)
            messages.append(tool_message)
        else:
            protocol_recovery.record_tool_failure(
                messages,
                assistant_message=response.assistant_message,
                tool_message=tool_message,
            )
        completion = response.completion
        audits.append(
            GroundingToolCallAudit(
                round_number=round_number,
                call_id=call.call_id,
                name=call.name,
                raw_arguments=call.arguments,
                assistant_content=audited_assistant_content(
                    response.assistant_message.get("content")
                ),
                feedback=feedback,
                elapsed_ms=elapsed_ms,
                response_id=completion.response_id,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                cached_tokens=completion.cached_tokens,
            )
        )
        if accepted_box is not None:
            located_boxes.append(accepted_box)
        if accepted_finish is not None:
            return UnitGroundingSessionResult(
                unit_id=unit.unit_id,
                status="completed",
                page_numbers=page_numbers,
                anchors=anchors,
                located_boxes=tuple(located_boxes),
                finish_reason=accepted_finish.reason,
                rounds=round_number,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                prompt_tokens=_sum_optional(audit.prompt_tokens for audit in audits),
                completion_tokens=_sum_optional(
                    audit.completion_tokens for audit in audits
                ),
                cached_tokens=_sum_optional(audit.cached_tokens for audit in audits),
                tool_calls=tuple(audits),
                error=None,
            )

    return _failed_session(
        unit,
        page_numbers=page_numbers,
        anchors=anchors,
        located_boxes=located_boxes,
        audits=audits,
        started_at=started_at,
        error=f"达到最大轮次 {maximum_rounds}，仍未形成有效 finish。",
    )


def _public_unit_location(result: UnitGroundingSessionResult) -> UnitLocation:
    """只把完整会话定位框提升为下游可用的权威结果。"""
    if result.status == "failed":
        return UnitLocation(
            unit_id=result.unit_id,
            status="failed",
            regions=(),
            error=result.error,
        )
    return UnitLocation(
        unit_id=result.unit_id,
        status="located",
        regions=tuple(
            UnitLocationRegion(
                anchor_ids=box.anchor_ids,
                page_number=box.page_number,
                bbox_2d=box.bbox_2d,
            )
            for box in result.located_boxes
        ),
        error=None,
    )


async def locate_document_units(
    state: VisualGroundingState,
) -> VisualGroundingState:
    """并发定位全部语义单元，每个单元只发送其连续跨度涉及的页面。

    固定使用 non-strict + auto 工具契约；无单工具响应会短期记忆反馈并
    有限重试，坐标状态机与本地校验保持不变。
    """
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    prompt_context = state["prompt_context"]
    structure = state["document_structure"]
    if prepared_pdf.document_id != structure.document_id:
        raise ValueError("视觉定位输入 PDF 与文档结构的 document_id 不一致")
    if not structure.units:
        raise ValueError("视觉定位至少需要一个文档单元")

    settings = get_settings().mllm
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    async with MLLMClient(settings) as client:
        unit_results = tuple(
            await asyncio.gather(
                *(
                    _locate_one_unit(
                        unit,
                        pages=prepared_pdf.pages,
                        prompt_pages=prompt_context.pages,
                        client=client,
                        semaphore=semaphore,
                        settings=settings,
                    )
                    for unit in structure.units
                )
            )
        )

    failed_count = sum(result.status == "failed" for result in unit_results)
    if failed_count == 0:
        status = "completed"
    elif failed_count == len(unit_results):
        status = "failed"
    else:
        status = "partial"
    grounding_result = UnitVisualGroundingResult(
        status=status,
        document_id=prepared_pdf.document_id,
        model=settings.model,
        prompt_version=UNIT_VISUAL_GROUNDING_PROMPT_VERSION,
        tool_version=VISUAL_GROUNDING_TOOL_VERSION,
        units=unit_results,
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
    )
    updated_structure: DocumentStructureMetadata = structure.model_copy(
        update={
            "unit_locations": tuple(
                _public_unit_location(result) for result in unit_results
            )
        }
    )
    return {
        "unit_grounding": grounding_result,
        "document_structure": updated_structure,
    }


__all__ = [
    "build_localization_anchors",
    "locate_document_units",
    "select_unit_pages",
]
