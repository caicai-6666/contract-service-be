# 合同沟通智能体

> **当前状态：** 已建立门禁初始化子图、输入暂存、轮次创建与订阅激活、SSE、快照和主动取消；具体门禁规则、工作流调用和上下文继承尚未实现。

整体目标、会话轮次边界与待定事项见 [Communication 总体设计](../../system/contract-communication.md)。本页是智能体工作流的唯一包内入口。

---

## 专题导航

- [轮次有序轨迹设计](turn-trace.md)：内存轮次记录、方法与阶段说明交错顺序、压缩和上下文边界，尚未实现。
- [业务门禁子图](business-gate.md)：当前初始化结构与已确认的相关性、PDF 筛选和确认分支。
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
  agent_core/
    __init__.py
```

两个子包的职责及当前状态如下：

| 子包 | 预定职责 |
| --- | --- |
| `business_gate` | 已初始化子图；后续实现用户问题、文件及任务准入条件的业务校验。 |
| `agent_core` | 仅包结构；后续实现记忆管理、任务规划与拆解、长期规划，以及合同查询等工具的调度。 |

后续能力通过确定性程序工具或处理专一任务的子智能体接入；本次不预设工具协议或子智能体实现。门禁的状态、节点、调用方式和未实现边界见[业务门禁子图](business-gate.md)。

---

## 接口与依赖边界

- `business_gate` 导出 `build_business_gate_subgraph()` 和 `BusinessGateSubgraphState`；父包和 `agent_core` 尚无执行入口。HTTP 创建、激活与取消使用独立事件服务，尚不调用子图。
- 门禁子图复用项目已有的 LangGraph 与 `typing_extensions`，不创建模型客户端、网络连接或持久化资源，不需要新增配置或依赖。
- 后续实现遵循 [Agent 工作流包](../../../capability/application/agent-workflow.md)的分层边界；业务门禁不替代 API 和服务层已有的认证、权限校验。
- 后续新增模型提示词或多轮节点时，分别遵循[提示词工程规范](../../../standard/prompt-engineering.md)和[多轮 Agent 上下文与记忆管理规范](../../../standard/agent-context-management.md)。

---

## 验证方式

在项目根目录使用项目 Python 环境执行以下命令，检查三个包可正常导入：

```bash
python -B -c 'import app.agent.contract_communication; import app.agent.contract_communication.business_gate; import app.agent.contract_communication.agent_core'
```

导入检查仅验证包结构，不代表门禁、记忆、规划或合同查询能力已经可用；子图调用验证见[业务门禁子图](business-gate.md)。
