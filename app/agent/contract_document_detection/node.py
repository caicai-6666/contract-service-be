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
    DocumentAdmissionOutcome,
    FileQualityResult,
    FileQualityGeneration,
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
_MAXIMUM_CONSECUTIVE_THINKS = 2


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
            f"evidence.page_number 超出上传文档的 1-{page_count} 页：{invalid}；"
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
    settings = get_settings().mllm.for_contract_extraction()
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
                    max_completion_tokens=generation.max_completion_tokens,
                    temperature=generation.temperature,
                    top_p=generation.top_p,
                    top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,
                    enable_thinking=True,
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
                if consecutive_thinks >= _MAXIMUM_CONSECUTIVE_THINKS:
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


__all__ = ["detect_contract_document", "check_contract_file_quality", "finalize_document_admission"]


async def check_contract_file_quality(
    state: ContractDocumentDetectionState,
) -> ContractDocumentDetectionState:
    """读取全部页面，原生思考后提交严格 JSON，最多三次校验尝试。"""
    detection = state["result"]
    if detection.status != "contract":
        raise ValueError("只有合同性质判断通过后才能执行文件质量检查")
    from app.agent.contract_document_detection.prompt import (
        build_file_quality_messages, FILE_QUALITY_PROMPT_VERSION,
    )
    from app.infrastructure.model_json import load_model_json, validate_model_payload

    pdf = state["prepared_pdf"]
    if len(pdf.pages) != pdf.page_count:
        raise ValueError("PreparedPDF 页面数量与 page_count 不一致")
    settings = get_settings().mllm.for_contract_extraction()
    messages = build_file_quality_messages(pdf)
    audit = []
    error = "质量检查未形成可靠结果"
    try:
        async with MLLMClient(settings) as client:
            for attempt in range(1, 4):
                response = await client.create_json_chat_completion(
                    messages=messages,
                    json_schema=FileQualityGeneration.model_json_schema(),
                    schema_name="contract_file_quality",
                    enable_thinking=True,
                    temperature=1.0, top_p=0.95, top_k=20, min_p=0.0,
                    presence_penalty=0.0, repetition_penalty=1.0,
                    max_completion_tokens=settings.generation.max_completion_tokens,
                )
                record = {"attempt": attempt, "response": response.raw_response, "accepted": False,
                    "prompt_version": FILE_QUALITY_PROMPT_VERSION,
                    "model": settings.model, "enable_thinking": True}
                audit.append(record)
                try:
                    if response.refusal or response.has_tool_calls or response.finish_reason != "stop":
                        raise ValueError("必须返回正常结束的完整 JSON，不接受拒答、工具调用或截断结果")
                    raw = load_model_json(response.content)
                    if not isinstance(raw, dict) or list(raw) != list(FileQualityGeneration.model_fields):
                        raise ValueError("字段必须按 evidence、malicious_content_detected、readability_insufficient 顺序输出")
                    output = validate_model_payload(FileQualityGeneration, raw)
                    if any(page > pdf.page_count for item in output.evidence for page in item.page_numbers):
                        raise ValueError("证据页码超出当前文件范围")
                    record["accepted"] = True
                    # 仅完全有效的结果进入状态；失败响应始终只留在私有审计。
                    del messages[2:]
                    quality = FileQualityResult(document_id=pdf.document_id,
                        status="generated", **output.model_dump(), audit=tuple(audit))
                    return {"result": detection.model_copy(update={"file_quality": quality})}
                except (ValueError, TypeError) as exc:
                    # 不把异常中的模型输入值重新注入上下文，纠错只报告字段位置。
                    error = (
                        "字段校验失败：" + "、".join(
                            ".".join(map(str, item["loc"])) for item in exc.errors()[:5]
                        ) if isinstance(exc, ValidationError) else str(exc)
                    )
                    record["error"] = error
                    messages.append({"role": "user", "content":
                        "输出校验失败，请修正后重新提交完整 JSON。" + error})
    except (MLLMRequestError, MLLMUnavailableError) as exc:
        error = str(exc)
        audit.append({"error": error, "type": type(exc).__name__})
    quality = FileQualityResult(document_id=pdf.document_id, status="failed",
        error=error, audit=tuple(audit))
    return {"result": detection.model_copy(update={"file_quality": quality})}


def finalize_document_admission(
    state: ContractDocumentDetectionState,
) -> ContractDocumentDetectionState:
    """统一收束两个检查结果；只依据已校验事实构造反馈，不再调用模型。"""
    result = state["result"]
    pdf = state["prepared_pdf"]
    quality = result.file_quality
    if result.document_id != pdf.document_id:
        outcome = DocumentAdmissionOutcome(status="failed", source="contract_detection",
            message="合同文档检查未能完成", error="合同判断与当前文件身份不一致")
    elif result.status == "failed":
        outcome = DocumentAdmissionOutcome(status="failed", source="contract_detection",
            message="合同性质判断未能完成", error=result.error)
    elif result.status == "not_contract":
        evidence = "；".join(f"第 {item.page_number} 页：{item.observation}" for item in result.evidence)
        outcome = DocumentAdmissionOutcome(status="rejected", source="contract_detection",
            message=f"上传内容不属于合同文档，处理已停止。{result.reasoning_summary} 依据：{evidence}")
    elif quality is None or quality.document_id != pdf.document_id:
        outcome = DocumentAdmissionOutcome(status="failed", source="file_quality",
            message="文件准入与质量检查未能完成", error="缺少当前文件的质量检查结果")
    elif any(page > pdf.page_count for item in quality.evidence for page in item.page_numbers):
        outcome = DocumentAdmissionOutcome(status="failed", source="file_quality",
            message="文件准入与质量检查未能完成", error="问题页码超出文件范围")
    elif quality.status == "failed":
        outcome = DocumentAdmissionOutcome(status="failed", source="file_quality",
            message="文件准入与质量检查未能完成", error=quality.error)
    elif quality.rejected:
        reasons = []
        if quality.malicious_content_detected:
            reasons.append("存在恶意或明显无关内容")
        if quality.readability_insufficient:
            reasons.append("存在妨碍可靠提取的模糊、不可读或缺失内容")
        evidence = "；".join(
            f"第 {','.join(map(str, item.page_numbers))} 页：{item.description}"
            for item in quality.evidence
            if (item.kind == "malicious_content" and quality.malicious_content_detected)
            or (item.kind == "readability" and quality.readability_insufficient)
        )
        outcome = DocumentAdmissionOutcome(status="rejected", source="file_quality",
            message=f"文件检查未通过：{'；'.join(reasons)}。{evidence}。处理已停止，请调整文件后重新上传。")
    else:
        outcome = DocumentAdmissionOutcome(status="passed", source="file_quality",
            message="合同性质与文件质量检查通过，开始重复性判断。")
    return {"result": result.model_copy(update={"outcome": outcome})}
