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
| `app/router/dependency.py` | 查询中间件登录及平台管理员资格，并注入稳定用户身份。 |
| `app.tool` | 存放无业务状态的通用技术工具，例如 PDF 页面渲染和压缩。 |

路由层只负责协议转换和依赖装配；服务层负责业务用例、权限、幂等性和持久化边界；复杂的模型编排与状态流转位于 `agent`。`app.router.contract` 是合同事务的统一 HTTP/SSE 路由模块，当前承载合同上传、查重暂停与继续、提取状态、草稿、重试和正式入库接口，并统一使用 `/knowledge-accumulation/contract/api/contract` 业务前缀。

`app/core/routing.py` 统一定义应用前缀 `/knowledge-accumulation` 与 API 前缀 `/knowledge-accumulation/contract/api`。应用工厂在一次路由挂载中应用 API 前缀，Swagger UI、ReDoc、OpenAPI 和 Swagger OAuth 回调也置于应用前缀下；不使用 `root_path` 模拟路径迁移。待审详情返回的 PDF 地址复用同一 API 常量，旧无前缀地址不保留。调用方和网关须保留完整新路径，数据库内 `file_uri` 与外部中间件接口地址保持自身契约。

`app.bootstrap` 是框架入口与业务实现之间唯一的启动期组合根。`app.main` 只将其 `lifespan` 注册到 FastAPI；`core` 不依赖具体基础设施，`infrastructure` 也不负责组装业务服务。这样可以在不扩大领域层职责的前提下集中管理 Elasticsearch、内存任务服务、正式入库服务和业务目录快照。

`tool` 中的函数不应依赖 FastAPI 请求对象、工作流状态或业务字段定义；`agent` 与 `service` 可调用它们完成 PDF、图像、哈希和稳定序列化等通用处理。

---

## 正式持久化边界

SQLite 是正式合同文件管理的权威目录，Elasticsearch 是完整合同内容和检索后端。应用启动时按 `ELASTICSEARCH_HOSTS` 创建一个无认证 HTTP `AsyncElasticsearch` 客户端并探测 `ELASTICSEARCH_INDEX_NAME`：索引不存在时创建完整合同 mapping，存在时增量补齐配置新增的 Core mapping；随后初始化 `CONTRACT_METADATA_DATABASE_FILE` 指向的 SQLite 文件（默认 `data/abstract/contracts.db`），并对非就绪记录执行 PDF 与 ES 对账。Elasticsearch 不可达、mapping 冲突或启动对账无法完成都会阻止 API 启动；客户端在应用关闭或启动失败时统一释放。

> **安全边界：** 当前客户端不配置身份认证和 TLS，只能连接本机回环地址或由网络层隔离的受信 Elasticsearch 节点，不得直接连接公网暴露的实例。

自动合同处理的原始 PDF 只在创建请求期间存在，随后释放；按视觉预算重新封装的处理版 PDF、同源页面缓存、阶段状态和草稿不写入 Elasticsearch，只驻留当前 API 进程内存。只有用户提交且经外部审核批准的最终对象才允许进入正式索引；完整边界见[合同提取应用运行时](../../architecture/system/contract-extraction-runtime.md)。

需要访问 Elasticsearch 的路由或服务复用应用生命周期中的共享客户端。正式文件列表只读取 SQLite 中的 `ready` 记录，不依赖 ES。SQLite 目录见[合同 SQLite 元数据结构](../../architecture/data/contract-sqlite-metadata.md)，目标索引结构和启动同步边界见[合同 Elasticsearch 文档结构](../../architecture/data/contract-elasticsearch-document.md)；四处持久化流程见[复核后合同正式入库](contract-ingestion.md)。

---

## 启动期业务定义目录

应用启动时从 `CONTRACT_CATEGORY_DEFINITION_DIR` 全量读取合同类别定义及 positive、negative 专家卡片，构造不可变 `ContractCategoryCatalog` 并保存到 `application.state.contract_category_catalog`。默认目录为 `assets/definition/contract-category`，相对路径按项目根目录解析。

加载发生在服务开始接收请求之前；任一文件布局、Schema 或跨类别引用错误都会使启动失败。目录只在每个应用进程启动时读取一次，运行期间分类用例复用同一个内存快照。类别对象、严格校验和内容指纹的完整契约见[合同交易类别定义结构](../../architecture/data/contract-category-definition.md)。

同一生命周期还会从 `FIELD_DEFINITION_DIR` 全量读取 Core YAML，构造不可变 `FieldDefinitionCatalog` 并保存到 `application.state.field_definition_catalog`。Core 必须非空，名称和稳定索引 `code` 必须全局唯一；根目录不允许出现 `core` 之外的职责目录。目录加载完成后，该快照立即用于正式索引 mapping 同步。字段对象结构、目录边界和指纹规则见[模型提取对象定义结构](../../architecture/data/field-definition.md)。

认证服务复用后台平台会话，查询中间件有效用户登录及当前平台管理员列表。独立依赖注入业务路由，用户身份包含 userid、username、departments 和 ttl；departments 仅作用户资料透传，不加载本地用户目录或签发令牌。见[用户身份与所有权](../../architecture/data/user-identity.md)。

---

## 配置与启动

配置从项目根目录的 `.env` 加载；可复制 `.env.example` 作为本地模板。合同类别目录使用 `CONTRACT_CATEGORY_DEFINITION_DIR`，字段定义总目录使用 `FIELD_DEFINITION_DIR`，检索问题指南目录使用 `RETRIEVAL_VIEW_GUIDE_DIR`。`RETRIEVAL_VIEW_MAX_QUESTIONS` 是单份合同由模型提出的问题数量上限，默认值为 `8`，必须大于零；实际问题数量可以更少。Elasticsearch 通过逗号分隔的 `ELASTICSEARCH_HOSTS` 配置节点地址，中文全文字段分析器由 `ELASTICSEARCH_TEXT_ANALYZER` 指定并默认使用 `smartcn`；本机 Compose 的启动、插件、数据卷和安全边界见[Elasticsearch 本地开发部署](../infrastructure/elasticsearch-development.md)。


### 中间件服务配置

通过 `MIDDLEWARE_BASE_URL` 配置中间件服务地址，格式为 `http://<IP>:<端口>` 或 `https://<IP>:<端口>`，例如 `http://127.0.0.1:8000`（示例端口，请替换为实际部署值）。`.env` 和 `.env.example` 默认留空，解析为 `None`；非空值必须为合法 HTTP/HTTPS URL。

本平台身份通过以下配置提供，默认均留空，等待填写中间件分配的信息：

| 环境变量 | 配置属性 | 用途 |
| --- | --- | --- |
| `MIDDLEWARE_PLATFORM_CODE` | `middleware_platform_code` | 本平台登记的 `platform_code`，加载时去除首尾空白。 |
| `MIDDLEWARE_SECRET` | `middleware_secret` | 本平台的登录 `secret`，使用 `SecretStr` 避免配置对象常规输出泄露明文；保留原值，不裁剪凭证。 |

真实 secret 只填写本地环境或部署密钥配置，`.env.example` 保持空值；不得记录到日志。仅在登录请求发送时通过 `get_secret_value()` 读取凭证明文。

应用通过 `get_settings().middleware_base_url` 读取地址。配置随启动加载并缓存，修改后需重启后端。应用生命周期已接入后台平台登录与心跳，详见[中间件平台会话](../infrastructure/middleware-session.md)。中间件不可用不阻止后端启动。

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
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m app.main
```

首次配置及依赖准备见[仓库 README](../../../README.md#启动方式)。Lindera 是需单独准备的原生扩展，不能仅靠安装 Python 依赖完成启动环境。

也可使用 Conda 创建独立的 Python 3.12 开发环境，在项目根目录执行：

```bash
conda create -n contract-service-be python=3.12 pip -y
conda activate contract-service-be
python -m pip install -r requirements.txt
python -m pip check
```

后续开发先执行 `conda activate contract-service-be`；IDE 的 Python 解释器选择该环境中的 `bin/python`。Conda 环境仍需上述外部服务配置与 [Lindera 原生扩展](../infrastructure/sqlite-fts.md)，安装 Python 包不代表外部服务已就绪。

> **本地验证记录：** 本文列出的 `experiment/`、`tests/` 或 `output/` 路径未随源码分发，仅保留历史追溯信息；本次文档核对未重新执行这些实验或核验缺失产物。

运行依赖统一维护在项目根目录的 `requirements.txt`，使用兼容版本区间而不是应用打包元数据。 网页搜索复用 `httpx` 异步请求DeepSeek Search，`trafilatura>=2.2,<3.0` 用于本地 HTML 正文提取；搜索和结果列表工具已接入会话缓存、分页与主循环；网页子图已实现 HTTP、正文提取和模型精炼，打开工具已注册主循环并接入正文缓存与分页，详见[网页工具](../../architecture/workflow/contract-communication/web-search.md)。 历史搜索与正文提取验证见归档报告（本地路径：`experiment/web-search-extraction/REPORT.md`），已记录误召回、权限限制提示遗漏及附件链接丢失等边界。`.venv` 只用于本机隔离且已被 Git 忽略。`app.main` 的 `__main__` 分支直接调用 `uvicorn.run`；从项目根目录执行 `python -m app.main`，默认监听 `0.0.0.0:17000`，设置 `APP_PATH_PREFIX=/dev`，当前 `reload=false`，不自动热重载。开发 API 与文档路径均增加 `/dev`；Docker 的 ASGI 入口仍使用内部端口 `20000` 和生产路径。IDE 运行时应将项目根目录加入 Python 包搜索路径。

热重载会重启唯一工作进程并清空内存合同任务，只适合本地开发。部署入口应由外部 ASGI 进程管理器加载 `app.main:app`，关闭热重载，并继续保持单 worker。

开发入口必须在导入 `bootstrap`、路由或其他引用 `app.core.routing` 的模块之前设置 `APP_PATH_PREFIX=/dev`。前缀在模块首次导入时计算；关闭热重载时，Uvicorn 在当前进程再次导入 `app.main`，不会重算已缓存的 routing 常量。若设置过晚，带 `/dev` 的健康检查、`auth/me` 与 OpenAPI 都会返回路由级 404。修改后须重启开发进程，不能只刷新前端。

本地 `tests/test_development_entrypoint.py` 通过独立进程覆盖直接运行脚本、`python -m app.main`、默认 ASGI 导入和显式 `/dev` 的 ASGI 导入；替换 Uvicorn 启动函数，不运行 lifespan 或连接外部服务，核验所有路由与文档前缀，并确认缺少用户令牌的 `auth/me` 返回 401。

正式写入及启动同步使用 `ELASTICSEARCH_INDEX_NAME=contracts-v1`；入库验收只能使用 `ELASTICSEARCH_INGESTION_EXPERIMENT_INDEX_NAME=contracts-ingestion-experiment-v1`，可删除并按正式 mapping 重建，但绝不能触碰正式索引。向量维度、分片数和副本数分别由 `ELASTICSEARCH_VECTOR_DIMENSIONS`、`ELASTICSEARCH_NUMBER_OF_SHARDS` 和 `ELASTICSEARCH_NUMBER_OF_REPLICAS` 配置。启动同步只处理正式索引，不会自动创建或修改实验索引。

---

## 本地模型配置

两个本地 vLLM 服务均通过环境变量配置，API 密钥分别从 `VLLM_MLLM_API_KEY` 和 `VLLM_EMBEDDING_API_KEY` 读取。

`VLLM_MLLM_TOOL_TAG_FILE` 指定 `assets/tool-tag` 下的工具格式模板文件名，默认 `qwen3.8-flash-next-nvfp4.txt`。应用启动时校验并加载为进程级共享文本，文件错误会阻止启动，运行期间不热更新；读取入口及工作流接入边界见[模型工具调用格式资产](../infrastructure/vllm-chat-template.md#模型工具调用格式资产)。

| 模型 | 默认地址 | 端点 | 主要职责 |
| --- | --- | --- | --- |
| MLLM | `http://127.0.0.1:8000/v1` | `chat_completions` | 合同的 Core、Clause 与 Retrieval Question 生成。 |
| Embedding | `http://127.0.0.1:8001/v1` | `embeddings` | 字段、合同与候选的向量化。 |

单 worker 内所有合同、业务线路与后台任务共享 `VLLM_MLLM_MAX_CONCURRENT_REQUESTS=20`；文本及页面向量化独立共用 `VLLM_EMBEDDING_MAX_CONCURRENT_REQUESTS=10`。节点局部限制继续保留，最终发送由统一客户端执行全局准入；配置与排队边界见[模型全局并发额度](../infrastructure/model-concurrency.md)。应用使用官方异步 `AsyncOpenAI` 客户端和自定义 `base_url` 对接 vLLM；本地服务没有配置 key 时，适配器仅为满足 SDK 初始化提供非敏感占位值。`VLLM_MLLM_USE_MEDIA_REFERENCES=true` 默认启用媒体 UUID 协议，使同页首次上传后只传引用；服务版本、缓存失效和回退要求见[vLLM 多模态媒体引用](../infrastructure/vllm-media-reference.md)。代码在未配置 `VLLM_MLLM_ENABLE_THINKING` 时回退为 `false`；当前 `.env.example` 显式设置为 `True`。以实际环境值为准，节点可按任务配置显式开启。思考开关与 JSON Schema 输出约束分别配置，不能仅因使用结构化输出就要求关闭思考。

MLLM 默认使用 `262144` token 上下文。视觉预算从上下文中扣除 `8192` 最大生成、`4096` 公共提示词和 `10240` 多轮工具历史与安全余量后动态计算；实际视觉预算再随 PDF 页数增长，最大为 `239616`。`VLLM_MLLM_MAX_VISUAL_TOKENS_PER_REQUEST` 留空表示启用动态预算，也可以设置更小的人工上限。

本地 vLLM 必须以相同或更大的上下文启动：

启动命令统一维护在 [vLLM 启动方式](../infrastructure/vllm-chat-template.md#启动方式)，使用当前模型 `Qwen3.8-Flash-Next-NVFP4` 和服务名称 `qwen38-flash-next`。应用默认地址仍为 `8000`；若按该部署示例使用 `6006`，将 `VLLM_MLLM_BASE_URL` 设置为 `http://127.0.0.1:6006/v1`。

该模板及 `tool_placement` 接入契约见 [vLLM 自定义聊天模板](../infrastructure/vllm-chat-template.md)。命令应从项目根目录执行；若从其他目录启动，应将 `--chat-template` 改为模板的绝对路径。

若 vLLM 因 KV cache 容量不足而拒绝 `262144`，应优先降低 `--max-num-seqs`，再根据实际显存调整 `--gpu-memory-utilization`，不能让应用配置的上下文大于服务端上限。配置模型同时校验 MLLM 的非视觉预留和显式视觉上限不超过上下文窗口、重排 `top_n` 不超过候选上限，以及 Embedding 输出维度与 Elasticsearch 向量维度一致。

所有业务路由统一使用 `/knowledge-accumulation/contract/api` 前缀，仅健康检查公开，其余入口统一验证用户登录及本平台管理员资格。任务所有者采用 userid；用户名只用于展示。应用启动时装配合同文档识别图和 PDF 查重图；合同提取服务先执行合同门禁，再使用共享 Elasticsearch 客户端和 `data/contract` 候选加载器执行查重。暂停事件只返回 Elasticsearch 中的友好文件名和 `file_uri`，不内联合同字节或生成运行级下载 URL。应用同时装配受限本地合同文件存储，供[资源文件 API](../../api/resource.md)按 `file_uri` 流式返回正式合同 PDF。登录接口见[用户访问认证 API](../../api/auth.md)，合同上传、合同文档判断、查重暂停、继续、状态、SSE 与重试接口见[合同 API](../../api/contract.md)。ASGI 服务器应加载 `app.main:app`。


## Neo4j 生命周期

`app.bootstrap` 创建共享 `Neo4jClient` 和 `ContractGraphStore`，分别注入 `app.state.neo4j`、`app.state.contract_graph_store`。入库服务初始化时检查连接、创建合同 ID 唯一约束、恢复未完成入库/删除并补建历史节点；失败阻止 API 就绪。正常关闭及启动失败时均释放驱动连接池。具体配置见 [Neo4j 部署与开发连接](../infrastructure/neo4j-development.md)。

---

## 审核反馈后台任务

正式合同存储就绪后还会启动 `file_permission_consumer`，轮询中间件文件等级调整，先原子更新等级与历史，再 ack。目标不存在时记录跳过并确认；关闭时先停止消费者。配置及恢复规则见[文件等级调整消费与历史](file-permission-adjustments.md)。

`IngestionReviewPublisher` 复用平台会话定时上传，利用中间件 `(platform_code, source_id)` 查重，自动重试不确定结果，409重复响应关联此前申请；旧挂起及进程中断申请启动后恢复延迟重试。见[待审请求后台发布](ingestion-review-publisher.md)。

正式入库服务就绪后，lifespan 装配并启动 `IngestionReviewConsumer`，通过已登录的中间件会话周期性拉取审核结果。成功处理后再 ack；网络异常不阻塞启动。关闭时先停止消费者，再释放正式入库使用的 ES、Neo4j 和平台会话。配置及恢复规则见[审核反馈拉取与批准入库](ingestion-review-consumer.md)。

---

## 待审申请清理后台任务

`ingestion_review_cleanup_service` 在审核消费者启动后开始运行，默认按处理完成时间保留 7 天、每小时清理一轮。应用退出时先停止清理并等待当前删除事务，再关闭消费者及存储依赖。配置与重投恢复见[已完成待审申请定时清理](ingestion-review-cleanup.md)。

`deletion_review_publisher` 在正式合同及中间件会话服务就绪后启动，定时将待发送删除申请的业务字段提交中间件，不读取或上传 PDF，201成功或409重复回执保存后进入待审核，不确定结果按原 source_id 定时重试；启动时恢复旧挂起及中断记录。退出时在存储和会话关闭前停止发布；配置及不确定发送边界见[删除审核申请后台发布](deletion-review-publisher.md)。

正式入库服务就绪后还启动 `deletion_review_executor`，定时执行删除审核明确批准的正式合同删除；失败记录并延迟重试。应用退出时先停止该执行器并等待当前删除结束，再释放正式存储依赖。配置、清理范围和恢复边界见[已批准删除申请后台执行](deletion-review-executor.md)。

`deletion_review_service` 复用正式入库服务的文档锁；HTTP 删除入口保存申请并禁止重复申请，不直接清理。启动时及定时扫描通过最新申请协调 can_delete，恢复跨库写入中断和拒绝后标志，见[删除审核存储](../../architecture/data/deletion-review.md)。

`deletion_review_cleanup_service` 在正式删除执行器启动后启动，按批准删除完成或拒绝标志协调完成的本地时间计龄，默认保留7天、每小时清理一轮。关闭时先停止清理并等待当前事务，再停止执行器及释放正式存储依赖。清理只删除审核行并保存最小幂等凭据，不删除引用的正式PDF，配置见[删除审核定时清理](deletion-review-cleanup.md)。

`deletion_review_query_service` 同时装配为只读服务，复用删除审核库、正式元数据和文件存储；路由以登录用户名称限定删除提交人或合同上传人，返回 ID 列表与分区详情，见[删除审核 API](../../api/deletion-review.md)。

删除执行器就绪后启动 `DeletionReviewConsumer`，保存为 `app.state.deletion_review_consumer`，从 `/middleware-service/api/deletion-results/pull` 拉取反馈。批准后复用执行器完成资源删除、拒绝后恢复标志，确认本地处理完成才 ack；关闭时优先停止消费者，再关闭执行器和依赖。配置与恢复规则见[删除审核反馈消费](deletion-review-consumer.md)。

---

## A2A 子平台消费者

`A2A_ENABLED=true` 时 lifespan 在平台会话启动后装配独立 A2A worker，关闭时先停止 A2A，再关闭会话与业务资源。默认不开启；上下文只驻留内存，不接入 Communication 归档与 SSE。配置、外部接口依赖与权限边界见 [A2A 子平台任务运行时](../../architecture/workflow/contract-communication/a2a-runtime.md)。
