"""合同分类子图节点。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_extraction.context import context_sha256
from app.agent.contract_extraction.subgraph.classification.definition import (
    ContractCategory,
)
from app.agent.contract_extraction.subgraph.classification.prompt import (
    CLASSIFICATION_CATEGORY_PROMPT_VERSION,
    CLASSIFICATION_COMMON_PROMPT_VERSION,
    append_classification_prefill_task,
    append_unmapped_type_description_task,
    build_category_judgment_messages,
    build_classification_common_messages,
)
from app.agent.contract_extraction.subgraph.classification.state import (
    CategoryJudgmentOutcome,
    ClassificationContext,
    ClassificationPreheatResult,
    ClassificationSubgraphState,
    ClassificationToolCallAudit,
    ContractClassificationResult,
    ContractClassificationRun,
    FailedCategory,
    MatchedCategory,
    NotMatchedCategory,
    UnmappedTypeDescription,
)
from app.agent.contract_extraction.subgraph.classification.tool import (
    CLASSIFICATION_TOOL_CHOICE,
    CLASSIFICATION_TOOLS,
    DESCRIBE_UNMAPPED_TYPE_TOOL,
    BelongToCategoryArguments,
    ClassificationToolFeedback,
    NotBelongToCategoryArguments,
    ThinkArguments,
    build_category_match_card,
    parse_classification_tool_arguments,
    parse_unmapped_type_description_arguments,
    successful_tool_feedback,
    validation_error_feedback,
)
from app.core.config import get_settings
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

_MAXIMUM_CATEGORY_ROUNDS = 8
_MAXIMUM_CONSECUTIVE_THINKS = 2
_MAXIMUM_COMPLETION_TOKENS = 4096
_UNMAPPED_DESCRIPTION_MAXIMUM_TOKENS = 1024


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """只在至少一次响应提供用量时返回总和。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _runtime_values(
    audits: list[ClassificationToolCallAudit],
) -> dict[str, int | None]:
    """汇总一个单类别会话的模型用量。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(
            audit.completion_tokens for audit in audits
        ),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _tool_message(
    call: MLLMToolCall,
    feedback: ClassificationToolFeedback,
) -> dict[str, str]:
    """把最小工具反馈写入当前类别的短期记忆。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _validate_evidence_pages(
    page_numbers: Iterable[int],
    *,
    page_count: int,
) -> ClassificationToolFeedback | None:
    """拒绝超出当前 PDF 物理页范围的分类证据。"""
    invalid = sorted(
        {page_number for page_number in page_numbers if not 1 <= page_number <= page_count}
    )
    if not invalid:
        return None
    return ClassificationToolFeedback(
        ok=False,
        message=(
            f"evidence.page_number：引用了合同范围外的页码 {invalid}，"
            f"本合同只有 1-{page_count} 页；请核对页面标签后重新提交。"
        ),
    )


def _failed_category(
    category: ContractCategory,
    *,
    started_at: float,
    audits: list[ClassificationToolCallAudit],
    error: str,
) -> FailedCategory:
    """构造不泄露未完成决定的单类别失败结果。"""
    return FailedCategory(
        category_code=category.definition.code,
        category_name=category.definition.name,
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        tool_calls=tuple(audits),
        error=error,
        **_runtime_values(audits),
    )


async def _judge_one_category(
    category: ContractCategory,
    *,
    context: ClassificationContext,
    page_count: int,
    client: MLLMClient,
    semaphore: asyncio.Semaphore,
) -> CategoryJudgmentOutcome:
    """维护一个类别独占的短期记忆，直到形成互斥终止决定。"""
    started_at = perf_counter()
    messages = build_category_judgment_messages(context.messages, category)
    settings = get_settings().mllm
    generation = settings.generation
    audits: list[ClassificationToolCallAudit] = []
    consecutive_thinks = 0

    for round_number in range(1, _MAXIMUM_CATEGORY_ROUNDS + 1):
        request_started_at = perf_counter()
        try:
            async with semaphore:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(CLASSIFICATION_TOOLS),
                    tool_choice=CLASSIFICATION_TOOL_CHOICE,
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
                    tool_placement="before_task",
                )
        except (MLLMRequestError, MLLMUnavailableError) as exc:
            return _failed_category(
                category,
                started_at=started_at,
                audits=audits,
                error=str(exc),
            )

        elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
        if len(response.tool_calls) != 1:
            return _failed_category(
                category,
                started_at=started_at,
                audits=audits,
                error=(
                    "模型每轮必须返回且只能返回一个函数工具调用；"
                    f"第 {round_number} 轮实际返回 {len(response.tool_calls)} 个。"
                ),
            )

        call = response.tool_calls[0]
        messages.append(response.assistant_message)
        accepted_match = None
        accepted_not_match: NotBelongToCategoryArguments | None = None
        try:
            arguments = parse_classification_tool_arguments(
                call.name,
                call.arguments,
            )
        except (ValueError, ValidationError) as exc:
            feedback = validation_error_feedback(exc)
        else:
            if isinstance(arguments, ThinkArguments):
                consecutive_thinks += 1
                if consecutive_thinks > _MAXIMUM_CONSECUTIVE_THINKS:
                    feedback = ClassificationToolFeedback(
                        ok=False,
                        message=(
                            "reasoning：已经连续调用 think 两次但没有形成决定；"
                            "请根据当前证据调用 belong_to_category 或 "
                            "not_belong_to_category。"
                        ),
                    )
                else:
                    feedback = successful_tool_feedback("think")
            elif isinstance(arguments, NotBelongToCategoryArguments):
                consecutive_thinks = 0
                page_error = _validate_evidence_pages(
                    (evidence.page_number for evidence in arguments.evidence),
                    page_count=page_count,
                )
                if page_error is not None:
                    feedback = page_error
                else:
                    feedback = successful_tool_feedback(
                        "not_belong_to_category"
                    )
                    accepted_not_match = arguments
            elif isinstance(arguments, BelongToCategoryArguments):
                consecutive_thinks = 0
                page_error = _validate_evidence_pages(
                    (evidence.page_number for evidence in arguments.evidence),
                    page_count=page_count,
                )
                if page_error is not None:
                    feedback = page_error
                else:
                    feedback = successful_tool_feedback("belong_to_category")
                    accepted_match = build_category_match_card(
                        arguments,
                        category_code=category.definition.code,
                        category_name=category.definition.name,
                    )
            else:  # pragma: no cover - 联合类型已覆盖全部工具参数
                feedback = ClassificationToolFeedback(
                    ok=False,
                    message=(
                        f"tool：当前不能调用 {call.name}；"
                        "请使用本轮提供的分类工具。"
                    ),
                )

        messages.append(_tool_message(call, feedback))
        completion = response.completion
        audits.append(
            ClassificationToolCallAudit(
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
        runtime = _runtime_values(audits)
        if accepted_match is not None:
            return MatchedCategory(
                category_code=category.definition.code,
                category_name=category.definition.name,
                rounds=round_number,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                tool_calls=tuple(audits),
                match=accepted_match,
                **runtime,
            )
        if accepted_not_match is not None:
            return NotMatchedCategory(
                category_code=category.definition.code,
                category_name=category.definition.name,
                rounds=round_number,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                tool_calls=tuple(audits),
                evidence=tuple(accepted_not_match.evidence),
                reasoning_summary=accepted_not_match.reasoning_summary,
                **runtime,
            )

    return _failed_category(
        category,
        started_at=started_at,
        audits=audits,
        error=(
            f"达到最大轮次 {_MAXIMUM_CATEGORY_ROUNDS}，"
            "仍未形成有效的属于或不属于决定。"
        ),
    )


async def _describe_unmapped_type(
    *,
    context: ClassificationContext,
    page_count: int,
    client: MLLMClient,
) -> tuple[UnmappedTypeDescription | None, str | None]:
    """在全部正式类别均未命中时额外生成一次简短类型描述。"""
    settings = get_settings().mllm
    generation = settings.generation
    try:
        response = await client.create_tool_chat_completion(
            messages=append_unmapped_type_description_task(context.messages),
            tools=[DESCRIBE_UNMAPPED_TYPE_TOOL],
            tool_choice=CLASSIFICATION_TOOL_CHOICE,
            max_completion_tokens=min(
                generation.max_completion_tokens,
                _UNMAPPED_DESCRIPTION_MAXIMUM_TOKENS,
            ),
            temperature=generation.temperature,
            top_p=generation.top_p,
            top_k=generation.top_k,
            presence_penalty=generation.presence_penalty,
            repetition_penalty=generation.repetition_penalty,
            seed=generation.seed,
            enable_thinking=False,
            tool_placement="before_task",
        )
    except (MLLMRequestError, MLLMUnavailableError) as exc:
        return None, str(exc)

    if len(response.tool_calls) != 1:
        return None, (
            "未映射类型描述必须返回且只能返回一个 describe_unmapped_type "
            f"调用；实际返回 {len(response.tool_calls)} 个。"
        )
    call = response.tool_calls[0]
    if call.name != "describe_unmapped_type":
        return None, f"未映射类型描述调用了不允许的工具：{call.name}"
    try:
        arguments = parse_unmapped_type_description_arguments(call.arguments)
    except (ValueError, ValidationError) as exc:
        return None, validation_error_feedback(exc).message
    page_error = _validate_evidence_pages(
        (evidence.page_number for evidence in arguments.evidence),
        page_count=page_count,
    )
    if page_error is not None:
        return None, page_error.message
    return (
        UnmappedTypeDescription(
            evidence=tuple(arguments.evidence),
            reasoning_summary=arguments.reasoning_summary,
            description=arguments.description,
        ),
        None,
    )


def assemble_classification_context(
    state: ClassificationSubgraphState,
) -> ClassificationSubgraphState:
    """在合同基础前缀尾部追加所有单类别判别共享的稳定规则。"""
    base_context = state["base_context"]
    messages = build_classification_common_messages(base_context.messages)
    return {
        "classification_context": ClassificationContext(
            document_id=base_context.document_id,
            prompt_version=CLASSIFICATION_COMMON_PROMPT_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


async def prefill_classification_context(
    state: ClassificationSubgraphState,
) -> ClassificationSubgraphState:
    """携带分类工具向本地 vLLM 发送单 token 公共前缀预热请求。"""
    context = state["classification_context"]
    settings = get_settings().mllm
    started_at = perf_counter()

    async with MLLMClient(settings) as client:
        try:
            response = await client.create_tool_chat_completion(
                messages=append_classification_prefill_task(context.messages),
                tools=list(CLASSIFICATION_TOOLS),
                tool_choice=CLASSIFICATION_TOOL_CHOICE,
                max_completion_tokens=1,
                temperature=0,
                top_p=settings.generation.top_p,
                top_k=settings.generation.top_k,
                presence_penalty=settings.generation.presence_penalty,
                repetition_penalty=settings.generation.repetition_penalty,
                seed=settings.generation.seed,
                enable_thinking=False,
                tool_placement="before_task",
            )
        except MLLMUnavailableError as exc:
            return {
                "classification_preheat": ClassificationPreheatResult(
                    status="degraded",
                    document_id=context.document_id,
                    prompt_version=context.prompt_version,
                    model=settings.model,
                    completed_at=datetime.now(UTC),
                    prefix_sha256=context.prefix_sha256,
                    elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                    error=str(exc),
                )
            }

    completion = response.completion
    return {
        "classification_preheat": ClassificationPreheatResult(
            status="warmed",
            document_id=context.document_id,
            prompt_version=context.prompt_version,
            model=completion.model or settings.model,
            completed_at=datetime.now(UTC),
            prefix_sha256=context.prefix_sha256,
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cached_tokens=completion.cached_tokens,
        )
    }


async def classify_contract(
    state: ClassificationSubgraphState,
) -> ClassificationSubgraphState:
    """并发判定全部类别，隔离审计历史并输出紧凑命中结果。"""
    started_at = perf_counter()
    context = state["classification_context"]
    catalog = state["category_catalog"]
    page_count = state["page_count"]
    if page_count < 1:
        raise ValueError("合同分类 page_count 必须大于 0")
    if not catalog.categories:
        raise ValueError("合同分类目录不能为空")

    settings = get_settings().mllm
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    unmapped_description: UnmappedTypeDescription | None = None
    unmapped_description_error: str | None = None
    async with MLLMClient(settings) as client:
        outcomes = tuple(
            await asyncio.gather(
                *(
                    _judge_one_category(
                        category,
                        context=context,
                        page_count=page_count,
                        client=client,
                        semaphore=semaphore,
                    )
                    for category in catalog.categories
                )
            )
        )
        has_failed = any(isinstance(outcome, FailedCategory) for outcome in outcomes)
        has_matched = any(isinstance(outcome, MatchedCategory) for outcome in outcomes)
        if not has_failed and not has_matched:
            unmapped_description, unmapped_description_error = (
                await _describe_unmapped_type(
                    context=context,
                    page_count=page_count,
                    client=client,
                )
            )

    failed = tuple(
        outcome for outcome in outcomes if isinstance(outcome, FailedCategory)
    )
    matched = tuple(
        outcome for outcome in outcomes if isinstance(outcome, MatchedCategory)
    )
    if not failed:
        run_status = "completed"
    elif len(failed) == len(outcomes):
        run_status = "failed"
    else:
        run_status = "partial"

    if len(failed) == len(outcomes):
        result_status = "failed"
    elif failed:
        result_status = "partial"
    elif matched:
        result_status = "classified"
    else:
        result_status = "unmapped"

    elapsed_ms = round((perf_counter() - started_at) * 1000, 3)
    run = ContractClassificationRun(
        status=run_status,
        document_id=context.document_id,
        model=settings.model,
        common_prompt_version=CLASSIFICATION_COMMON_PROMPT_VERSION,
        category_prompt_version=CLASSIFICATION_CATEGORY_PROMPT_VERSION,
        catalog_sha256=catalog.content_sha256,
        categories=outcomes,
        unmapped_type_description=unmapped_description,
        unmapped_type_description_error=unmapped_description_error,
        elapsed_ms=elapsed_ms,
    )
    return {
        "classification_run": run,
        "classification": ContractClassificationResult(
            status=result_status,
            document_id=context.document_id,
            model=settings.model,
            common_prompt_version=CLASSIFICATION_COMMON_PROMPT_VERSION,
            category_prompt_version=CLASSIFICATION_CATEGORY_PROMPT_VERSION,
            catalog_sha256=catalog.content_sha256,
            matches=tuple(outcome.match for outcome in matched),
            failed_category_codes=tuple(
                outcome.category_code for outcome in failed
            ),
            unmapped_type_description=(
                unmapped_description.description
                if unmapped_description is not None
                else None
            ),
        ),
    }
