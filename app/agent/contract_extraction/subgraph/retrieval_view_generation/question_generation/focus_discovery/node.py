"""合同检索问题关注点的异步受控发现节点。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.prompt import (
    QUESTION_FOCUS_DISCOVERY_PROMPT_VERSION,
    QUESTION_FOCUS_DISCOVERY_TOOL_PLACEMENT,
    build_question_focus_discovery_messages,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.state import (
    QuestionFocusDiscoveryResult,
    QuestionFocusToolCallAudit,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.tool import (
    QUESTION_FOCUS_DISCOVERY_TOOL_CHOICE,
    QUESTION_FOCUS_DISCOVERY_TOOL_VERSION,
    QUESTION_FOCUS_DISCOVERY_TOOLS,
    THINK_QUESTION_FOCUS_TOOL,
    FinishQuestionFocusDiscoveryArguments,
    GeneratedQuestionFocus,
    GenerateQuestionFocusArguments,
    QuestionFocusToolFeedback,
    ThinkQuestionFocusArguments,
    build_generated_question_focus,
    parse_question_focus_tool_arguments,
    question_focus_validation_error_feedback,
    successful_question_focus_tool_feedback,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.state import (
    QuestionGenerationSubgraphState,
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

_MAXIMUM_COMPLETION_TOKENS = 4096
_MAXIMUM_CONSECUTIVE_THINKS = 2


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """仅在至少一轮返回指标时汇总模型用量。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _runtime_values(
    audits: list[QuestionFocusToolCallAudit],
) -> dict[str, int | None]:
    """汇总关注点发现会话的 token 指标。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(audit.completion_tokens for audit in audits),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _tool_message(
    call: MLLMToolCall,
    feedback: QuestionFocusToolFeedback,
) -> dict[str, str]:
    """把最小工具反馈写入当前关注点发现会话。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _result(
    *,
    status: str,
    termination_reason: str,
    document_id: str,
    model: str,
    focuses: tuple[GeneratedQuestionFocus, ...],
    finish_reasoning: str | None,
    audits: list[QuestionFocusToolCallAudit],
    started_at: float,
    error: str | None,
) -> QuestionFocusDiscoveryResult:
    """统一构造关注点发现成功或失败结果。"""
    return QuestionFocusDiscoveryResult.model_validate(
        {
            "status": status,
            "termination_reason": termination_reason,
            "document_id": document_id,
            "model": model,
            "prompt_version": QUESTION_FOCUS_DISCOVERY_PROMPT_VERSION,
            "tool_version": QUESTION_FOCUS_DISCOVERY_TOOL_VERSION,
            "focuses": focuses,
            "finish_reasoning": finish_reasoning,
            "rounds": len(audits),
            "elapsed_ms": round((perf_counter() - started_at) * 1000, 3),
            "tool_calls": tuple(audits),
            "error": error,
            **_runtime_values(audits),
        }
    )


def _validate_evidence_pages(
    arguments: GenerateQuestionFocusArguments,
    *,
    page_count: int,
) -> QuestionFocusToolFeedback | None:
    """拒绝超出当前 PDF 范围的关注点证据页码。"""
    invalid = sorted(
        {
            item.page_number
            for item in arguments.evidence
            if not 1 <= item.page_number <= page_count
        }
    )
    if not invalid:
        return None
    return QuestionFocusToolFeedback(
        ok=False,
        message=(
            f"evidence.page_number：引用了合同范围外的页码 {invalid}，"
            f"本合同只有 1-{page_count} 页；请核对页面标签后重新提交。"
        ),
    )


def _allowed_attention_codes(state: QuestionGenerationSubgraphState) -> set[str]:
    """从启动期权威指南对象生成当前允许引用的稳定标识集合。"""
    catalog = state["retrieval_view_guide_catalog"].question
    allowed = {f"common.{point.code}" for point in catalog.common.attention_points}
    for guide in catalog.categories:
        allowed.update(
            f"{guide.category_code}.{point.code}" for point in guide.attention_points
        )
    return allowed


def _validate_attention_codes(
    arguments: GenerateQuestionFocusArguments,
    *,
    allowed_codes: set[str],
) -> QuestionFocusToolFeedback | None:
    """拒绝格式正确但不属于当前权威指南目录的标识。"""
    unknown = [code for code in arguments.attention_codes if code not in allowed_codes]
    if not unknown:
        return None
    return QuestionFocusToolFeedback(
        ok=False,
        message=(
            "attention_codes：引用了指南目录中不存在的标识 "
            f"{unknown}；请只使用当前提问指南明确提供的稳定标识。"
        ),
    )


def _focus_fingerprint(focus: str) -> str:
    """生成只用于拒绝完全相同关注点要求的保守文本指纹。"""
    return re.sub(r"\s+", "", focus).rstrip("？?。.").casefold()


async def discover_question_focuses(
    state: QuestionGenerationSubgraphState,
) -> QuestionGenerationSubgraphState:
    """首轮强制思考，再按价值顺序发现混合问题关注点。"""
    prepared_pdf = state["prepared_pdf"]
    context = state["question_generation_context"]
    if prepared_pdf.document_id != context.document_id:
        raise ValueError("关注点发现输入 PDF 与提问指南上下文的 document_id 不一致")

    settings = get_settings().mllm
    generation = settings.generation
    hidden_limit = context.maximum_questions
    started_at = perf_counter()
    focuses: tuple[GeneratedQuestionFocus, ...] = ()
    focus_fingerprints: set[str] = set()
    allowed_attention_codes = _allowed_attention_codes(state)
    initial_think_completed = False
    consecutive_thinks = 0
    audits: list[QuestionFocusToolCallAudit] = []
    protocol_recovery = ToolProtocolRecovery()
    messages = build_question_focus_discovery_messages(context)
    maximum_rounds = max(8, hidden_limit * 4 + 4)
    model = settings.model

    async with MLLMClient(settings) as client:
        for round_number in range(1, maximum_rounds + 1):
            tools = (
                (THINK_QUESTION_FOCUS_TOOL,)
                if not initial_think_completed
                else QUESTION_FOCUS_DISCOVERY_TOOLS
            )
            available_tool_names = {tool["function"]["name"] for tool in tools}
            request_started_at = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(tools),
                    tool_choice=QUESTION_FOCUS_DISCOVERY_TOOL_CHOICE,
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
                    tool_placement=QUESTION_FOCUS_DISCOVERY_TOOL_PLACEMENT,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                return {
                    "question_focus_discovery": _result(
                        status="failed",
                        termination_reason="failed",
                        document_id=context.document_id,
                        model=model,
                        focuses=focuses,
                        finish_reasoning=None,
                        audits=audits,
                        started_at=started_at,
                        error=str(exc),
                    )
                }

            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            completion = response.completion
            model = completion.model or model
            temporary_failure_memory_cleared = False

            if len(response.tool_calls) != 1:
                feedback = QuestionFocusToolFeedback(
                    ok=False,
                    message=build_protocol_recovery_message(
                        tool_call_count=len(response.tool_calls),
                        result_label="问题关注点动作",
                    )["content"],
                )
                audits.append(
                    QuestionFocusToolCallAudit(
                        round_number=round_number,
                        call_id=None,
                        name="protocol_recovery",
                        raw_arguments="",
                        assistant_content=audited_assistant_content(
                            response.assistant_message.get("content")
                        ),
                        feedback=feedback,
                        accepted_focus_count=len(focuses),
                        temporary_failure_memory_cleared=False,
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
                    result_label="问题关注点动作",
                )
                if exceeded:
                    return {
                        "question_focus_discovery": _result(
                            status="failed",
                            termination_reason="failed",
                            document_id=context.document_id,
                            model=model,
                            focuses=focuses,
                            finish_reasoning=None,
                            audits=audits,
                            started_at=started_at,
                            error="连续三轮未生成且仅生成一个合法工具调用。",
                        )
                    }
                continue

            call = response.tool_calls[0]
            protocol_recovery.accept_protocol()
            accepted_think = False
            accepted_focus: GeneratedQuestionFocus | None = None
            accepted_finish: FinishQuestionFocusDiscoveryArguments | None = None
            try:
                if call.name not in available_tool_names:
                    if not initial_think_completed:
                        raise ValueError(
                            f"tool：首轮不能调用 {call.name}；请先调用 think 扫描高价值关注点"
                        )
                    raise ValueError(
                        f"tool：当前没有提供 {call.name}；请调用当前可用的关注点发现工具"
                    )
                arguments = parse_question_focus_tool_arguments(
                    call.name,
                    call.arguments,
                )
            except (ValueError, ValidationError) as exc:
                feedback = question_focus_validation_error_feedback(exc)
            else:
                if isinstance(arguments, ThinkQuestionFocusArguments):
                    consecutive_thinks += 1
                    if consecutive_thinks > _MAXIMUM_CONSECUTIVE_THINKS:
                        feedback = QuestionFocusToolFeedback(
                            ok=False,
                            message=(
                                "reasoning：已经连续调用 think 两次但没有形成动作；"
                                "请提交当前价值最高的关注点，或在确认没有遗漏后结束。"
                            ),
                        )
                    else:
                        accepted_think = True
                        feedback = successful_question_focus_tool_feedback("think")
                elif isinstance(arguments, GenerateQuestionFocusArguments):
                    consecutive_thinks = 0
                    if (
                        page_error := _validate_evidence_pages(
                            arguments,
                            page_count=prepared_pdf.page_count,
                        )
                    ) is not None:
                        feedback = page_error
                    elif (
                        code_error := _validate_attention_codes(
                            arguments,
                            allowed_codes=allowed_attention_codes,
                        )
                    ) is not None:
                        feedback = code_error
                    elif _focus_fingerprint(arguments.focus) in focus_fingerprints:
                        feedback = QuestionFocusToolFeedback(
                            ok=False,
                            message=(
                                "focus：该关注点要求与当前成功轨迹中的已有内容重复；"
                                "请提交尚未覆盖的独立关注点，或结束发现。"
                            ),
                        )
                    else:
                        accepted_focus = build_generated_question_focus(
                            arguments,
                            order=len(focuses) + 1,
                        )
                        feedback = successful_question_focus_tool_feedback(
                            "generate_question_focus",
                            generated_focus=accepted_focus,
                        )
                elif isinstance(
                    arguments,
                    FinishQuestionFocusDiscoveryArguments,
                ):
                    consecutive_thinks = 0
                    accepted_finish = arguments
                    feedback = successful_question_focus_tool_feedback(
                        "finish_question_focus_discovery"
                    )
                else:  # pragma: no cover - 联合类型已覆盖全部动作
                    feedback = QuestionFocusToolFeedback(
                        ok=False,
                        message="tool：当前动作无法识别；请调用当前提供的工具。",
                    )

            tool_message = _tool_message(call, feedback)
            if accepted_think:
                temporary_failure_memory_cleared = (
                    protocol_recovery.memory_start is not None
                )
                protocol_recovery.accept_correction(messages)
                initial_think_completed = True
                messages.append(response.assistant_message)
                messages.append(tool_message)
            elif accepted_focus is not None:
                temporary_failure_memory_cleared = (
                    protocol_recovery.memory_start is not None
                )
                protocol_recovery.accept_correction(messages)
                focuses = (*focuses, accepted_focus)
                focus_fingerprints.add(_focus_fingerprint(accepted_focus.focus))
                messages.append(response.assistant_message)
                messages.append(tool_message)
            elif accepted_finish is not None:
                temporary_failure_memory_cleared = (
                    protocol_recovery.memory_start is not None
                )
                protocol_recovery.accept_correction(messages)
            elif feedback.ok:
                temporary_failure_memory_cleared = (
                    protocol_recovery.memory_start is not None
                )
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
                QuestionFocusToolCallAudit(
                    round_number=round_number,
                    call_id=call.call_id,
                    name=call.name,
                    raw_arguments=call.arguments,
                    assistant_content=audited_assistant_content(
                        response.assistant_message.get("content")
                    ),
                    feedback=feedback,
                    accepted_focus_count=len(focuses),
                    temporary_failure_memory_cleared=(temporary_failure_memory_cleared),
                    elapsed_ms=elapsed_ms,
                    response_id=completion.response_id,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cached_tokens=completion.cached_tokens,
                )
            )

            if accepted_finish is not None:
                return {
                    "question_focus_discovery": _result(
                        status="completed",
                        termination_reason="model_finished",
                        document_id=context.document_id,
                        model=model,
                        focuses=focuses,
                        finish_reasoning=accepted_finish.reasoning_summary,
                        audits=audits,
                        started_at=started_at,
                        error=None,
                    )
                }
            if accepted_focus is not None and len(focuses) >= hidden_limit:
                return {
                    "question_focus_discovery": _result(
                        status="completed",
                        termination_reason="hidden_limit_reached",
                        document_id=context.document_id,
                        model=model,
                        focuses=focuses,
                        finish_reasoning=None,
                        audits=audits,
                        started_at=started_at,
                        error=None,
                    )
                }

    return {
        "question_focus_discovery": _result(
            status="failed",
            termination_reason="failed",
            document_id=context.document_id,
            model=model,
            focuses=focuses,
            finish_reasoning=None,
            audits=audits,
            started_at=started_at,
            error=f"达到最大轮次 {maximum_rounds}，仍未完成问题关注点发现。",
        )
    }


__all__ = ["discover_question_focuses"]
