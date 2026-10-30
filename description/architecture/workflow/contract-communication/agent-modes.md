# Exploration 与 A2A 交互划分

> **用途：** 将面向用户的 SSE 探索交互与面向调用平台的任务交付分开，复用同一 Agent Core。A2A 已接入中间件子平台任务收发、强制受理和内存响应链，详见 [A2A 子平台任务运行时](a2a-runtime.md)。

---

## 职责与目录

| 包 | 职责 | 对外入口 |
| --- | --- | --- |
| `agent_core` | 模型循环、工具协议、上下文预算、工作区、摘要、页面展示与已接受轨迹提交 | `run_agent_loop(host, bindings, ...)` |
| `exploration` | 用户角色、中途和最终输出、开场及周期沟通提醒、SSE 观察器、现有会话适配 | `run_exploration(service, conversation_id, turn_id, owner, ...)` |
| `a2a` | 跨平台任务角色、唯一最终交付工具、单任务输入输出及内存宿主 | `run_a2a_task(request, ...)` |

上述目录均位于 `app/agent/contract_communication/`。Core 不根据模式名分支，也不导入 SSE 数据类型或调用 Communication 服务；模式工厂在执行前提供 `AgentCoreBindings`。旧 `agent_core.runtime.run_agent_core`、输出观察器及交互工具导出保留为探索兼容入口，正式 bootstrap 已切换到 `run_exploration`。

---

## 交互与提交

| 行为 | exploration | a2a |
| --- | --- | --- |
| 默认输出工具 | `emit_progress`、`finish_task` | `finish_task` |
| 中途正文与沟通提醒 | 保留 | 不装配 |
| 正文流式回调 | `InteractionOutputStream` | 不装配 |
| 工具状态回调 | 既有 `task.progress` | 不装配 |
| 完成结果 | SSE 任务终态及既有历史备份 | 返回一次 `A2ATaskResult` |
| 默认状态来源 | Communication 会话历史与工作区 | 每任务独立内存宿主 |

`RegisteredTool.output_kind` 声明 `intermediate/final`，仅供程序消费，不注入模型 Schema。公共输出适配负责校验输出回执；输出工具必须有必填字符串 `content`。Core 根据注册元数据消费结束信号，不硬编码 `emit_progress` 或 `finish_task`。默认工具（日期计算、三个工作区工具）和扩展工具由同一注册表生成 Schema 与执行绑定，重复工具名直接拒绝。

探索 SSE 的事件类型、消息 ID、预览中断及终态仍遵循[交互工具契约](interaction-tools.md)，工具状态仍遵循[工具执行状态](tool-execution.md#工具执行状态)。开场与周期建议仅进入本次请求副本，不进入成功轨迹或摘要。

A2A 的最终输出先经过工具与 FIFO 校验，记录完整已接受交互，再由宿主完成任务，最后返回结果。失败、取消或容量不足不返回半成品成功对象；已接受的工作区与工具副作用不自动回滚或重放。完整原生 thinking 随已接受 assistant 消息保留，错误轨迹成功后清理，私有审计通过 `audit` 参数由宿主管理。

---

## 宿主与模式绑定

`AgentCoreHost` 提供一致的上下文读取、工作区版本提交、摘要提交、已接受交互提交与任务完成接口。`ExplorationHost` 将这些操作转给现有会话服务，保留当前所有权校验与后台备份。

`AgentCoreBindings` 负责稳定系统提示词、输出工具、可选输出观察器、请求建议策略与工具状态回调。观察器每次生成独立创建，提醒策略每次任务独立创建；两个用户或任务不会共享临时纠错、计时或展示状态。

提示词公共协议为 `agent-core-interaction-v21`，模式交付规则分别由 `exploration-interaction-v1` 与 `a2a-interaction-v1` 维护。版本只在代码中追踪，不写入模型消息；请求组装、固定预算与实际生成使用同一套提示词和工具定义。

---

## A2A 内部使用

受信 Python 调用方可在已初始化模型配置与 tool-tag 的应用中执行：

```python
from app.agent.contract_communication.a2a import A2ATaskRequest, run_a2a_task

result = await run_a2a_task(
    A2ATaskRequest(content="根据所提供的材料核对付款条件并说明证据。"),
    settings=settings.mllm,
    additional_tools=authorized_tools,
)
# result.task_id、result.status、result.content、result.rounds
```

`authorized_tools` 必须由宿主按真实身份和权限绑定，不从任务内容接收工具权限。当前入口不会自动复用某位用户的 Communication 会话、附件或查询池，也不会自动装配正式合同查询工具。任务只有默认工具时不能声称能够读取或发送平台资料；调用方可显式注入已受权的注册项和 `page_resolver`。注入网页图片查看器时，需用 `with viewer.task_scope():` 包围整次调用，保证任何退出路径均释放图片，详见[网页图片阅读](web-images.md)。

独立 Python 进程需要先执行 `initialize_mllm_tool_tag(settings.mllm)`；应用 lifespan 已完成该初始化时不必重复。模型客户端、生成参数与 token 计数继续使用既有配置；跨平台消费者的启用配置见 [A2A 子平台任务运行时](a2a-runtime.md#配置与验证)。

需要在调用后检查私有任务状态时，可先构造 `InMemoryA2ATask(request)` 并传入 `host`。宿主与请求必须一致；每个宿主只允许执行一次，失败或取消不自动重跑。状态依次为 `pending → processing → completed/failed/cancelled`，其中只有完成才返回 `A2ATaskResult`。

内存宿主可接收调用方提供的已接受历史；响应服务负责按 response_id 恢复整条链和工作区。不执行持久化或崩溃恢复，无法安全整理历史时沿用 Core 的容量失败行为。合同工具由 A2A 专属装配器绑定服务端过滤；禁止注入附件、记忆检索或未绑定访问策略的合同工具。

---

## 验证

`tests/test_agent_modes.py` 使用真实共享循环、工具注册和管理图，替换模型与计数接口，验证两种输出模式、SSE 预览及拒绝收束、模式工具隔离、工作区版本提交、失败清理、取消、禁止宿主重复执行和任务并发隔离。结合原生 thinking 回归共15项通过；无真实模型、跨平台服务或数据库联调。
