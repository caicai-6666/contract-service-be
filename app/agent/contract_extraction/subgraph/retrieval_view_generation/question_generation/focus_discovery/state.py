"""问题关注点发现节点的结果与私有审计契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.focus_discovery.tool import (
    GeneratedQuestionFocus,
    QuestionFocusToolFeedback,
)


class QuestionFocusDiscoveryModel(BaseModel):
    """关注点发现正式状态和审计记录的不可变基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class QuestionFocusToolCallAudit(QuestionFocusDiscoveryModel):
    """一次关注点发现模型工具动作的私有审计。"""

    round_number: int
    call_id: str | None
    name: str
    raw_arguments: str
    assistant_content: str | None = None
    feedback: QuestionFocusToolFeedback
    accepted_focus_count: int
    temporary_failure_memory_cleared: bool
    elapsed_ms: float
    response_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None


class QuestionFocusDiscoveryResult(QuestionFocusDiscoveryModel):
    """当前合同的问题关注点目录及完整运行审计。"""

    status: Literal["completed", "failed"]
    termination_reason: Literal[
        "model_finished",
        "hidden_limit_reached",
        "failed",
    ]
    document_id: str
    model: str
    prompt_version: str
    tool_version: str
    focuses: tuple[GeneratedQuestionFocus, ...]
    finish_reasoning: str | None
    rounds: int
    elapsed_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_calls: tuple[QuestionFocusToolCallAudit, ...]
    error: str | None


__all__ = [
    "QuestionFocusDiscoveryResult",
    "QuestionFocusToolCallAudit",
]
