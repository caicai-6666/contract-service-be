"""核心字段目录加载与并行单字段提取节点。"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from pydantic import ValidationError
import yaml

from app.agent.contract_extraction.subgraph.field_extraction.core_field.prompt import (
    CORE_FIELD_EXTRACTION_PROMPT_VERSION,
    build_core_field_messages,
)
from app.agent.contract_extraction.subgraph.field_extraction.core_field.state import (
    AbandonedCoreField,
    CoreFieldCatalog,
    CoreFieldExtractionResult,
    CoreFieldOutcome,
    CoreFieldSubgraphState,
    ExtractedCoreField,
    ExtractedFieldObject,
    FailedCoreField,
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
from app.core.config import get_settings
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[6]
_CORE_FIELD_DIRECTORY = _PROJECT_ROOT / "data/definition/field/core"
_MAXIMUM_SINGLE_ROUNDS = 8
_MAXIMUM_MULTIPLE_ROUNDS = 32
_MAXIMUM_CONSECUTIVE_THINKS = 2
_MAXIMUM_COMPLETION_TOKENS = 2048


class CoreFieldDefinitionError(RuntimeError):
    """核心字段目录缺失、重复或不符合机器契约。"""


def _sum_optional(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def load_core_field_definitions(
    state: CoreFieldSubgraphState,
) -> CoreFieldSubgraphState:
    """节点一：按文件名稳定加载并校验一个文件一个字段的 Core 目录。"""
    del state
    paths = sorted(_CORE_FIELD_DIRECTORY.glob("*.yaml"))
    if not paths:
        raise CoreFieldDefinitionError(
            f"核心字段目录没有 YAML 定义：{_CORE_FIELD_DIRECTORY}"
        )

    definitions: list[FieldDefinition] = []
    seen_names: dict[str, Path] = {}
    digest = sha256()
    for path in paths:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
        try:
            payload = yaml.safe_load(raw)
            definition = FieldDefinition.model_validate(payload)
        except (yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
            raise CoreFieldDefinitionError(
                f"核心字段定义无效：{path.name}：{exc}"
            ) from exc
        previous = seen_names.get(definition.name)
        if previous is not None:
            raise CoreFieldDefinitionError(
                f"核心字段名称重复：{definition.name} 同时出现在 "
                f"{previous.name} 与 {path.name}"
            )
        seen_names[definition.name] = path
        definitions.append(definition)

    return {
        "core_field_catalog": CoreFieldCatalog(
            directory="data/definition/field/core",
            sha256=digest.hexdigest(),
            definitions=tuple(definitions),
        )
    }


def _validation_feedback(
    error: Exception,
    *,
    definition: FieldDefinition,
    raw_arguments: str,
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
        if "reasoning" in path or "理由" in problem:
            suggestion = "按照错误给出的固定结尾改写 reasoning"
        elif "page_number" in path:
            suggestion = "使用当前合同范围内的物理页码"
        elif "bbox" in path:
            suggestion = "使用 0～1000 的有效矩形坐标，无法定位时传 null"
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
) -> FailedCoreField:
    return FailedCoreField(
        name=definition.name,
        cardinality=definition.cardinality,
        property_names=tuple(item.name for item in definition.properties),
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        tool_calls=tuple(audits),
        partial_objects=tuple(extracted_objects),
        error=error,
        **_runtime_values(audits),
    )


async def _extract_one_core_field(
    definition: FieldDefinition,
    *,
    state: CoreFieldSubgraphState,
    client: MLLMClient,
    semaphore: asyncio.Semaphore,
) -> CoreFieldOutcome:
    """为一个定义维护隔离短期记忆，并按 cardinality 收集对象。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    messages = build_core_field_messages(state["prefill_context"], definition)
    settings = get_settings().mllm
    generation = settings.generation
    audits: list[FieldToolCallAudit] = []
    extracted_objects: list[ExtractedFieldObject] = []
    extracted_fingerprints: set[str] = set()
    consecutive_thinks = 0
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
            return _failed_field(
                definition,
                started_at=started_at,
                audits=audits,
                extracted_objects=extracted_objects,
                error=(
                    "模型每轮必须返回且只能返回一个函数工具调用；"
                    f"第 {round_number} 轮实际返回 {len(response.tool_calls)} 个。"
                ),
            )

        call = response.tool_calls[0]
        messages.append(response.assistant_message)
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
                definition=definition,
                raw_arguments=call.arguments,
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

        messages.append(_tool_message(call, feedback))
        completion = response.completion
        audits.append(
            FieldToolCallAudit(
                round_number=round_number,
                call_id=call.call_id,
                name=call.name,
                raw_arguments=call.arguments,
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
                return ExtractedCoreField(
                    name=definition.name,
                    cardinality=definition.cardinality,
                    property_names=tuple(
                        item.name for item in definition.properties
                    ),
                    rounds=round_number,
                    elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                    tool_calls=tuple(audits),
                    objects=tuple(extracted_objects),
                    finish_reasoning=None,
                    **runtime,
                )
        if accepted_abandon is not None:
            return AbandonedCoreField(
                name=definition.name,
                cardinality=definition.cardinality,
                property_names=tuple(item.name for item in definition.properties),
                rounds=round_number,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                tool_calls=tuple(audits),
                reasoning=accepted_abandon.reasoning,
                **runtime,
            )
        if accepted_finish is not None:
            return ExtractedCoreField(
                name=definition.name,
                cardinality=definition.cardinality,
                property_names=tuple(item.name for item in definition.properties),
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


async def extract_core_fields(
    state: CoreFieldSubgraphState,
) -> CoreFieldSubgraphState:
    """节点二：并发提取全部 Core 字段，每个字段拥有独立短期记忆。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    prefill_context = state["prefill_context"]
    if prepared_pdf.document_id != prefill_context.document_id:
        raise ValueError("核心字段输入 PDF 与公共前缀的 document_id 不一致")

    catalog = state["core_field_catalog"]
    settings = get_settings().mllm
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    async with MLLMClient(settings) as client:
        fields = tuple(
            await asyncio.gather(
                *(
                    _extract_one_core_field(
                        definition,
                        state=state,
                        client=client,
                        semaphore=semaphore,
                    )
                    for definition in catalog.definitions
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
        "core_field": CoreFieldExtractionResult(
            status=status,
            document_id=prepared_pdf.document_id,
            model=settings.model,
            prompt_version=CORE_FIELD_EXTRACTION_PROMPT_VERSION,
            catalog_sha256=catalog.sha256,
            fields=fields,
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        )
    }


__all__ = [
    "CoreFieldDefinitionError",
    "extract_core_fields",
    "load_core_field_definitions",
]
