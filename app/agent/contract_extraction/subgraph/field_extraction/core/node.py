"""核心字段快照选择、公共任务组装与并行单字段提取节点。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_extraction.context import context_sha256
from app.agent.contract_extraction.progress import ParallelProgressTracker
from app.agent.contract_extraction.subgraph.field_extraction.core.prompt import (
    CORE_COMMON_PROMPT_VERSION,
    CORE_EXTRACTION_PROMPT_VERSION,
    build_core_common_messages,
    build_core_messages,
)
from app.agent.contract_extraction.subgraph.field_extraction.core.state import (
    AbandonedCore,
    CoreContext,
    CoreExtractionResult,
    CoreOutcome,
    CoreSubgraphState,
    ExtractedCore,
    ExtractedFieldObject,
    FailedCore,
    FieldToolCallAudit,
    FieldToolFeedback,
)
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinition,
)
from app.agent.contract_extraction.subgraph.field_extraction.tool import (
    FIELD_TOOL_CHOICE,
    AbandonExtractionArguments,
    ExtractObjectArguments,
    FieldObjectValidationError,
    FinishExtractionArguments,
    ThinkArguments,
    build_field_tools,
    canonical_field_value,
    parse_field_tool_arguments,
)
from app.agent.contract_extraction.tool_protocol import (
    ToolProtocolRecovery,
    audited_assistant_content,
    build_protocol_recovery_message,
)
from app.core.config import get_settings
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

_MAXIMUM_SINGLE_ROUNDS = 8
_MAXIMUM_MULTIPLE_ROUNDS = 32
_MAXIMUM_CONSECUTIVE_THINKS = 2
_MAXIMUM_COMPLETION_TOKENS = 2048


def _sum_optional(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def select_core_definitions(
    state: CoreSubgraphState,
) -> CoreSubgraphState:
    """从应用启动期内存快照选择 Core 定义，不执行文件 I/O。"""
    return {"core_definitions": state["field_definition_catalog"].core}


def assemble_core_context(
    state: CoreSubgraphState,
) -> CoreSubgraphState:
    """节点一：在最终合同前缀后追加全部 Core 字段共享的任务规则。"""
    prepared_pdf = state["prepared_pdf"]
    prefill_context = state["prefill_context"]
    if prepared_pdf.document_id != prefill_context.document_id:
        raise ValueError("核心字段输入 PDF 与最终公共前缀的 document_id 不一致")

    messages = build_core_common_messages(prefill_context)
    return {
        "core_context": CoreContext(
            document_id=prefill_context.document_id,
            prompt_version=CORE_COMMON_PROMPT_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


def _validation_feedback(
    error: Exception,
) -> FieldToolFeedback:
    """把工具解析错误转为包含位置、问题和修正方向的短反馈。"""
    if isinstance(error, FieldObjectValidationError):
        return FieldToolFeedback(
            ok=False,
            message=(
                f"{error.path}：{error.problem}；"
                f"请{error.correction}。"
            ),
        )
    if not isinstance(error, ValidationError):
        return FieldToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按当前工具参数定义修正后重试。",
        )

    errors = error.errors(include_url=False)
    messages: list[str] = []
    for item in errors[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        if ("reasoning" in path or "理由" in problem) and "必须以" in problem:
            suggestion = "按照错误给出的固定结尾改写 reasoning"
        elif "reasoning" in path or "理由" in problem:
            suggestion = "补充证据如何支持或不支持当前对象的简洁推理摘要"
        elif "page_number" in path:
            suggestion = "使用当前合同范围内的物理页码"
        elif path.startswith("evidence"):
            suggestion = "提供至少一条带页码和可核对内容的页面证据"
        elif path.startswith("value"):
            suggestion = "按当前扁平对象 Schema 补齐必填属性并修正类型"
        else:
            suggestion = "按照当前工具的参数定义修正该位置"
        messages.append(f"{path}：{problem}；请{suggestion}。")
    return FieldToolFeedback(ok=False, message="\n".join(messages))


def _validate_evidence_pages(
    page_numbers: Iterable[int],
    *,
    page_count: int,
) -> FieldToolFeedback | None:
    invalid = sorted({number for number in page_numbers if not 1 <= number <= page_count})
    if not invalid:
        return None
    return FieldToolFeedback(
        ok=False,
        message=(
            f"evidence.page_number：引用了合同范围外的页码 {invalid}，"
            f"本合同只有 1-{page_count} 页；请改为有效物理页码。"
        ),
    )


def _tool_message(call: MLLMToolCall, feedback: FieldToolFeedback) -> dict[str, str]:
    """只把最短友好反馈写入当前字段短期记忆。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.message,
    }


def _runtime_values(
    audits: list[FieldToolCallAudit],
) -> dict[str, int | None]:
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(
            audit.completion_tokens for audit in audits
        ),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _failed_field(
    definition: FieldDefinition,
    *,
    started_at: float,
    audits: list[FieldToolCallAudit],
    extracted_objects: list[ExtractedFieldObject],
    error: str,
) -> FailedCore:
    return FailedCore(
        name=definition.name,
        code=definition.code,
        cardinality=definition.cardinality,
        property_names=tuple(item.name for item in definition.properties),
        property_codes={
            item.name: item.code for item in definition.properties
        },
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        tool_calls=tuple(audits),
        partial_objects=tuple(extracted_objects),
        error=error,
        **_runtime_values(audits),
    )


async def _extract_one_core(
    definition: FieldDefinition,
    *,
    state: CoreSubgraphState,
    client: MLLMClient,
    semaphore: asyncio.Semaphore,
) -> CoreOutcome:
    """为一个定义维护隔离短期记忆，并按 cardinality 收集对象。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    messages = build_core_messages(state["core_context"], definition)
    settings = get_settings().mllm
    generation = settings.generation
    audits: list[FieldToolCallAudit] = []
    extracted_objects: list[ExtractedFieldObject] = []
    extracted_fingerprints: set[str] = set()
    consecutive_thinks = 0
    protocol_recovery = ToolProtocolRecovery()
    maximum_rounds = (
        _MAXIMUM_SINGLE_ROUNDS
        if definition.cardinality is FieldCardinality.SINGLE
        else _MAXIMUM_MULTIPLE_ROUNDS
    )

    for round_number in range(1, maximum_rounds + 1):
        tools = list(
            build_field_tools(
                definition,
                has_extracted_objects=bool(extracted_objects),
            )
        )
        request_started_at = perf_counter()
        try:
            async with semaphore:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=tools,
                    tool_choice=FIELD_TOOL_CHOICE,
                    max_completion_tokens=min(
                        generation.max_completion_tokens,
                        _MAXIMUM_COMPLETION_TOKENS,
                    ),
                    temperature=generation.temperature,
                    top_p=generation.top_p,
                    top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,
                    enable_thinking=False,
                    tool_placement="after_task",
                )
        except (MLLMRequestError, MLLMUnavailableError) as exc:
            return _failed_field(
                definition,
                started_at=started_at,
                audits=audits,
                extracted_objects=extracted_objects,
                error=str(exc),
            )

        request_elapsed_ms = round(
            (perf_counter() - request_started_at) * 1000,
            3,
        )
        if len(response.tool_calls) != 1:
            feedback = FieldToolFeedback(
                ok=False,
                message=build_protocol_recovery_message(
                    tool_call_count=len(response.tool_calls),
                    result_label="字段提取结果",
                )["content"],
            )
            completion = response.completion
            audits.append(
                FieldToolCallAudit(
                    round_number=round_number,
                    call_id=None,
                    name="protocol_recovery",
                    raw_arguments="",
                    assistant_content=audited_assistant_content(
                        response.assistant_message.get("content")
                    ),
                    feedback=feedback,
                    elapsed_ms=request_elapsed_ms,
                    response_id=completion.response_id,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cached_tokens=completion.cached_tokens,
                )
            )
            exceeded = protocol_recovery.record_protocol_failure(
                messages,
                assistant_message=response.assistant_message,
                tool_call_count=len(response.tool_calls),
                result_label="字段提取结果",
            )
            if exceeded:
                return _failed_field(
                    definition,
                    started_at=started_at,
                    audits=audits,
                    extracted_objects=extracted_objects,
                    error="连续三轮未生成且仅生成一个合法工具调用。",
                )
            continue

        call = response.tool_calls[0]
        protocol_recovery.accept_protocol()
        accepted_object: ExtractedFieldObject | None = None
        accepted_abandon: AbandonExtractionArguments | None = None
        accepted_finish: FinishExtractionArguments | None = None

        try:
            arguments = parse_field_tool_arguments(
                definition,
                call.name,
                call.arguments,
            )
        except (ValueError, ValidationError) as exc:
            feedback = _validation_feedback(
                exc,
            )
        else:
            if isinstance(arguments, ThinkArguments):
                consecutive_thinks += 1
                if consecutive_thinks > _MAXIMUM_CONSECUTIVE_THINKS:
                    feedback = FieldToolFeedback(
                        ok=False,
                        message=(
                            "reasoning：已经连续调用 think 两次但没有推进对象状态；"
                            "请提取对象，或根据当前可用工具结束任务。"
                        ),
                    )
                else:
                    feedback = FieldToolFeedback(
                        ok=True,
                        message="继续：推理已记录，请作出下一步判断。",
                    )
            elif isinstance(arguments, ExtractObjectArguments):
                consecutive_thinks = 0
                page_error = _validate_evidence_pages(
                    (evidence.page_number for evidence in arguments.evidence),
                    page_count=prepared_pdf.page_count,
                )
                if page_error is not None:
                    feedback = page_error
                else:
                    fingerprint = canonical_field_value(arguments.value)
                    if fingerprint in extracted_fingerprints:
                        feedback = FieldToolFeedback(
                            ok=False,
                            message=(
                                f"value：对象 {fingerprint} 已经成功提取；"
                                "请不要重复提交，继续查找其他对象或结束提取。"
                            ),
                        )
                    else:
                        accepted_object = ExtractedFieldObject(
                            evidence=tuple(arguments.evidence),
                            reasoning=arguments.reasoning,
                            value=arguments.value,
                        )
                        next_count = len(extracted_objects) + 1
                        if definition.cardinality is FieldCardinality.MULTIPLE:
                            message = (
                                f"成功：已记录第 {next_count} 个“{definition.name}”"
                                f"对象={fingerprint}。请继续查找其他对象；"
                                "确认全部完成后调用 finish_extraction。"
                            )
                        else:
                            message = (
                                f"成功：已提取“{definition.name}”对象={fingerprint}。"
                            )
                        feedback = FieldToolFeedback(ok=True, message=message)
            elif isinstance(arguments, AbandonExtractionArguments):
                consecutive_thinks = 0
                if extracted_objects:
                    feedback = FieldToolFeedback(
                        ok=False,
                        message=(
                            "tool：已有成功对象后不能放弃整个定义；"
                            "请继续提取或调用 finish_extraction。"
                        ),
                    )
                else:
                    feedback = FieldToolFeedback(
                        ok=True,
                        message=f"成功：已确认“{definition.name}”没有可靠对象。",
                    )
                    accepted_abandon = arguments
            elif isinstance(arguments, FinishExtractionArguments):
                consecutive_thinks = 0
                if definition.cardinality is not FieldCardinality.MULTIPLE:
                    feedback = FieldToolFeedback(
                        ok=False,
                        message=(
                            "tool：single 定义不使用 finish_extraction；"
                            "成功提交一个对象后会自动结束。"
                        ),
                    )
                elif not extracted_objects:
                    feedback = FieldToolFeedback(
                        ok=False,
                        message=(
                            "tool：尚未成功提取任何对象，不能声明提取完成；"
                            "请提取至少一个对象或调用 abandon_extraction。"
                        ),
                    )
                else:
                    feedback = FieldToolFeedback(
                        ok=True,
                        message=(
                            f"成功：已完成“{definition.name}”提取，"
                            f"共保留 {len(extracted_objects)} 个对象。"
                        ),
                    )
                    accepted_finish = arguments
            else:
                feedback = FieldToolFeedback(
                    ok=False,
                    message=(
                        f"tool：当前状态不能调用 {call.name}；"
                        "请使用本轮提供的对象提取工具。"
                    ),
                )

        tool_message = _tool_message(call, feedback)
        if feedback.ok:
            # 正确动作只继承此前有效记忆；连续错误调用与反馈从模型上下文删除。
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
            FieldToolCallAudit(
                round_number=round_number,
                call_id=call.call_id,
                name=call.name,
                raw_arguments=call.arguments,
                assistant_content=audited_assistant_content(
                    response.assistant_message.get("content")
                ),
                feedback=feedback,
                elapsed_ms=request_elapsed_ms,
                response_id=completion.response_id,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                cached_tokens=completion.cached_tokens,
            )
        )
        runtime = _runtime_values(audits)
        if accepted_object is not None:
            extracted_objects.append(accepted_object)
            extracted_fingerprints.add(canonical_field_value(accepted_object.value))
            if definition.cardinality is FieldCardinality.SINGLE:
                return ExtractedCore(
                    name=definition.name,
                    code=definition.code,
                    cardinality=definition.cardinality,
                    property_names=tuple(
                        item.name for item in definition.properties
                    ),
                    property_codes={
                        item.name: item.code for item in definition.properties
                    },
                    rounds=round_number,
                    elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                    tool_calls=tuple(audits),
                    objects=tuple(extracted_objects),
                    finish_reasoning=None,
                    **runtime,
                )
        if accepted_abandon is not None:
            return AbandonedCore(
                name=definition.name,
                code=definition.code,
                cardinality=definition.cardinality,
                property_names=tuple(
                    item.name for item in definition.properties
                ),
                property_codes={
                    item.name: item.code for item in definition.properties
                },
                rounds=round_number,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                tool_calls=tuple(audits),
                reasoning=accepted_abandon.reasoning,
                **runtime,
            )
        if accepted_finish is not None:
            return ExtractedCore(
                name=definition.name,
                code=definition.code,
                cardinality=definition.cardinality,
                property_names=tuple(
                    item.name for item in definition.properties
                ),
                property_codes={
                    item.name: item.code for item in definition.properties
                },
                rounds=round_number,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                tool_calls=tuple(audits),
                objects=tuple(extracted_objects),
                finish_reasoning=accepted_finish.reasoning,
                **runtime,
            )

    return _failed_field(
        definition,
        started_at=started_at,
        audits=audits,
        extracted_objects=extracted_objects,
        error=(
            f"达到最大轮次 {maximum_rounds}，仍未形成有效终止决定；"
            f"已保留 {len(extracted_objects)} 个部分对象。"
        ),
    )


async def extract_core(
    state: CoreSubgraphState,
) -> CoreSubgraphState:
    """并发提取全部 Core 字段，每个字段拥有独立短期记忆。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    core_context = state["core_context"]
    if prepared_pdf.document_id != core_context.document_id:
        raise ValueError("核心字段输入 PDF 与 Core 公共前缀的 document_id 不一致")

    definitions = state["core_definitions"]
    settings = get_settings().mllm
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    progress = ParallelProgressTracker(len(definitions.definitions))
    await progress.report_counted()
    async with MLLMClient(settings) as client:
        fields = tuple(
            await asyncio.gather(
                *(
                    progress.track(
                        _extract_one_core(
                            definition,
                            state=state,
                            client=client,
                            semaphore=semaphore,
                        )
                    )
                    for definition in definitions.definitions
                )
            )
        )

    failed_count = sum(field.status == "failed" for field in fields)
    if failed_count == 0:
        status = "completed"
    elif failed_count == len(fields):
        status = "failed"
    else:
        status = "partial"
    return {
        "core": CoreExtractionResult(
            status=status,
            document_id=prepared_pdf.document_id,
            model=settings.model,
            prompt_version=CORE_EXTRACTION_PROMPT_VERSION,
            catalog_sha256=definitions.content_sha256,
            fields=fields,
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        )
    }


__all__ = [
    "assemble_core_context",
    "extract_core",
    "select_core_definitions",
]
