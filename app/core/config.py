"""从本地环境变量加载应用与模型配置。"""

from functools import lru_cache
from os import getenv
from pathlib import Path
from typing import Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MLLMGenerationSettings(BaseModel):
    """多模态模型的单次生成参数。"""

    model_config = ConfigDict(frozen=True)

    enable_thinking: bool = False
    temperature: float = Field(default=0.7, ge=0)
    top_p: float = Field(default=0.8, ge=0, le=1)
    top_k: int = Field(default=20, ge=0)
    presence_penalty: float = 1.5
    repetition_penalty: float = Field(default=1.0, ge=0)
    seed: int = 3407
    max_completion_tokens: int = Field(default=8192, gt=0)


class MLLMVisionSettings(BaseModel):
    """合同页渲染和视觉 token 预算。"""

    model_config = ConfigDict(frozen=True)

    max_render_scale: float = Field(default=2.0, gt=0)
    visual_token_patch_size: int = Field(default=32, gt=0)
    max_visual_tokens_per_page: int = Field(default=4096, gt=0)
    max_visual_tokens_per_request: int | None = Field(default=None, gt=0)
    reserved_prompt_tokens: int = Field(default=4096, ge=0)
    reserved_runtime_tokens: int = Field(default=10240, ge=0)

    @model_validator(mode="after")
    def validate_page_budget(self) -> Self:
        """显式视觉上限存在时，单页预算不能超过它。"""
        if (
            self.max_visual_tokens_per_request is not None
            and self.max_visual_tokens_per_page
            > self.max_visual_tokens_per_request
        ):
            raise ValueError("MLLM 单页视觉 token 预算不能超过单次请求预算")
        return self


class MLLMSettings(BaseModel):
    """用于合同提取的本地多模态 vLLM 服务。"""

    model_config = ConfigDict(frozen=True)

    provider: str = "vllm"
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str | None = None
    model: str = "qwen3.6-35b-a3b-fp8"
    endpoint: str = "chat_completions"
    timeout_seconds: int = Field(default=300, gt=0)
    max_concurrent_requests: int = Field(default=20, gt=0)
    use_media_references: bool = True
    context_window_tokens: int = Field(default=262144, gt=0)
    generation: MLLMGenerationSettings = MLLMGenerationSettings()
    vision: MLLMVisionSettings = MLLMVisionSettings()

    @model_validator(mode="after")
    def validate_token_budget(self) -> Self:
        """确保生成、提示词和运行时余量能够放入上下文窗口。"""
        non_visual_tokens = (
            self.generation.max_completion_tokens
            + self.vision.reserved_prompt_tokens
            + self.vision.reserved_runtime_tokens
        )
        if non_visual_tokens >= self.context_window_tokens:
            raise ValueError("MLLM 生成、提示词和运行时预留已占满上下文窗口")
        if (
            self.vision.max_visual_tokens_per_request is not None
            and self.vision.max_visual_tokens_per_request
            > self.context_window_tokens - non_visual_tokens
        ):
            raise ValueError("MLLM 显式视觉 token 上限超出上下文可用预算")
        return self

    @property
    def visual_token_ceiling(self) -> int:
        """返回扣除生成、提示词和工具历史预留后的视觉容量。"""
        available = (
            self.context_window_tokens
            - self.generation.max_completion_tokens
            - self.vision.reserved_prompt_tokens
            - self.vision.reserved_runtime_tokens
        )
        configured = self.vision.max_visual_tokens_per_request
        return available if configured is None else min(available, configured)

    def visual_token_budget(self, page_count: int) -> int:
        """按页数增长视觉总预算，直至上下文视觉容量上限。"""
        if page_count <= 0:
            raise ValueError("PDF 页数必须大于 0")
        return min(
            self.visual_token_ceiling,
            page_count * self.vision.max_visual_tokens_per_page,
        )

    def visual_token_budget_per_page(self, page_count: int) -> int:
        """把视觉容量分摊给全部页面，使完整 PDF 一次进入模型。"""
        if page_count <= 0:
            raise ValueError("PDF 页数必须大于 0")
        distributed_budget = self.visual_token_ceiling // page_count
        if distributed_budget <= 0:
            raise ValueError("MLLM 视觉容量不足以为每个页面分配 token")
        return min(
            self.vision.max_visual_tokens_per_page,
            distributed_budget,
        )


class EmbeddingSettings(BaseModel):
    """用于检索向量化的本地 vLLM 服务。"""

    model_config = ConfigDict(frozen=True)

    provider: str = "vllm"
    base_url: str = "http://127.0.0.1:8001/v1"
    api_key: str | None = None
    model: str = "qwen3-vl-embedding-8b"
    endpoint: str = "embeddings"
    timeout_seconds: int = Field(default=60, gt=0)
    batch_size: int = Field(default=32, gt=0)
    max_concurrent_requests: int = Field(default=10, gt=0)
    dimensions: int = Field(default=4096, gt=0)
    normalize: bool = True


class PDFDeduplicationSettings(BaseModel):
    """PDF 查重候选召回与逐候选判定配置。"""

    model_config = ConfigDict(frozen=True)

    single_shot_visual_token_ratio: float = Field(
        default=0.75,
        gt=0,
        le=1,
    )
    single_shot_max_total_pages: int = Field(default=20, gt=0)
    minimum_recall_cosine_similarity: float = Field(
        default=0.60,
        ge=-1,
        le=1,
    )


class Settings(BaseModel):
    """应用运行所需的不可变配置。"""

    model_config = ConfigDict(frozen=True)

    app_env: str = "development"
    contract_category_definition_dir: Path = Path(
        "data/definition/contract-category"
    )
    field_definition_dir: Path = Path("data/definition/field")
    retrieval_view_guide_dir: Path = Path("data/definition/retrieval-view")
    reviewer_user_file: Path = Path("data/user/users.yaml")
    contract_metadata_database_file: Path = Path(
        "data/abstract/contracts.db"
    )
    auth_login_code_ttl_seconds: int = Field(default=3600, gt=0)
    retrieval_view_max_questions: int = Field(default=8, gt=0)
    contract_extraction_run_ttl_seconds: int = Field(default=3600, gt=0)
    contract_deduplication_review_ttl_seconds: int = Field(
        default=600,
        gt=0,
        le=600,
    )
    contract_extraction_cleanup_interval_seconds: int = Field(default=30, gt=0)
    contract_extraction_event_buffer_size: int = Field(default=256, gt=0)
    contract_extraction_sse_heartbeat_seconds: int = Field(default=15, gt=0)
    contract_extraction_max_stage_attempts: int = Field(default=3, gt=0)
    elasticsearch_hosts: tuple[str, ...] = ("http://127.0.0.1:9200",)
    elasticsearch_index_name: str = "contracts-v1"
    elasticsearch_ingestion_experiment_index_name: str = (
        "contracts-ingestion-experiment-v1"
    )
    elasticsearch_text_analyzer: str = Field(
        default="smartcn",
        min_length=1,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    elasticsearch_vector_dimensions: int = Field(default=4096, gt=0)
    elasticsearch_number_of_shards: int = Field(default=1, gt=0)
    elasticsearch_number_of_replicas: int = Field(default=0, ge=0)
    mllm: MLLMSettings = MLLMSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    pdf_deduplication: PDFDeduplicationSettings = PDFDeduplicationSettings()

    @model_validator(mode="after")
    def validate_integrations(self) -> Self:
        """校验跨组件配置约束。"""
        if self.embedding.dimensions != self.elasticsearch_vector_dimensions:
            raise ValueError("嵌入模型维度必须与 Elasticsearch 向量维度一致")
        return self

    @property
    def contract_category_definition_path(self) -> Path:
        """将相对类别目录固定解析到项目根目录。"""
        if self.contract_category_definition_dir.is_absolute():
            return self.contract_category_definition_dir
        return _PROJECT_ROOT / self.contract_category_definition_dir

    @property
    def field_definition_path(self) -> Path:
        """将相对字段定义目录固定解析到项目根目录。"""
        if self.field_definition_dir.is_absolute():
            return self.field_definition_dir
        return _PROJECT_ROOT / self.field_definition_dir

    @property
    def retrieval_view_guide_path(self) -> Path:
        """将相对检索视图指南目录固定解析到项目根目录。"""
        if self.retrieval_view_guide_dir.is_absolute():
            return self.retrieval_view_guide_dir
        return _PROJECT_ROOT / self.retrieval_view_guide_dir

    @property
    def reviewer_user_path(self) -> Path:
        """将相对审核用户文件固定解析到项目根目录。"""
        if self.reviewer_user_file.is_absolute():
            return self.reviewer_user_file
        return _PROJECT_ROOT / self.reviewer_user_file

    @property
    def contract_metadata_database_path(self) -> Path:
        """将 SQLite 合同元数据文件固定解析到项目根目录。"""
        if self.contract_metadata_database_file.is_absolute():
            return self.contract_metadata_database_file
        return _PROJECT_ROOT / self.contract_metadata_database_file


def _optional_env(name: str) -> str | None:
    """将空环境变量统一解析为未配置。"""
    value = getenv(name)
    return value if value else None


def _env(name: str, default: str) -> str:
    """读取带默认值的环境变量。"""
    return getenv(name, default)


def _hosts_env() -> tuple[str, ...]:
    """读取逗号分隔的 Elasticsearch 节点列表。"""
    hosts = tuple(
        host.strip()
        for host in _env(
            "ELASTICSEARCH_HOSTS",
            "http://127.0.0.1:9200",
        ).split(",")
        if host.strip()
    )
    if not hosts:
        raise ValueError("ELASTICSEARCH_HOSTS 至少需要一个节点")
    return hosts


@lru_cache
def get_settings() -> Settings:
    """加载一次 `.env`，并缓存解析后的配置。"""
    load_dotenv(_PROJECT_ROOT / ".env")
    return Settings(
        app_env=_env("APP_ENV", "development"),
        contract_category_definition_dir=_env(
            "CONTRACT_CATEGORY_DEFINITION_DIR",
            "data/definition/contract-category",
        ),
        field_definition_dir=_env(
            "FIELD_DEFINITION_DIR",
            "data/definition/field",
        ),
        retrieval_view_guide_dir=_env(
            "RETRIEVAL_VIEW_GUIDE_DIR",
            "data/definition/retrieval-view",
        ),
        reviewer_user_file=_env(
            "REVIEWER_USER_FILE",
            "data/user/users.yaml",
        ),
        contract_metadata_database_file=_env(
            "CONTRACT_METADATA_DATABASE_FILE",
            "data/abstract/contracts.db",
        ),
        auth_login_code_ttl_seconds=_env(
            "AUTH_LOGIN_CODE_TTL_SECONDS",
            "3600",
        ),
        retrieval_view_max_questions=_env(
            "RETRIEVAL_VIEW_MAX_QUESTIONS",
            "8",
        ),
        contract_extraction_run_ttl_seconds=_env(
            "CONTRACT_EXTRACTION_RUN_TTL_SECONDS",
            "3600",
        ),
        contract_deduplication_review_ttl_seconds=_env(
            "CONTRACT_DEDUPLICATION_REVIEW_TTL_SECONDS",
            "600",
        ),
        contract_extraction_cleanup_interval_seconds=_env(
            "CONTRACT_EXTRACTION_CLEANUP_INTERVAL_SECONDS",
            "30",
        ),
        contract_extraction_event_buffer_size=_env(
            "CONTRACT_EXTRACTION_EVENT_BUFFER_SIZE",
            "256",
        ),
        contract_extraction_sse_heartbeat_seconds=_env(
            "CONTRACT_EXTRACTION_SSE_HEARTBEAT_SECONDS",
            "15",
        ),
        contract_extraction_max_stage_attempts=_env(
            "CONTRACT_EXTRACTION_MAX_STAGE_ATTEMPTS",
            "3",
        ),
        elasticsearch_hosts=_hosts_env(),
        elasticsearch_index_name=_env("ELASTICSEARCH_INDEX_NAME", "contracts-v1"),
        elasticsearch_ingestion_experiment_index_name=_env(
            "ELASTICSEARCH_INGESTION_EXPERIMENT_INDEX_NAME",
            "contracts-ingestion-experiment-v1",
        ),
        elasticsearch_text_analyzer=_env(
            "ELASTICSEARCH_TEXT_ANALYZER",
            "smartcn",
        ),
        elasticsearch_vector_dimensions=_env("ELASTICSEARCH_VECTOR_DIMENSIONS", "4096"),
        elasticsearch_number_of_shards=_env("ELASTICSEARCH_NUMBER_OF_SHARDS", "1"),
        elasticsearch_number_of_replicas=_env("ELASTICSEARCH_NUMBER_OF_REPLICAS", "0"),
        mllm=MLLMSettings(
            provider=_env("VLLM_MLLM_PROVIDER", "vllm"),
            base_url=_env("VLLM_MLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key=_optional_env("VLLM_MLLM_API_KEY"),
            model=_env("VLLM_MLLM_MODEL", "qwen3.6-35b-a3b-fp8"),
            endpoint=_env("VLLM_MLLM_ENDPOINT", "chat_completions"),
            timeout_seconds=_env("VLLM_MLLM_TIMEOUT_SECONDS", "300"),
            max_concurrent_requests=_env("VLLM_MLLM_MAX_CONCURRENT_REQUESTS", "20"),
            use_media_references=_env(
                "VLLM_MLLM_USE_MEDIA_REFERENCES",
                "true",
            ),
            context_window_tokens=_env(
                "VLLM_MLLM_CONTEXT_WINDOW_TOKENS", "262144"
            ),
            generation=MLLMGenerationSettings(
                enable_thinking=_env("VLLM_MLLM_ENABLE_THINKING", "false"),
                temperature=_env("VLLM_MLLM_TEMPERATURE", "0.7"),
                top_p=_env("VLLM_MLLM_TOP_P", "0.8"),
                top_k=_env("VLLM_MLLM_TOP_K", "20"),
                presence_penalty=_env("VLLM_MLLM_PRESENCE_PENALTY", "1.5"),
                repetition_penalty=_env("VLLM_MLLM_REPETITION_PENALTY", "1.0"),
                seed=_env("VLLM_MLLM_SEED", "3407"),
                max_completion_tokens=_env("VLLM_MLLM_MAX_COMPLETION_TOKENS", "8192"),
            ),
            vision=MLLMVisionSettings(
                max_render_scale=_env("VLLM_MLLM_MAX_RENDER_SCALE", "2.0"),
                visual_token_patch_size=_env("VLLM_MLLM_VISUAL_TOKEN_PATCH_SIZE", "32"),
                max_visual_tokens_per_page=_env(
                    "VLLM_MLLM_MAX_VISUAL_TOKENS_PER_PAGE", "4096"
                ),
                max_visual_tokens_per_request=_optional_env(
                    "VLLM_MLLM_MAX_VISUAL_TOKENS_PER_REQUEST"
                ),
                reserved_prompt_tokens=_env(
                    "VLLM_MLLM_RESERVED_PROMPT_TOKENS", "4096"
                ),
                reserved_runtime_tokens=_env(
                    "VLLM_MLLM_RESERVED_RUNTIME_TOKENS", "10240"
                ),
            ),
        ),
        embedding=EmbeddingSettings(
            provider=_env("VLLM_EMBEDDING_PROVIDER", "vllm"),
            base_url=_env("VLLM_EMBEDDING_BASE_URL", "http://127.0.0.1:8001/v1"),
            api_key=_optional_env("VLLM_EMBEDDING_API_KEY"),
            model=_env("VLLM_EMBEDDING_MODEL", "qwen3-vl-embedding-8b"),
            endpoint=_env("VLLM_EMBEDDING_ENDPOINT", "embeddings"),
            timeout_seconds=_env("VLLM_EMBEDDING_TIMEOUT_SECONDS", "60"),
            batch_size=_env("VLLM_EMBEDDING_BATCH_SIZE", "32"),
            max_concurrent_requests=_env(
                "VLLM_EMBEDDING_MAX_CONCURRENT_REQUESTS", "10"
            ),
            dimensions=_env("VLLM_EMBEDDING_DIMENSIONS", "4096"),
            normalize=_env("VLLM_EMBEDDING_NORMALIZE", "true"),
        ),
        pdf_deduplication=PDFDeduplicationSettings(
            single_shot_visual_token_ratio=_env(
                "PDF_DEDUP_SINGLE_SHOT_VISUAL_TOKEN_RATIO",
                "0.75",
            ),
            single_shot_max_total_pages=_env(
                "PDF_DEDUP_SINGLE_SHOT_MAX_TOTAL_PAGES",
                "20",
            ),
            minimum_recall_cosine_similarity=_env(
                "PDF_DEDUP_MINIMUM_RECALL_COSINE_SIMILARITY",
                "0.60",
            ),
        ),
    )
