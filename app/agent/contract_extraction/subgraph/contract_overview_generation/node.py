"""合同概览生成子图节点。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_extraction.context import context_sha256
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)
from app.agent.contract_extraction.subgraph.contract_overview_generation.prompt import (
    CONTRACT_OVERVIEW_GENERATION_PROMPT_VERSION,
    build_contract_overview_generation_messages,
)
from app.agent.contract_extraction.subgraph.contract_overview_generation.state import (
    ContractOverviewGenerationContext,
    ContractOverviewGenerationSubgraphState,
    ContractOverviewGenerationToolCallAudit,
    ContractOverviewResult,
)
from app.agent.contract_extraction.subgraph.contract_overview_generation.tool import (
    CONTRACT_OVERVIEW_GENERATION_TOOLS,
    CONTRACT_OVERVIEW_GENERATION_TOOL_CHOICE,
    CONTRACT_OVERVIEW_GENERATION_TOOL_PLACEMENT,
    CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION,
    ContractOverviewGenerationToolFeedback,
    SubmitContractOverviewArguments,
    ContractOverviewEvidence,
    ThinkArguments,
    parse_contract_overview_generation_tool_arguments,
    validation_error_feedback,
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

_MAXIMUM_ROUNDS = 6
_MAXIMUM_CONSECUTIVE_THINKS = 2


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """仅在至少一轮返回指标时汇总模型用量。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _runtime_values(
    audits: list[ContractOverviewGenerationToolCallAudit],
) -> dict[str, int | None]:
    """汇总合同概览生成会话的 token 指标。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(
            audit.completion_tokens for audit in audits
        ),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _tool_message(
    call: MLLMToolCall,
    feedback: ContractOverviewGenerationToolFeedback,
) -> dict[str, str]:
    """把最小工具反馈写入当前命名会话。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _validate_evidence_pages(
    evidence: Iterable[ContractOverviewEvidence],
    *,
    page_count: int,
) -> ContractOverviewGenerationToolFeedback | None:
    """拒绝超出当前合同页面范围的命名证据。"""
    invalid = sorted(
        {
            item.page_number
            for item in evidence
            if not 1 <= item.page_number <= page_count
        }
    )
    if not invalid:
        return None
    return ContractOverviewGenerationToolFeedback(
        ok=False,
        message=(
            f"evidence.page_number：引用了合同范围外的页码 {invalid}，"
            f"本合同只有 1-{page_count} 页；请核对页面标签后重新提交。"
        ),
    )


def _failed_result(
    *,
    document_id: str,
    model: str | None,
    audits: list[ContractOverviewGenerationToolCallAudit],
    started_at: float,
    error: str,
) -> ContractOverviewResult:
    """形成不携带半成品名称或摘要的技术失败结果。"""
    return ContractOverviewResult(
        status="failed",
        document_id=document_id,
        model=model,
        prompt_version=CONTRACT_OVERVIEW_GENERATION_PROMPT_VERSION,
        tool_version=CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION,
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        tool_calls=tuple(audits),
        error=error,
        **_runtime_values(audits),
    )


def assemble_contract_overview_context(
    state: ContractOverviewGenerationSubgraphState,
) -> ContractOverviewGenerationSubgraphState:
    """节点一：组装页面、结构、分类摘要与命名任务的不可变上下文。"""
    base_context = state["base_context"]
    classification = state["classification"]
    if not isinstance(classification, ContractClassificationResult):
        raise TypeError(
            "classification 必须是 ContractClassificationResult，"
            "不能将分类运行审计或占位对象写入命名上下文"
        )
    if classification.document_id != base_context.document_id:
        raise ValueError("分类结果与合同基础上下文的 document_id 不一致")

    messages = build_contract_overview_generation_messages(
        base_context,
        classification,
    )
    return {
        "contract_overview_context": ContractOverviewGenerationContext(
            document_id=base_context.document_id,
            prompt_version=CONTRACT_OVERVIEW_GENERATION_PROMPT_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


async def generate_contract_overview(
    state: ContractOverviewGenerationSubgraphState,
) -> ContractOverviewGenerationSubgraphState:
    """节点二：通过有限工具循环生成包含建议名称与摘要的合同概览。"""
    context = state["contract_overview_context"]
    page_count = state["page_count"]
    if page_count < 1:
        raise ValueError("合同概览生成所需的合同总页数必须大于等于 1")
    if context.prompt_version != CONTRACT_OVERVIEW_GENERATION_PROMPT_VERSION:
        raise ValueError("建议文件名上下文的提示词版本与当前生成节点不一致")
    if context.prefix_sha256 != context_sha256(context.messages):
        raise ValueError("建议文件名上下文消息与 prefix_sha256 不一致")
    if context.document_id != state["base_context"].document_id:
        raise ValueError("建议文件名上下文与合同基础上下文的 document_id 不一致")
    if context.document_id != state["classification"].document_id:
        raise ValueError("建议文件名上下文与分类结果的 document_id 不一致")

    started_at = perf_counter()
    messages = deepcopy(list(context.messages))
    settings = get_settings().mllm.for_contract_extraction()
    generation = settings.generation
    audits: list[ContractOverviewGenerationToolCallAudit] = []
    recovery = ToolProtocolRecovery()
    consecutive_thinks = 0
    response_model: str | None = settings.model

    async with MLLMClient(settings) as client:
        for round_number in range(1, _MAXIMUM_ROUNDS + 1):
            request_started_at = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(CONTRACT_OVERVIEW_GENERATION_TOOLS),
                    tool_choice=CONTRACT_OVERVIEW_GENERATION_TOOL_CHOICE,
                    max_completion_tokens=generation.max_completion_tokens,
                    temperature=generation.temperature,
                    top_p=generation.top_p,
                    top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,
                    enable_thinking=True,
                    tool_placement=CONTRACT_OVERVIEW_GENERATION_TOOL_PLACEMENT,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                return {
                    "contract_overview": _failed_result(
                        document_id=context.document_id,
                        model=response_model,
                        audits=audits,
                        started_at=started_at,
                        error=str(exc),
                    )
                }

            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            completion = response.completion
            response_model = completion.model or response_model
            assistant_content = audited_assistant_content(
                response.assistant_message.get("content")
            )

            if len(response.tool_calls) != 1:
                feedback = ContractOverviewGenerationToolFeedback(
                    ok=False,
                    message=build_protocol_recovery_message(
                        tool_call_count=len(response.tool_calls),
                        result_label="合同概览生成结果",
                    )["content"],
                )
                audits.append(
                    ContractOverviewGenerationToolCallAudit(
                        round_number=round_number,
                        call_id=None,
                        name="protocol_recovery",
                        raw_arguments="",
                        assistant_content=assistant_content,
                        feedback=feedback,
                        elapsed_ms=elapsed_ms,
                        response_id=completion.response_id,
                        prompt_tokens=completion.prompt_tokens,
                        completion_tokens=completion.completion_tokens,
                        cached_tokens=completion.cached_tokens,
                    )
                )
                exceeded = recovery.record_protocol_failure(
                    messages,
                    assistant_message=response.assistant_message,
                    tool_call_count=len(response.tool_calls),
                    result_label="合同概览生成结果",
                )
                if exceeded:
                    return {
                        "contract_overview": _failed_result(
                            document_id=context.document_id,
                            model=response_model,
                            audits=audits,
                            started_at=started_at,
                            error="连续三轮未生成且仅生成一个合法工具调用。",
                        )
                    }
                continue

            call = response.tool_calls[0]
            recovery.accept_protocol()
            arguments: ThinkArguments | SubmitContractOverviewArguments | None = None
            accepted: SubmitContractOverviewArguments | None = None
            if assistant_content is not None:
                feedback = ContractOverviewGenerationToolFeedback(
                    ok=False,
                    message=(
                        "assistant.content：工具调用之外不得输出普通文本；"
                        "请只调用一个当前工具且调用后不要追加说明。"
                    ),
                )
            else:
                try:
                    arguments = parse_contract_overview_generation_tool_arguments(
                        call.name,
                        call.arguments,
                    )
                except (ValueError, ValidationError) as exc:
                    feedback = validation_error_feedback(exc)

            if isinstance(arguments, ThinkArguments):
                if consecutive_thinks >= _MAXIMUM_CONSECUTIVE_THINKS:
                    feedback = ContractOverviewGenerationToolFeedback(
                        ok=False,
                        message=(
                            "reasoning：已经连续完成两次 think；请根据现有页面证据"
                            "提交建议名称与合同摘要，不得继续无界推理。"
                        ),
                    )
                else:
                    consecutive_thinks += 1
                    feedback = ContractOverviewGenerationToolFeedback(
                        ok=True,
                        message="思考已记录，请继续完成名称与合同摘要。",
                    )
            elif isinstance(arguments, SubmitContractOverviewArguments):
                page_error = _validate_evidence_pages(
                    arguments.evidence,
                    page_count=page_count,
                )
                if page_error is not None:
                    feedback = page_error
                else:
                    consecutive_thinks = 0
                    accepted = arguments
                    feedback = ContractOverviewGenerationToolFeedback(
                        ok=True,
                        message="建议文件名和合同摘要已接受。",
                    )

            tool_message = _tool_message(call, feedback)
            if feedback.ok:
                recovery.accept_correction(messages)
                messages.append(response.assistant_message)
                messages.append(tool_message)
            else:
                recovery.record_tool_failure(
                    messages,
                    assistant_message=response.assistant_message,
                    tool_message=tool_message,
                )
            audits.append(
                ContractOverviewGenerationToolCallAudit(
                    round_number=round_number,
                    call_id=call.call_id,
                    name=call.name,
                    raw_arguments=call.arguments,
                    assistant_content=assistant_content,
                    feedback=feedback,
                    elapsed_ms=elapsed_ms,
                    response_id=completion.response_id,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cached_tokens=completion.cached_tokens,
                )
            )

            if accepted is not None:
                return {
                    "contract_overview": ContractOverviewResult(
                        status="generated",
                        document_id=context.document_id,
                        evidence=tuple(accepted.evidence),
                        reasoning=accepted.reasoning,
                        file_name=accepted.file_name,
                        summary=accepted.summary,
                        model=response_model,
                        prompt_version=CONTRACT_OVERVIEW_GENERATION_PROMPT_VERSION,
                        tool_version=CONTRACT_OVERVIEW_GENERATION_TOOL_VERSION,
                        rounds=round_number,
                        elapsed_ms=round(
                            (perf_counter() - started_at) * 1000,
                            3,
                        ),
                        tool_calls=tuple(audits),
                        **_runtime_values(audits),
                    )
                }

    return {
        "contract_overview": _failed_result(
            document_id=context.document_id,
            model=response_model,
            audits=audits,
            started_at=started_at,
            error=f"达到最大轮次 {_MAXIMUM_ROUNDS}，仍未形成有效合同概览。",
        )
    }


__all__ = ["assemble_contract_overview_context", "generate_contract_overview"]
