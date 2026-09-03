"""条款候选发现与内容提取节点。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_extraction.context import context_sha256
from app.agent.contract_extraction.progress import ParallelProgressTracker
from app.agent.contract_extraction.subgraph.clause_extraction.prompt import (
    CLAUSE_CONTENT_COMMON_PROMPT_VERSION,
    CLAUSE_CONTENT_TARGET_PROMPT_VERSION,
    CLAUSE_CONTENT_TOOL_PLACEMENT,
    CLAUSE_DISCOVERY_PROMPT_VERSION,
    CLAUSE_DISCOVERY_TOOL_PLACEMENT,
    append_clause_content_target,
    build_clause_content_common_messages,
    build_clause_discovery_messages,
)
from app.agent.contract_extraction.subgraph.clause_extraction.state import (
    ClauseCandidateDiscoveryResult,
    ClauseContentGenerationProfile,
    ClauseContentOutcome,
    ClauseContentRequestAudit,
    ClauseContentToolCallAudit,
    ClauseDiscoveryToolCallAudit,
    ClauseExtractionContext,
    ClauseExtractionResult,
    ClauseExtractionSubgraphState,
    ExtractedClause,
    FailedClause,
)
from app.agent.contract_extraction.subgraph.clause_extraction.tool import (
    CANDIDATE_START_CLAUSE_DISCOVERY_TOOLS,
    CLAUSE_CONTENT_TOOL_CHOICE,
    CLAUSE_CONTENT_TOOL_VERSION,
    CLAUSE_CONTENT_TOOLS,
    CLAUSE_DISCOVERY_TOOL_CHOICE,
    CLAUSE_DISCOVERY_TOOL_VERSION,
    CLAUSE_DISCOVERY_TOOLS,
    INITIAL_CLAUSE_DISCOVERY_TOOLS,
    AnalyzeClauseHierarchyArguments,
    ClauseCandidateWorkspaceItem,
    ClauseContentToolFeedback,
    ClauseDiscoveryToolError,
    ClauseDiscoveryToolFeedback,
    FinishClauseDiscoveryArguments,
    RecordClauseCandidateArguments,
    ReviseLastClauseCandidateArguments,
    ThinkArguments,
    clause_content_validation_error_feedback,
    extract_clause_content,
    parse_clause_content_tool_arguments,
    parse_clause_discovery_tool_arguments,
    record_clause_candidate,
    revise_last_clause_candidate,
    successful_clause_content_feedback,
    successful_tool_feedback,
    validate_clause_hierarchy_analysis,
    validate_finish_clause_discovery,
    validation_error_feedback,
)
from app.agent.contract_extraction.tool_protocol import (
    ToolProtocolRecovery,
    audited_assistant_content,
    build_protocol_recovery_message,
)
from app.core.config import MLLMGenerationSettings, get_settings
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

_MAXIMUM_DISCOVERY_ROUNDS = 256
_MAXIMUM_COMPLETION_TOKENS = 4096
_MAXIMUM_CONTENT_ROUNDS = 6


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """仅在服务至少返回一次用量时汇总该指标。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _runtime_values(
    audits: list[ClauseDiscoveryToolCallAudit],
) -> dict[str, int | None]:
    """汇总条款候选发现会话的模型用量。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(audit.completion_tokens for audit in audits),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _tool_message(
    call: MLLMToolCall,
    feedback: ClauseDiscoveryToolFeedback,
) -> dict[str, str]:
    """把工具反馈追加到当前短期记忆。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _result(
    *,
    status: str,
    document_id: str,
    model: str,
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    completion: FinishClauseDiscoveryArguments | None,
    audits: list[ClauseDiscoveryToolCallAudit],
    started_at: float,
    error: str | None,
) -> ClauseCandidateDiscoveryResult:
    """统一构造成功或失败终态，并保留已校验的部分工作区。"""
    return ClauseCandidateDiscoveryResult.model_validate(
        {
            "status": status,
            "document_id": document_id,
            "model": model,
            "prompt_version": CLAUSE_DISCOVERY_PROMPT_VERSION,
            "tool_version": CLAUSE_DISCOVERY_TOOL_VERSION,
            "hierarchy_analysis": hierarchy_analysis,
            "candidates": workspace,
            "completion": completion,
            "rounds": len(audits),
            "elapsed_ms": round((perf_counter() - started_at) * 1000, 3),
            "tool_calls": tuple(audits),
            "error": error,
            **_runtime_values(audits),
        }
    )


async def discover_clause_candidates(
    state: ClauseExtractionSubgraphState,
) -> ClauseExtractionSubgraphState:
    """节点一：用短期记忆和可恢复工作区顺序发现全部条款候选。"""
    prepared_pdf = state["prepared_pdf"]
    prefill_context = state["prefill_context"]
    if prepared_pdf.document_id != prefill_context.document_id:
        raise ValueError("条款发现输入 PDF 与最终公共前缀的 document_id 不一致")

    settings = get_settings().mllm
    generation = settings.generation
    started_at = perf_counter()
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None = None
    workspace: tuple[ClauseCandidateWorkspaceItem, ...] = ()
    messages = build_clause_discovery_messages(
        prefill_context,
        workspace,
        hierarchy_analysis,
    )
    audits: list[ClauseDiscoveryToolCallAudit] = []
    protocol_recovery = ToolProtocolRecovery()

    async with MLLMClient(settings) as client:
        for round_number in range(1, _MAXIMUM_DISCOVERY_ROUNDS + 1):
            if hierarchy_analysis is None:
                tools = INITIAL_CLAUSE_DISCOVERY_TOOLS
            elif not workspace:
                tools = CANDIDATE_START_CLAUSE_DISCOVERY_TOOLS
            else:
                tools = CLAUSE_DISCOVERY_TOOLS
            available_tool_names = {tool["function"]["name"] for tool in tools}
            request_started_at = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(tools),
                    tool_choice=CLAUSE_DISCOVERY_TOOL_CHOICE,
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
                    tool_placement=CLAUSE_DISCOVERY_TOOL_PLACEMENT,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                return {
                    "clause_candidates": _result(
                        status="failed",
                        document_id=prefill_context.document_id,
                        model=settings.model,
                        hierarchy_analysis=hierarchy_analysis,
                        workspace=workspace,
                        completion=None,
                        audits=audits,
                        started_at=started_at,
                        error=str(exc),
                    )
                }

            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            if len(response.tool_calls) != 1:
                feedback = ClauseDiscoveryToolFeedback(
                    ok=False,
                    message=build_protocol_recovery_message(
                        tool_call_count=len(response.tool_calls),
                        result_label="条款候选发现结果",
                    )["content"],
                )
                completion = response.completion
                audits.append(
                    ClauseDiscoveryToolCallAudit(
                        round_number=round_number,
                        call_id=None,
                        name="protocol_recovery",
                        raw_arguments="",
                        assistant_content=audited_assistant_content(
                            response.assistant_message.get("content")
                        ),
                        feedback=feedback,
                        workspace_size=len(workspace),
                        short_term_reset=False,
                        elapsed_ms=elapsed_ms,
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
                    result_label="条款候选发现结果",
                )
                if exceeded:
                    return {
                        "clause_candidates": _result(
                            status="failed",
                            document_id=prefill_context.document_id,
                            model=completion.model or settings.model,
                            hierarchy_analysis=hierarchy_analysis,
                            workspace=workspace,
                            completion=None,
                            audits=audits,
                            started_at=started_at,
                            error="连续三轮未生成且仅生成一个合法工具调用。",
                        )
                    }
                continue

            call = response.tool_calls[0]
            protocol_recovery.accept_protocol()
            accepted_hierarchy_analysis: AnalyzeClauseHierarchyArguments | None = None
            accepted_item: ClauseCandidateWorkspaceItem | None = None
            accepted_completion: FinishClauseDiscoveryArguments | None = None
            short_term_reset = False
            try:
                if call.name not in available_tool_names:
                    required_direction = (
                        "首轮调用 analyze_clause_hierarchy 完成层级分析"
                        if hierarchy_analysis is None
                        else "只调用当前可用的候选发现工具"
                    )
                    raise ClauseDiscoveryToolError(
                        "tool",
                        f"当前可用工具中没有 {call.name}",
                        required_direction,
                    )
                arguments = parse_clause_discovery_tool_arguments(
                    call.name,
                    call.arguments,
                )
            except (ValueError, ValidationError) as exc:
                feedback = validation_error_feedback(exc)
            else:
                try:
                    if isinstance(arguments, AnalyzeClauseHierarchyArguments):
                        validate_clause_hierarchy_analysis(
                            arguments,
                            page_count=prepared_pdf.page_count,
                        )
                        accepted_hierarchy_analysis = arguments
                        feedback = successful_tool_feedback("analyze_clause_hierarchy")
                    elif isinstance(arguments, ThinkArguments):
                        feedback = successful_tool_feedback("think")
                    elif isinstance(arguments, RecordClauseCandidateArguments):
                        accepted_item = record_clause_candidate(
                            arguments,
                            workspace=workspace,
                            page_count=prepared_pdf.page_count,
                        )
                        feedback = successful_tool_feedback(
                            "record_clause_candidate",
                            workspace_item=accepted_item,
                        )
                    elif isinstance(arguments, ReviseLastClauseCandidateArguments):
                        accepted_item = revise_last_clause_candidate(
                            arguments,
                            workspace=workspace,
                            page_count=prepared_pdf.page_count,
                        )
                        feedback = successful_tool_feedback(
                            "revise_last_clause_candidate",
                            workspace_item=accepted_item,
                        )
                    elif isinstance(arguments, FinishClauseDiscoveryArguments):
                        validate_finish_clause_discovery(
                            arguments,
                            workspace=workspace,
                            page_count=prepared_pdf.page_count,
                        )
                        feedback = successful_tool_feedback("finish_clause_discovery")
                        accepted_completion = arguments
                    else:  # pragma: no cover - 联合类型已覆盖全部工具参数
                        raise TypeError(f"当前不能调用工具 {call.name}")
                except (TypeError, ValueError, ValidationError) as exc:
                    feedback = validation_error_feedback(exc)

            if accepted_hierarchy_analysis is not None:
                protocol_recovery.accept_correction(messages)
                hierarchy_analysis = accepted_hierarchy_analysis
                # 首轮分析成为工作区长记忆后，清空工具调用短期历史并永久移除首轮工具。
                messages = build_clause_discovery_messages(
                    prefill_context,
                    workspace,
                    hierarchy_analysis,
                )
                short_term_reset = True
            elif accepted_item is not None:
                protocol_recovery.accept_correction(messages)
                if isinstance(arguments, RecordClauseCandidateArguments):
                    workspace = (*workspace, accepted_item)
                else:
                    workspace = (*workspace[:-1], accepted_item)
                # 成功记录或修正后丢弃全部助手/工具短期历史。新的动态消息
                # 尾部会点名最后候选，并指引模型从该条款之后继续。
                messages = build_clause_discovery_messages(
                    prefill_context,
                    workspace,
                    hierarchy_analysis,
                )
                short_term_reset = True
            else:
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
                ClauseDiscoveryToolCallAudit(
                    round_number=round_number,
                    call_id=call.call_id,
                    name=call.name,
                    raw_arguments=call.arguments,
                    assistant_content=audited_assistant_content(
                        response.assistant_message.get("content")
                    ),
                    feedback=feedback,
                    workspace_size=len(workspace),
                    short_term_reset=short_term_reset,
                    elapsed_ms=elapsed_ms,
                    response_id=completion.response_id,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cached_tokens=completion.cached_tokens,
                )
            )
            if accepted_completion is not None:
                return {
                    "clause_candidates": _result(
                        status="completed",
                        document_id=prefill_context.document_id,
                        model=completion.model or settings.model,
                        hierarchy_analysis=hierarchy_analysis,
                        workspace=workspace,
                        completion=accepted_completion,
                        audits=audits,
                        started_at=started_at,
                        error=None,
                    )
                }

    return {
        "clause_candidates": _result(
            status="failed",
            document_id=prefill_context.document_id,
            model=settings.model,
            hierarchy_analysis=hierarchy_analysis,
            workspace=workspace,
            completion=None,
            audits=audits,
            started_at=started_at,
            error=(f"达到最大轮次 {_MAXIMUM_DISCOVERY_ROUNDS}，仍未完成条款候选发现。"),
        )
    }


def assemble_clause_extraction_context(
    state: ClauseExtractionSubgraphState,
) -> ClauseExtractionSubgraphState:
    """节点二：确定性组装条款详情公共上下文。"""
    prepared_pdf = state["prepared_pdf"]
    prefill_context = state["prefill_context"]
    discovery = state["clause_candidates"]
    document_ids = {
        prepared_pdf.document_id,
        prefill_context.document_id,
        discovery.document_id,
    }
    if len(document_ids) != 1:
        raise ValueError("条款详情上下文输入的 document_id 不一致")
    if discovery.status != "completed":
        raise ValueError("条款候选发现未成功，不能组装条款详情公共上下文")
    if not discovery.candidates:
        raise ValueError("条款候选目录为空，不能组装条款详情公共上下文")

    # 当前候选不进入该上下文；节点三只在此稳定边界之后追加单候选目标。
    messages = build_clause_content_common_messages(
        prefill_context,
        discovery.candidates,
    )
    return {
        "clause_extraction_context": ClauseExtractionContext(
            document_id=prefill_context.document_id,
            prompt_version=CLAUSE_CONTENT_COMMON_PROMPT_VERSION,
            tool_version=CLAUSE_CONTENT_TOOL_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


def _content_runtime_values(
    requests: list[ClauseContentRequestAudit],
) -> dict[str, int | None]:
    """汇总一个候选详情会话的模型用量。"""
    return {
        "prompt_tokens": _sum_optional(request.prompt_tokens for request in requests),
        "completion_tokens": _sum_optional(
            request.completion_tokens for request in requests
        ),
        "cached_tokens": _sum_optional(request.cached_tokens for request in requests),
    }


def build_clause_content_generation_profile(
    generation: MLLMGenerationSettings,
) -> ClauseContentGenerationProfile:
    """复用正式模型采样参数与统一生成上限。"""
    if generation.max_completion_tokens < 1:
        raise ValueError("条款正文生成配置上限必须大于等于 1")
    return ClauseContentGenerationProfile(
        max_completion_tokens=generation.max_completion_tokens,
        temperature=generation.temperature,
        top_p=generation.top_p,
        top_k=generation.top_k,
        presence_penalty=generation.presence_penalty,
        repetition_penalty=generation.repetition_penalty,
    )


def _is_length_finish_reason(finish_reason: str | None) -> bool:
    """兼容 SDK 返回普通字符串或带枚举前缀的长度终止原因。"""
    return bool(
        finish_reason
        and finish_reason.casefold().rsplit(".", maxsplit=1)[-1] == "length"
    )


def _content_tool_message(
    call: MLLMToolCall,
    feedback: ClauseContentToolFeedback,
) -> dict[str, str]:
    """把最小内容校验反馈写入当前候选的短期记忆。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.message,
    }


def _failed_clause(
    candidate: ClauseCandidateWorkspaceItem,
    *,
    started_at: float,
    requests: list[ClauseContentRequestAudit],
    audits: list[ClauseContentToolCallAudit],
    error: str,
) -> FailedClause:
    """构造保留完整工具审计的单候选失败结果。"""
    return FailedClause(
        candidate=candidate,
        rounds=len(audits),
        request_attempts=len(requests),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        requests=tuple(requests),
        tool_calls=tuple(audits),
        error=error,
        **_content_runtime_values(requests),
    )


async def _extract_one_clause(
    candidate: ClauseCandidateWorkspaceItem,
    *,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    context: ClauseExtractionContext,
    client: MLLMClient,
    semaphore: asyncio.Semaphore,
    generation_profile: ClauseContentGenerationProfile,
    seed: int,
) -> ClauseContentOutcome:
    """为一个条款候选维护隔离短期记忆，直到成功或明确失败。"""
    started_at = perf_counter()
    messages = append_clause_content_target(
        context.messages,
        candidate,
        workspace,
    )
    requests: list[ClauseContentRequestAudit] = []
    audits: list[ClauseContentToolCallAudit] = []
    protocol_recovery = ToolProtocolRecovery()

    for round_number in range(1, _MAXIMUM_CONTENT_ROUNDS + 1):
        request_number = len(requests) + 1
        maximum_tokens = generation_profile.max_completion_tokens
        queued_at = perf_counter()
        try:
            async with semaphore:
                queue_elapsed_ms = round((perf_counter() - queued_at) * 1000, 3)
                request_started_at = perf_counter()
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(CLAUSE_CONTENT_TOOLS),
                    tool_choice=CLAUSE_CONTENT_TOOL_CHOICE,
                    max_completion_tokens=maximum_tokens,
                    temperature=generation_profile.temperature,
                    top_p=generation_profile.top_p,
                    top_k=generation_profile.top_k,
                    presence_penalty=generation_profile.presence_penalty,
                    repetition_penalty=generation_profile.repetition_penalty,
                    seed=seed,
                    enable_thinking=False,
                    tool_placement=CLAUSE_CONTENT_TOOL_PLACEMENT,
                )
        except (MLLMRequestError, MLLMUnavailableError) as exc:
            requests.append(
                ClauseContentRequestAudit(
                    request_number=request_number,
                    round_number=round_number,
                    max_completion_tokens=maximum_tokens,
                    finish_reason=None,
                    tool_call_count=0,
                    queue_elapsed_ms=queue_elapsed_ms,
                    elapsed_ms=round((perf_counter() - request_started_at) * 1000, 3),
                    response_id=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    cached_tokens=None,
                )
            )
            return _failed_clause(
                candidate,
                started_at=started_at,
                requests=requests,
                audits=audits,
                error=str(exc),
            )

        elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
        completion = response.completion
        requests.append(
            ClauseContentRequestAudit(
                request_number=request_number,
                round_number=round_number,
                max_completion_tokens=maximum_tokens,
                finish_reason=completion.finish_reason,
                tool_call_count=len(response.tool_calls),
                queue_elapsed_ms=queue_elapsed_ms,
                elapsed_ms=elapsed_ms,
                response_id=completion.response_id,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                cached_tokens=completion.cached_tokens,
            )
        )
        if _is_length_finish_reason(completion.finish_reason) and response.tool_calls:
            return _failed_clause(
                candidate,
                started_at=started_at,
                requests=requests,
                audits=audits,
                error=(
                    "条款内容工具调用在统一生成上限 "
                    f"{maximum_tokens} token 下因 length 截断。"
                ),
            )

        if len(response.tool_calls) != 1:
            feedback = ClauseContentToolFeedback(
                ok=False,
                message=build_protocol_recovery_message(
                    tool_call_count=len(response.tool_calls),
                    result_label="条款正文提取结果",
                )["content"],
            )
            audits.append(
                ClauseContentToolCallAudit(
                    round_number=round_number,
                    request_number=request_number,
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
            exceeded = protocol_recovery.record_protocol_failure(
                messages,
                assistant_message=response.assistant_message,
                tool_call_count=len(response.tool_calls),
                result_label="条款正文提取结果",
            )
            if exceeded:
                return _failed_clause(
                    candidate,
                    started_at=started_at,
                    requests=requests,
                    audits=audits,
                    error="连续三轮未生成且仅生成一个合法工具调用。",
                )
            continue

        call = response.tool_calls[0]
        protocol_recovery.accept_protocol()
        accepted_content = None
        try:
            arguments = parse_clause_content_tool_arguments(
                call.name,
                call.arguments,
            )
            accepted_content = extract_clause_content(
                arguments,
                candidate=candidate,
                workspace=workspace,
            )
        except (ValueError, ValidationError) as exc:
            feedback = clause_content_validation_error_feedback(exc)
        else:
            feedback = successful_clause_content_feedback(accepted_content)
        tool_message = _content_tool_message(call, feedback)
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

        audits.append(
            ClauseContentToolCallAudit(
                round_number=round_number,
                request_number=request_number,
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
        if accepted_content is not None:
            return ExtractedClause(
                candidate=candidate,
                rounds=round_number,
                request_attempts=len(requests),
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                requests=tuple(requests),
                tool_calls=tuple(audits),
                reasoning_summary=accepted_content.reasoning_summary,
                content=accepted_content.content,
                **_content_runtime_values(requests),
            )

    return _failed_clause(
        candidate,
        started_at=started_at,
        requests=requests,
        audits=audits,
        error=(
            f"达到最大轮次 {_MAXIMUM_CONTENT_ROUNDS}，"
            "仍未提交通过边界校验的条款完整直接原文。"
        ),
    )


async def extract_clause_contents(
    state: ClauseExtractionSubgraphState,
) -> ClauseExtractionSubgraphState:
    """节点三：隔离短期记忆并发提取全部候选的完整直接内容。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    discovery = state["clause_candidates"]
    context = state["clause_extraction_context"]
    document_ids = {
        prepared_pdf.document_id,
        discovery.document_id,
        context.document_id,
    }
    if len(document_ids) != 1:
        raise ValueError("条款详情提取输入的 document_id 不一致")
    if discovery.status != "completed" or not discovery.candidates:
        raise ValueError("条款候选发现未成功生成非空目录，不能提取条款详情")
    if context.prompt_version != CLAUSE_CONTENT_COMMON_PROMPT_VERSION:
        raise ValueError("条款详情公共提示词版本与当前实现不一致")
    if context.tool_version != CLAUSE_CONTENT_TOOL_VERSION:
        raise ValueError("条款详情公共工具版本与当前实现不一致")
    settings = get_settings().mllm
    generation_profile = build_clause_content_generation_profile(settings.generation)
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    progress = ParallelProgressTracker(len(discovery.candidates))
    await progress.report_counted()
    async with MLLMClient(settings) as client:
        clauses = tuple(
            await asyncio.gather(
                *(
                    progress.track(
                        _extract_one_clause(
                            candidate,
                            workspace=discovery.candidates,
                            context=context,
                            client=client,
                            semaphore=semaphore,
                            generation_profile=generation_profile,
                            seed=settings.generation.seed,
                        )
                    )
                    for candidate in discovery.candidates
                )
            )
        )

    failed_count = sum(clause.status == "failed" for clause in clauses)
    if failed_count == 0:
        status = "completed"
    elif failed_count == len(clauses):
        status = "failed"
    else:
        status = "partial"
    return {
        "clause_extraction": ClauseExtractionResult(
            status=status,
            document_id=context.document_id,
            model=settings.model,
            common_prompt_version=context.prompt_version,
            target_prompt_version=CLAUSE_CONTENT_TARGET_PROMPT_VERSION,
            tool_version=context.tool_version,
            prefix_sha256=context.prefix_sha256,
            generation_profile=generation_profile,
            clauses=clauses,
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        )
    }


__all__ = [
    "assemble_clause_extraction_context",
    "build_clause_content_generation_profile",
    "discover_clause_candidates",
    "extract_clause_contents",
]
