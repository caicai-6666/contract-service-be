"""从本地环境变量加载应用与模型配置。"""

from functools import lru_cache
from os import getenv
from pathlib import Path
from typing import Literal, Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MLLMGenerationSettings(BaseModel):
    """多模态模型的单次生成参数。"""

    model_config = ConfigDict(frozen=True)

    enable_thinking: bool = False
    reasoning_effort: Literal["low", "medium", "xhigh"] = Field(
        default="xhigh", description="本地 MLLM 默认推理强度 low/medium/xhigh；合同提取和会话业务门禁独立配置，适配层转换模型协议。",
    )
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

    progress_reminder_interval_seconds: float = Field(default=60, ge=0, allow_inf_nan=False, description='主模型中途反馈提醒间隔秒数；0关闭。成功emit_progress后重计，未响应提醒时每轮重注入。')
    page_display_rounds: int = Field(default=5, ge=1, description='临时页面保留的完整模型生成轮数；从首次展示开始计数，落盘仅保存隐藏占位。')
    reasoning_window_rounds: int = Field(default=3, ge=0, description='独立思考 FIFO 的保留轮数，0 表示不回注。')
    reasoning_window_max_tokens: int = Field(default=16384, ge=0, description='思考 FIFO 的 token 上限，按最早完整条目驱逐，0 表示不保留。')
    provider: str = "vllm"
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str | None = None
    model: str = "qwen38-flash-next"
    tool_tag_file: str = Field(
        default="qwen3.8-flash-next-nvfp4.txt",
        description="data/tool-tag 下的工具调用模板文件名；启动时读取，不允许目录路径。",
    )
    endpoint: str = "chat_completions"
    timeout_seconds: int = Field(default=300, gt=0)
    max_concurrent_requests: int = Field(
        default=20, gt=0,
        description="单 worker 内所有 MLLM 客户端共享的在途请求上限，同时供节点局部并发控制使用。",
    )
    use_media_references: bool = True
    context_window_tokens: int = Field(default=262144, gt=0)
    generation: MLLMGenerationSettings = MLLMGenerationSettings()
    vision: MLLMVisionSettings = MLLMVisionSettings()
    extraction_reasoning_effort: Literal["low", "medium", "xhigh"] = Field(
        default="low", description="合同提取全链路的独立推理强度，包含文档识别、质量判断、查重和结构化提取。",
    )
    business_gate_reasoning_effort: Literal["low", "medium", "xhigh"] = Field(
        default="xhigh", description="会话业务门禁的独立推理强度，包含附件可读性、摘要、相关性、主题冲突及拒绝回复。",
    )

    def for_contract_extraction(self) -> Self:
        """构造不可变的提取配置副本，避免并发提取改变会话侧推理强度。"""
        return self.model_copy(update={"generation": self.generation.model_copy(update={
            "enable_thinking": True,
            "reasoning_effort": self.extraction_reasoning_effort,
        })})

    def for_business_gate(self) -> Self:
        """在创建门禁客户端前选择独立强度，不修改共享配置或调用方传入对象。"""
        return self.model_copy(update={"generation": self.generation.model_copy(update={
            "enable_thinking": True,
            "reasoning_effort": self.business_gate_reasoning_effort,
        })})

    def thinking_template_kwargs(self, enable_thinking: bool) -> dict:
        """生成与分词共用模型适配；不依赖可随意设置的服务模型别名。"""
        result = {"enable_thinking": enable_thinking}
        if enable_thinking:
            effort = self.generation.reasoning_effort
            # tool-tag 已显式选择模型协议。原生 DS 编码器会绕过 Jinja，
            # 因而先转换为其接受的数值；自定义 DS 模板也接受相同数值。
            profiles = {
                "deepseek-v4.1-flash.txt": {"low": 50, "medium": 75, "xhigh": 100},
                "glm-5.3-flash.txt": {"low": "low", "medium": "high", "xhigh": "max"},
            }
            result["reasoning_effort"] = profiles.get(self.tool_tag_file, {}).get(effort, effort)
        return result

    @field_validator("tool_tag_file")
    @classmethod
    def validate_tool_tag_file(cls, value: str) -> str:
        """只接受单个文件名，避免配置绕过固定模板目录。"""
        if (
            not value.strip()
            or value != value.strip()
            or value in {".", ".."}
            or any(character in value for character in ("/", "\\", "\x00"))
        ):
            raise ValueError("MLLM 工具调用模板必须是非空文件名，不能包含目录路径")
        return value

    @property
    def tool_tag_path(self) -> Path:
        """模板位置固定于项目 data/tool-tag，不受启动工作目录影响。"""
        return _PROJECT_ROOT / "data" / "tool-tag" / self.tool_tag_file

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


class DeepSeekSettings(BaseModel):
    """外部专家 Responses 连接及生成配置；由宿主显式创建客户端。"""

    model_config = ConfigDict(frozen=True)

    base_url: str = Field(default="https://api.deepseek.com", min_length=1)
    api_key: SecretStr | None = Field(default=None, repr=False)
    model: str = Field(default="deepseek-v4-pro", min_length=1)
    reasoning_effort: Literal["none", "low", "high", "max"] = Field(default="high", description="专家思考强度；none 关闭，low/high/max 开启，统一由此项控制思考开关。")
    timeout_seconds: int = Field(default=300, gt=0)
    max_concurrent_requests: int = Field(default=3, gt=0)
    max_completion_tokens: int = Field(default=8192, gt=0)


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
    max_concurrent_requests: int = Field(
        default=10, gt=0,
        description="单 worker 内文本及页面 Embedding 共用的在途请求上限，同时供节点局部并发控制使用。",
    )
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
    communication_trace_enabled: bool = False
    communication_database_file: Path = Path("data/communication/communication.db")
    sqlite_lindera_extension_path: Path = Field(default=Path('data/extensions/lindera/liblindera_sqlite'), description='Lindera原生扩展路径，可省略平台后缀；相对项目根目录解析。')
    lindera_config_path: Path = Field(default=Path('config/lindera-jieba.yml'), description='进程统一使用的Lindera中文分词配置，相对项目根目录解析。')
    communication_contract_file_cache_max_files: int = Field(
        default=32, gt=0, description='共享合同文件池的最大文件数；达到容量后按 LRU 驱逐闲置模型。',
    )
    communication_session_file_cache_max_files: int = Field(
        default=8, gt=0, description='每个会话附件文件池的最大文件数；不是所有会话的合计上限。',
    )
    communication_memory_query_cache_max_queries: int = Field(default=10, gt=0, description='每个会话记忆查询结果池容量，必须为正整数。')
    communication_memory_query_page_size: int = Field(default=3, gt=0, description='记忆查询每页任务数，必须为正整数。')
    communication_expert_session_cache_max_sessions: int = Field(default=10, gt=0, description='每个会话外部专家会话池容量，必须为正整数。')
    communication_contract_relations_cache_max_contracts: int = Field(default=10, gt=0, description='每个会话合同关联快照池容量，必须为正整数。')
    communication_contract_relations_page_size: int = Field(default=5, gt=0, description='合同关联每页关系数，必须为正整数。')
    communication_contract_notes_cache_max_contracts: int = Field(default=10, gt=0, description='每个会话注意事项快照池容量，正整数。')
    communication_contract_notes_page_size: int = Field(default=5, gt=0, description='注意事项每页记录数，正整数。')
    communication_contract_search_cache_max_queries: int = Field(default=10, gt=0, description='每个会话合同检索结果集LRU容量。')
    communication_web_page_cache_max_entries: int = Field(default=10, gt=0, description='每个会话最多驻留的网页与关注重点精炼结果数。')
    communication_web_page_chars: int = Field(default=3000, gt=0, description='网页精炼内容每页最大字符数。')
    communication_web_search_cache_max_queries: int = Field(default=10, gt=0, description='每个会话驻留网页搜索结果集上限。')
    communication_web_search_page_size: int = Field(default=5, gt=0, description='网页搜索每页候选数量。')
    communication_web_search_max_results: int = Field(default=20, gt=0, description='单次网页搜索最多获取的候选数量。')
    communication_web_search_timeout_seconds: int = Field(default=10, gt=0, description='DDGS底层请求超时秒数。')
    communication_web_search_max_concurrent_requests: int = Field(default=2, gt=0, description='网页搜索并发调用上限。')
    communication_contract_search_page_size: int = Field(default=5, gt=0, description='合同检索结果每页合同数。')
    communication_contract_retrieval_cache_max_queries: int = Field(default=10, ge=1, description='每会话最终合同候选结果集独立LRU容量。')
    communication_contract_retrieval_page_size: int = Field(default=5, ge=1, le=50, description='最终合同候选每页条数。')
    communication_contract_retrieval_max_rounds: int = Field(default=16, ge=1, le=64, description='合同候选子Agent最多工具调用轮数。')
    communication_relation_search_top_k: int = Field(default=10, ge=1, le=50, description='关系检索最多返回边数。')
    communication_relation_search_cache_max_queries: int = Field(default=10, ge=1, description='每会话关系检索结果集驻留上限。')
    communication_relation_search_page_size: int = Field(default=3, ge=1, le=50, description='关系检索每页边数。')
    communication_contract_retrieval_rrf_k: int = Field(default=60, ge=1, description='跨查询 RRF 平滑常数。')
    communication_contract_retrieval_history_weight: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False, description='跨查询 RRF 历史排名权重；本轮为1减该值。')
    communication_contract_clause_search_top_k: int = Field(default=10, ge=1, le=50, description='条款 BM25 检索最多返回的合同数。')
    communication_contract_name_search_top_k: int = Field(default=10, ge=1, le=50, description='名称 BM25 检索最多返回的合同数。')
    communication_contract_summary_search_top_k: int = Field(default=10, ge=1, le=50, description='摘要混合检索最多返回的合同数。')
    communication_contract_note_search_top_k: int = Field(default=10, ge=1, le=50, description='备注混合检索最多返回的合同数。')
    communication_contract_question_search_top_k: int = Field(default=10, ge=1, le=50, description='问题向量检索最多返回的合同数。')
    communication_contract_question_search_minimum_similarity: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False, description='问题检索余弦下限，未配置则不设置门槛。')
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
    # 图数据库当前关闭认证；连接配置预备给后续基础设施接入使用。
    neo4j_uri: str = Field(default="bolt://127.0.0.1:7687", min_length=1)
    neo4j_database: str = Field(default="neo4j", min_length=1)
    neo4j_connection_timeout_seconds: float = Field(default=30, gt=0)
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
    deepseek: DeepSeekSettings = DeepSeekSettings()
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

    @property
    def communication_database_path(self) -> Path:
        """会话数据库相对路径固定解析到项目根目录。"""
        if self.communication_database_file.is_absolute():
            return self.communication_database_file
        return _PROJECT_ROOT / self.communication_database_file


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
        communication_trace_enabled=_env("COMMUNICATION_TRACE_ENABLED", "false"),
        communication_database_file=_env("COMMUNICATION_DATABASE_FILE", "data/communication/communication.db"),
        sqlite_lindera_extension_path=_env('SQLITE_LINDERA_EXTENSION_PATH', 'data/extensions/lindera/liblindera_sqlite'),
        lindera_config_path=_env('LINDERA_CONFIG_PATH', 'config/lindera-jieba.yml'),
        communication_contract_file_cache_max_files=_env("COMMUNICATION_CONTRACT_FILE_CACHE_MAX_FILES", "32"),
        communication_session_file_cache_max_files=_env("COMMUNICATION_SESSION_FILE_CACHE_MAX_FILES", "8"),
        communication_memory_query_cache_max_queries=_env("COMMUNICATION_MEMORY_QUERY_CACHE_MAX_QUERIES", "10"),
        communication_memory_query_page_size=_env("COMMUNICATION_MEMORY_QUERY_PAGE_SIZE", "3"),
        communication_expert_session_cache_max_sessions=_env("COMMUNICATION_EXPERT_SESSION_CACHE_MAX_SESSIONS", "10"),
        communication_contract_relations_cache_max_contracts=_env("COMMUNICATION_CONTRACT_RELATIONS_CACHE_MAX_CONTRACTS", "10"),
        communication_contract_relations_page_size=_env("COMMUNICATION_CONTRACT_RELATIONS_PAGE_SIZE", "5"),
        communication_contract_notes_cache_max_contracts=_env("COMMUNICATION_CONTRACT_NOTES_CACHE_MAX_CONTRACTS", "10"),
        communication_contract_notes_page_size=_env("COMMUNICATION_CONTRACT_NOTES_PAGE_SIZE", "5"),
        communication_contract_search_cache_max_queries=_env("COMMUNICATION_CONTRACT_SEARCH_CACHE_MAX_QUERIES", "10"),
        communication_web_page_cache_max_entries=_env("COMMUNICATION_WEB_PAGE_CACHE_MAX_ENTRIES", "10"),
        communication_web_page_chars=_env("COMMUNICATION_WEB_PAGE_CHARS", "3000"),
        communication_web_search_cache_max_queries=_env("COMMUNICATION_WEB_SEARCH_CACHE_MAX_QUERIES", "10"),
        communication_web_search_page_size=_env("COMMUNICATION_WEB_SEARCH_PAGE_SIZE", "5"),
        communication_web_search_max_results=_env("COMMUNICATION_WEB_SEARCH_MAX_RESULTS", "20"),
        communication_web_search_timeout_seconds=_env("COMMUNICATION_WEB_SEARCH_TIMEOUT_SECONDS", "10"),
        communication_web_search_max_concurrent_requests=_env("COMMUNICATION_WEB_SEARCH_MAX_CONCURRENT_REQUESTS", "2"),
        communication_contract_search_page_size=_env("COMMUNICATION_CONTRACT_SEARCH_PAGE_SIZE", "5"),
        communication_contract_retrieval_cache_max_queries=int(_env("COMMUNICATION_CONTRACT_RETRIEVAL_CACHE_MAX_QUERIES", "10")),
        communication_contract_retrieval_page_size=int(_env("COMMUNICATION_CONTRACT_RETRIEVAL_PAGE_SIZE", "5")),
        communication_contract_retrieval_max_rounds=int(_env("COMMUNICATION_CONTRACT_RETRIEVAL_MAX_ROUNDS", "16")),
        communication_relation_search_top_k=int(_env("COMMUNICATION_RELATION_SEARCH_TOP_K", "10")),
        communication_relation_search_cache_max_queries=int(_env("COMMUNICATION_RELATION_SEARCH_CACHE_MAX_QUERIES", "10")),
        communication_relation_search_page_size=int(_env("COMMUNICATION_RELATION_SEARCH_PAGE_SIZE", "3")),
        communication_contract_retrieval_rrf_k=int(_env("COMMUNICATION_CONTRACT_RETRIEVAL_RRF_K", "60")),
        communication_contract_retrieval_history_weight=float(_env("COMMUNICATION_CONTRACT_RETRIEVAL_HISTORY_WEIGHT", "0.5")),
        communication_contract_clause_search_top_k=int(_env("COMMUNICATION_CONTRACT_CLAUSE_SEARCH_TOP_K", "10")),
        communication_contract_name_search_top_k=int(_env("COMMUNICATION_CONTRACT_NAME_SEARCH_TOP_K", "10")),
        communication_contract_summary_search_top_k=int(_env("COMMUNICATION_CONTRACT_SUMMARY_SEARCH_TOP_K", "10")),
        communication_contract_note_search_top_k=int(_env("COMMUNICATION_CONTRACT_NOTE_SEARCH_TOP_K", "10")),
        communication_contract_question_search_top_k=_env("COMMUNICATION_CONTRACT_QUESTION_SEARCH_TOP_K", "10"),
        communication_contract_question_search_minimum_similarity=_env("COMMUNICATION_CONTRACT_QUESTION_SEARCH_MINIMUM_SIMILARITY", "").strip() or None,
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
        neo4j_uri=_env("NEO4J_URI", "bolt://127.0.0.1:7687"),
        neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
        neo4j_connection_timeout_seconds=_env("NEO4J_CONNECTION_TIMEOUT_SECONDS", "30"),
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
        deepseek=DeepSeekSettings(
            base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            api_key=_optional_env("DEEPSEEK_API_KEY"),
            model=_env("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            reasoning_effort=_env("DEEPSEEK_REASONING_EFFORT", "high"),
            timeout_seconds=_env("DEEPSEEK_TIMEOUT_SECONDS", "300"),
            max_concurrent_requests=_env("DEEPSEEK_MAX_CONCURRENT_REQUESTS", "3"),
            max_completion_tokens=_env("DEEPSEEK_MAX_COMPLETION_TOKENS", "8192"),
        ),
        mllm=MLLMSettings(
            progress_reminder_interval_seconds=_env("VLLM_MLLM_PROGRESS_REMINDER_INTERVAL_SECONDS", "60"),
            page_display_rounds=_env("VLLM_MLLM_PAGE_DISPLAY_ROUNDS", "5"),
            reasoning_window_rounds=_env("VLLM_MLLM_REASONING_WINDOW_ROUNDS", "3"),
            reasoning_window_max_tokens=_env("VLLM_MLLM_REASONING_WINDOW_MAX_TOKENS", "16384"),
            provider=_env("VLLM_MLLM_PROVIDER", "vllm"),
            base_url=_env("VLLM_MLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key=_optional_env("VLLM_MLLM_API_KEY"),
            model=_env("VLLM_MLLM_MODEL", "qwen38-flash-next"),
            tool_tag_file=_env(
                "VLLM_MLLM_TOOL_TAG_FILE", "qwen3.8-flash-next-nvfp4.txt"
            ),
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
                reasoning_effort=_env("VLLM_MLLM_REASONING_EFFORT", "xhigh"),
                temperature=_env("VLLM_MLLM_TEMPERATURE", "0.7"),
                top_p=_env("VLLM_MLLM_TOP_P", "0.8"),
                top_k=_env("VLLM_MLLM_TOP_K", "20"),
                presence_penalty=_env("VLLM_MLLM_PRESENCE_PENALTY", "1.5"),
                repetition_penalty=_env("VLLM_MLLM_REPETITION_PENALTY", "1.0"),
                seed=_env("VLLM_MLLM_SEED", "3407"),
                max_completion_tokens=_env("VLLM_MLLM_MAX_COMPLETION_TOKENS", "8192"),
            ),
            extraction_reasoning_effort=_env("VLLM_MLLM_EXTRACTION_REASONING_EFFORT", "low"),
            business_gate_reasoning_effort=_env("VLLM_MLLM_BUSINESS_GATE_REASONING_EFFORT", "xhigh"),
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
