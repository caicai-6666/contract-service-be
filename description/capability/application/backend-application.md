# FastAPI 后端应用骨架

> **用途：** 本能力文档说明 HTTP 服务的分层边界、启动期只读业务目录、共享客户端生命周期、配置和应用启动方式。合同处理接口见[合同 API](../../api/contract.md)，状态机见[合同提取应用运行时](../../architecture/system/contract-extraction-runtime.md)。

---

## 分层职责

| 包 | 职责 |
| --- | --- |
| `app.main` | 创建 FastAPI 应用、注册顶层路由，并提供本地开发服务器入口。 |
| `app.bootstrap` | 作为应用组合根，装配服务依赖、加载启动期只读业务目录，并管理共享资源生命周期。 |
| `app.router` | 定义 HTTP 路由并组合版本化接口；合同相关事务统一归入 `app.router.contract`。 |
| `app.schema` | 定义跨能力复用的简单 HTTP Schema。 |
| `app.core` | 加载和校验应用配置。 |
| `app.infrastructure` | 适配 Elasticsearch 等外部系统。 |
| `app.agent` | 存放可复用的合同处理 Agent 工作流。 |
| `app.service` | 承载业务用例及其应用契约，调用 Agent 工作流并协调外部依赖。 |
| `app.user` | 定义审核用户对象，并加载启动期 YAML 内存快照。 |
| `app.tool` | 存放无业务状态的通用技术工具，例如 PDF 页面渲染和压缩。 |

路由层只负责协议转换和依赖装配；服务层负责业务用例、权限、幂等性和持久化边界；复杂的模型编排与状态流转位于 `agent`。`app.router.contract` 是合同事务的统一 HTTP/SSE 路由模块，当前承载合同上传、查重暂停与继续、提取状态、草稿和重试接口，并统一使用 `/contract/api/contract` 业务前缀。

`app.bootstrap` 是框架入口与业务实现之间唯一的启动期组合根。`app.main` 只将其 `lifespan` 注册到 FastAPI；`core` 不依赖具体基础设施，`infrastructure` 也不负责组装业务服务。这样可以在不扩大领域层职责的前提下集中管理 Elasticsearch、内存任务服务和业务目录快照。

`tool` 中的函数不应依赖 FastAPI 请求对象、工作流状态或业务字段定义；`agent` 与 `service` 可调用它们完成 PDF、图像、哈希和稳定序列化等通用处理。

---

## Elasticsearch 边界

Elasticsearch 是当前唯一规划的正式持久化和检索后端。应用启动时按 `ELASTICSEARCH_HOSTS` 创建一个无认证 HTTP `AsyncElasticsearch` 客户端，随后探测 `ELASTICSEARCH_INDEX_NAME`：索引不存在时创建完整合同 mapping，存在时增量补齐配置新增的 Core mapping。Elasticsearch 不可达或 mapping 冲突会阻止 API 启动；客户端在应用关闭或启动失败时统一释放。

> **安全边界：** 当前客户端不配置身份认证和 TLS，只能连接本机回环地址或由网络层隔离的受信 Elasticsearch 节点，不得直接连接公网暴露的实例。

自动合同处理的原始 PDF 只在创建请求期间存在，随后释放；按视觉预算重新封装的处理版 PDF、同源页面缓存、阶段状态和草稿不写入 Elasticsearch，只驻留当前 API 进程内存。只有未来由专家确认后的最终对象才允许进入正式索引；完整边界见[合同提取应用运行时](../../architecture/system/contract-extraction-runtime.md)。

需要访问 Elasticsearch 的路由或服务通过 `get_elasticsearch_client` 注入客户端。复核后合同的目标索引结构和启动同步边界见[合同 Elasticsearch 文档结构](../../architecture/data/contract-elasticsearch-document.md)；当前已经实现索引及 mapping 初始化，但尚未实现正式入库。

---

## 启动期业务定义目录

应用启动时从 `CONTRACT_CATEGORY_DEFINITION_DIR` 全量读取合同类别定义及 positive、negative 专家卡片，构造不可变 `ContractCategoryCatalog` 并保存到 `application.state.contract_category_catalog`。默认目录为 `data/definition/contract-category`，相对路径按项目根目录解析。

加载发生在服务开始接收请求之前；任一文件布局、Schema 或跨类别引用错误都会使启动失败。目录只在每个应用进程启动时读取一次，运行期间分类用例复用同一个内存快照。类别对象、严格校验和内容指纹的完整契约见[合同交易类别定义结构](../../architecture/data/contract-category-definition.md)。

同一生命周期还会从 `FIELD_DEFINITION_DIR` 全量读取 Core YAML，构造不可变 `FieldDefinitionCatalog` 并保存到 `application.state.field_definition_catalog`。Core 必须非空，名称和稳定索引 `code` 必须全局唯一；根目录不允许出现 `core` 之外的职责目录。目录加载完成后，该快照立即用于正式索引 mapping 同步。字段对象结构、目录边界和指纹规则见[模型提取对象定义结构](../../architecture/data/field-definition.md)。

审核用户从 `REVIEWER_USER_FILE` 指定的单个 YAML 文件加载，默认文件为 `data/user/users.yaml`。每个条目被校验并转换为不可变 `ReviewerUser`，完整快照保存到 `application.state.reviewer_user_catalog`；文件不存在、用户为空、名称或密钥重复、密钥为空或出现未定义字段都会阻止应用启动。密钥使用 `SecretStr` 保存在对象中，普通日志与对象表示不会输出明文。生命周期同时创建 `application.state.login_code_cache` 和 `application.state.auth_service`，供登录路由签发与解析免登码。完整用户契约见[审核用户 YAML 定义](../../architecture/data/reviewer-user-definition.md)，接口见[审核用户登录 API](../../api/auth.md)。

---

## 配置与启动

配置从项目根目录的 `.env` 加载；可复制 `.env.example` 作为本地模板。合同类别目录使用 `CONTRACT_CATEGORY_DEFINITION_DIR`，字段定义总目录使用 `FIELD_DEFINITION_DIR`，检索问题指南目录使用 `RETRIEVAL_VIEW_GUIDE_DIR`，审核用户文件使用 `REVIEWER_USER_FILE`。`RETRIEVAL_VIEW_MAX_QUESTIONS` 是单份合同由模型提出的问题数量上限，默认值为 `8`，必须大于零；实际问题数量可以更少。Elasticsearch 通过逗号分隔的 `ELASTICSEARCH_HOSTS` 配置节点地址，中文全文字段分析器由 `ELASTICSEARCH_TEXT_ANALYZER` 指定并默认使用 `smartcn`；本机 Compose 的启动、插件、数据卷和安全边界见[Elasticsearch 本地开发部署](../infrastructure/elasticsearch-development.md)。

`AUTH_LOGIN_CODE_TTL_SECONDS` 控制免登码在当前 API 进程内存中的空闲存活时间，默认 `3600` 秒且必须大于零。缓存使用单调时钟判断过期，不受系统墙上时间调整影响；每次成功校验都会刷新对应免登码的过期点，普通缓存快照不会续期。缓存不持久化，应用关闭或热重载后全部免登码失效。

### 合同处理内存配置

合同处理期间的 PDF、阶段结果和事件只驻留当前 API 进程。以下配置决定内存任务的保留、订阅和重试边界；状态转换语义见[合同提取应用运行时](../../architecture/system/contract-extraction-runtime.md)。

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `CONTRACT_EXTRACTION_RUN_TTL_SECONDS` | `3600` | 最近一次状态更新后的内存保留时间。 |
| `CONTRACT_DEDUPLICATION_REVIEW_TTL_SECONDS` | `600` | 查重结果返回后等待继续请求的固定期限；允许配置更短值，但不能超过 600 秒。 |
| `CONTRACT_EXTRACTION_CLEANUP_INTERVAL_SECONDS` | `30` | 到期任务扫描间隔。 |
| `CONTRACT_EXTRACTION_EVENT_BUFFER_SIZE` | `256` | 每个任务可供 SSE 回放的业务事件数。 |
| `CONTRACT_EXTRACTION_SSE_HEARTBEAT_SECONDS` | `15` | 无业务事件时的 SSE 心跳间隔。 |
| `CONTRACT_EXTRACTION_MAX_STAGE_ATTEMPTS` | `3` | 每个用户业务阶段包含首次执行在内的最大尝试次数。 |

本地开发可从项目根目录直接启动应用：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m app.main
```

运行依赖统一维护在项目根目录的 `requirements.txt`，使用兼容版本区间而不是应用打包元数据。`.venv` 只用于本机隔离且已被 Git 忽略。`app.main` 的 `__main__` 分支直接调用 `uvicorn.run`，因此可以在 IDE 中运行该文件；监听地址、端口、日志级别和热重载开关均在入口代码中显式列出。默认监听 `127.0.0.1:10000` 并开启源码热重载，避免与默认监听 `8000` 的 MLLM 冲突。

热重载会重启唯一工作进程并清空内存合同任务，只适合本地开发。部署入口应由外部 ASGI 进程管理器加载 `app.main:app`，关闭热重载，并继续保持单 worker。

正式写入及启动同步使用 `ELASTICSEARCH_INDEX_NAME=contracts-v1`；入库验收只能使用 `ELASTICSEARCH_INGESTION_EXPERIMENT_INDEX_NAME=contracts-ingestion-experiment-v1`，可删除并按正式 mapping 重建，但绝不能触碰正式索引。向量维度、分片数和副本数分别由 `ELASTICSEARCH_VECTOR_DIMENSIONS`、`ELASTICSEARCH_NUMBER_OF_SHARDS` 和 `ELASTICSEARCH_NUMBER_OF_REPLICAS` 配置。启动同步只处理正式索引，不会自动创建或修改实验索引。

---

## 本地模型配置

三个本地 vLLM 服务均通过环境变量配置，API 密钥分别从 `VLLM_MLLM_API_KEY`、`VLLM_EMBEDDING_API_KEY` 和 `VLLM_RERANKER_API_KEY` 读取。

| 模型 | 默认地址 | 端点 | 主要职责 |
| --- | --- | --- | --- |
| MLLM | `http://127.0.0.1:8000/v1` | `chat_completions` | 合同的 Core、Clause 与 Retrieval Question 生成。 |
| Embedding | `http://127.0.0.1:8001/v1` | `embeddings` | 字段、合同与候选的向量化。 |
| Reranker | `http://127.0.0.1:8002/v1` | `rerank` | 检索候选的重排。 |

MLLM 的三条业务线路共享 `VLLM_MLLM_MAX_CONCURRENT_REQUESTS=20`。应用使用官方异步 `AsyncOpenAI` 客户端和自定义 `base_url` 对接 vLLM；本地服务没有配置 key 时，适配器仅为满足 SDK 初始化提供非敏感占位值。`VLLM_MLLM_USE_MEDIA_REFERENCES=true` 默认启用 vLLM 0.21+ 的媒体 UUID 协议，使同页首次上传后只传引用；服务版本、缓存失效和回退要求见[vLLM 多模态媒体引用](../infrastructure/vllm-media-reference.md)。严格 JSON 提取必须使用 `VLLM_MLLM_ENABLE_THINKING=false`，不能继承模型默认思考模式。

MLLM 默认使用 `262144` token 上下文。视觉预算从上下文中扣除 `8192` 最大生成、`4096` 公共提示词和 `10240` 多轮工具历史与安全余量后动态计算；实际视觉预算再随 PDF 页数增长，最大为 `239616`。`VLLM_MLLM_MAX_VISUAL_TOKENS_PER_REQUEST` 留空表示启用动态预算，也可以设置更小的人工上限。

本地 vLLM 必须以相同或更大的上下文启动：

```bash
export OMP_NUM_THREADS=8
export MKL_NUM_THREADS=8

vllm serve ~/autodl-tmp/model/Qwen3.6-35B-A3B-FP8 \
    --trust-remote-code \
    --quantization fp8 \
    --gpu-memory-utilization 0.58 \
    --max-model-len 262144 \
    --max-num-seqs 512 \
    --enable-prefix-caching \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    --chat-template data/template/qwen3.6-tools-placement.jinja \
    --chat-template-content-format openai \
    --structured-outputs-config '{"backend":"xgrammar"}' \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name qwen3.6-35b-a3b-fp8
```

该模板及 `tool_placement` 接入契约见 [vLLM 自定义聊天模板](../infrastructure/vllm-chat-template.md)。命令应从项目根目录执行；若从其他目录启动，应将 `--chat-template` 改为模板的绝对路径。

若 vLLM 因 KV cache 容量不足而拒绝 `262144`，应优先降低 `--max-num-seqs`，再根据实际显存调整 `--gpu-memory-utilization`，不能让应用配置的上下文大于服务端上限。配置模型同时校验 MLLM 的非视觉预留和显式视觉上限不超过上下文窗口、重排 `top_n` 不超过候选上限，以及 Embedding 输出维度与 Elasticsearch 向量维度一致。

所有业务路由统一使用 `/contract/api` 前缀。`GET /contract/api/health` 和审核用户登录保持公开，其他路由在聚合时统一注入免登码校验依赖；依赖从 `Authorization: Bearer <login_code>` 解析当前审核人名称。应用启动时装配合同文档识别图和 PDF 查重图；合同提取服务先执行合同门禁，再使用共享 Elasticsearch 客户端和 `data/contract` 候选加载器执行查重。暂停事件只返回 Elasticsearch 中的友好文件名和 `file_uri`，不内联合同字节或生成运行级下载 URL。应用同时装配受限本地合同文件存储，供[资源文件 API](../../api/resource.md)按 `file_uri` 流式返回正式合同 PDF。登录接口见[审核用户登录 API](../../api/auth.md)，合同上传、合同文档判断、查重暂停、继续、状态、SSE 与重试接口见[合同 API](../../api/contract.md)。ASGI 服务器应加载 `app.main:app`。
