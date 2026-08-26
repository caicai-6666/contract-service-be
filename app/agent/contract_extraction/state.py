"""合同信息抽取工作流的输入、状态与占位输出契约。"""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SerializeAsAny
from typing_extensions import TypedDict


class ContractExtractionRequest(BaseModel):
    """工作流的最小输入；字段定义和模型参数将在后续节点中补充。"""

    model_config = ConfigDict(frozen=True)

    pdf_path: Path


class WorkflowPlaceholder(BaseModel):
    """尚未实现的节点或子图返回的可观察状态。"""

    model_config = ConfigDict(frozen=True)

    node: str
    status: Literal["placeholder"] = "placeholder"
    message: str


class FieldExtractionResult(BaseModel):
    """字段提取父子图汇总 Core 正式结果与 Attribute 当前结果。"""

    model_config = ConfigDict(frozen=True)

    core: SerializeAsAny[BaseModel]
    attribute: WorkflowPlaceholder


class PreparedPDFPage(BaseModel):
    """PDF 预处理节点生成的确定性页面图像。"""

    model_config = ConfigDict(frozen=True)

    page_number: int
    png_bytes: bytes
    width_pixels: int
    height_pixels: int
    render_scale: float
    visual_tokens: int
    content_sha256: str
    was_scaled: bool


class PreparedPDF(BaseModel):
    """经过检查、渲染和动态预算缩放后的完整 PDF。"""

    model_config = ConfigDict(frozen=True)

    document_id: str
    source_path: Path
    file_size_bytes: int
    page_count: int
    total_visual_tokens: int
    visual_tokens_per_page_budget: int
    visual_tokens_per_request_budget: int
    pages: tuple[PreparedPDFPage, ...]


class PDFPromptPage(BaseModel):
    """一页标准图像对应的确定性提示词描述。"""

    model_config = ConfigDict(frozen=True)

    page_number: int
    width_pixels: int
    height_pixels: int
    descriptor: str


class PDFPromptContext(BaseModel):
    """整份 PDF 的确定性提示词上下文。"""

    model_config = ConfigDict(frozen=True)

    prompt_version: str
    pages: tuple[PDFPromptPage, ...]


class ContractBaseContext(BaseModel):
    """供合同分类读取的 PDF 与文档结构不可变基础前缀。"""

    model_config = ConfigDict(frozen=True)

    document_id: str
    prompt_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class ContractPrefillContext(BaseModel):
    """追加分类结果后，供最终预热与三个下游子图复用的不可变前缀。"""

    model_config = ConfigDict(frozen=True)

    document_id: str
    prompt_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class ContractPreheatResult(BaseModel):
    """包含 PDF、文档结构与分类结果的最终公共前缀预热结果。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["warmed", "degraded"]
    document_id: str
    prompt_version: str
    model: str
    completed_at: datetime
    prefix_sha256: str
    elapsed_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    error: str | None = None


class ContractExtractionResult(BaseModel):
    """结构理解与三个业务子图合并后的工作流结果。"""

    model_config = ConfigDict(frozen=True)

    pdf_path: Path
    classification: SerializeAsAny[BaseModel]
    preheat: ContractPreheatResult
    document_structure: SerializeAsAny[BaseModel]
    field_extraction: FieldExtractionResult
    clause_extraction: WorkflowPlaceholder
    summary_generation: WorkflowPlaceholder


class ContractExtractionState(TypedDict, total=False):
    """在预处理、并行子图和合并节点之间传递的共享状态。"""

    request: ContractExtractionRequest
    category_catalog: BaseModel
    field_definition_catalog: BaseModel
    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    base_context: ContractBaseContext
    classification: BaseModel
    prefill_context: ContractPrefillContext
    preheat: ContractPreheatResult
    document_structure: BaseModel
    field_extraction: FieldExtractionResult
    clause_extraction: WorkflowPlaceholder
    summary_generation: WorkflowPlaceholder
    result: ContractExtractionResult
