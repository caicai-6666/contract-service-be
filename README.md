# 现象合同智能管理平台 · 后端

本项目提供合同 PDF 提取、合同库管理和智能问答服务。通过多模态模型生成可复核的结构化合同内容，通过 SQLite、Elasticsearch 和 Neo4j 管理合同元数据、检索内容及文件关系，并以 HTTP/SSE 向前端提供处理进度和回答。

当前 `main` 分支采用多平台审核流程：用户确认入库后先保存待审快照，经中间件送审，批准后才写入正式合同库。前端和 Docker Compose 部署分别由 `contract-service-fe`、`contract-service-deploy` 仓库维护。

## 阅读入口

- [主要能力](#主要能力)
- [技术与依赖](#技术与依赖)
- [目录结构](#目录结构)
- [启动方式](#启动方式)
- [配置与接口](#配置与接口)
- [数据与运行边界](#数据与运行边界)
- [开发与文档](#开发与文档)

详细说明从[项目文档导航](description/readme.md)进入；请求参数、响应和事件协议以[API 参考](description/api/readme.md)及运行服务的 OpenAPI 为准。

---

## 主要能力

| 能力 | 内容 |
| --- | --- |
| 合同提取 | PDF 页面准备、合同判断、文件准入与质量检查、重复/相似判断、结构理解、分类、名称与摘要生成，以及 Core、条款和检索问题提取。 |
| 合同管理 | 合同元数据与摘要、PDF 预览、用户注意事项、不可修改的合同关联、入库审核及删除审核记录。 |
| 智能问答 | 业务门禁后进入 Agent Core，支持合同与附件查看、合同检索、记忆检索、日期计算、合同库统计、网页搜索与精炼、外部专家多轮求助。 |
| 上下文管理 | 工作区增删改、任务轨迹容量管理与结构化摘要、原生思考窗口、页面内容折叠及会话记忆持久化。 |
| 实时反馈 | 合同提取进度、问答中途输出、最终回答、工具执行状态、断线快照及有界 SSE 回放。 |

合同入库的主要流程：

```text
上传 PDF → 准入检查 → 查重 → 分类与结构化提取
          → 用户确认名称、摘要、Core、条款及 note
          → 入库审核区 → 中间件送审 → 外部审核结果
          → 批准后正式入库 / 拒绝后保留审核记录
```

确认入库不等于立即落入正式数据库。发现重复合同会阻断流程；存在相似候选时需要用户确认；没有超过阈值的候选时自动继续。

删除申请先保存申请人备注并将合同 `can_delete` 置为 `false`，原合同仍可查看和检索。后台送审已实现，已批准申请的正式删除及拒绝后的标志恢复也已实现；**删除审核反馈从中间件拉取与 ack 尚未接入**，目前不能视为完整的自动审批闭环。

---

## 技术与依赖

| 组件 | 用途与运行要求 |
| --- | --- |
| Python 3.12 | 与后端 Docker 镜像一致；Python 依赖见 `requirements.txt`。 |
| FastAPI / Uvicorn | HTTP、OpenAPI 与 SSE 服务；后端必须使用单 worker。 |
| LangGraph | 合同提取、业务门禁、工作区、摘要与检索等工作流编排。 |
| vLLM MLLM | 多模态生成、原生思考和函数工具调用；上下文计数还需要可用的 `/tokenize` 接口。 |
| vLLM Embedding | 文本及 PDF 页面向量化；向量维度须与 ES 配置一致。 |
| SQLite / sqlite-vec / Lindera | 元数据、审核与会话存储，以及向量距离计算和中文 FTS5/BM25 检索。Lindera 是额外的原生动态库。 |
| Elasticsearch / SmartCN | 完整合同 Core、条款、检索问题及融合向量的存储和检索。 |
| Neo4j | 合同节点及带描述、创建人、创建时间的无向关联。 |
| 消息中间件 | 平台登录与心跳、入库/删除申请发布，以及入库审核反馈拉取与确认。 |
| DeepSeek / 博查 | 分别提供外部专家与网页搜索能力，使用独立凭证。 |

MLLM、Embedding、消息中间件及外部 API 不由本仓库启动。Compose 提供前后端、ES 和 Neo4j 的编排。

---

## 目录结构

```text
app/
  main.py                    FastAPI 应用及本地开发入口
  bootstrap.py               依赖装配、启动初始化和后台服务生命周期
  agent/                     合同提取、沟通智能体、查重与记忆工作流
  service/                   业务服务、正式入库及审核区后台作业
  infrastructure/            模型、数据库、文件和中间件适配
  router/                    HTTP/SSE 路由
  schema/                    请求、响应及数据契约
  core/                      配置与基础规则
  user/                      用户配置与认证
  tool/                      PDF 等通用技术工具
config/                      Lindera 分词配置与编译依赖锁
data/
  definition/                合同类别、Core 字段与检索问题指南
  tool-tag/                  模型工具协议说明
  template/                  模型部署侧 Jinja 模板
  user/                      用户 YAML 配置
  contract/                  正式合同 PDF
  abstract/                  正式合同 SQLite
  ingestion-review/          入库审核数据库、快照与临时 PDF
  deletion-review/           删除审核数据库及正式文件引用
  communication/             会话数据库与已准入附件
description/                 项目、API、架构、能力及开发规范
.env.example                 后端环境变量模板
Dockerfile                   后端镜像及 Lindera 编译
```

运行数据库、合同 PDF 和本地输出不进入版本控制。`tests/`、`experiment/`、`scripts/` 是本机辅助目录，被 Git 忽略，克隆仓库不会获得这些内容；部署构建不依赖它们。

---

## 启动方式

### Compose 部署

首次部署建议使用同级 `contract-service-deploy` 项目。三个仓库的常见布局为：

```text
项目目录/
  contract-service-be/
  contract-service-fe/
  contract-service-deploy/
```

在 deploy 目录首次准备配置，已有配置时不要覆盖：

```bash
cd ../contract-service-deploy
test -f .env || cp .env.example .env
test -f user.yaml || cp user.yaml.example user.yaml
```

编辑 deploy 的 `.env`，设置同级源码路径：

```dotenv
BACKEND_SOURCE=../contract-service-be
FRONTEND_SOURCE=../contract-service-fe
```

同时填写模型服务地址、中间件平台编码与 secret，并替换 `user.yaml` 中的示例密钥。宿主机服务可使用容器可达的 `host.docker.internal` 地址；容器内的 `127.0.0.1` 指向容器自身。中间件需支持当前的 source_id 去重及审核反馈契约。

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:17080/contract/api/health
```

默认前端为 `http://127.0.0.1:17080/contract/`，后端宿主机端口为 `17000`；两者可在 deploy 配置中调整。镜像自动编译 Linux Lindera 扩展，宿主机无需安装 Rust。详见 deploy 的 README 和[后端镜像说明](description/capability/infrastructure/docker-deployment.md)。

`main` 对应多平台审核；若使用独立应用部署版本，前端、后端和 deploy 应同时切换到匹配的 `local-deploy` 分支，并遵循该版本文档。

### 本地源码运行

在后端根目录安装 Python 依赖：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

启动前需要完成以下配置：

1. 按 `.env.example` 填写本机可达的 ES、Neo4j、MLLM 与 Embedding 地址；宿主机不能直接使用 Compose 服务名 `elasticsearch` 或 `neo4j`。
2. 配置 `REVIEWER_USER_FILE` 对应的用户 YAML，包含 `users` 列表及每人的 `name`、`secret_key`；所有用户拥有统一操作权限。
3. 准备与当前操作系统和 CPU 架构匹配的 Lindera Jieba 动态库，设置 `SQLITE_LINDERA_EXTENSION_PATH` 与 `LINDERA_CONFIG_PATH`。仅执行 pip 安装不能完成这一步，Linux 容器的动态库也不能直接用于 macOS。构建依据见 [SQLite 中文检索依赖](description/capability/infrastructure/sqlite-fts.md)与根目录 Dockerfile。
4. 按需填写中间件、DeepSeek 与博查凭证。中间件配置留空时不登录，审核申请可以本地保存，但不会完成外部送审。

启动开发服务：

```bash
python -m app.main
```

当前开发入口监听 `0.0.0.0:20000` 并开启热重载。需要固定监听地址且关闭热重载时使用：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 20000 --workers 1
```

健康检查为 `http://127.0.0.1:20000/contract/api/health`，Swagger UI 为 `/docs`，OpenAPI JSON 为 `/openapi.json`。健康检查不探测模型和中间件是否可用；完整验证需执行真实提取、问答及送审请求。

---

## 配置与接口

`.env.example` 是本地配置入口，Compose 部署使用 deploy 自己的 `.env.example`。修改环境变量、用户文件或业务定义后需重启；Compose 环境变量变化需重新创建容器，单纯 restart 不会更新环境。

| 配置组 | 主要用途 |
| --- | --- |
| `VLLM_MLLM_*` / `VLLM_EMBEDDING_*` | 模型地址、采样参数、上下文与生成预算、并发，以及主模型、提取和门禁的推理强度。 |
| `ELASTICSEARCH_*` / `NEO4J_*` | 合同索引、向量维度、中文分析器和图数据库连接。 |
| `MIDDLEWARE_*` | 中间件地址、平台身份、登录重试和请求超时。 |
| `INGESTION_REVIEW_*` / `DELETION_REVIEW_*` | 两个审核区的位置、后台扫描、发送重试、处理及清理策略。 |
| `COMMUNICATION_*` | 会话存储与工具结果池、分页、文件缓存和开发追踪。 |
| `DEEPSEEK_*` / `BOCHA_*` | 外部专家和网页搜索连接。 |

所有业务接口位于 `/contract/api`。除健康检查和登录外均需 `Authorization: Bearer <login_code>`；通过用户密钥登录取得免登码，不使用中间件平台令牌访问本平台业务 API。

| 接口文档 | 内容 |
| --- | --- |
| [登录](description/api/auth.md) | 用户密钥登录与免登码。 |
| [合同](description/api/contract.md) | 合同库、提取、SSE、确认入库、删除申请、注意事项与关联。 |
| [会话问答](description/api/communication.md) | 会话与任务创建、事件流、快照、取消及历史恢复。 |
| [入库审核](description/api/ingestion-review.md) | 保留中的申请 ID、状态、双方备注、PDF 和清理时间。 |
| [删除审核](description/api/deletion-review.md) | 本人申请或本人上传合同的删除审核与实际删除状态。 |
| [资源读取](description/api/resource.md) | 正式、待审、提取中及会话附件 PDF。 |

入库确认和删除申请均要求提交申请人 `note`。当前入库审核查询允许所有已登录用户访问；删除审核查询按删除申请人或原合同上传人过滤。会话、附件和提取任务仍执行所有者隔离。

---

## 数据与运行边界

SQLite 保存正式目录、摘要、上传人、passport、注意事项、审核记录及会话状态；ES 保存结构化合同内容和检索向量；Neo4j 保存合同关联。PDF 与数据库共同组成业务数据，备份及迁移需同步覆盖，不能只复制单个数据库文件。

入库审核保存完整快照与临时 PDF；删除审核仅引用正式 PDF。审核记录默认在本地处理完成后保留 7 天，未完成的申请不按提交时间清理。发布结果不确定时按原 source_id 延迟重试；权限、参数或本地数据错误进入 `blocked`，不会自动重试，也不表示审核拒绝。

运行时还需遵守以下边界：

- 后端使用单 worker。登录态、提取任务及工具缓存驻留内存；重启会使登录码失效并丢失未持久化任务。已保存的审核记录与会话数据按现有恢复机制加载。
- 合同类别、Core 和检索指南由 `data/definition` 在启动时加载；动态工具和字段约束从这些定义编译，运行时不自由生成新 Core 字段。
- 中间件暂时不可用不阻止 API 启动；ES、Neo4j、用户或业务定义初始化失败会阻止启动。模型不可用时真实业务请求会失败。
- 同一 platform_code 只有一个活跃中间件会话，避免本机开发服务与容器同时使用同一身份。
- 当前部署中的 ES、Neo4j 关闭认证，按 deploy 配置限制网络访问；公网业务入口使用 HTTPS 和合适的反向代理，SSE 需关闭响应缓冲。

---

## 开发与文档

开发前阅读 [AGENTS.md](AGENTS.md)及[项目说明](description/project.md)。代码、提示词、接口与文档应同步维护；具体规则见[文档风格](description/documentation.md)、[提示词规范](description/standard/prompt-engineering.md)和[多轮 Agent 上下文规范](description/standard/agent-context-management.md)。

深入实现可从以下专题进入：

- [合同提取工作流](description/architecture/workflow/contract-extraction/readme.md)
- [合同沟通智能体](description/architecture/workflow/contract-communication/readme.md)
- [合同候选检索](description/architecture/workflow/contract-communication/contract-retrieval.md)
- [正式合同元数据](description/architecture/data/contract-sqlite-metadata.md)
- [中间件平台会话](description/capability/infrastructure/middleware-session.md)
- [入库审核发布](description/capability/application/ingestion-review-publisher.md)与[反馈处理](description/capability/application/ingestion-review-consumer.md)
- [删除审核发布](description/capability/application/deletion-review-publisher.md)与[正式删除执行](description/capability/application/deletion-review-executor.md)
