"""单份召回候选与上传 PDF 的判重子图状态。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import Self, TypedDict

from app.agent.contract_extraction.state import PreparedPDF
from app.agent.pdf_deduplication.state import (
    PDFCandidateJudgment,
    PDFDeduplicationModel,
    PDFDuplicateCandidate,
)

PDFCandidateJudgmentStrategy = Literal[
    "full_document",
    "page_navigation_agent",
]
PDFCandidateRoutingReason = Literal[
    "within_single_shot_limits",
    "visual_token_limit_exceeded",
    "page_limit_exceeded",
    "visual_token_and_page_limits_exceeded",
]


class PDFCandidateRoutingDecision(PDFDeduplicationModel):
    """一对 PDF 进入全量输入或翻页 Agent 的可审计路由决定。"""

    uploaded_document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: PDFCandidateJudgmentStrategy
    reason: PDFCandidateRoutingReason
    combined_visual_tokens: int = Field(gt=0)
    single_shot_visual_token_limit: int = Field(gt=0)
    combined_page_count: int = Field(gt=0)
    single_shot_max_total_pages: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_strategy_and_reason(self) -> Self:
        """确保路由结论与记录的两个硬限制完全一致。"""
        token_exceeded = (
            self.combined_visual_tokens > self.single_shot_visual_token_limit
        )
        page_exceeded = self.combined_page_count > self.single_shot_max_total_pages
        expected_reason: PDFCandidateRoutingReason
        if token_exceeded and page_exceeded:
            expected_reason = "visual_token_and_page_limits_exceeded"
        elif token_exceeded:
            expected_reason = "visual_token_limit_exceeded"
        elif page_exceeded:
            expected_reason = "page_limit_exceeded"
        else:
            expected_reason = "within_single_shot_limits"
        expected_strategy: PDFCandidateJudgmentStrategy = (
            "full_document"
            if expected_reason == "within_single_shot_limits"
            else "page_navigation_agent"
        )
        if self.reason != expected_reason or self.strategy != expected_strategy:
            raise ValueError("PDF 候选判重策略与视觉 token、页数限制不一致")
        return self


class PDFCandidateJudgmentState(TypedDict, total=False):
    """逐候选子图输入、路由决定和最终判断。"""

    uploaded_pdf: PreparedPDF
    candidate_pdf: PreparedPDF
    candidate: PDFDuplicateCandidate
    routing_decision: PDFCandidateRoutingDecision
    judgment: PDFCandidateJudgment


__all__ = [
    "PDFCandidateJudgmentState",
    "PDFCandidateJudgmentStrategy",
    "PDFCandidateRoutingDecision",
    "PDFCandidateRoutingReason",
]
