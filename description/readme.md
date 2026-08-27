# 项目文档导航

> 本页是项目文档的统一入口。请按当前任务选择需要阅读的文档；尚未建立的专题不预留无效链接。

---

## 从哪里开始

| 当前目标 | 建议阅读 |
| --- | --- |
| 了解项目目标、范围、术语与全局约束 | [项目说明](project.md) |
| 新建、修改或审查项目文档 | [文档撰写风格手册](standard/documentation.md) |
| 新增或修改模型提示词 | [提示词工程规范](standard/prompt-engineering.md) |
| 修改 PDF 预处理或页面事实 | [PDF 预处理子图](architecture/subgraph/pdf-preprocessing.md) |
| 设计合同主题与内容单元 | [文档结构发现节点](architecture/subgraph/document-structure.md) |
| 定义供模型读取的动态提取对象 | [模型提取对象定义结构](architecture/field-definition.md) |
| 进行代码开发或功能扩展 | 先阅读项目根目录的 [`AGENTS.md`](../AGENTS.md)，再阅读[项目说明](project.md) |

---

## 目录结构

```text
description/
  project.md                 项目目标、范围与全局边界
  readme.md                  文档统一导航
  standard/                  跨模块开发与验证规范
  architecture/              系统、工作流与数据流设计
    subgraph/                单个 Agent 子图的权威设计
  capability/                可复用应用或技术能力说明
```

根目录只保留项目说明和统一导航。新增文档必须先确定职责层级，不在根目录继续平铺专题文件。

---

## 规范

- [文档撰写风格手册](standard/documentation.md)：约束文档命名、分层、排版、链接和审查方式。
- [实验验证规范](standard/experiment.md)：规定实验方案、运行产物、分析报告和复现检查格式。
- [提示词工程规范](standard/prompt-engineering.md)：规定公共前缀、证据推理协议和结构化输出要求。

---

## 架构与子图

- [合同信息抽取 Agent 工作流](architecture/contract-extraction-agent-workflow.md)：说明预处理、基础前缀组装、分类、最终预热、三个并行任务与合并节点的总体拓扑。
- [模型提取对象定义结构](architecture/field-definition.md)：定义单值或多值扁平对象的 YAML 结构、基数、基本类型及禁止嵌套约束。
- [合同交易类别定义结构](architecture/contract-category-definition.md)：定义一类一文件的交易类别 YAML、类别边界与后续加载约束。
- [PDF 预处理子图](architecture/subgraph/pdf-preprocessing.md)：定义 PDF 检查、逐页事实、动态渲染和文档结构发现。
- [文档结构发现节点](architecture/subgraph/document-structure.md)：定义合同主题、宏观内容单元和精确边界表示。
- [合同分类子图](architecture/subgraph/classification.md)：定义分类单节点的基础前缀输入、私有状态和当前占位边界。
- [最终公共前缀预热子图](architecture/subgraph/preheat.md)：定义分类结果的稳定追加、vLLM 预热和下游复用契约。
- [字段提取子图](architecture/subgraph/field-extraction.md)：定义 Core 两节点并行提取、共享工具及 Core → Attribute 状态边界。
- [条款提取子图](architecture/subgraph/clause-extraction.md)：定义候选顺序发现、详情公共上下文组装与预热，以及逐候选并发内容提取的完整三节点实现。

---

## 能力

- [FastAPI 后端应用骨架](capability/backend-application.md)：说明 API 分层、Elasticsearch 客户端和当前系统接口。
- [Agent 工作流包](capability/agent-workflow.md)：说明 `app.agent` 与 API、服务层和基础设施的调用边界。
- [PDF 页面压缩工具](capability/pdf-page-compression.md)：说明 PDF 渲染和动态视觉 token 预算。
- [vLLM 自定义聊天模板](capability/vllm-chat-template.md)：说明 Qwen3.6 工具前后置布局、启动参数和接入边界。

---

## 实验

- [PDF 公共前缀 Prefill 实验](../experiment/prefill/README.md)：说明本地 vLLM 缓存实验的运行方法、产物和观测指标。
- [PDF 预处理边界验证实验](../experiment/preheat-boundary/README.md)：验证真实合同中页数与渲染分辨率极值样本的预处理可运行性。
- [文档结构发现节点实验](../experiment/document-structure/README.md)：验证首轮总结、单元发现工具循环、极简反馈和显式终止协议。
- [Core 字段提取实验](../experiment/core-field-extraction/README.md)：使用 `test-data` 的五份合同验证 Core 目录边界、单字段工具循环、并行提取和基础值准确性。

项目协作要求见 [`AGENTS.md`](../AGENTS.md)。

---

## 文档职责

不同层级的文档应回答不同问题，避免把全部信息集中到项目说明中：

| 分类 | 回答的问题 | 典型内容 |
| --- | --- | --- |
| 项目总览 | 项目为何存在、边界是什么、有哪些全局规则？ | 目标、范围、术语、质量原则、总览流程 |
| 专题设计 | 为什么这样设计、多个模块如何协作？ | 运行模式、处理流程、数据流、关键取舍 |
| 能力说明 | 一个能力负责什么、如何使用？ | 对外接口、依赖、配置、限制和验证方式 |
| 参考契约 | 精确且可执行的规则是什么？ | 字段、状态、DTO、Schema、索引定义 |
| 运行维护 | 如何配置、执行、测试和排障？ | 环境初始化、运行命令、监控与故障处理 |
| 实验验证 | 如何复现、验证和分析方案？ | 实验条件、步骤、产物、指标和分析结论 |

> **职责边界：** 项目总览负责稳定的全局信息；专题文档负责设计与使用说明；机器可读定义和程序校验负责最终执行约束。

---

## 文档扩展约定

1. 新建文档前先判断其阅读目的，再选择目录和标题；不要因代码文件位于某处而机械建立同名文档。

2. 一项规则、契约或流程只保留一个完整的主文档，其他文档仅保留必要上下文并链接到主文档。

3. 创建专题文档后，应在本页增加入口；目录尚未创建或内容尚不存在时，不添加占位链接。

4. 移动或重命名文档时，应同步更新所有相对链接和本导航页，并检查文档内锚点。

5. 文档命名、结构、排版和审查要求统一遵循[文档撰写风格手册](standard/documentation.md)。
