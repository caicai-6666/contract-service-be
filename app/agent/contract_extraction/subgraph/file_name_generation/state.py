"""合同建议文件名生成子图状态、审计与结果契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self, TypedDict

from app.agent.contract_extraction.state import ContractBaseContext
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)
from app.agent.contract_extraction.subgraph.file_name_generation.tool import (
    FileNameGenerationToolFeedback,
    SuggestedFileNameEvidence,
)


class FileNameGenerationModel(BaseModel):
    """建议文件名上下文与结果使用的不可变严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FileNameGenerationContext(FileNameGenerationModel):
    """追加文件命名任务后供生成节点读取的不可变上下文。"""

    document_id: str
    prompt_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class FileNameGenerationToolCallAudit(FileNameGenerationModel):
    """不包含页面图像的一轮命名工具调用私有审计。"""

    round_number: int = Field(ge=1)
    call_id: str | None
    name: str = Field(min_length=1)
    raw_arguments: str
    assistant_content: str | None = None
    feedback: FileNameGenerationToolFeedback
    elapsed_ms: float = Field(ge=0)
    response_id: str | None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)


class SuggestedFileNameResult(FileNameGenerationModel):
    """证据化建议文件名或没有伪造名称的技术失败。"""

    status: Literal["generated", "failed"]
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[SuggestedFileNameEvidence, ...] = ()
    reasoning: str | None = None
    file_name: str | None = None
    model: str | None = Field(default=None, min_length=1)
    prompt_version: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    rounds: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    tool_calls: tuple[FileNameGenerationToolCallAudit, ...] = ()
    error: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        """成功结果与技术失败必须使用互斥负载。"""
        if self.status == "failed":
            if (
                self.evidence
                or self.reasoning is not None
                or self.file_name is not None
                or not self.error
            ):
                raise ValueError("failed 命名结果只能包含技术错误和运行审计")
            return self

        if (
            not self.evidence
            or not self.reasoning
            or not self.file_name
            or not self.model
            or self.error is not None
        ):
            raise ValueError("可靠建议文件名必须包含证据、命名理由和最终名称")
        return self


class FileNameGenerationSubgraphState(TypedDict, total=False):
    """在上下文组装与建议文件名生成之间传递的子图状态。"""

    base_context: ContractBaseContext
    classification: ContractClassificationResult
    page_count: int
    file_name_context: FileNameGenerationContext
    suggested_file_name: SuggestedFileNameResult


__all__ = [
    "FileNameGenerationContext",
    "FileNameGenerationModel",
    "FileNameGenerationSubgraphState",
    "FileNameGenerationToolCallAudit",
    "SuggestedFileNameResult",
]
