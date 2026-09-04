"""PDF 逐候选判重的分流与处理节点。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal

from pydantic import ValidationError

from app.agent.contract_extraction.state import PreparedPDF, PreparedPDFPage
from app.agent.contract_extraction.tool_protocol import (
    ToolProtocolRecovery,
    audited_assistant_content,
    build_protocol_recovery_message,
)
from app.agent.pdf_deduplication.prompt import (
    FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION,
    FULL_DOCUMENT_TOOL_PLACEMENT,
    PAGE_NAVIGATION_JUDGMENT_PROMPT_VERSION,
    PAGE_NAVIGATION_TOOL_PLACEMENT,
    append_page_navigation_round_context,
    build_full_document_judgment_messages,
    build_page_navigation_judgment_messages,
)
from app.agent.pdf_deduplication.state import (
    DifferentPDFCandidate,
    DuplicatePDFCandidate,
    FailedPDFCandidateJudgment,
    PDFCandidateToolCallAudit,
    PDFCandidateToolFeedback,
    PDFDuplicateEvidence,
    SimilarPDFCandidate,
)
from app.agent.pdf_deduplication.subgraph.candidate_judgment.state import (
    PDFCandidateJudgmentState,
    PDFCandidateRoutingDecision,
    PDFCandidateRoutingReason,
)
from app.agent.pdf_deduplication.subgraph.candidate_judgment.tool import (
    FULL_DOCUMENT_JUDGMENT_TOOLS,
    FULL_DOCUMENT_JUDGMENT_TOOL_CHOICE,
    ContractRelationEvidence,
    ReportUnableToDetermineRelationArguments,
    SubmitContractRelationArguments,
    ThinkArguments,
    parse_full_document_tool_arguments,
)
from app.agent.pdf_deduplication.subgraph.candidate_judgment.navigation_tool import (
    PAGE_NAVIGATION_JUDGMENT_TOOLS,
    PAGE_NAVIGATION_JUDGMENT_TOOL_CHOICE,
    InspectCandidatePagesArguments,
    RecordCandidatePageObservationsArguments,
    parse_page_navigation_tool_arguments,
)
from app.core.config import get_settings
from app.infrastructure.mllm import (
    MLLMClient,
    MLLMRequestError,
    MLLMToolCall,
    MLLMUnavailableError,
)

FULL_DOCUMENT_NODE = "judge_full_documents"
PAGE_NAVIGATION_AGENT_NODE = "judge_with_page_navigation_agent"

_FULL_DOCUMENT_MAXIMUM_ROUNDS = 8
_FULL_DOCUMENT_MAXIMUM_COMPLETION_TOKENS = 4096
_FULL_DOCUMENT_MAXIMUM_CONSECUTIVE_THINKS = 2
_FULL_DOCUMENT_THINK_MAXIMUM_TOKENS = 1024

_PAGE_NAVIGATION_MAXIMUM_ROUNDS = 24
_PAGE_NAVIGATION_MAXIMUM_COMPLETION_TOKENS = 4096
_PAGE_NAVIGATION_MAXIMUM_CONSECUTIVE_THINKS = 2
_PAGE_NAVIGATION_THINK_MAXIMUM_TOKENS = 1024
_PAGE_NAVIGATION_MAXIMUM_INSPECTIONS = 6
_PAGE_NAVIGATION_MAXIMUM_UNIQUE_PAGES = 12


@dataclass(slots=True)
class _PageNavigationWorkspace:
    """单个候选会话的页面状态与已校验观察。"""

    candidate_document_id: str
    page_count: int
    visible_page_numbers: tuple[int, ...] = ()
    hidden_page_numbers: set[int] = field(default_factory=set)
    view_counts: dict[int, int] = field(default_factory=dict)
    observations: list[PDFDuplicateEvidence] = field(default_factory=list)
    next_focus: str = "先查看候选合同 B 的合同身份页和文件边界页。"
    inspection_count: int = 0

    @property
    def viewed_page_numbers(self) -> set[int]:
        return set(self.view_counts)

    def render(self) -> dict[str, Any]:
        """形成字段顺序和列表顺序稳定的模型可见工作区。"""
        page_states = [
            {
                "page_number": page_number,
                "status": (
                    "visible"
                    if page_number in self.visible_page_numbers
                    else "hidden"
                ),
                "view_count": self.view_counts[page_number],
            }
            for page_number in sorted(self.view_counts)
        ]
        observations = [
            {
                "observation_id": f"obs-{index:03d}",
                **observation.model_dump(mode="json"),
            }
            for index, observation in enumerate(self.observations, start=1)
        ]
        return {
            "workspace_version": "candidate-page-navigation-workspace-v1",
            "candidate_document_id": self.candidate_document_id,
            "candidate_page_count": self.page_count,
            "page_states": page_states,
            "hidden_page_numbers": sorted(self.hidden_page_numbers),
            "current_visible_page_numbers": list(self.visible_page_numbers),
            "accepted_observations": observations,
            "next_focus": self.next_focus,
            "budget": {
                "inspection_count": self.inspection_count,
                "maximum_inspections": _PAGE_NAVIGATION_MAXIMUM_INSPECTIONS,
                "unique_page_count": len(self.viewed_page_numbers),
                "maximum_unique_pages": _PAGE_NAVIGATION_MAXIMUM_UNIQUE_PAGES,
                "remaining_inspections": max(
                    0,
                    _PAGE_NAVIGATION_MAXIMUM_INSPECTIONS
                    - self.inspection_count,
                ),
                "remaining_unique_pages": max(
                    0,
                    _PAGE_NAVIGATION_MAXIMUM_UNIQUE_PAGES
                    - len(self.viewed_page_numbers),
                ),
            },
            "current_action": (
                "核对当前可见 B 页面并记录观察，或在证据充分时提交终态。"
                if self.visible_page_numbers
                else "按 next_focus 查看下一批 B 页面，或在证据充分时提交终态。"
            ),
        }


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """只在至少一次模型响应返回用量时汇总 token。"""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _runtime_values(
    audits: list[PDFCandidateToolCallAudit],
) -> dict[str, int | None]:
    """汇总单候选全部模型轮次的推理用量。"""
    return {
        "prompt_tokens": _sum_optional(audit.prompt_tokens for audit in audits),
        "completion_tokens": _sum_optional(
            audit.completion_tokens for audit in audits
        ),
        "cached_tokens": _sum_optional(audit.cached_tokens for audit in audits),
    }


def _tool_message(
    call: MLLMToolCall,
    feedback: PDFCandidateToolFeedback,
) -> dict[str, str]:
    """把最小动作反馈写入当前候选独占的短期记忆。"""
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": feedback.model_dump_json(),
    }


def _validation_feedback(error: Exception) -> PDFCandidateToolFeedback:
    """把工具参数错误压缩为位置明确的有限纠错反馈。"""
    if not isinstance(error, ValidationError):
        return PDFCandidateToolFeedback(
            ok=False,
            message=f"arguments：{error}；请按当前工具参数定义修正后重新调用。",
        )

    messages: list[str] = []
    errors = error.errors(include_url=False)
    for item in errors[:3]:
        path = ".".join(str(part) for part in item["loc"]) or "arguments"
        problem = str(item["msg"]).removeprefix("Value error, ")
        error_type = item["type"]
        if error_type == "missing":
            correction = "补充该必填参数"
        elif error_type == "extra_forbidden":
            correction = "删除该未定义参数"
        else:
            correction = "按工具 Schema 提交正确类型和有效取值"
        messages.append(f"{path}：{problem}；请{correction}。")
    if len(errors) > 3:
        messages.append("其余参数请一并按工具 Schema 检查。")
    return PDFCandidateToolFeedback(ok=False, message="\n".join(messages))


def _validate_relation_evidence_pages(
    evidence: Iterable[ContractRelationEvidence],
    *,
    uploaded_page_count: int,
    candidate_page_count: int,
) -> PDFCandidateToolFeedback | None:
    """分别校验 A、B 证据页码，避免两份 PDF 的物理页范围混用。"""
    items = tuple(evidence)
    invalid_uploaded = sorted(
        {
            item.uploaded_page_number
            for item in items
            if not 1 <= item.uploaded_page_number <= uploaded_page_count
        }
    )
    invalid_candidate = sorted(
        {
            item.candidate_page_number
            for item in items
            if not 1 <= item.candidate_page_number <= candidate_page_count
        }
    )
    if not invalid_uploaded and not invalid_candidate:
        return None

    problems: list[str] = []
    if invalid_uploaded:
        problems.append(
            f"evidence.uploaded_page_number 超出 A 的 1-{uploaded_page_count} 页："
            f"{invalid_uploaded}"
        )
    if invalid_candidate:
        problems.append(
            f"evidence.candidate_page_number 超出 B 的 1-{candidate_page_count} 页："
            f"{invalid_candidate}"
        )
    return PDFCandidateToolFeedback(
        ok=False,
        message="；".join(problems) + "；请核对页面标签后重新提交。",
    )


def _failed_full_document_judgment(
    state: PDFCandidateJudgmentState,
    *,
    started_at: float,
    audits: list[PDFCandidateToolCallAudit],
    model: str,
    error: str,
) -> PDFCandidateJudgmentState:
    """形成保留全部私有审计、但不暴露半成品关系的失败终态。"""
    candidate = state["candidate"]
    judgment = FailedPDFCandidateJudgment(
        candidate_document_id=candidate.document_id,
        rank=candidate.rank,
        model=model,
        prompt_version=FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION,
        rounds=len(audits),
        elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
        tool_calls=tuple(audits),
        error=error,
        **_runtime_values(audits),
    )
    return {**state, "judgment": judgment}


def _validate_routing_pdf(label: str, pdf: PreparedPDF) -> None:
    """拒绝页面数量或视觉 token 汇总不可信的 PreparedPDF。"""
    if len(pdf.pages) != pdf.page_count:
        raise ValueError(f"{label} PreparedPDF 页面数量与 page_count 不一致")
    actual_visual_tokens = sum(page.visual_tokens for page in pdf.pages)
    if actual_visual_tokens != pdf.total_visual_tokens:
        raise ValueError(f"{label} PreparedPDF 页面视觉 token 汇总不一致")


def decide_candidate_judgment_route(
    state: PDFCandidateJudgmentState,
) -> PDFCandidateJudgmentState:
    """根据一对 PDF 的真实视觉 token 和合计页数形成分流决定。"""
    uploaded = state["uploaded_pdf"]
    candidate_pdf = state["candidate_pdf"]
    candidate = state["candidate"]
    _validate_routing_pdf("上传", uploaded)
    _validate_routing_pdf("候选", candidate_pdf)
    if candidate_pdf.document_id != candidate.document_id:
        raise ValueError("已加载候选 PDF 的 document_id 与 ES 候选不一致")
    if candidate_pdf.page_count != candidate.page_count:
        raise ValueError("已加载候选 PDF 的页数与 ES 候选不一致")

    settings = get_settings()
    routing = settings.pdf_deduplication
    combined_visual_tokens = (
        uploaded.total_visual_tokens + candidate_pdf.total_visual_tokens
    )
    # visual_token_ceiling 已扣除生成、提示词和多轮运行预留；比例限制继续
    # 留出注意力余量，避免“技术上装得下”被误认为“适合全量比较”。
    visual_token_limit = max(
        1,
        int(
            settings.mllm.visual_token_ceiling
            * routing.single_shot_visual_token_ratio
        ),
    )
    combined_page_count = uploaded.page_count + candidate_pdf.page_count
    token_exceeded = combined_visual_tokens > visual_token_limit
    page_exceeded = (
        combined_page_count > routing.single_shot_max_total_pages
    )
    reason: PDFCandidateRoutingReason
    if token_exceeded and page_exceeded:
        reason = "visual_token_and_page_limits_exceeded"
    elif token_exceeded:
        reason = "visual_token_limit_exceeded"
    elif page_exceeded:
        reason = "page_limit_exceeded"
    else:
        reason = "within_single_shot_limits"

    decision = PDFCandidateRoutingDecision(
        uploaded_document_id=uploaded.document_id,
        candidate_document_id=candidate.document_id,
        strategy=(
            "full_document"
            if reason == "within_single_shot_limits"
            else "page_navigation_agent"
        ),
        reason=reason,
        combined_visual_tokens=combined_visual_tokens,
        single_shot_visual_token_limit=visual_token_limit,
        combined_page_count=combined_page_count,
        single_shot_max_total_pages=routing.single_shot_max_total_pages,
    )
    return {**state, "routing_decision": decision}


def route_candidate_judgment(
    state: PDFCandidateJudgmentState,
) -> Literal["judge_full_documents", "judge_with_page_navigation_agent"]:
    """把已形成的决定映射到 LangGraph 条件边。"""
    strategy = state["routing_decision"].strategy
    if strategy == "full_document":
        return FULL_DOCUMENT_NODE
    return PAGE_NAVIGATION_AGENT_NODE


async def judge_full_documents(
    state: PDFCandidateJudgmentState,
) -> PDFCandidateJudgmentState:
    """每轮向 MLLM 提供两份 PDF 的全部页面并形成有限多轮判断。"""
    started_at = perf_counter()
    uploaded_pdf = state["uploaded_pdf"]
    candidate_pdf = state["candidate_pdf"]
    candidate = state["candidate"]
    routing_decision = state["routing_decision"]
    _validate_routing_pdf("上传", uploaded_pdf)
    _validate_routing_pdf("候选", candidate_pdf)
    if routing_decision.strategy != "full_document":
        raise ValueError("非 full_document 路由不能执行全量双 PDF 判断")
    if routing_decision.uploaded_document_id != uploaded_pdf.document_id:
        raise ValueError("全量判断路由的上传 document_id 与 PDF 不一致")
    if routing_decision.candidate_document_id != candidate_pdf.document_id:
        raise ValueError("全量判断路由的候选 document_id 与 PDF 不一致")
    if candidate.document_id != candidate_pdf.document_id:
        raise ValueError("全量判断候选身份与已加载 PDF 不一致")

    messages = build_full_document_judgment_messages(
        uploaded_pdf,
        candidate_pdf,
    )
    settings = get_settings().mllm
    generation = settings.generation
    audits: list[PDFCandidateToolCallAudit] = []
    protocol_recovery = ToolProtocolRecovery()
    consecutive_thinks = 0
    accepted_thinks = 0
    response_model = settings.model

    async with MLLMClient(settings) as client:
        for round_number in range(1, _FULL_DOCUMENT_MAXIMUM_ROUNDS + 1):
            request_started_at = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(FULL_DOCUMENT_JUDGMENT_TOOLS),
                    tool_choice=FULL_DOCUMENT_JUDGMENT_TOOL_CHOICE,
                    max_completion_tokens=min(
                        generation.max_completion_tokens,
                        _FULL_DOCUMENT_MAXIMUM_COMPLETION_TOKENS,
                    ),
                    temperature=generation.temperature,
                    top_p=generation.top_p,
                    top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,
                    # 显式 think 工具承担可审计推理，不启用模型私有思考块。
                    enable_thinking=False,
                    tool_placement=FULL_DOCUMENT_TOOL_PLACEMENT,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                return _failed_full_document_judgment(
                    state,
                    started_at=started_at,
                    audits=audits,
                    model=response_model,
                    error=str(exc),
                )

            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            completion = response.completion
            response_model = completion.model or response_model
            if len(response.tool_calls) != 1:
                recovery_message = build_protocol_recovery_message(
                    tool_call_count=len(response.tool_calls),
                    result_label="合同关系判断结果",
                )["content"]
                feedback = PDFCandidateToolFeedback(
                    ok=False,
                    message=recovery_message,
                )
                audits.append(
                    PDFCandidateToolCallAudit(
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
                exceeded = protocol_recovery.record_protocol_failure(
                    messages,
                    assistant_message=response.assistant_message,
                    tool_call_count=len(response.tool_calls),
                    result_label="合同关系判断结果",
                )
                if exceeded:
                    return _failed_full_document_judgment(
                        state,
                        started_at=started_at,
                        audits=audits,
                        model=response_model,
                        error="连续三轮未生成且仅生成一个合法工具调用。",
                    )
                continue

            call = response.tool_calls[0]
            protocol_recovery.accept_protocol()
            accepted_submit: SubmitContractRelationArguments | None = None
            accepted_unable: ReportUnableToDetermineRelationArguments | None = None
            arguments: (
                ThinkArguments
                | SubmitContractRelationArguments
                | ReportUnableToDetermineRelationArguments
                | None
            ) = None
            assistant_content = audited_assistant_content(
                response.assistant_message.get("content")
            )
            if assistant_content is not None:
                feedback = PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        "assistant.content：工具调用之外不得输出普通文本；"
                        "请只调用一个当前工具且调用后不要追加说明。"
                    ),
                )
            else:
                try:
                    arguments = parse_full_document_tool_arguments(
                        call.name,
                        call.arguments,
                    )
                except (ValueError, ValidationError) as exc:
                    feedback = _validation_feedback(exc)

            if arguments is not None:
                if isinstance(arguments, ThinkArguments):
                    completion_tokens = completion.completion_tokens
                    if completion_tokens is None:
                        feedback = PDFCandidateToolFeedback(
                            ok=False,
                            message=(
                                "reasoning：本轮响应没有返回 completion_tokens，"
                                "无法验证 think 的 1024 completion tokens 上限；"
                                "请重新简短思考。"
                            ),
                        )
                    elif completion_tokens > _FULL_DOCUMENT_THINK_MAXIMUM_TOKENS:
                        feedback = PDFCandidateToolFeedback(
                            ok=False,
                            message=(
                                f"reasoning：本轮完整工具响应使用 {completion_tokens} "
                                "completion tokens，超过 think 的 1024 tokens 上限；"
                                "请压缩推理后重新调用 think，或直接提交终止决定。"
                            ),
                        )
                    elif (
                        consecutive_thinks
                        >= _FULL_DOCUMENT_MAXIMUM_CONSECUTIVE_THINKS
                    ):
                        feedback = PDFCandidateToolFeedback(
                            ok=False,
                            message=(
                                "reasoning：已经连续完成两次 think；"
                                "请根据现有证据调用 submit_contract_relation，"
                                "或在满足退出条件时调用 "
                                "report_unable_to_determine_relation。"
                            ),
                        )
                    else:
                        consecutive_thinks += 1
                        accepted_thinks += 1
                        feedback = PDFCandidateToolFeedback(
                            ok=True,
                            message="思考已记录，请继续判断。",
                        )
                elif isinstance(arguments, SubmitContractRelationArguments):
                    page_error = _validate_relation_evidence_pages(
                        arguments.evidence,
                        uploaded_page_count=uploaded_pdf.page_count,
                        candidate_page_count=candidate_pdf.page_count,
                    )
                    if page_error is not None:
                        feedback = page_error
                    else:
                        consecutive_thinks = 0
                        accepted_submit = arguments
                        feedback = PDFCandidateToolFeedback(
                            ok=True,
                            message="合同关系决定已接受。",
                        )
                elif isinstance(
                    arguments,
                    ReportUnableToDetermineRelationArguments,
                ):
                    page_error = _validate_relation_evidence_pages(
                        arguments.evidence,
                        uploaded_page_count=uploaded_pdf.page_count,
                        candidate_page_count=candidate_pdf.page_count,
                    )
                    if accepted_thinks < 1:
                        feedback = PDFCandidateToolFeedback(
                            ok=False,
                            message=(
                                "report_unable_to_determine_relation："
                                "调用失败出口前必须至少完成一次有效 think，"
                                "核对材料为何无法支持三分类。"
                            ),
                        )
                    elif page_error is not None:
                        feedback = page_error
                    else:
                        consecutive_thinks = 0
                        accepted_unable = arguments
                        feedback = PDFCandidateToolFeedback(
                            ok=True,
                            message="无法判断出口已接受。",
                        )
                else:  # pragma: no cover - 联合参数类型已覆盖三项工具
                    feedback = PDFCandidateToolFeedback(
                        ok=False,
                        message=f"tool：当前不能调用 {call.name}。",
                    )

            tool_message = _tool_message(call, feedback)
            if feedback.ok:
                # 通过校验的动作清除此前整段失败轨迹；合法 think 历史保留。
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
                PDFCandidateToolCallAudit(
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

            runtime = _runtime_values(audits)
            common_values = {
                "candidate_document_id": candidate.document_id,
                "rank": candidate.rank,
                "model": response_model,
                "prompt_version": FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION,
                "rounds": round_number,
                "elapsed_ms": round((perf_counter() - started_at) * 1000, 3),
                "tool_calls": tuple(audits),
                **runtime,
            }
            if accepted_submit is not None:
                evidence = tuple(
                    PDFDuplicateEvidence(
                        uploaded_page_number=item.uploaded_page_number,
                        candidate_page_number=item.candidate_page_number,
                        observation=item.observation,
                    )
                    for item in accepted_submit.evidence
                )
                result_type = {
                    "duplicate": DuplicatePDFCandidate,
                    "similar": SimilarPDFCandidate,
                    "different": DifferentPDFCandidate,
                }[accepted_submit.decision]
                return {
                    **state,
                    "judgment": result_type(
                        evidence=evidence,
                        reasoning_summary=accepted_submit.reasoning_summary,
                        **common_values,
                    ),
                }
            if accepted_unable is not None:
                return {
                    **state,
                    "judgment": FailedPDFCandidateJudgment(
                        error=(
                            "模型无法可靠判断合同关系："
                            f"{accepted_unable.reason}；"
                            f"{accepted_unable.reasoning_summary}"
                        ),
                        **common_values,
                    ),
                }

    return _failed_full_document_judgment(
        state,
        started_at=started_at,
        audits=audits,
        model=response_model,
        error=(
            f"达到最大轮次 {_FULL_DOCUMENT_MAXIMUM_ROUNDS}，"
            "仍未形成有效合同关系终止决定。"
        ),
    )


async def judge_with_page_navigation_agent(
    state: PDFCandidateJudgmentState,
) -> PDFCandidateJudgmentState:
    """通过有界短期记忆和按需翻页形成长 PDF 判断。"""
    started_at = perf_counter()
    uploaded_pdf = state["uploaded_pdf"]
    candidate_pdf = state["candidate_pdf"]
    candidate = state["candidate"]
    routing_decision = state["routing_decision"]
    _validate_routing_pdf("上传", uploaded_pdf)
    _validate_routing_pdf("候选", candidate_pdf)
    if routing_decision.strategy != "page_navigation_agent":
        raise ValueError("非 page_navigation_agent 路由不能执行候选页面导航")
    if routing_decision.uploaded_document_id != uploaded_pdf.document_id:
        raise ValueError("页面导航路由的上传 document_id 与 PDF 不一致")
    if routing_decision.candidate_document_id != candidate_pdf.document_id:
        raise ValueError("页面导航路由的候选 document_id 与 PDF 不一致")
    if candidate.document_id != candidate_pdf.document_id:
        raise ValueError("页面导航候选身份与已加载 PDF 不一致")
    candidate_pages = {
        page.page_number: page
        for page in sorted(candidate_pdf.pages, key=lambda item: item.page_number)
    }
    if tuple(candidate_pages) != tuple(range(1, candidate_pdf.page_count + 1)):
        raise ValueError("候选 PreparedPDF 页面必须从 1 开始连续排列")

    settings = get_settings().mllm
    generation = settings.generation
    available_candidate_visual_tokens = (
        settings.visual_token_ceiling - uploaded_pdf.total_visual_tokens
    )
    if available_candidate_visual_tokens <= 0 or not any(
        page.visual_tokens <= available_candidate_visual_tokens
        for page in candidate_pages.values()
    ):
        return _failed_page_navigation_judgment(
            state,
            started_at=started_at,
            audits=[],
            model=settings.model,
            error=(
                "完整上传合同 A 已没有足够视觉预算加载任何候选合同 B 页面"
            ),
        )

    candidate_guide = _build_candidate_navigation_guide(
        candidate_pdf,
        available_candidate_visual_tokens=available_candidate_visual_tokens,
    )
    base_messages = build_page_navigation_judgment_messages(
        uploaded_pdf,
        candidate_guide,
    )
    workspace = _PageNavigationWorkspace(
        candidate_document_id=candidate.document_id,
        page_count=candidate_pdf.page_count,
    )
    audits: list[PDFCandidateToolCallAudit] = []
    short_term_memory: list[str] = []
    correction_memory: list[str] = []
    recovery_messages: list[dict[str, Any]] = []
    protocol_recovery = ToolProtocolRecovery()
    consecutive_thinks = 0
    accepted_thinks = 0
    response_model = settings.model

    async with MLLMClient(settings) as client:
        for round_number in range(1, _PAGE_NAVIGATION_MAXIMUM_ROUNDS + 1):
            visible_pages = tuple(
                candidate_pages[page_number]
                for page_number in workspace.visible_page_numbers
            )
            messages = append_page_navigation_round_context(
                base_messages,
                short_term_memory=short_term_memory,
                correction_memory=correction_memory,
                visible_candidate_pages=visible_pages,
                workspace=workspace.render(),
            )
            request_started_at = perf_counter()
            try:
                response = await client.create_tool_chat_completion(
                    messages=messages,
                    tools=list(PAGE_NAVIGATION_JUDGMENT_TOOLS),
                    tool_choice=PAGE_NAVIGATION_JUDGMENT_TOOL_CHOICE,
                    max_completion_tokens=min(
                        generation.max_completion_tokens,
                        _PAGE_NAVIGATION_MAXIMUM_COMPLETION_TOKENS,
                    ),
                    temperature=generation.temperature,
                    top_p=generation.top_p,
                    top_k=generation.top_k,
                    presence_penalty=generation.presence_penalty,
                    repetition_penalty=generation.repetition_penalty,
                    seed=generation.seed,
                    enable_thinking=False,
                    tool_placement=PAGE_NAVIGATION_TOOL_PLACEMENT,
                )
            except (MLLMRequestError, MLLMUnavailableError) as exc:
                return _failed_page_navigation_judgment(
                    state,
                    started_at=started_at,
                    audits=audits,
                    model=response_model,
                    error=str(exc),
                )

            elapsed_ms = round((perf_counter() - request_started_at) * 1000, 3)
            completion = response.completion
            response_model = completion.model or response_model
            if len(response.tool_calls) != 1:
                recovery_text = build_protocol_recovery_message(
                    tool_call_count=len(response.tool_calls),
                    result_label="候选合同页面导航动作或关系判断结果",
                )["content"]
                feedback = PDFCandidateToolFeedback(
                    ok=False,
                    message=recovery_text,
                )
                audits.append(
                    _navigation_audit(
                        round_number=round_number,
                        call=None,
                        name="protocol_recovery",
                        raw_arguments="",
                        assistant_content=audited_assistant_content(
                            response.assistant_message.get("content")
                        ),
                        feedback=feedback,
                        elapsed_ms=elapsed_ms,
                        completion=completion,
                    )
                )
                exceeded = protocol_recovery.record_protocol_failure(
                    recovery_messages,
                    assistant_message=response.assistant_message,
                    tool_call_count=len(response.tool_calls),
                    result_label="候选合同页面导航动作或关系判断结果",
                )
                correction_memory.append(recovery_text)
                if exceeded:
                    return _failed_page_navigation_judgment(
                        state,
                        started_at=started_at,
                        audits=audits,
                        model=response_model,
                        error="连续多轮未生成且仅生成一个合法页面导航工具调用。",
                    )
                continue

            call = response.tool_calls[0]
            protocol_recovery.accept_protocol()
            arguments: Any | None = None
            assistant_content = audited_assistant_content(
                response.assistant_message.get("content")
            )
            if assistant_content is not None:
                feedback = PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        "assistant.content：工具调用之外不得输出普通文本；"
                        "请只调用一个当前工具且调用后不要追加说明。"
                    ),
                )
            else:
                try:
                    arguments = parse_page_navigation_tool_arguments(
                        call.name,
                        call.arguments,
                    )
                except (ValueError, ValidationError) as exc:
                    feedback = _validation_feedback(exc)

            terminal: (
                SubmitContractRelationArguments
                | ReportUnableToDetermineRelationArguments
                | None
            ) = None
            if arguments is not None:
                feedback, terminal = _validate_and_apply_navigation_action(
                    arguments,
                    workspace=workspace,
                    uploaded_page_count=uploaded_pdf.page_count,
                    candidate_pages=candidate_pages,
                    available_candidate_visual_tokens=(
                        available_candidate_visual_tokens
                    ),
                    completion_tokens=completion.completion_tokens,
                    accepted_thinks=accepted_thinks,
                    consecutive_thinks=consecutive_thinks,
                )

            audits.append(
                _navigation_audit(
                    round_number=round_number,
                    call=call,
                    name=call.name,
                    raw_arguments=call.arguments,
                    assistant_content=assistant_content,
                    feedback=feedback,
                    elapsed_ms=elapsed_ms,
                    completion=completion,
                )
            )
            tool_message = _tool_message(call, feedback)
            if not feedback.ok:
                protocol_recovery.record_tool_failure(
                    recovery_messages,
                    assistant_message=response.assistant_message,
                    tool_message=tool_message,
                )
                correction_memory.append(feedback.message)
                continue

            protocol_recovery.accept_correction(recovery_messages)
            correction_memory.clear()
            if isinstance(arguments, ThinkArguments):
                consecutive_thinks += 1
                accepted_thinks += 1
                short_term_memory.append(f"有效 think：{arguments.reasoning}")
            else:
                consecutive_thinks = 0

            if isinstance(arguments, InspectCandidatePagesArguments):
                short_term_memory = [
                    "已打开候选合同 B 页面 "
                    f"{arguments.page_numbers}；当前图像位于工作区之前。"
                ]
                continue
            if isinstance(arguments, RecordCandidatePageObservationsArguments):
                short_term_memory = [
                    "上一批候选页面观察已经记录，页面当前已隐藏。"
                ]
                continue
            if terminal is not None:
                return _finish_page_navigation_judgment(
                    state,
                    started_at=started_at,
                    audits=audits,
                    model=response_model,
                    terminal=terminal,
                )

    return _failed_page_navigation_judgment(
        state,
        started_at=started_at,
        audits=audits,
        model=response_model,
        error=(
            f"达到最大轮次 {_PAGE_NAVIGATION_MAXIMUM_ROUNDS}，"
            "仍未形成有效合同关系终止决定。"
        ),
    )


def _build_candidate_navigation_guide(
    candidate_pdf: PreparedPDF,
    *,
    available_candidate_visual_tokens: int,
) -> dict[str, Any]:
    """从已加载处理版 PDF 形成不包含推测内容的确定性页面指南。"""
    page_count = candidate_pdf.page_count
    fallback_pages = sorted(
        {
            1,
            page_count,
            max(1, round(page_count * 0.25)),
            max(1, round(page_count * 0.5)),
            max(1, round(page_count * 0.75)),
        }
    )
    return {
        "guide_version": "candidate-document-guide-v1",
        "document_id": candidate_pdf.document_id,
        "page_count": page_count,
        "available_candidate_visual_tokens_per_request": (
            available_candidate_visual_tokens
        ),
        "deterministic_landmarks": {
            "first_page": 1,
            "last_page": page_count,
            "fallback_coverage_pages": fallback_pages,
        },
        "pages": [
            {
                "page_number": page.page_number,
                "width_pixels": page.width_pixels,
                "height_pixels": page.height_pixels,
                "orientation": (
                    "landscape"
                    if page.width_pixels > page.height_pixels
                    else "portrait"
                    if page.width_pixels < page.height_pixels
                    else "square"
                ),
                "visual_tokens": page.visual_tokens,
            }
            for page in sorted(
                candidate_pdf.pages,
                key=lambda item: item.page_number,
            )
        ],
        "authority": (
            "以上仅为候选文档的确定性页面元数据和位置提示；"
            "当前没有复核后 Core、条款页定位或自动页面摘要。"
        ),
    }


def _navigation_audit(
    *,
    round_number: int,
    call: MLLMToolCall | None,
    name: str,
    raw_arguments: str,
    assistant_content: str | None,
    feedback: PDFCandidateToolFeedback,
    elapsed_ms: float,
    completion: Any,
) -> PDFCandidateToolCallAudit:
    """统一形成页面导航单轮工具审计。"""
    return PDFCandidateToolCallAudit(
        round_number=round_number,
        call_id=call.call_id if call is not None else None,
        name=name,
        raw_arguments=raw_arguments,
        assistant_content=assistant_content,
        feedback=feedback,
        elapsed_ms=elapsed_ms,
        response_id=completion.response_id,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        cached_tokens=completion.cached_tokens,
    )


def _validate_navigation_evidence(
    evidence: Iterable[ContractRelationEvidence],
    *,
    workspace: _PageNavigationWorkspace,
    uploaded_page_count: int,
) -> PDFCandidateToolFeedback | None:
    """终态只允许引用当前可见页或工作区中完全一致的隐藏页观察。"""
    items = tuple(evidence)
    page_error = _validate_relation_evidence_pages(
        items,
        uploaded_page_count=uploaded_page_count,
        candidate_page_count=workspace.page_count,
    )
    if page_error is not None:
        return page_error
    accepted_hidden = {
        (
            item.uploaded_page_number,
            item.candidate_page_number,
            item.observation,
        )
        for item in workspace.observations
    }
    unavailable = [
        index
        for index, item in enumerate(items)
        if item.candidate_page_number not in workspace.visible_page_numbers
        and (
            item.uploaded_page_number,
            item.candidate_page_number,
            item.observation,
        )
        not in accepted_hidden
    ]
    if unavailable:
        return PDFCandidateToolFeedback(
            ok=False,
            message=(
                f"evidence 索引 {unavailable} 引用了当前不可见且未在工作区中"
                "完整记录的候选页面观察；请复制已接受观察，或重新打开对应 B 页面。"
            ),
        )
    return None


def _validate_and_apply_navigation_action(
    arguments: Any,
    *,
    workspace: _PageNavigationWorkspace,
    uploaded_page_count: int,
    candidate_pages: dict[int, PreparedPDFPage],
    available_candidate_visual_tokens: int,
    completion_tokens: int | None,
    accepted_thinks: int,
    consecutive_thinks: int,
) -> tuple[
    PDFCandidateToolFeedback,
    SubmitContractRelationArguments
    | ReportUnableToDetermineRelationArguments
    | None,
]:
    """校验当前动作状态并只在通过后原子更新页面工作区。"""
    if workspace.inspection_count == 0 and not isinstance(
        arguments,
        InspectCandidatePagesArguments,
    ):
        return (
            PDFCandidateToolFeedback(
                ok=False,
                message="第一次动作必须调用 inspect_candidate_pages 查看候选页面。",
            ),
            None,
        )

    if isinstance(arguments, InspectCandidatePagesArguments):
        if workspace.visible_page_numbers:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        "当前仍有可见候选页面；请先记录观察、提交终态或使用放弃出口。"
                    ),
                ),
                None,
            )
        if workspace.inspection_count >= _PAGE_NAVIGATION_MAXIMUM_INSPECTIONS:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message="候选页面查看批次预算已经耗尽，请形成终态。",
                ),
                None,
            )
        invalid_pages = [
            page_number
            for page_number in arguments.page_numbers
            if page_number not in candidate_pages
        ]
        if invalid_pages:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        f"page_numbers 超出 B 的 1-{workspace.page_count} 页："
                        f"{invalid_pages}；请按候选指南修正。"
                    ),
                ),
                None,
            )
        hidden_requested = set(arguments.page_numbers) & workspace.hidden_page_numbers
        if hidden_requested and arguments.revisit_reason is None:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        f"页面 {sorted(hidden_requested)} 当前已隐藏；"
                        "重新打开时必须填写 revisit_reason。"
                    ),
                ),
                None,
            )
        if not hidden_requested and arguments.revisit_reason is not None:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        "本批没有已隐藏页面，revisit_reason 必须为 null。"
                    ),
                ),
                None,
            )
        new_pages = set(arguments.page_numbers) - workspace.viewed_page_numbers
        if (
            len(workspace.viewed_page_numbers | new_pages)
            > _PAGE_NAVIGATION_MAXIMUM_UNIQUE_PAGES
        ):
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message="本次查看会超过候选合同不同页面数量预算，请减少新页。",
                ),
                None,
            )
        selected_visual_tokens = sum(
            candidate_pages[number].visual_tokens
            for number in arguments.page_numbers
        )
        if selected_visual_tokens > available_candidate_visual_tokens:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        f"本批页面需要 {selected_visual_tokens} visual tokens，"
                        f"超过候选可用预算 {available_candidate_visual_tokens}；"
                        "请减少页面数量或选择更小页面。"
                    ),
                ),
                None,
            )
        workspace.hidden_page_numbers.difference_update(arguments.page_numbers)
        workspace.visible_page_numbers = tuple(arguments.page_numbers)
        workspace.inspection_count += 1
        for page_number in arguments.page_numbers:
            workspace.view_counts[page_number] = (
                workspace.view_counts.get(page_number, 0) + 1
            )
        workspace.next_focus = arguments.purpose
        return (
            PDFCandidateToolFeedback(
                ok=True,
                message=f"已打开候选合同 B 页面 {arguments.page_numbers}。",
            ),
            None,
        )

    if isinstance(arguments, RecordCandidatePageObservationsArguments):
        if not workspace.visible_page_numbers:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message="当前没有可见候选页面，不能记录页面观察。",
                ),
                None,
            )
        page_error = _validate_relation_evidence_pages(
            arguments.observations,
            uploaded_page_count=uploaded_page_count,
            candidate_page_count=workspace.page_count,
        )
        if page_error is not None:
            return page_error, None
        invalid_observations = [
            index
            for index, item in enumerate(arguments.observations)
            if item.candidate_page_number not in workspace.visible_page_numbers
        ]
        if invalid_observations:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        f"observations 索引 {invalid_observations} 引用了非当前可见 B 页面；"
                        f"当前可见页为 {list(workspace.visible_page_numbers)}。"
                    ),
                ),
                None,
            )
        converted = [
            PDFDuplicateEvidence(
                uploaded_page_number=item.uploaded_page_number,
                candidate_page_number=item.candidate_page_number,
                observation=item.observation,
            )
            for item in arguments.observations
        ]
        existing = {
            (
                item.uploaded_page_number,
                item.candidate_page_number,
                item.observation,
            )
            for item in workspace.observations
        }
        # 同一批模型输出也可能重复同一观察；逐项更新键集合，确保工作区
        # 对完全一致的证据保持幂等，避免后续提示词和终态证据膨胀。
        for item in converted:
            key = (
                item.uploaded_page_number,
                item.candidate_page_number,
                item.observation,
            )
            if key in existing:
                continue
            workspace.observations.append(item)
            existing.add(key)
        workspace.hidden_page_numbers.update(workspace.visible_page_numbers)
        workspace.visible_page_numbers = ()
        workspace.next_focus = arguments.next_focus
        return (
            PDFCandidateToolFeedback(
                ok=True,
                message="观察已记录，当前候选页面已隐藏。",
            ),
            None,
        )

    if isinstance(arguments, ThinkArguments):
        if completion_tokens is None:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        "reasoning：本轮没有 completion_tokens，"
                        "无法验证 1024 tokens 上限。"
                    ),
                ),
                None,
            )
        if completion_tokens > _PAGE_NAVIGATION_THINK_MAXIMUM_TOKENS:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message=(
                        f"reasoning：本轮使用 {completion_tokens} completion tokens，"
                        "超过 1024 tokens 上限；请压缩推理。"
                    ),
                ),
                None,
            )
        if consecutive_thinks >= _PAGE_NAVIGATION_MAXIMUM_CONSECUTIVE_THINKS:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message="已经连续完成两次 think，请查看页面或形成终态。",
                ),
                None,
            )
        return PDFCandidateToolFeedback(ok=True, message="思考已记录。"), None

    if isinstance(arguments, SubmitContractRelationArguments):
        evidence_error = _validate_navigation_evidence(
            arguments.evidence,
            workspace=workspace,
            uploaded_page_count=uploaded_page_count,
        )
        if evidence_error is not None:
            return evidence_error, None
        return (
            PDFCandidateToolFeedback(ok=True, message="合同关系决定已接受。"),
            arguments,
        )

    if isinstance(arguments, ReportUnableToDetermineRelationArguments):
        if accepted_thinks < 1:
            return (
                PDFCandidateToolFeedback(
                    ok=False,
                    message="使用无法判断出口前必须至少完成一次有效 think。",
                ),
                None,
            )
        evidence_error = _validate_navigation_evidence(
            arguments.evidence,
            workspace=workspace,
            uploaded_page_count=uploaded_page_count,
        )
        if evidence_error is not None:
            return evidence_error, None
        return (
            PDFCandidateToolFeedback(ok=True, message="无法判断出口已接受。"),
            arguments,
        )

    return (
        PDFCandidateToolFeedback(
            ok=False,
            message="当前工具参数类型不受候选页面导航支持。",
        ),
        None,
    )


def _finish_page_navigation_judgment(
    state: PDFCandidateJudgmentState,
    *,
    started_at: float,
    audits: list[PDFCandidateToolCallAudit],
    model: str,
    terminal: SubmitContractRelationArguments
    | ReportUnableToDetermineRelationArguments,
) -> PDFCandidateJudgmentState:
    """把导航终止工具转换为现有候选判断结果。"""
    candidate = state["candidate"]
    common_values = {
        "candidate_document_id": candidate.document_id,
        "rank": candidate.rank,
        "model": model,
        "prompt_version": PAGE_NAVIGATION_JUDGMENT_PROMPT_VERSION,
        "rounds": len(audits),
        "elapsed_ms": round((perf_counter() - started_at) * 1000, 3),
        "tool_calls": tuple(audits),
        **_runtime_values(audits),
    }
    if isinstance(terminal, ReportUnableToDetermineRelationArguments):
        judgment = FailedPDFCandidateJudgment(
            error=(
                "模型无法可靠判断合同关系："
                f"{terminal.reason}；{terminal.reasoning_summary}"
            ),
            **common_values,
        )
    else:
        evidence = tuple(
            PDFDuplicateEvidence(
                uploaded_page_number=item.uploaded_page_number,
                candidate_page_number=item.candidate_page_number,
                observation=item.observation,
            )
            for item in terminal.evidence
        )
        result_type = {
            "duplicate": DuplicatePDFCandidate,
            "similar": SimilarPDFCandidate,
            "different": DifferentPDFCandidate,
        }[terminal.decision]
        judgment = result_type(
            evidence=evidence,
            reasoning_summary=terminal.reasoning_summary,
            **common_values,
        )
    return {**state, "judgment": judgment}


def _failed_page_navigation_judgment(
    state: PDFCandidateJudgmentState,
    *,
    started_at: float,
    audits: list[PDFCandidateToolCallAudit],
    model: str,
    error: str,
) -> PDFCandidateJudgmentState:
    """形成带导航提示词版本和完整用量的失败终态。"""
    candidate = state["candidate"]
    return {
        **state,
        "judgment": FailedPDFCandidateJudgment(
            candidate_document_id=candidate.document_id,
            rank=candidate.rank,
            model=model,
            prompt_version=PAGE_NAVIGATION_JUDGMENT_PROMPT_VERSION,
            rounds=len(audits),
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
            tool_calls=tuple(audits),
            error=error,
            **_runtime_values(audits),
        ),
    }


__all__ = [
    "FULL_DOCUMENT_NODE",
    "PAGE_NAVIGATION_AGENT_NODE",
    "decide_candidate_judgment_route",
    "judge_full_documents",
    "judge_with_page_navigation_agent",
    "route_candidate_judgment",
]
