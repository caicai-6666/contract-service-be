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
    context_window_tokens: int = Field(default=65536, gt=0)
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


class RerankerSettings(BaseModel):
    """用于检索重排的本地 vLLM 服务。"""

    model_config = ConfigDict(frozen=True)

    provider: str = "vllm"
    base_url: str = "http://127.0.0.1:8002/v1"
    api_key: str | None = None
    model: str = "qwen3-vl-reranker-8b"
    endpoint: str = "rerank"
    timeout_seconds: int = Field(default=60, gt=0)
    candidate_limit: int = Field(default=20, gt=0)
    top_n: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> Self:
        """重排结果数不能超过候选数。"""
        if self.top_n > self.candidate_limit:
            raise ValueError("RERANKER_TOP_N 不能大于 RERANKER_CANDIDATE_LIMIT")
        return self


class Settings(BaseModel):
    """应用运行所需的不可变配置。"""

    model_config = ConfigDict(frozen=True)

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    elasticsearch_hosts: tuple[str, ...] = ("https://localhost:9200",)
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None
    elasticsearch_ca_certs: Path = Path("data/certs/http_ca.crt")
    elasticsearch_verify_certs: bool = True
    elasticsearch_index_name: str = "contracts-v1"
    elasticsearch_ingestion_experiment_index_name: str = (
        "contracts-ingestion-experiment-v1"
    )
    elasticsearch_vector_dimensions: int = Field(default=4096, gt=0)
    elasticsearch_number_of_shards: int = Field(default=1, gt=0)
    elasticsearch_number_of_replicas: int = Field(default=0, ge=0)
    mllm: MLLMSettings = MLLMSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    reranker: RerankerSettings = RerankerSettings()

    @model_validator(mode="after")
    def validate_integrations(self) -> Self:
        """校验跨组件配置约束。"""
        if bool(self.elasticsearch_username) != bool(self.elasticsearch_password):
            raise ValueError(
                "ELASTICSEARCH_USERNAME 与 ELASTICSEARCH_PASSWORD 必须同时配置"
            )
        if self.embedding.dimensions != self.elasticsearch_vector_dimensions:
            raise ValueError("嵌入模型维度必须与 Elasticsearch 向量维度一致")
        return self

    @property
    def elasticsearch_ca_cert_path(self) -> Path:
        """将相对 CA 路径固定解析到项目根目录。"""
        if self.elasticsearch_ca_certs.is_absolute():
            return self.elasticsearch_ca_certs
        return _PROJECT_ROOT / self.elasticsearch_ca_certs


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
        for host in _env("ELASTICSEARCH_HOSTS", "https://localhost:9200").split(",")
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
        app_host=_env("APP_HOST", "127.0.0.1"),
        app_port=_env("APP_PORT", "8000"),
        log_level=_env("LOG_LEVEL", "INFO"),
        elasticsearch_hosts=_hosts_env(),
        elasticsearch_username=_optional_env("ELASTICSEARCH_USERNAME"),
        elasticsearch_password=_optional_env("ELASTICSEARCH_PASSWORD"),
        elasticsearch_ca_certs=_env("ELASTICSEARCH_CA_CERTS", "data/certs/http_ca.crt"),
        elasticsearch_verify_certs=_env("ELASTICSEARCH_VERIFY_CERTS", "true"),
        elasticsearch_index_name=_env("ELASTICSEARCH_INDEX_NAME", "contracts-v1"),
        elasticsearch_ingestion_experiment_index_name=_env(
            "ELASTICSEARCH_INGESTION_EXPERIMENT_INDEX_NAME",
            "contracts-ingestion-experiment-v1",
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
            context_window_tokens=_env("VLLM_MLLM_CONTEXT_WINDOW_TOKENS", "65536"),
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
        reranker=RerankerSettings(
            provider=_env("VLLM_RERANKER_PROVIDER", "vllm"),
            base_url=_env("VLLM_RERANKER_BASE_URL", "http://127.0.0.1:8002/v1"),
            api_key=_optional_env("VLLM_RERANKER_API_KEY"),
            model=_env("VLLM_RERANKER_MODEL", "qwen3-vl-reranker-8b"),
            endpoint=_env("VLLM_RERANKER_ENDPOINT", "rerank"),
            timeout_seconds=_env("VLLM_RERANKER_TIMEOUT_SECONDS", "60"),
            candidate_limit=_env("VLLM_RERANKER_CANDIDATE_LIMIT", "20"),
            top_n=_env("VLLM_RERANKER_TOP_N", "5"),
        ),
    )
