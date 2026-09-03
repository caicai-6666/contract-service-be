# Agent 工作流包

> **用途：** `app.agent` 是 PDF 与合同处理 Agent 工作流的唯一归属。工作流由服务层调用，再通过 FastAPI 接口面向外部请求。

---

## 调用边界

```text
外部客户端
  → app.router：HTTP 协议、认证与响应映射
  → app.service：业务用例、幂等性、持久化协调
  → app.agent：合同处理工作流与模型节点编排
  → app.infrastructure：Elasticsearch 与模型服务等外部适配
```

`app.agent` 不直接依赖 FastAPI 的 `Request`、`Response` 或路由对象，也不自行决定 HTTP 状态码。这样同一个工作流既可被同步接口、异步任务、批处理和实验调用，也可独立测试。

---

## 职责

每个工作流应以一个明确的业务目标命名，并包含：输入与输出契约、状态定义、节点拓扑、失败隔离、模型调用边界和必要的审计信息。

- 工作流负责组织 Core、Clause、Retrieval Question、字段发现或审核节点的执行顺序与状态流转。
- 工作流通过依赖注入使用模型客户端、Elasticsearch 适配器或其他基础设施；不得在模块导入时创建网络连接。
- 服务层负责选择并调用工作流，处理请求级权限、幂等性、结果落盘和外部副作用。
- API 层负责将 HTTP 请求转换为服务层输入，并将领域结果或错误映射为响应。
- 新增或修改工作流中的模型提示词时，必须遵循[提示词工程规范](../../standard/prompt-engineering.md)。
- 新增或修改多轮工具会话、工作区或恢复逻辑时，必须遵循[多轮 Agent 上下文与记忆管理规范](../../standard/agent-context-management.md)。

---

## 命名与组织

一个工作流使用独立子包；子包内按实际复杂度划分状态、节点、提示词和测试，不为简单流程预先建立空目录。

```text
app/agent/
  contract_document_detection/
    __init__.py
    workflow.py
    state.py
    node.py
    tool.py
    prompt/
  pdf_deduplication/
    __init__.py
    workflow.py
    state.py
    node.py
  contract_extraction/
    __init__.py
    workflow.py
    state.py
    node.py
    context.py
    subgraph/
        document_understanding/
            __init__.py
            prompt.py
            document_structure/
                __init__.py
                node.py
                state.py
                tool.py
                prompt/
                    __init__.py
                    unit_discovery.py
        classification/
            __init__.py
            node.py
            state.py
        field_extraction/
            __init__.py
            definition.py
            state.py
            tool.py
            core/
                __init__.py
                node.py
                state.py
                prompt/
                    __init__.py
                    extraction.py
        clause_extraction/
            __init__.py
            prompt.py
        retrieval_view_generation/
            __init__.py
            catalog.py
            definition.py
            prompt/
            question_generation/
```

`workflow.py` 只负责主图编排；`node.py` 保持节点粒度的业务操作；`state.py` 定义可序列化的流程状态。每个 `subgraph/` 模块自行定义私有状态、节点和边，并导出已装配的子图构建函数；主图只引用这些子图。

`contract_document_detection`、`pdf_deduplication` 与 `contract_extraction` 是并列工作流。合同文档识别读取处理版 PDF 全部页面，形成有证据的是或否判断；只有合同才进入查重。查重工作流实现页面向量融合、带阈值的 ES Top 3 召回、`data/contract` 本地候选加载、并发编排、预算分流、全量双 PDF 判断和候选按页导航判断。两者均已接入合同创建任务；完整设计见[合同文档识别 Agent 工作流](../../architecture/workflow/contract-document-detection/readme.md)与[PDF 查重 Agent 工作流](../../architecture/workflow/pdf-deduplication/readme.md)。

每个子图都是独立包；子图实际调用模型时，在包内保存自身拥有的指令、版本和消息入口，不为未实现能力预建占位包。字段提取父子图只调用 Core 子图并汇总结果；字段定义契约和工具位于父包，Core 的专属提示词、节点和状态留在自己的包内。模型工具请求统一使用 non-strict Schema 绕过服务端 grammar，参数仍由本地严格契约校验。PDF 检查与渲染由工作流外的异步服务负责；文档结构理解子图使用 `prompt.py` 保存阅读规范、页面编码与唯一 PDF 消息构造器，并在内部 `document_structure/prompt/` 和 `document_structure/visual_grounding/prompt/` 分别保存结构发现与视觉定位节点的专属提示词。

基础前缀组装是主图单节点，确定性上下文函数集中在 `context.py`。它输出“PDF + 文档结构”的 `ContractBaseContext` 供分类子图读取；分类完成后，`assemble_prefill_context` 追加分类结果并输出 `ContractPrefillContext`。三个并行下游子图必须直接复用该最终上下文，再追加自己的任务后缀；主图不再发送通用预热请求。

模型请求使用异步客户端。当前 MLLM 适配器基于官方 `AsyncOpenAI`，通过自定义 `base_url` 对接本地 vLLM；环境变量未配置 key 时只在 SDK 内使用非敏感占位值。


---

## 验证

每个工作流至少验证正常完成、模型失败、Schema 校验失败和外部依赖失败四类路径。涉及工具调用时，还应验证错误调用及反馈只在纠错期间进入模型短期记忆、正确动作被接受后会清除失败轨迹，同时私有审计仍完整保留错误轮次。涉及多节点模型调用时，还应验证公共提示词前缀的确定性、状态中证据的可追溯性，以及重试不会覆盖原始审计信息。
