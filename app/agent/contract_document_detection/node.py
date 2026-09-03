"""合同文档识别 Agent 的有限多轮 MLLM 节点。"""

from __future__ import annotations

from collections.abc import Iterable
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_document_detection.prompt import (
    CONTRACT_DOCUMENT_DETECTION_PROMPT_VERSION,
    CONTRACT_DOCUMENT_DETECTION_TOOL_PLACEMENT,
    build_contract_document_detection_messages,
)
from app.agent.contract_document_detection.state import (
    ContractDocumentDetectionResult,
    ContractDocumentDetectionState,
    ContractDocumentDetectionToolCallAudit,
)
from app.agent.contract_document_detection.tool import (
    CONTRACT_DOCUMENT_DETECTION_TOOLS,
    CONTRACT_DOCUMENT_DETECTION_TOOL_CHOICE,
    CONTRACT_DOCUMENT_DETECTION_TOOL_VERSION,
    ContractDocumentDetectionToolFeedback,
    ContractDocumentEvidence,
    SubmitContractDocumentJudgmentArguments,
    ThinkArguments,
    parse_contract_document_detection_tool_arguments,
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
_MAXIMUM_COMPLETION_TOKENS = 4096
_MAXIMUM_CONSECUTIVE_THINKS = 2
_THINK_MAXIMUM_TOKENS = 1024


def _sum_optional(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _runtime_values(
    audits: list[ContractDocumentDetectionToolCallAudit],
) -> dict[str, int | None]:
    """汇总合同识别全部模型轮次的 token 用量。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(
            audit.completion_tokens for audit in audits
        ),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _tool_message(
    call: MLLMToolCall,
    feedback: ContractDocumentDetectionToolFeedback,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _validate_evidence_pages(
    evidence: Iterable[ContractDocumentEvidence],
    *,
    page_count: int,
) -> ContractDocumentDetectionToolFeedback | None:
    invalid = sorted(
        {
            item.page_number
            for item in evidence
            if not 1 <= item.page_number <= page_count
        }
    )
    if not invalid:
        return None
    return ContractDocumentDetectionToolFeedback(
        ok=False,
        message=(
            f"evidence.page_number 超出上传 PDF 的 1-{page_count} 页：{invalid}；"
            "请根据页面标签修正后重新提交。"
        ),
    )


def _failed_result(
    state: ContractDocumentDetectionState,
    *,
    started_at: float,
    audits: list[ContractDocumentDetectionToolCallAudit],
    model: str | None,
    error: str,
) -> ContractDocumentDetectionState:
    prepared_pdf = state["prepared_pdf"]
    return {
        **state,
        "result": ContractDocumentDetectionResult(
            status="failed",
            document_id=prepared_pdf.document_id,
            model=model,
            prompt_version=CONTRACT_DOCUMENT_DETECTION_PROMPT_VERSION,
            tool_version=CONTRACT_DOCUMENT_DETECTION_TOOL_VERSION,
            rounds=len(audits),
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
            tool_calls=tuple(audits),
            error=error,
            **_runtime_values(audits),
        ),
    }


async def detect_contract_document(
    state: ContractDocumentDetectionState,
) -> ContractDocumentDetectionState:
    """查看处理版 PDF 全部页面并形成有证据的合同二分类结果。"""
    started_at = perf_counter()
    prepared_pdf = state["prepared_pdf"]
    if prepared_pdf.page_count != len(prepared_pdf.pages):
        raise ValueError("PreparedPDF 页面数量与 page_count 不一致")

    messages = build_contract_document_detection_messages(prepared_pdf)
    settings = get_settings().mllm
    generation = settings.generation
    audits: list[ContractDocumentDetectionToolCallAudit] = []
    recovery = ToolProtocolRecovery()
    consecutive_thinks = 0
    response_model: str | None = settings.model

    async with MLLMClient(settings) as client:
        for round_number in range(1, _MAXIMUM_ROUNDS + 1):
            request_started_at = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(CONTRACT_DOCUMENT_DETECTION_TOOLS),
                    tool_choice=CONTRACT_DOCUMENT_DETECTION_TOOL_CHOICE,
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
                    tool_placement=CONTRACT_DOCUMENT_DETECTION_TOOL_PLACEMENT,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                return _failed_result(
                    state,
                    started_at=started_at,
                    audits=audits,
                    model=response_model,
                    error=str(exc),
                )

            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            completion = response.completion
            response_model = completion.model or response_model
            assistant_content = audited_assistant_content(
                response.assistant_message.get("content")
            )

            if len(response.tool_calls) != 1:
                feedback = ContractDocumentDetectionToolFeedback(
                    ok=False,
                    message=build_protocol_recovery_message(
                        tool_call_count=len(response.tool_calls),
                        result_label="合同文档识别结果",
                    )["content"],
                )
                audits.append(
                    ContractDocumentDetectionToolCallAudit(
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
                    result_label="合同文档识别结果",
                )
                if exceeded:
                    return _failed_result(
                        state,
                        started_at=started_at,
                        audits=audits,
                        model=response_model,
                        error="连续三轮未生成且仅生成一个合法工具调用。",
                    )
                continue

            call = response.tool_calls[0]
            recovery.accept_protocol()
            arguments: (
                ThinkArguments | SubmitContractDocumentJudgmentArguments | None
            ) = None
            accepted: SubmitContractDocumentJudgmentArguments | None = None
            if assistant_content is not None:
                feedback = ContractDocumentDetectionToolFeedback(
                    ok=False,
                    message=(
                        "assistant.content：工具调用之外不得输出普通文本；"
                        "请只调用一个当前工具且调用后不要追加说明。"
                    ),
                )
            else:
                try:
                    arguments = parse_contract_document_detection_tool_arguments(
                        call.name,
                        call.arguments,
                    )
                except (ValueError, ValidationError) as exc:
                    feedback = validation_error_feedback(exc)

            if isinstance(arguments, ThinkArguments):
                if completion.completion_tokens is None:
                    feedback = ContractDocumentDetectionToolFeedback(
                        ok=False,
                        message=(
                            "reasoning：本轮响应没有返回 completion_tokens，无法验证"
                            " think 上限；请重新简短思考。"
                        ),
                    )
                elif completion.completion_tokens > _THINK_MAXIMUM_TOKENS:
                    feedback = ContractDocumentDetectionToolFeedback(
                        ok=False,
                        message=(
                            f"reasoning：本轮完整工具响应使用 "
                            f"{completion.completion_tokens} completion tokens，超过"
                            " think 的 1024 tokens 上限；请压缩推理后重新调用。"
                        ),
                    )
                elif consecutive_thinks >= _MAXIMUM_CONSECUTIVE_THINKS:
                    feedback = ContractDocumentDetectionToolFeedback(
                        ok=False,
                        message=(
                            "reasoning：已经连续完成两次 think；证据充分时请提交"
                            "正式判断，证据不足时不要猜测，有限执行将形成技术失败。"
                        ),
                    )
                else:
                    consecutive_thinks += 1
                    feedback = ContractDocumentDetectionToolFeedback(
                        ok=True,
                        message="思考已记录，请继续判断。",
                    )
            elif isinstance(
                arguments,
                SubmitContractDocumentJudgmentArguments,
            ):
                page_error = _validate_evidence_pages(
                    arguments.evidence,
                    page_count=prepared_pdf.page_count,
                )
                if page_error is not None:
                    feedback = page_error
                else:
                    consecutive_thinks = 0
                    accepted = arguments
                    feedback = ContractDocumentDetectionToolFeedback(
                        ok=True,
                        message="合同文档判断已接受。",
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
                ContractDocumentDetectionToolCallAudit(
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
                    **state,
                    "result": ContractDocumentDetectionResult(
                        status=(
                            "contract" if accepted.is_contract else "not_contract"
                        ),
                        document_id=prepared_pdf.document_id,
                        is_contract=accepted.is_contract,
                        evidence=tuple(accepted.evidence),
                        reasoning_summary=accepted.reasoning_summary,
                        model=response_model,
                        prompt_version=CONTRACT_DOCUMENT_DETECTION_PROMPT_VERSION,
                        tool_version=CONTRACT_DOCUMENT_DETECTION_TOOL_VERSION,
                        rounds=round_number,
                        elapsed_ms=round(
                            (perf_counter() - started_at) * 1000,
                            3,
                        ),
                        tool_calls=tuple(audits),
                        **_runtime_values(audits),
                    ),
                }

    return _failed_result(
        state,
        started_at=started_at,
        audits=audits,
        model=response_model,
        error=f"达到最大轮次 {_MAXIMUM_ROUNDS}，仍未形成有效合同文档判断。",
    )


__all__ = ["detect_contract_document"]
