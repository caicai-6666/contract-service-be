"""检索问题指南组装、并发问题生成与向量化节点。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from time import perf_counter

from pydantic import ValidationError

from app.agent.contract_extraction.context import context_sha256
from app.agent.contract_extraction.progress import ParallelProgressTracker
from app.agent.contract_extraction.state import PreparedPDF
from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    RetrievalViewGuideCatalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.tool import (
    GeneratedQuestionFocus,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.prompt import (
    QUESTION_GENERATION_CONTEXT_VERSION,
    QUESTION_PROPOSAL_COMMON_PROMPT_VERSION,
    QUESTION_PROPOSAL_TARGET_PROMPT_VERSION,
    QUESTION_PROPOSAL_TOOL_PLACEMENT,
    RETRIEVAL_EMBEDDING_PROMPT_VERSION,
    append_question_plan_target,
    build_question_generation_messages,
    build_question_proposal_common_messages,
    render_contract_question_embedding_input,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.state import (
    ContractRetrievalVectorResult,
    FailedQuestionProposal,
    GeneratedQuestionProposal,
    QuestionGenerationContext,
    QuestionGenerationSubgraphState,
    QuestionGenerationToolCallAudit,
    QuestionProposalContext,
    QuestionProposalOutcome,
    RetrievalQuestionEmbedding,
    RetrievalQuestionEmbeddingResult,
    RetrievalQuestionGenerationResult,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.tool import (
    PROPOSE_QUESTION_TOOL,
    QUESTION_GENERATION_TOOL_CHOICE,
    QUESTION_GENERATION_TOOL_VERSION,
    GeneratedQuestion,
    ProposeQuestionArguments,
    QuestionGenerationToolFeedback,
    build_generated_question,
    parse_question_generation_tool_arguments,
    validation_error_feedback,
)
from app.agent.contract_extraction.tool_protocol import (
    ToolProtocolRecovery,
    audited_assistant_content,
    build_protocol_recovery_message,
)
from app.core.config import MLLMGenerationSettings, get_settings
from app.infrastructure.embedding import (
    EmbeddingClient,
    EmbeddingCompletion,
)
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

_MAXIMUM_COMPLETION_TOKENS = 4096
_MAXIMUM_PROPOSAL_ROUNDS = 4
_QUESTION_VECTOR_FUSION_VERSION = "retrieval-question-mean-l2-v1"


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """仅在至少一轮返回指标时汇总模型用量。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _tool_message(
    call: MLLMToolCall,
    feedback: QuestionGenerationToolFeedback,
) -> dict[str, str]:
    """把最小工具反馈写入当前问题会话。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _validate_evidence_pages(
    arguments: ProposeQuestionArguments,
    *,
    page_count: int,
) -> QuestionGenerationToolFeedback | None:
    """拒绝超出当前 PDF 范围的选题证据页码。"""
    invalid = sorted(
        {
            item.page_number
            for item in arguments.evidence
            if not 1 <= item.page_number <= page_count
        }
    )
    if not invalid:
        return None
    return QuestionGenerationToolFeedback(
        ok=False,
        message=(
            f"evidence.page_number：引用了合同范围外的页码 {invalid}，"
            f"本合同只有 1-{page_count} 页；请核对页面标签后重新提交。"
        ),
    )


def assemble_question_generation_context(
    state: QuestionGenerationSubgraphState,
) -> QuestionGenerationSubgraphState:
    """把启动期指南对象渲染到最终合同公共前缀尾部。"""
    prefill_context = state["prefill_context"]
    guide_catalog = state["retrieval_view_guide_catalog"]
    maximum_questions = get_settings().retrieval_view_max_questions
    messages = build_question_generation_messages(
        prefill_context.messages,
        guide_catalog=guide_catalog,
    )
    return {
        "question_generation_context": QuestionGenerationContext(
            document_id=prefill_context.document_id,
            prompt_version=QUESTION_GENERATION_CONTEXT_VERSION,
            guide_catalog_sha256=guide_catalog.question.content_sha256,
            maximum_questions=maximum_questions,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
        )
    }


def _proposal_result_values(
    audits: list[QuestionGenerationToolCallAudit],
) -> dict[str, int | None]:
    """汇总一份问题规划会话的 token 指标。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(audit.completion_tokens for audit in audits),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _failed_proposal(
    *,
    focus_id: str,
    focus_order: int,
    started_at: float,
    audits: list[QuestionGenerationToolCallAudit],
    error: str,
) -> FailedQuestionProposal:
    """构造保留独立工具审计的单规划失败结果。"""
    return FailedQuestionProposal(
        focus_id=focus_id,
        focus_order=focus_order,
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        tool_calls=tuple(audits),
        error=error,
        **_proposal_result_values(audits),
    )


async def _generate_one_question_from_plan(
    *,
    focus: GeneratedQuestionFocus,
    prepared_pdf: PreparedPDF,
    guide_catalog: RetrievalViewGuideCatalog,
    context: QuestionProposalContext,
    client: MLLMClient,
    semaphore: asyncio.Semaphore,
    generation: MLLMGenerationSettings,
) -> QuestionProposalOutcome:
    """为一份可组合关注点的问题规划维护隔离的纠错会话。"""
    focus_id = focus.focus_id
    focus_order = focus.order
    started_at = perf_counter()
    messages = append_question_plan_target(
        context.messages,
        guide_catalog=guide_catalog,
        focus=focus,
    )
    audits: list[QuestionGenerationToolCallAudit] = []
    protocol_recovery = ToolProtocolRecovery()

    for round_number in range(1, _MAXIMUM_PROPOSAL_ROUNDS + 1):
        request_started_at = perf_counter()
        try:
            async with semaphore:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=[PROPOSE_QUESTION_TOOL],
                    tool_choice=QUESTION_GENERATION_TOOL_CHOICE,
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
                    tool_placement=QUESTION_PROPOSAL_TOOL_PLACEMENT,
                )
        except (MLLMRequestError, MLLMUnavailableError) as exc:
            return _failed_proposal(
                focus_id=focus_id,
                focus_order=focus_order,
                started_at=started_at,
                audits=audits,
                error=str(exc),
            )

        elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
        completion = response.completion
        temporary_failure_memory_cleared = False

        if len(response.tool_calls) != 1:
            feedback = QuestionGenerationToolFeedback(
                ok=False,
                message=build_protocol_recovery_message(
                    tool_call_count=len(response.tool_calls),
                    result_label="正式问题",
                )["content"],
            )
            audits.append(
                QuestionGenerationToolCallAudit(
                    round_number=round_number,
                    focus_id=focus_id,
                    call_id=None,
                    name="protocol_recovery",
                    raw_arguments="",
                    assistant_content=audited_assistant_content(
                        response.assistant_message.get("content")
                    ),
                    feedback=feedback,
                    accepted_question_count=0,
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
                result_label="正式问题",
            )
            if exceeded:
                return _failed_proposal(
                    focus_id=focus_id,
                    focus_order=focus_order,
                    started_at=started_at,
                    audits=audits,
                    error="连续三轮未生成且仅生成一个合法 propose_question 调用。",
                )
            continue

        call = response.tool_calls[0]
        protocol_recovery.accept_protocol()
        accepted_question: GeneratedQuestion | None = None
        try:
            if call.name != "propose_question":
                raise ValueError(
                    f"tool：当前没有提供 {call.name}；请调用 propose_question"
                )
            arguments = parse_question_generation_tool_arguments(
                call.name,
                call.arguments,
            )
        except (ValueError, ValidationError) as exc:
            feedback = validation_error_feedback(exc)
        else:
            if not isinstance(arguments, ProposeQuestionArguments):
                feedback = QuestionGenerationToolFeedback(
                    ok=False,
                    message=(
                        "tool：当前只接受 propose_question；"
                        "请按证据、推理摘要和正式问题重新提交。"
                    ),
                )
            elif (
                page_error := _validate_evidence_pages(
                    arguments,
                    page_count=prepared_pdf.page_count,
                )
            ) is not None:
                feedback = page_error
            else:
                accepted_question = build_generated_question(
                    arguments,
                    order=focus_order,
                    focus_id=focus_id,
                    attention_codes=focus.attention_codes,
                )
                feedback = QuestionGenerationToolFeedback(
                    ok=True,
                    message=(
                        f"已记录 {accepted_question.question_id}，"
                        "当前问题规划处理完成。"
                    ),
                )

        tool_message = _tool_message(call, feedback)
        if accepted_question is not None:
            temporary_failure_memory_cleared = (
                protocol_recovery.memory_start is not None
            )
            protocol_recovery.accept_correction(messages)
        else:
            protocol_recovery.record_tool_failure(
                messages,
                assistant_message=response.assistant_message,
                tool_message=tool_message,
            )

        audits.append(
            QuestionGenerationToolCallAudit(
                round_number=round_number,
                focus_id=focus_id,
                call_id=call.call_id,
                name=call.name,
                raw_arguments=call.arguments,
                assistant_content=audited_assistant_content(
                    response.assistant_message.get("content")
                ),
                feedback=feedback,
                accepted_question_count=(1 if accepted_question else 0),
                temporary_failure_memory_cleared=(temporary_failure_memory_cleared),
                elapsed_ms=elapsed_ms,
                response_id=completion.response_id,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                cached_tokens=completion.cached_tokens,
            )
        )
        if accepted_question is not None:
            return GeneratedQuestionProposal(
                focus_id=focus_id,
                focus_order=focus_order,
                rounds=len(audits),
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                tool_calls=tuple(audits),
                question=accepted_question,
                **_proposal_result_values(audits),
            )

    return _failed_proposal(
        focus_id=focus_id,
        focus_order=focus_order,
        started_at=started_at,
        audits=audits,
        error=(f"达到最大轮次 {_MAXIMUM_PROPOSAL_ROUNDS}，仍未生成合法正式问题。"),
    )


async def generate_questions_from_plans(
    state: QuestionGenerationSubgraphState,
) -> QuestionGenerationSubgraphState:
    """从每份问题规划精确选取指南，并发生成一一对应的正式问题。"""
    prepared_pdf = state["prepared_pdf"]
    prefill_context = state["prefill_context"]
    guide_catalog = state["retrieval_view_guide_catalog"]
    discovery = state["question_focus_discovery"]
    if discovery.status != "completed":
        raise ValueError("问题规划发现未成功，不能生成正式问题")
    if (
        len(
            {
                prepared_pdf.document_id,
                prefill_context.document_id,
                discovery.document_id,
            }
        )
        != 1
    ):
        raise ValueError("正式问题生成输入的 document_id 不一致")

    common_messages = build_question_proposal_common_messages(prefill_context)
    context = QuestionProposalContext(
        document_id=prefill_context.document_id,
        common_prompt_version=QUESTION_PROPOSAL_COMMON_PROMPT_VERSION,
        target_prompt_version=QUESTION_PROPOSAL_TARGET_PROMPT_VERSION,
        tool_version=QUESTION_GENERATION_TOOL_VERSION,
        messages=tuple(common_messages),
        prefix_sha256=context_sha256(common_messages),
    )
    settings = get_settings().mllm
    started_at = perf_counter()
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    progress = ParallelProgressTracker(len(discovery.focuses))
    await progress.report_counted()

    async with MLLMClient(settings) as client:
        outcomes = tuple(
            await asyncio.gather(
                *(
                    progress.track(
                        _generate_one_question_from_plan(
                            focus=focus,
                            prepared_pdf=prepared_pdf,
                            guide_catalog=guide_catalog,
                            context=context,
                            client=client,
                            semaphore=semaphore,
                            generation=settings.generation,
                        )
                    )
                    for focus in discovery.focuses
                )
            )
        )

    generated = tuple(
        outcome
        for outcome in outcomes
        if isinstance(outcome, GeneratedQuestionProposal)
    )
    failures = tuple(
        outcome for outcome in outcomes if isinstance(outcome, FailedQuestionProposal)
    )
    questions = tuple(item.question for item in generated)
    audits = [audit for outcome in outcomes for audit in outcome.tool_calls]
    if not failures:
        status = "completed"
        error = None
    elif generated:
        status = "partial"
        error = "; ".join(f"{item.focus_id}: {item.error}" for item in failures)
    else:
        status = "failed"
        error = (
            "; ".join(f"{item.focus_id}: {item.error}" for item in failures)
            or "问题规划目录为空，未生成正式问题。"
        )

    result = RetrievalQuestionGenerationResult(
        status=status,
        document_id=context.document_id,
        model=settings.model,
        prompt_version=QUESTION_PROPOSAL_TARGET_PROMPT_VERSION,
        tool_version=QUESTION_GENERATION_TOOL_VERSION,
        questions=questions,
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        prompt_tokens=_sum_optional(audit.prompt_tokens for audit in audits),
        completion_tokens=_sum_optional(audit.completion_tokens for audit in audits),
        cached_tokens=_sum_optional(audit.cached_tokens for audit in audits),
        tool_calls=tuple(audits),
        error=error,
    )
    return {
        "question_proposal_context": context,
        "question_proposals": outcomes,
        "retrieval_questions": result,
    }


def _normalize_embedding(
    vector: tuple[float, ...],
    *,
    expected_dimensions: int,
    normalize: bool,
) -> tuple[float, ...]:
    """校验向量边界，并按配置执行确定性的 L2 归一化。"""
    if len(vector) != expected_dimensions:
        raise ValueError(
            "Embedding 向量维度不匹配："
            f"expected={expected_dimensions}, actual={len(vector)}"
        )
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("Embedding 向量包含非有限数值")
    if not normalize:
        return vector
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm == 0:
        raise ValueError("Embedding 向量不能是零向量")
    return tuple(value / norm for value in vector)


async def _embed_question_batch(
    *,
    questions: tuple[GeneratedQuestion, ...],
    client: EmbeddingClient,
    semaphore: asyncio.Semaphore,
) -> tuple[tuple[GeneratedQuestion, ...], EmbeddingCompletion]:
    """在共享连接与并发额度内提交一个问题批次。"""
    inputs = [
        render_contract_question_embedding_input(question.question)
        for question in questions
    ]
    async with semaphore:
        completion = await client.create_embeddings(inputs=inputs)
    return questions, completion


async def embed_questions(
    state: QuestionGenerationSubgraphState,
) -> QuestionGenerationSubgraphState:
    """批量并发向量化正式问题，并保持问题身份和原始顺序。"""
    source = state["retrieval_questions"]
    settings = get_settings().embedding
    started_at = perf_counter()
    questions = source.questions

    if not questions:
        status = "failed" if source.status == "failed" else source.status
        return {
            "retrieval_question_embeddings": RetrievalQuestionEmbeddingResult(
                status=status,
                document_id=source.document_id,
                model=settings.model,
                prompt_version=RETRIEVAL_EMBEDDING_PROMPT_VERSION,
                dimensions=settings.dimensions,
                normalized=settings.normalize,
                embeddings=(),
                failed_question_ids=(),
                request_count=0,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                prompt_tokens=0,
                error=source.error,
            )
        }

    batches = tuple(
        questions[index : index + settings.batch_size]
        for index in range(0, len(questions), settings.batch_size)
    )
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    async with EmbeddingClient(settings) as client:
        outcomes = await asyncio.gather(
            *(
                _embed_question_batch(
                    questions=batch,
                    client=client,
                    semaphore=semaphore,
                )
                for batch in batches
            ),
            return_exceptions=True,
        )

    embeddings_by_id: dict[str, RetrievalQuestionEmbedding] = {}
    failed_question_ids: list[str] = []
    errors: list[str] = []
    prompt_tokens: list[int] = []
    response_models: list[str] = []
    for batch, outcome in zip(batches, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            # 工作流取消必须向上传播，不能伪装成可恢复的单批服务失败。
            raise outcome
        if isinstance(outcome, BaseException):
            failed_question_ids.extend(question.question_id for question in batch)
            errors.append(f"{batch[0].question_id}..{batch[-1].question_id}: {outcome}")
            continue

        completed_questions, completion = outcome
        try:
            normalized_vectors = tuple(
                _normalize_embedding(
                    vector,
                    expected_dimensions=settings.dimensions,
                    normalize=settings.normalize,
                )
                for vector in completion.vectors
            )
            if len(normalized_vectors) != len(completed_questions):
                raise ValueError(
                    "Embedding 响应向量数量与问题数量不一致："
                    f"expected={len(completed_questions)}, "
                    f"actual={len(normalized_vectors)}"
                )
        except ValueError as exc:
            failed_question_ids.extend(
                question.question_id for question in completed_questions
            )
            errors.append(
                f"{completed_questions[0].question_id}.."
                f"{completed_questions[-1].question_id}: {exc}"
            )
            continue

        for question, vector in zip(
            completed_questions,
            normalized_vectors,
            strict=True,
        ):
            embeddings_by_id[question.question_id] = RetrievalQuestionEmbedding(
                question_id=question.question_id,
                order=question.order,
                vector=vector,
            )
        if completion.prompt_tokens is not None:
            prompt_tokens.append(completion.prompt_tokens)
        if completion.model:
            response_models.append(completion.model)

    embeddings = tuple(
        embeddings_by_id[question.question_id]
        for question in questions
        if question.question_id in embeddings_by_id
    )
    if not embeddings:
        status = "failed"
    elif failed_question_ids or source.status == "partial":
        status = "partial"
    else:
        status = "completed"
    error_parts = [part for part in (source.error, *errors) if part]
    return {
        "retrieval_question_embeddings": RetrievalQuestionEmbeddingResult(
            status=status,
            document_id=source.document_id,
            model=response_models[0] if response_models else settings.model,
            prompt_version=RETRIEVAL_EMBEDDING_PROMPT_VERSION,
            dimensions=settings.dimensions,
            normalized=settings.normalize,
            embeddings=embeddings,
            failed_question_ids=tuple(failed_question_ids),
            request_count=len(batches),
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
            prompt_tokens=(
                sum(prompt_tokens) if len(prompt_tokens) == len(batches) else None
            ),
            error="; ".join(error_parts) or None,
        )
    }


def fuse_question_embeddings(
    state: QuestionGenerationSubgraphState,
) -> QuestionGenerationSubgraphState:
    """将成功问题向量取算术平均并再次归一化为合同级向量。"""
    source = state["retrieval_question_embeddings"]
    started_at = perf_counter()
    source_question_ids = tuple(item.question_id for item in source.embeddings)
    source_embedding_count = len(source.embeddings)

    def build_result(
        *,
        status: str,
        vector: tuple[float, ...] | None,
        error: str | None,
    ) -> ContractRetrievalVectorResult:
        """用统一元数据构造合同级融合结果。"""
        return ContractRetrievalVectorResult.model_validate(
            {
                "status": status,
                "document_id": source.document_id,
                "fusion_version": _QUESTION_VECTOR_FUSION_VERSION,
                "fusion_method": "arithmetic_mean_l2_normalized",
                "embedding_model": source.model,
                "embedding_prompt_version": source.prompt_version,
                "dimensions": source.dimensions,
                "normalized": True,
                "source_question_ids": source_question_ids,
                "source_embedding_count": source_embedding_count,
                "vector": vector,
                "elapsed_ms": round((perf_counter() - started_at) * 1000, 3),
                "error": error,
            }
        )

    if not source.embeddings:
        return {
            "contract_retrieval_vector": build_result(
                status="failed",
                vector=None,
                error=source.error or "没有可用于融合的成功问题向量。",
            )
        }

    try:
        for item in source.embeddings:
            _normalize_embedding(
                item.vector,
                expected_dimensions=source.dimensions,
                normalize=False,
            )
        mean_vector = tuple(
            math.fsum(item.vector[dimension] for item in source.embeddings)
            / source_embedding_count
            for dimension in range(source.dimensions)
        )
        vector = _normalize_embedding(
            mean_vector,
            expected_dimensions=source.dimensions,
            normalize=True,
        )
    except (IndexError, ValueError) as exc:
        return {
            "contract_retrieval_vector": build_result(
                status="failed",
                vector=None,
                error=f"合同问题向量融合失败：{exc}",
            )
        }

    return {
        "contract_retrieval_vector": build_result(
            status="completed" if source.status == "completed" else "partial",
            vector=vector,
            error=source.error,
        )
    }


__all__ = [
    "assemble_question_generation_context",
    "embed_questions",
    "fuse_question_embeddings",
    "generate_questions_from_plans",
]
