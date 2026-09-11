# 合同沟通智能体

> **当前状态：** 文件可读性、逐文件摘要、文件、文字业务相关性及文件文字整体判断已接入正式门禁，并支持订阅激活、流式拒绝提示、历史备份、快照与主动取消。上下文相关性、服务端历史选择及四维加权聚合也已接通；核心问答与其上下文组装尚未接入；可启用[混合联调](../../../capability/application/communication-ui-demo.md)，在真实门禁通过后串接同轮模拟问答。

整体目标、会话轮次边界与待定事项见 [Communication 总体设计](../../system/contract-communication.md)。本页是智能体工作流的唯一包内入口。

---

## 专题导航

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
```

两个子包的职责及当前状态如下：

| 子包 | 预定职责 |
| --- | --- |
| `business_gate` | 可读性之后并发生成文件名称与摘要；四个相关性维度、服务端历史选择和加权阈值聚合已实现。 |
| `agent_core` | 仅包结构；后续实现记忆管理、任务规划与拆解、长期规划，以及合同查询等工具的调度。 |

后续能力通过确定性程序工具或处理专一任务的子智能体接入；本次不预设工具协议或子智能体实现。门禁的状态、节点、调用方式和未实现边界见[业务门禁子图](business-gate.md)。

文件与文字联合判断已接入[全部有序摘要的整体判断](file-text-relevance.md)，一次处理完整文字与文件摘要，支持明确的当前或历史文件指代。

---

## 接口与依赖边界

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
