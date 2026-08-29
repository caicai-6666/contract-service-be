"""检索问题提出子图的私有状态、结果与审计契约。"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    PreparedPDF,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    RetrievalViewGuideCatalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.state import (
    QuestionFocusDiscoveryResult,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.tool import (
    GeneratedQuestion,
    QuestionGenerationToolFeedback,
)


class QuestionGenerationModel(BaseModel):
    """问题提出正式状态和审计记录的不可变基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class QuestionGenerationContext(QuestionGenerationModel):
    """包含模型可见提问指南和后台隐式数量限制的不可变上下文。"""

    document_id: str
    prompt_version: str
    guide_catalog_sha256: str
    maximum_questions: int
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class QuestionGenerationToolCallAudit(QuestionGenerationModel):
    """一次模型工具动作的私有审计记录。"""

    round_number: int
    focus_id: str | None = None
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None = None
    feedback: QuestionGenerationToolFeedback
    accepted_question_count: int
    temporary_failure_memory_cleared: bool
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class RetrievalQuestionGenerationResult(QuestionGenerationModel):
    """合同动态问题目录及完整运行审计。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    prompt_version: str
    tool_version: str
    questions: tuple[GeneratedQuestion, ...]
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[QuestionGenerationToolCallAudit, ...]
    error: str | None


class RetrievalQuestionEmbedding(QuestionGenerationModel):
    """一个正式问题与其独立检索向量的稳定映射。"""

    question_id: str
    order: int
    vector: tuple[float, ...]


class RetrievalQuestionEmbeddingResult(QuestionGenerationModel):
    """问题侧批量向量化结果及最小运行观测。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    model: str
    prompt_version: str
    dimensions: int
    normalized: bool
    embeddings: tuple[RetrievalQuestionEmbedding, ...]
    failed_question_ids: tuple[str, ...]
    request_count: int
    elapsed_ms: float
    prompt_tokens: int | None
    error: str | None


class ContractRetrievalVectorResult(QuestionGenerationModel):
    """由成功问题向量融合得到的合同级检索向量。"""

    status: Literal["completed", "partial", "failed"]
    document_id: str
    fusion_version: str
    fusion_method: Literal["arithmetic_mean_l2_normalized"]
    embedding_model: str
    embedding_prompt_version: str
    dimensions: int
    normalized: bool
    source_question_ids: tuple[str, ...]
    source_embedding_count: int
    vector: tuple[float, ...] | None
    elapsed_ms: float
    error: str | None


class QuestionProposalContext(QuestionGenerationModel):
    """供全部问题规划并发请求共享的不可变表达任务前缀。"""

    document_id: str
    common_prompt_version: str
    target_prompt_version: str
    tool_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class QuestionProposalOutcomeBase(QuestionGenerationModel):
    """单份问题规划成功或失败结果共享的运行观测。"""

    focus_id: str
    focus_order: int
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[QuestionGenerationToolCallAudit, ...]


class GeneratedQuestionProposal(QuestionProposalOutcomeBase):
    """已经通过全部校验的单份问题规划生成结果。"""

    status: Literal["generated"] = "generated"
    question: GeneratedQuestion


class FailedQuestionProposal(QuestionProposalOutcomeBase):
    """单份问题规划的请求、协议或参数失败结果。"""

    status: Literal["failed"] = "failed"
    error: str


QuestionProposalOutcome: TypeAlias = GeneratedQuestionProposal | FailedQuestionProposal


class QuestionGenerationSubgraphState(TypedDict, total=False):
    """问题提出只读取合同公共资料，并拥有问题目录结果。"""

    prepared_pdf: PreparedPDF
    document_structure: BaseModel
    prefill_context: ContractPrefillContext
    retrieval_view_guide_catalog: RetrievalViewGuideCatalog
    question_generation_context: QuestionGenerationContext
    question_focus_discovery: QuestionFocusDiscoveryResult
    question_proposal_context: QuestionProposalContext
    question_proposals: tuple[QuestionProposalOutcome, ...]
    retrieval_questions: RetrievalQuestionGenerationResult
    retrieval_question_embeddings: RetrievalQuestionEmbeddingResult
    contract_retrieval_vector: ContractRetrievalVectorResult


__all__ = [
    "ContractRetrievalVectorResult",
    "FailedQuestionProposal",
    "GeneratedQuestionProposal",
    "QuestionGenerationContext",
    "QuestionGenerationSubgraphState",
    "QuestionGenerationToolCallAudit",
    "QuestionProposalContext",
    "QuestionProposalOutcome",
    "RetrievalQuestionEmbedding",
    "RetrievalQuestionEmbeddingResult",
    "RetrievalQuestionGenerationResult",
]
