# FastAPI 后端应用骨架

> **用途：** 本能力文档说明 HTTP 服务的分层边界、启动期只读业务目录、Elasticsearch 客户端生命周期和当前可用的系统接口。

---

## 分层职责

| 包 | 职责 |
| --- | --- |
| `app.main` | 创建 FastAPI 应用，加载只读业务目录，并管理共享客户端的启动与关闭。 |
| `app.api` | 定义 HTTP 路由并组合版本化接口。 |
| `app.schema` | 定义请求和响应的 Pydantic 契约。 |
| `app.core` | 加载和校验应用配置。 |
| `app.infrastructure` | 适配 Elasticsearch 等外部系统。 |
| `app.agent` | 存放可复用的合同处理 Agent 工作流。 |
| `app.service` | 承载业务用例，调用 Agent 工作流并协调外部依赖。 |
| `app.tool` | 存放无业务状态的通用技术工具，例如 PDF 页面渲染和压缩。 |

路由层只负责协议转换和依赖装配；服务层负责业务用例、权限、幂等性和持久化边界；复杂的模型编排与状态流转位于 `agent`。

`tool` 中的函数不应依赖 FastAPI 请求对象、工作流状态或业务字段定义；`agent` 与 `service` 可调用它们完成 PDF、图像、哈希和稳定序列化等通用处理。

---

## Elasticsearch 边界

Elasticsearch 是当前唯一规划的持久化和检索后端。应用启动时使用 HTTPS、CA 证书校验和基本认证创建一个 `AsyncElasticsearch` 客户端，并在关闭时释放；创建客户端本身不探测服务连通性，因此 `/api/health` 仅反映 API 进程是否可用。

需要访问 Elasticsearch 的路由或服务通过 `get_elasticsearch_client` 注入客户端。具体索引、mapping 和查询策略应随相应业务能力建立专题文档和机器可读契约。

---

## 启动期业务定义目录

应用启动时从 `CONTRACT_CATEGORY_DEFINITION_DIR` 全量读取合同类别定义及 positive、negative 专家卡片，构造不可变 `ContractCategoryCatalog` 并保存到 `application.state.contract_category_catalog`。默认目录为 `data/definition/contract-category`，相对路径按项目根目录解析。

加载发生在服务开始接收请求之前；任一文件布局、Schema 或跨类别引用错误都会使启动失败。目录只在每个应用进程启动时读取一次，运行期间分类用例复用同一个内存快照。类别对象、严格校验和内容指纹的完整契约见[合同交易类别定义结构](../architecture/contract-category-definition.md)。

同一生命周期还会从 `FIELD_DEFINITION_DIR` 全量读取 Core 与 Attribute YAML，构造不可变 `FieldDefinitionCatalog` 并保存到 `application.state.field_definition_catalog`。Core 必须非空，Attribute 允许为空；两类名称必须全局唯一。字段对象结构、目录边界和指纹规则见[模型提取对象定义结构](../architecture/field-definition.md)。

---

## 配置与接口

配置从项目根目录的 `.env` 加载；可复制 `.env.example` 作为本地模板。合同类别目录使用 `CONTRACT_CATEGORY_DEFINITION_DIR`，字段定义总目录使用 `FIELD_DEFINITION_DIR`。Elasticsearch 使用逗号分隔的 `ELASTICSEARCH_HOSTS`、`ELASTICSEARCH_USERNAME`、`ELASTICSEARCH_PASSWORD`、`ELASTICSEARCH_CA_CERTS` 和 `ELASTICSEARCH_VERIFY_CERTS`。用户名和密码必须同时配置。

正式写入使用 `ELASTICSEARCH_INDEX_NAME=contracts-v1`；入库验收只能使用 `ELASTICSEARCH_INGESTION_EXPERIMENT_INDEX_NAME=contracts-ingestion-experiment-v1`，可删除并按正式 mapping 重建，但绝不能触碰正式索引。向量维度、分片数和副本数分别由 `ELASTICSEARCH_VECTOR_DIMENSIONS`、`ELASTICSEARCH_NUMBER_OF_SHARDS` 和 `ELASTICSEARCH_NUMBER_OF_REPLICAS` 配置。`data/certs/` 是本地运行时证书目录，已被 Git 忽略。

## 本地模型配置

三个本地 vLLM 服务均通过环境变量配置，API 密钥分别从 `VLLM_MLLM_API_KEY`、`VLLM_EMBEDDING_API_KEY` 和 `VLLM_RERANKER_API_KEY` 读取。

| 模型 | 默认地址 | 端点 | 主要职责 |
| --- | --- | --- | --- |
| MLLM | `http://127.0.0.1:8000/v1` | `chat_completions` | 合同的 Core→Attribute、Clause 与 Abstract 提取。 |
| Embedding | `http://127.0.0.1:8001/v1` | `embeddings` | 字段、合同与候选的向量化。 |
| Reranker | `http://127.0.0.1:8002/v1` | `rerank` | 检索候选的重排。 |

MLLM 的三条业务线路共享 `VLLM_MLLM_MAX_CONCURRENT_REQUESTS=20`。应用使用官方异步 `AsyncOpenAI` 客户端和自定义 `base_url` 对接 vLLM；本地服务没有配置 key 时，适配器仅为满足 SDK 初始化提供非敏感占位值。严格 JSON 提取必须使用 `VLLM_MLLM_ENABLE_THINKING=false`，不能继承模型默认思考模式。

MLLM 默认使用 `65536` token 上下文。视觉预算不再固定为 `18432`，而是从上下文中扣除 `8192` 最大生成、`4096` 公共提示词和 `10240` 多轮工具历史与安全余量后动态计算；实际视觉预算再随 PDF 页数增长，最大为 `43008`。`VLLM_MLLM_MAX_VISUAL_TOKENS_PER_REQUEST` 留空表示启用动态预算，也可以设置更小的人工上限。

本地 vLLM 必须以相同或更大的上下文启动：

```bash
export OMP_NUM_THREADS=8
export MKL_NUM_THREADS=8

vllm serve ~/autodl-tmp/model/Qwen3.6-35B-A3B-FP8 \
    --trust-remote-code \
    --quantization fp8 \
    --gpu-memory-utilization 0.58 \
    --max-model-len 65536 \
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

该模板及 `tool_placement` 接入契约见 [vLLM 自定义聊天模板](vllm-chat-template.md)。命令应从项目根目录执行；若从其他目录启动，应将 `--chat-template` 改为模板的绝对路径。

若 vLLM 因 KV cache 容量不足而拒绝 `65536`，应优先降低 `--max-num-seqs`，再根据实际显存调整 `--gpu-memory-utilization`，不能让应用配置的上下文大于服务端上限。配置模型同时校验 MLLM 的非视觉预留和显式视觉上限不超过上下文窗口、重排 `top_n` 不超过候选上限，以及 Embedding 输出维度与 Elasticsearch 向量维度一致。

当前提供 `GET /api/health`，响应服务状态和运行环境。ASGI 服务器应加载 `app.main:app`。
