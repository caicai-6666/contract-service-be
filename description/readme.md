# 项目文档导航

> 本页是项目文档的统一入口。请按当前任务选择需要阅读的文档；尚未建立的专题不预留无效链接。

---

## 从哪里开始

| 当前目标 | 建议阅读 |
| --- | --- |
| 了解项目目标、范围、术语与全局约束 | [项目说明](project.md) |
| 新建、修改或审查项目文档 | [文档撰写风格手册](standard/documentation.md) |
| 新增或修改模型提示词 | [提示词工程规范](standard/prompt-engineering.md) |
| 新增或修改多轮 Agent、工具循环或错误恢复 | [多轮 Agent 上下文与记忆管理规范](standard/agent-context-management.md) |
| 修改 PDF 准备、页面事实或文档结构理解 | [PDF 准备服务与文档结构理解子图](architecture/workflow/contract-extraction/document-understanding.md) |
| 设计合同主题与内容单元 | [文档结构发现节点](architecture/workflow/contract-extraction/document-structure.md) |
| 定义供模型读取的动态提取对象 | [模型提取对象定义结构](architecture/data/field-definition.md) |
| 新增或修改 `data/definition` 下的 YAML | 按用途阅读[模型提取对象定义结构](architecture/data/field-definition.md)、[合同交易类别定义结构](architecture/data/contract-category-definition.md)或[检索问题指南定义结构](architecture/data/retrieval-view-definition.md) |
| 进行代码开发或功能扩展 | 先阅读项目根目录的 [`AGENTS.md`](../AGENTS.md)，再阅读[项目说明](project.md) |
| 查找服务入口、全局约定或业务接口 | [API 参考](api/readme.md) |
| 对接 Core 表单定义、未入库运行恢复、合同上传、查重暂停/继续、SSE 进度、Core/Clause 查询或失败阶段重试 | [合同 API](api/contract.md) |
| 修改内存任务、查重暂停 TTL、阶段状态、增量草稿或重试机制 | [合同提取应用运行时](architecture/system/contract-extraction-runtime.md) |
| 设计或实现复核后合同的 Elasticsearch 入库 | [合同 Elasticsearch 文档结构](architecture/data/contract-elasticsearch-document.md) |
| 配置审核用户名称与密钥 | [审核用户 YAML 定义](architecture/data/reviewer-user-definition.md) |
| 对接审核用户登录与免登码 | [审核用户登录 API](api/auth.md) |
| 根据 `file_uri` 读取本地正式合同 PDF | [资源文件 API](api/resource.md) |
| 设计处理版 PDF 的向量召回与多模态查重 | [PDF 查重 Agent 工作流](architecture/workflow/pdf-deduplication/readme.md) |
| 设计查重前的合同文档类型门禁 | [合同文档识别 Agent 工作流](architecture/workflow/contract-document-detection/readme.md) |
| 降低并发视觉请求的内存与网络重复载荷 | [vLLM 多模态媒体引用](capability/infrastructure/vllm-media-reference.md) |

---

## 目录结构

```text
description/
  project.md                 项目目标、范围与全局边界
  readme.md                  文档统一导航
  standard/                  跨模块开发与验证规范
  api/                       面向外部调用方的接口参考
  architecture/
    system/                  系统边界、总体流程与应用状态机
    workflow/
      contract-extraction/   合同提取工作流任务包
      contract-document-detection/ 合同文档识别工作流任务包
      pdf-deduplication/     PDF 查重工作流任务包
    data/                    业务定义与持久化数据契约
  capability/
    application/             后端应用与 Agent 包装配能力
    infrastructure/          外部服务适配、部署与观测能力
    document/                PDF 等文档处理工具
```

根目录只保留项目说明和统一导航，`architecture/` 与 `capability/` 根目录也不直接平铺文档。新增文档必须先判断它描述的是系统关系、工作流、数据契约还是可复用能力，再进入对应分类。

---

## 规范

- [文档撰写风格手册](standard/documentation.md)：约束文档命名、分层、排版、链接和审查方式。
- [实验验证规范](standard/experiment.md)：规定实验方案、运行产物、分析报告和复现检查格式。
- [提示词工程规范](standard/prompt-engineering.md)：规定公共前缀、证据推理协议和结构化输出要求。
- [多轮 Agent 上下文与记忆管理规范](standard/agent-context-management.md)：规定稳定前缀、工作区、短期记忆、临时纠错、私有审计及成功后清理协议。

---

## 架构

### 系统设计

- [合同提取应用运行时](architecture/system/contract-extraction-runtime.md)：说明进程内任务、查重前合同文档门禁、结构识别前的查重暂停、三路并发、增量草稿、重试、事件和 TTL 状态机。

### 工作流任务包

#### 合同文档识别

- [合同文档识别 Agent 工作流](architecture/workflow/contract-document-detection/readme.md)：已实现查重前合同文档类型门禁、权威定义提示词、有限工具循环、私有审计和 SSE 合同/非合同分流。

#### PDF 查重

- [PDF 查重 Agent 工作流](architecture/workflow/pdf-deduplication/readme.md)：已实现处理版 PDF 页面向量融合、ES Top 3 阈值召回、`data/contract` 候选加载、候选集合编排、逐候选预算分流、短 PDF 全量判断和长 PDF 候选按页导航判断，并已在结构识别前接入合同创建流程及确认暂停点。

#### 合同提取

- [合同信息抽取 Agent 工作流](architecture/workflow/contract-extraction/readme.md)：合同提取任务包入口，说明总体拓扑、共享状态和包内专题导航。

- [PDF 准备服务与文档结构理解子图](architecture/workflow/contract-extraction/document-understanding.md)：定义工作流外的 PDF 检查与动态渲染，以及基于标准页面的结构理解。
- [文档结构发现节点](architecture/workflow/contract-extraction/document-structure.md)：定义合同主题、宏观内容单元和精确边界表示。
- [合同分类子图](architecture/workflow/contract-extraction/classification.md)：定义分类公共上下文、逐类别并发判定、结果和失败边界。
- [最终公共前缀组装节点](architecture/workflow/contract-extraction/final-context-assembly.md)：定义分类结果的稳定追加和三个下游分支的统一上下文契约。
- [字段提取子图](architecture/workflow/contract-extraction/field-extraction.md)：定义 Core 目录选择、公共任务组装及逐定义并行提取边界。
- [条款提取子图](architecture/workflow/contract-extraction/clause-extraction.md)：定义候选顺序发现、详情公共上下文确定性组装，以及逐候选并发内容提取。
- [检索问题生成子图](architecture/workflow/contract-extraction/retrieval-view-generation.md)：定义动态问题规划、按规划并发生成、逐问题向量化和合同向量融合。

### 数据契约

- [合同 Elasticsearch 文档结构](architecture/data/contract-elasticsearch-document.md)：定义复核后合同的正式索引结构、启动创建及 Core mapping 增量同步边界。
- [模型提取对象定义结构](architecture/data/field-definition.md)：定义单值或多值扁平对象的 YAML 结构、稳定索引代码、分词策略、基本类型及禁止嵌套约束。
- [合同交易类别定义结构](architecture/data/contract-category-definition.md)：定义一类一文件的交易类别 YAML、类别边界与后续加载约束。
- [检索问题指南定义结构](architecture/data/retrieval-view-definition.md)：定义通用与领域提问 YAML 的字段、目录约束及用户扩展步骤。
- [审核用户 YAML 定义](architecture/data/reviewer-user-definition.md)：定义审核人名称、密钥和启动期内存对象边界。

---

## API

- [API 参考](api/readme.md)：定义服务入口、全局媒体类型、错误格式、健康检查和业务接口导航。
- [审核用户登录 API](api/auth.md)：定义仅凭审核用户密钥签发限时免登码的接口。
- [资源文件 API](api/resource.md)：定义按 Elasticsearch `file_uri` 安全读取本地正式合同 PDF 的接口。
- [合同 API](api/contract.md)：定义 Core 表单目录、PDF 上传、合同文档判断、查重结果、暂停继续、状态与 Core/Clause 查询、SSE 事件、断线续传、失败阶段重试、错误码和前端接入顺序。

---

## 能力

### 应用能力

- [FastAPI 后端应用骨架](capability/application/backend-application.md)：说明应用分层、启动生命周期、Elasticsearch 索引同步、运行方式和环境配置。
- [Agent 工作流包](capability/application/agent-workflow.md)：说明 `app.agent` 与 API、服务层和基础设施的调用边界。

### 基础设施能力

- [Elasticsearch 本地开发部署](capability/infrastructure/elasticsearch-development.md)：说明单节点 Docker Compose、应用连接、启动索引同步、数据卷和本机安全边界。
- [开发合同入库脚本](capability/infrastructure/development-contract-ingestion.md)：调用正式提取流程并为开发测试写入带双融合向量的合同文档。
- [vLLM 自定义聊天模板](capability/infrastructure/vllm-chat-template.md)：说明 Qwen3.6 工具前后置布局、启动参数和接入边界。
- [vLLM 多模态媒体引用](capability/infrastructure/vllm-media-reference.md)：说明页面首次填充、UUID-only 引用、并发协调和 cache-miss 自动重填。
- [模型推理指标观察能力](capability/infrastructure/inference-observability.md)：说明任务局部的 MLLM、Embedding 请求耗时、token 与 vLLM 逐请求指标采集。

### 文档处理能力

- [PDF 页面压缩工具](capability/document/pdf-page-compression.md)：说明 PDF 渲染和动态视觉 token 预算。

---

## 实验

- [合同提取质量与推理指标实验](../experiment/contract-extraction-quality/README.md)：批量验证 `test-data` 合同的结构化提取完成度、失败分布和逐请求推理性能。
- [PDF 页面向量召回实验](../experiment/pdf-page-embedding-recall/README.md)：比较 Qwen3-VL-Embedding 默认与近重复专用指令在单页编码、整份 PDF 平均融合后的召回效果。
- [PDF 页面向量鲁棒性实验](../experiment/pdf-page-embedding-robustness/README.md)：验证正式页面向量策略在缩放、非等比缩放、降质、组合扰动和缺页下的召回稳定性。
- [PDF 页面融合权重实验](../experiment/pdf-page-fusion-weighting/README.md)：在多页合同上比较首页加权、尾页加权与等权平均的召回表现。
- [PDF 全量查重判断实验](../experiment/pdf-full-document-deduplication/README.md)：以变换正例和异源负例验证短 PDF 全量双文档 MLLM 判重的关系准确率、工具协议和推理指标。
- [PDF 分页导航查重实验](../experiment/pdf-page-navigation-deduplication/README.md)：验证严重缺页上传件对长候选的 ES 召回和分页导航重复判断。

项目协作要求见 [`AGENTS.md`](../AGENTS.md)。

---

## 文档职责

不同层级的文档应回答不同问题，避免把全部信息集中到项目说明中：

| 分类 | 回答的问题 | 典型内容 |
| --- | --- | --- |
| 项目总览 | 项目为何存在、边界是什么、有哪些全局规则？ | 目标、范围、术语、质量原则、总览流程 |
| 专题设计 | 为什么这样设计、多个模块如何协作？ | 运行模式、处理流程、数据流、关键取舍 |
| API 参考 | 外部调用方应如何准确接入？ | 方法、路径、请求响应、事件、错误码、示例 |
| 能力说明 | 一个技术能力负责什么、如何运行？ | 依赖、配置、限制、使用方式和验证方式 |
| 参考契约 | 精确且可执行的规则是什么？ | 字段、状态、DTO、Schema、索引定义 |
| 运行维护 | 如何配置、执行、测试和排障？ | 环境初始化、运行命令、监控与故障处理 |
| 实验验证 | 如何复现、验证和分析方案？ | 实验条件、步骤、产物、指标和分析结论 |

> **职责边界：** 项目总览负责稳定的全局信息；架构文档解释内部设计；API 文档服务外部调用方；能力文档说明可复用技术与运行方式；机器可读定义和程序校验负责最终执行约束。

---

## 文档扩展约定

1. 新建文档前先判断其阅读目的，再选择目录和标题；不要因代码文件位于某处而机械建立同名文档。

2. 一项规则、契约或流程只保留一个完整的主文档，其他文档仅保留必要上下文并链接到主文档。

3. 创建专题文档后，应在本页增加入口；目录尚未创建或内容尚不存在时，不添加占位链接。

4. 移动或重命名文档时，应同步更新所有相对链接和本导航页，并检查文档内锚点。

5. 文档命名、结构、排版和审查要求统一遵循[文档撰写风格手册](standard/documentation.md)。
