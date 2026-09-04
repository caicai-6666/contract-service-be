"""合同信息抽取工作流的输入、状态与占位输出契约。"""

from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)
from typing_extensions import TypedDict


class ContractExtractionRequest(BaseModel):
    """PDF 准备服务的输入；文件路径和内存字节必须二选一。"""

    model_config = ConfigDict(frozen=True)

    pdf_path: Path | None = None
    pdf_bytes: bytes | None = Field(default=None, repr=False, exclude=True)
    file_name: str | None = None

    @field_validator("file_name")
    @classmethod
    def normalize_file_name(cls, value: str | None) -> str | None:
        """内存上传只保留安全的展示文件名，不把它解释为服务器路径。"""
        if value is None:
            return None
        normalized = Path(value.strip()).name
        if not normalized or normalized in {".", ".."}:
            raise ValueError("PDF 文件名不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_source(self) -> "ContractExtractionRequest":
        """拒绝无来源或同时提供两个来源的含混请求。"""
        has_path = self.pdf_path is not None
        has_bytes = self.pdf_bytes is not None
        if has_path == has_bytes:
            raise ValueError("pdf_path 与 pdf_bytes 必须且只能提供一个")
        if has_bytes:
            if not self.pdf_bytes:
                raise ValueError("PDF 字节不能为空")
            if self.file_name is None:
                raise ValueError("内存 PDF 必须提供 file_name")
        return self

    @property
    def source_name(self) -> str:
        """返回可安全展示的源文件名。"""
        if self.file_name is not None:
            return self.file_name
        assert self.pdf_path is not None
        return self.pdf_path.name

    @property
    def source_path(self) -> Path:
        """返回路径输入或仅用于兼容内部结果的展示路径。"""
        return self.pdf_path if self.pdf_path is not None else Path(self.source_name)

    @property
    def pdf_source(self) -> Path | bytes:
        """返回 PDF 准备服务能够直接读取的实际来源。"""
        if self.pdf_path is not None:
            return self.pdf_path
        assert self.pdf_bytes is not None
        return self.pdf_bytes


class WorkflowPlaceholder(BaseModel):
    """尚未实现的节点或子图返回的可观察状态。"""

    model_config = ConfigDict(frozen=True)

    node: str
    status: Literal["placeholder"] = "placeholder"
    message: str


class FieldExtractionResult(BaseModel):
    """字段提取子图汇总的 Core 正式结果。"""

    model_config = ConfigDict(frozen=True)

    core: SerializeAsAny[BaseModel]


class PreparedPDFPage(BaseModel):
    """异步 PDF 准备服务生成的确定性页面图像。"""

    model_config = ConfigDict(frozen=True)

    page_number: int
    png_bytes: bytes
    width_pixels: int
    height_pixels: int
    render_scale: float
    visual_tokens: int
    content_sha256: str
    media_uuid: str = Field(default_factory=lambda: str(uuid4()))
    was_scaled: bool

    @field_validator("media_uuid")
    @classmethod
    def validate_media_uuid(cls, value: str) -> str:
        """媒体引用使用不可预测 UUIDv4，避免共享 vLLM 的缓存身份冲突。"""
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("media_uuid 必须是规范格式的 UUIDv4")
        return value


class PreparedPDF(BaseModel):
    """经过检查、动态预算缩放并重新封装后的处理版 PDF。"""

    model_config = ConfigDict(frozen=True)

    document_id: str
    source_path: Path
    processed_pdf_bytes: bytes = Field(repr=False, exclude=True)
    source_file_size_bytes: int
    processed_file_size_bytes: int
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
    """追加分类结果后，供三个下游子图复用的不可变前缀。"""

    model_config = ConfigDict(frozen=True)

    document_id: str
    prompt_version: str
    messages: tuple[dict[str, Any], ...]
    prefix_sha256: str


class ContractExtractionResult(BaseModel):
    """结构理解、建议名称与三个业务子图合并后的工作流结果。"""

    model_config = ConfigDict(frozen=True)

    pdf_path: Path
    classification: SerializeAsAny[BaseModel]
    suggested_file_name: SerializeAsAny[BaseModel]
    document_structure: SerializeAsAny[BaseModel]
    field_extraction: FieldExtractionResult
    clause_extraction: SerializeAsAny[BaseModel]
    retrieval_questions: SerializeAsAny[BaseModel]
    retrieval_question_embeddings: SerializeAsAny[BaseModel]
    contract_retrieval_vector: SerializeAsAny[BaseModel]


class ContractExtractionState(TypedDict, total=False):
    """在文档理解、并行子图和合并节点之间传递的共享状态。"""

    category_catalog: BaseModel
    field_definition_catalog: BaseModel
    retrieval_view_guide_catalog: BaseModel
    prepared_pdf: PreparedPDF
    prompt_context: PDFPromptContext
    base_context: ContractBaseContext
    classification: BaseModel
    suggested_file_name: BaseModel
    prefill_context: ContractPrefillContext
    document_structure: BaseModel
    field_extraction: FieldExtractionResult
    clause_extraction: BaseModel
    retrieval_questions: BaseModel
    retrieval_question_embeddings: BaseModel
    contract_retrieval_vector: BaseModel
    result: ContractExtractionResult
