# 合同沟通智能体


整体目标、会话轮次边界与待定事项见 [Communication 总体设计](../../system/contract-communication.md)。本页是智能体工作流的唯一包内入口。

---

## 专题导航

- [网页搜索与正文读取工具](web-search.md)：搜索与列表翻页已注册主循环，支持会话 LRU、结果集引用与折叠；网页打开工具已接入，支持按关注重点精炼、缓存及分页。

- [按问题检索合同](contract-question-search.md)：根据合同能够回答的问题召回，支持已有结果集限定范围，已接入主循环。

- [合同图像相似检索](contract-image-search.md)：全文件图像融合检索、结果集引用与分页查看，已接入主循环。


- [外部专家求助工具](external-expert.md)：文字参数、专家职责、DeepSeek 联网与多轮会话池；已注册主循环，包含隔离、错误反馈与资源释放。

- [记忆检索子图](memory-retrieval.md)：检索契约、规划/召回/融合占位和统一失败返回，尚未注册工具。

- [主助手生成循环](agent-runtime.md)：完整请求计数、工具串行执行、工作区即时提交及摘要插入/备份。

- [门禁后上下文装配](context-assembly.md)：读取同一份驻留工作区、最新摘要及其后的任务，追加当前请求并传入第二层。

- [主助手上下文渲染](context-rendering.md)：历史摘要、工作区与单任务展示，包含附件目录、来源保留及任务结束包络。

- [用户交互工具](interaction-tools.md)：中途输出、最终输出、提交回调与任务结束信号。

- [主助手工具执行](tool-execution.md)：注册表分派、FIFO 外层与工作区内层装配、版本提交及回执保护。

- [FIFO 按主题摘要子图](fifo-summary.md)：联合旧摘要规划主题、并发单主题生成与最终摘要归并已实现。

- [上下文预算](context-budget.md)：扣除固定开销与预留后，按 3:7 分配工作区和轨迹区的契约与纯计算方法。

- [FIFO 管理提示词](fifo-management.md)：模型可见 80%/100% 规则、内部容量判断、完整任务边界、接口计数与未满分支反馈；压缩外围已接通，自动摘要和注册表工具执行器已接入，正式主助手循环已接入。

- [工作区管理子图](workspace-management.md)：接口计数、容量分支、目标保护、多轮自动压缩与统一返回；已通过工具适配器接通版本提交回调。

- [工作区管理工具](workspace-tools.md)：替换、新增和删除的路径、参数及提交边界。

- [Agent Core 主助手设计](agent-core.md)：六部分上下文、角色定位、系统交互与工作区定义提示词初稿，执行循环已实现。
- [轮次有序轨迹设计](turn-trace.md)：内存轮次记录、方法与阶段说明交错顺序、压缩和上下文边界，尚未实现。
- [业务门禁子图](business-gate.md)：文件硬性条件、四维相关性判断与加权阈值聚合。
- [统一拒绝与响应](business-gate.md#统一拒绝与响应)：所有门禁未放行出口统一为 rejected，基于业务日志生成友好回复，失败时兜底。
- [文字业务相关性判定规范](text-business-relevance.md)：业务范围、文字证据边界、约束解码、纠错和私有审计，已接入模型节点。
- [文件业务相关性判断](file-business-relevance.md)：只依据展示名和摘要，逐文件并发三态判断、有限纠错与多文件得分合成。
- [上下文相关性判断](context-relevance.md)：最近五轮可信历史选择、友好渲染、随机样例、严格输出与有限纠错。
- [文件可读性检查子图](file-readability.md)：顺序打开、预算内逐页渲染、内存页面对象与失败短路。
- [用户消息与上下文设计](user-context.md)：用户手动终止、信息补充及方向调整的模型可见语义。
- [多轮对话 API](../../../api/communication.md)：创建、替换、激活、事件、快照和主动取消的契约。
- [事件运行时](../../system/communication-events.md)：内部事件发布、单消息约束、缓存与生命周期。

---

## 用途与组织

本包为面向用户的合同工作助手预留独立入口，与已有合同文档识别、PDF 查重和合同信息抽取工作流并列，不替代或改变现有提取与入库流程。

```text
app/agent/contract_communication/
  __init__.py
  business_gate/
    __init__.py
    state.py
    node.py
    subgraph/
      __init__.py
      file_readability/
        __init__.py
        state.py
        node.py
  agent_core/
    __init__.py
    subgraph/
      __init__.py
      workspace_management/
        __init__.py
        schema.py
        state.py
        node.py
        workflow.py
    tool/
      __init__.py
      workspace.py
    prompt/
      __init__.py
      role.py
      interaction.py
      guidance.py
      workspace.py
```

两个子包的职责及当前状态如下：

| 子包 | 预定职责 |
| --- | --- |
| `business_gate` | 可读性之后并发生成文件名称与摘要；四个相关性维度、服务端历史选择和加权阈值聚合已实现。 |
| `agent_core` | 已提供角色定位、可注入工具模板的交互提示词和统一系统反馈构造方法；上下文组装、生成循环与 FIFO/工作区工具调度已实现；外部查询、规划和记忆检索工具尚未接入。 |

后续能力通过确定性程序工具或处理专一任务的子智能体接入；本次不预设工具协议或子智能体实现。门禁的状态、节点、调用方式和未实现边界见[业务门禁子图](business-gate.md)。

文件与文字联合判断已接入[全部有序摘要的整体判断](file-text-relevance.md)，一次处理完整文字与文件摘要，支持明确的当前或历史文件指代。

---

## 接口与依赖边界

大容量页面的可配置轮数的展示与自动折叠已接入主循环（需页面解析器）；会话资源生命周期管理尚未实现，见[页面内容折叠与资源建模](page-content-management.md)。

- `business_gate` 导出 `build_business_gate_subgraph()` 和 `BusinessGateSubgraphState`；父包和 `agent_core` 尚无执行入口。HTTP 创建仅暂存，正式 `CommunicationWorkflowService` 在首次 SSE 订阅后调用门禁，取消/替代时停止旧生产者。
- 门禁子图复用项目已有的 LangGraph、Pydantic、PyMuPDF 和 MLLM 客户端；视觉判断、摘要及三个已实现的相关性分支发起模型请求，不创建持久化资源，不新增环境变量或依赖。
- 后续实现遵循 [Agent 工作流包](../../../capability/application/agent-workflow.md)的分层边界；业务门禁不替代 API 和服务层已有的认证、权限校验。
- 后续新增模型提示词或多轮节点时，分别遵循[提示词工程规范](../../../standard/prompt-engineering.md)和[多轮 Agent 上下文与记忆管理规范](../../../standard/agent-context-management.md)。

---

## 验证方式

在项目根目录使用项目 Python 环境执行以下命令，检查三个包可正常导入：

```bash
python -B -c 'import app.agent.contract_communication; import app.agent.contract_communication.business_gate; import app.agent.contract_communication.agent_core'
```

导入检查仅验证包结构，不代表门禁、记忆、规划或合同查询能力已经可用；子图调用验证见[业务门禁子图](business-gate.md)。


原生思考独立保留、绝对位置回注与落盘恢复见[原生思考 FIFO 窗口](reasoning-window.md)。

- [用户备注混合检索](contract-note-search.md)：BM25 与向量召回、合同归并及命中备注分页。

- [合同摘要混合检索](contract-summary-search.md)：向量＋BM25、RRF 排名、范围限制与共享分页，已接入主循环。

- [合同名称检索](contract-name-search.md)：以“xxx合同”式名称进行 BM25 检索，复用范围与分页，已注册进入主循环。

- [合同关系检索](contract-relation-search.md)：可选起点筛选、关系描述 BM25＋向量、边结果集及两端合同分页。

- [合同候选查询子图](contract-retrieval.md)：四种文本查询已移入内部工具；主模型使用 `search_contracts` 获取最终结果集引用，再用 `view_contract_candidates` 分页查看，内部只回显池引用与数量。图片和关系工具保持独立。
