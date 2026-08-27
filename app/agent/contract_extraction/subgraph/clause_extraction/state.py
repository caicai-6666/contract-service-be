"""条款提取子图的私有状态。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.clause_extraction.tool import (
    AnalyzeClauseHierarchyArguments,
    ClauseCandidateWorkspaceItem,
    ClauseContentToolFeedback,
    ClauseDiscoveryToolFeedback,
    FinishClauseDiscoveryArguments,
)


class ClauseExtractionModel(BaseModel):
    """条款子图状态与审计记录共用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ClauseDiscoveryToolCallAudit(ClauseExtractionModel):
    """条款候选发现会话中的一次工具调用审计。"""

    round_number: int
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None = None
    feedback: ClauseDiscoveryToolFeedback
    workspace_size: int
    short_term_reset: bool
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class ClauseCandidateDiscoveryResult(ClauseExtractionModel):
    """顺序条款候选发现节点的终态结果。"""

    status: Literal["completed", "failed"]
    document_id: str
    model: str
    prompt_version: str
    tool_version: str
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None
    candidates: tuple[ClauseCandidateWorkspaceItem, ...]
    completion: FinishClauseDiscoveryArguments | None = None
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[ClauseDiscoveryToolCallAudit, ...]
    error: str | None = None


class ClauseExtractionContext(ClauseExtractionModel):
    """供全部单条款详情请求复用的不可变公共任务前缀。"""

    document_id: str
    prompt_version: str
    tool_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class ClauseExtractionPreheatResult(ClauseExtractionModel):
    """条款详情公共任务与固定工具块的预热观测结果。"""

    status: Literal["warmed", "degraded"]
    document_id: str
    prompt_version: str
    tool_version: str
    model: str
    completed_at: datetime
    prefix_sha256: str
    elapsed_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    error: str | None = None


class ClauseContentToolCallAudit(ClauseExtractionModel):
    """单条款详情会话中的一次工具调用审计。"""

    round_number: int
    request_number: int
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None = None
    feedback: ClauseContentToolFeedback
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class ClauseContentRequestAudit(ClauseExtractionModel):
    """单条款详情会话中的一次模型请求审计，包括无完整工具调用的截断响应。"""

    request_number: int
    round_number: int
    max_completion_tokens: int
    finish_reason: str | None
    tool_call_count: int
    queue_elapsed_ms: float
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class ClauseContentGenerationProfile(ClauseExtractionModel):
    """节点三复用的正式采样与单次生成上限配置。"""

    max_completion_tokens: int
    temperature: float
    top_p: float
    top_k: int
    presence_penalty: float
    repetition_penalty: float


class ClauseContentOutcomeBase(ClauseExtractionModel):
    """单条款成功或失败结果共享的候选身份与运行观测。"""

    candidate: ClauseCandidateWorkspaceItem
    rounds: int
    request_attempts: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    requests: tuple[ClauseContentRequestAudit, ...]
    tool_calls: tuple[ClauseContentToolCallAudit, ...]


class ExtractedClause(ClauseContentOutcomeBase):
    """已经通过候选边界校验的完整直接条款内容。"""

    status: Literal["extracted"] = "extracted"
    reasoning_summary: str
    content: str


class FailedClause(ClauseContentOutcomeBase):
    """单候选请求、协议或最大轮次失败结果。"""

    status: Literal["failed"] = "failed"
    error: str


ClauseContentOutcome: TypeAlias = ExtractedClause | FailedClause


class ClauseExtractionResult(ClauseExtractionModel):
    """按候选原始顺序汇总的并发条款详情提取结果。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    common_prompt_version: str
    target_prompt_version: str
    tool_version: str
    prefix_sha256: str
    preheat: ClauseExtractionPreheatResult
    generation_profile: ClauseContentGenerationProfile
    clauses: tuple[ClauseContentOutcome, ...]
    elapsed_ms: float


class ClauseExtractionSubgraphState(TypedDict, total=False):
    """条款子图只读公共合同状态，并拥有候选与最终条款结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    clause_candidates: ClauseCandidateDiscoveryResult
    clause_extraction_context: ClauseExtractionContext
    clause_extraction_preheat: ClauseExtractionPreheatResult
    clause_extraction: ClauseExtractionResult


__all__ = [
    "ClauseCandidateDiscoveryResult",
    "ClauseContentGenerationProfile",
    "ClauseContentOutcome",
    "ClauseContentOutcomeBase",
    "ClauseContentRequestAudit",
    "ClauseContentToolCallAudit",
    "ClauseDiscoveryToolCallAudit",
    "ClauseExtractionContext",
    "ClauseExtractionModel",
    "ClauseExtractionPreheatResult",
    "ClauseExtractionResult",
    "ClauseExtractionSubgraphState",
    "ExtractedClause",
    "FailedClause",
]
