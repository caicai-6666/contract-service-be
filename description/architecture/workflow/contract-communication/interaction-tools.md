# 用户交互工具与任务结束信号

> **当前状态：** 两个工具的参数 Schema、严格本地校验、消息提交适配和注册表装配已实现；正式消息发送、任务终态及[主模型循环](agent-runtime.md)已接入。

行为规则由 `prompt/interaction.py` 的 `agent-core-interaction-v14` 维护，实现位于 `agent_core/tool/interaction.py`，统一从 `tool` 包导出。调用经过[主助手工具执行](tool-execution.md)的 FIFO 外层，避免绕过容量管理。

---

## 工具定义

| 工具 | 唯一模型参数 | 成功后的行为 |
| --- | --- | --- |
| `emit_progress` | `content`：向用户展示的阶段性结果、必要说明或下一步安排。 | 继续执行当前任务，不结束或暂停等待用户。 |
| `finish_task` | `content`：最终答案、完成范围与限制，或需要用户回答的澄清问题。 | 请求结束本轮；后续用户输入开启新任务。 |

`build_interaction_tools()` 按上述顺序生成独立的函数定义；工具参数均有字段级 description。参数只接受真实 JSON 对象，拒绝空白文本、非字符串、额外字段、重复键和非标准数值；任务和消息身份不能由模型指定。

结束本轮不代表全部业务目标已实现。消息内容保留原有换行与文本，不解析普通文本中的伪工具调用。内容事实性由模型交互规则与业务核验负责，参数校验不能证明回答正确。

---

## 提交与装配

```python
from app.agent.contract_communication.agent_core.tool import UserOutputReceipt

async def publish_output(output):
    # 绑定当前用户和会话，并校验活动任务、取消或替代状态。
    message_id = await submit_bound_message(
        task_id=output.task_id,
        call_id=output.call_id,
        kind=output.kind,
        content=output.content,
    )
    return UserOutputReceipt(message_id=message_id)
```

该示例中的 `submit_bound_message` 由应用适配提供，不是已有项目函数。`UserOutput` 是不可变提交对象，`kind` 为 `intermediate` 或 `final`，任务与调用 ID 来自程序持有的 `FIFOOperation`。

在 `build_tool_management_subgraph(..., publish_output=publish_output)` 中注入回调后自动注册两工具；也可通过 `build_interaction_handlers(publish_output)` 获取处理器映射后交给自定义 `ToolExecutor`。自动注册时拒绝同名覆盖；未提供回调则不会自动启用这些工具，模型可见定义必须与实际注册能力一致。

成功返回 `FIFOExecutionResult(status='succeeded')`，`tool_result` 包含 `status=success`、`message_id`、`finish_requested` 和最小反馈文本；只有最终输出的 `finish_requested` 为 true。用户正文已经在调用参数及提交消息中，不再重复放入工具反馈。

---

## 任务封闭与失败边界

消息提交回调成功表示程序已接受该消息，不表示客户端已经收到。回调必须在消息实际接受后返回回执，不能仅排入未确认的后台操作就报告成功。

最终输出成功后，FIFO 先记录合法调用反馈，主循环再依据 `execution_status=succeeded` 与 `tool_result.finish_requested=true` 保存本次轨迹并将任务封闭为 `completed`，停止本轮请求。工具适配和 FIFO 本身不提前修改任务状态，也不把当前 active 任务暴露为可压缩前缀。即使随后容量处理失败，结束信号和成功结果仍保留，不能重复发送最终消息或继续本轮业务执行。

| 失败情形 | 结果 |
| --- | --- |
| 参数不合法 | `failed`，不调用消息提交回调。 |
| 回调抛出 `UserOutputRejected` | `failed`；此异常只用于确认消息未提交的拒绝。 |
| 回调异常或回执非法 | executor 返回 `unknown`，不能假定消息未发出或任务已结束。 |
| 同一调用重放 | 复用 executor 回执，不再次发送消息。 |

结束后的工具禁用、轮次封闭、展示历史和 FIFO 的协调持久化由主循环及应用运行时负责。当前工具是可注入执行组件，不会单独启动或结束正式 Communication 会话。

---

## 验证

`tests/test_interaction_tools.py` 覆盖两个实际工具 Schema、字段描述、非法参数拒绝、文本保真、两种输出、明确拒绝、提交状态不明、回执防重，以及 FIFO 执行后计数失败时保留最终输出结束信号。测试使用内存消息回调，不发送真实用户消息。


---

## 增量展示

主循环调用模型时启用流式工具响应。`MLLMClient.create_tool_chat_completion` 接受可选的异步 `on_tool_delta` 回调，服务端返回的增量在完整请求生命周期内合并；最终仍返回原有 `MLLMToolCompletion`，供协议和工具校验。其他节点不传回调时沿用非流式请求。全局并发额度持有到流结束，取消或异常时关闭连接，媒体缓存和用量观测继续复用原有链路。

展示逻辑位于 `agent_core/output_stream.py`，只识别 `emit_progress`、`finish_task`，注册表不新增流式属性。工具名及调用身份明确后，程序逐步解码 `content` 字符串并发送 `message.delta`，支持跨块 JSON 转义及 Unicode 代理对；不展示推理、其他工具参数或普通 assistant 文本。正文尚未完整到达时属于临时预览，不能认定工具已成功执行。

完整响应到齐后仍经过单调用协议、参数校验和 FIFO 管理。交互工具执行成功时，同一消息发送 `message.completed(status=completed)`；`finish_task` 再由主循环结束任务。自动压缩可能发生在预览后、正式提交前。没有可增量解码的前缀时，等待完整合法结果再发送正文，不猜补参数。

截断、非法调用或工具拒绝时，通过内部 `CommunicationEventService.interrupt_output` 将已发送预览收束为 `message.completed(status=interrupted)`，可在同一任务继续纠错。方法只允许收束本人的活动消息，从服务端快照读取正文，不能修改已展示文本；无预览不产生空消息。用户取消、替代和执行异常继续由任务终态逻辑收束。每次生成尝试使用独立消息 ID，失败预览仅保留在用户展示历史，不进入成功工具轨迹或摘要事实输入。

前端按消息 ID 累加 delta，并用 completed 中的全文覆盖；必须识别 interrupted，不能将中断预览当作正式答复。任务结束仍以 `turn.status` 为准。

`tests/test_agent_output_stream.py` 覆盖分块转义、推理隔离、流关闭、用量合并、返回前已收到增量、其他工具不展示、截断及参数错误恢复、最终收束和模型轨迹清洁；测试通过可控流替代模型，不代表真实服务的首字延迟。


---

## 主动中途反馈策略

主模型 system 的“用户输出与任务收敛”维护主动沟通规则（`agent-core-interaction-v18`）。连续查找、阅读、比较、核对或求助的任务，在开始较长处理前说明主要安排；出现有价值的阶段性发现时说明已知事实、限制和下一步；遇到影响结果的阻碍而调整方法时说明变化。自动 SSE 状态只描述动作，不能替代这些解释。

反馈应简短、有新增信息，不按固定调用轮数强制插入，不复述详细思考或调用日志。简单任务或已能交付时直接最终答复；必须等待用户补充时仍通过 `finish_task` 结束本轮，不用中途输出假装暂停。工具描述、参数、注册和执行机制不变，不增加程序强制调用逻辑。这是提示词引导，实际调用频率仍需真实任务验证。


### 首次生成的一次性建议

任务首次向主模型发送请求时，在动态消息末尾追加一条 `action_guidance` 类型的 user 角色 `<system-guidence>`（`task-start-guidence-v2`），正文仅包含两点：建议优先调用 `emit_progress`，简短说明需求理解和接下来准备做的事情，然后继续处理；如果已经能够直接回答用户，可以直接调用 `finish_task` 给出最终答复。它仍是建议，不强制首个工具选择。

提示仅加入首轮请求副本，参与该次 token 计数，不加入工作区、驻留任务轨迹、纠错记忆、摘要或持久化内容。下一轮重新装配时自然撤除，即使首轮输出非法也不会重发；恢复已有 assistant/tool 轨迹的任务时不重复注入。不会为提示更改工具选择、禁用其他工具或额外调用模型。

### 定时中途反馈提醒

主循环通过 `ProgressReminder` 为当前运行中的任务维护独立单调时钟，记录上次有效中途反馈与上次提醒时间。配置 `VLLM_MLLM_PROGRESS_REMINDER_INTERVAL_SECONDS` 控制间隔，默认 60 秒，0 表示关闭；拒绝负数和非有限值。进入本次主循环开始计时，恢复执行时重新计时，不持久化计时状态。

达到间隔后，在下一次主模型请求前追加 `progress-reminder-v1` 的 user 角色 `action_guidance`。建议调用 `emit_progress` 告知已收集信息、未确认事项及下一步；无新增信息时可说明阻碍，不编造发现或重复内容，也不输出内部思考。可以直接调用 `finish_task` 完成任务，提醒不强制选择工具。

提醒未被成功的 `emit_progress` 满足时，后续每次请求都重新在尾部注入一条，无需等待下一个间隔。成功提交中途反馈后解除待提醒状态并重新计时；模型只是生成调用、输出参数非法、工具执行失败、普通工具调用或自动 SSE 状态均不算有效反馈。未曾提醒时的主动中途反馈也会重新计时。

提示在页面和纠错上下文装配后加入当前请求副本，参与 token 计数；不放入 `temporary`、驻留任务、工作区、摘要或持久化内容。下一轮重建请求时上一条自然撤除，不叠加提醒。首次开场建议与定时提醒不在同一请求出现。私有审计记录 `progress_reminder_injected`，不改变模型业务轨迹。

工具耗时计入等待反馈的间隔，但不会中断在途工具或模型调用，也不会为提醒额外发起生成；长工具返回后，在下一次正常生成前检查提醒条件。因此间隔不是保证用户必定收到反馈的时限。任务结束后计时对象释放。

`tests/test_progress_reminder.py` 使用可控时钟验证时间边界、关闭、忽略后续提醒、失败反馈不解除、成功反馈重置、尾部位置和不污染权威轨迹；主循环回归测试继续覆盖开场建议及既有工具管理逻辑。
