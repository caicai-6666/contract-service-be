# 门禁后的 Agent Core 上下文装配

> **当前状态：** 正式门禁通过后，已接入一致快照读取、任务适配和三个区域的渲染拼接；支持将装配结果传入注入的 Agent Core 执行入口。[主模型生成循环](agent-runtime.md)、完整系统提示词与工具定义装配、整请求接口计数已接入。

本模块沿用 [Communication 会话驻留](../../system/communication-history.md)作为唯一工作区、摘要和任务存储，使用[上下文渲染函数](context-rendering.md)生成本次请求的派生文本。不创建第二份长期会话状态，不执行旧数据库迁移或清空数据。

---

## 门禁到第二层的顺序

```text
当前请求通过完整业务门禁
→ 关联已生成的文件摘要
→ 批准当前附件准入
→ 将门禁确认的页数与 agent_core_ready 写入当前任务
→ 在共享锁内读取工作区、最新摘要及其后的任务快照
→ 锁外调用 Agent Core 上下文装配函数
→ 传给 agent_core_runner（若已注入）
```

当前文件页数来自可读性门禁的 `open_check.opened_files`，按上传下标及原文件名绑定；数量或身份不一致时拒绝，不能从模型摘要推测。文件的 ID、名称、展示名称、摘要与页数保留在同一份原任务 payload，随现有轨迹备份机制持久化，不新增数据库列。

注册任务时 `payload.agent_core_ready=false`；只有正式门禁通过路径可设置为 true。未通过门禁不会调用上下文装配和第二层入口。当前任务结束后，身份与状态检查阻止继续读取或启动旧轮次。

生产环境完整门禁通过后进入主模型生成循环；直接创建且未注入 runner 的独立服务仍保留“后续问答尚未接入”的兜底反馈。附件已获得准入，随终态备份保存。未通过完整门禁的附件继续保持不可用。独立未绑定历史的事件源可以不生成上下文；正式 agent_core_runner 必须绑定会话历史。

---

## 快照与选择边界

`ConversationHistoryService.get_agent_core_snapshot_locked` 由持共享锁且已校验当前用户的工作流调用：

- 读取当前权威工作区完整快照，包含 revision。
- 在当前任务之前选择 sequence 最大的摘要记录，只选择最新一份。
- 选择该摘要之后、当前任务之前所有 `agent_core_ready=true` 且状态为 completed、cancelled、superseded 或 failed 的任务，保持原顺序；不套用门禁“最近五轮”的限制。
- 排除 rejected、expired、未激活、其他处理中的任务以及未通过门禁的记录。
- 将当前 processing 任务保留在末尾且仅出现一次，包含当前用户问题及附件。
- 返回同一时点的深拷贝，在锁外渲染。用户通过 refresh 加载更早历史，不扩大第二层的最新摘要边界。

展示任务身份采用 `record_id`，对应渲染的 task_id；运行时 turn_id 用于验证当前请求。展示序号沿用会话 sequence，可能因摘要或被过滤记录出现间隔；插入摘要会顺延后续 sequence，稳定身份仍是 record_id。

---

## 最新数据契约与适配

`agent_core/context.py` 提供：

```python
context = assemble_agent_core_context(
    workspace=workspace_snapshot,
    summary=latest_summary_payload,
    records=selected_task_records,
    current_turn_id=current_turn_id,
)
```

返回 `AgentCoreContext(workspace, summary, tasks, text, messages, fifo)`。text 只包含“工作区 → 最新摘要（若有）→ 已结束任务块 → 当前任务用户输入”，不包含当前执行轨迹。当前输入使用 `render_task_input`，不附加执行轨迹或最终输出标题。

**messages 是动态上下文的实际消息序列。** 第一条 user 消息携带 text；随后保留当前任务真实 assistant.tool_calls 与 tool.tool_call_id 消息，系统提示继续以独立 user 消息出现。程序持有的 source 留在原记录和 FIFO 中，传给模型的消息去掉该非协议字段。不能只发送 text，否则会遗漏当前执行轨迹。

fifo 为管理子图提供逐任务快照：已结束任务同时具有派生 rendered_content 和用于摘要过滤的来源消息；当前任务仅持有用户输入及原生消息，不得携带 rendered_content。派生对象不另行写入数据库。

摘要仅接受 `fifo-topic-summary-v2` 的完整 `FIFOTopicSummary`，或 None。存储侧新增 `append_topic_summary(summary=..., expected_sequence=...)`，复用原 summary 记录及尾部乐观检查；此装配入口不读取旧 text 摘要，也不将其虚构为主题。旧 `append_summary(text=...)` 接口仍用于既有独立流程，本次不迁移其数据。主循环压缩使用 insert_agent_summary 在驻留历史中按实际前缀插入；后续备份原子保存新摘要与排序，不使用尾部追加接口。

任务读取现有 `payload.input` 与 `payload.trace`：

- 用户问题来自 input.text，文件来自已 accepted 的 input.files；五项附件元数据必须齐全，缺失时显式失败。
- 有 agent_messages 时，以其真实工具参数和反馈作为模型轨迹；公开 trace 仅用于取得最终正文，不重复拼接中途展示消息。没有 agent_messages 的已结束历史任务可从公开 trace 渲染描述，但当前任务绝不据此伪造 function calling 消息。
- 历史中明确 failed/interrupted 的工具调用及对应结果不进入正常模型轨迹；原驻留历史和审计不修改。
- message 按 message_id 合并流式分片；只采纳 completed 消息，不把中断或 streaming 半成品当成权威输出。
- 中途消息进入执行轨迹；已完成的 final 消息移入最终输出区域，正文不重复出现。
- completed 必须存在实际最终正文；cancelled、superseded、failed 没有最终正文时仅展示结束方式，不生成业务说明。
- 明确封装的 system_guidence 保留；不根据用户正文中的标签判断来源。

这层已支持原生调用配对校验及存储，但不实现主模型生成循环、临时纠错记忆或完整私有审计。未知轨迹类型、重复最终输出、乱序任务和不符合最新 Schema 的数据直接拒绝，不补造默认业务内容。

---

## 执行入口与工作区更新

```python
async def runner(service, conversation_id, turn_id, owner, *, context):
    # context.messages 包含历史文本与当前原生调用；尚需组合完整 system 和 tools。
    ...

service = CommunicationWorkflowService(agent_core_runner=runner)
```

runner 接收已装配的 context，复用当前会话生命周期并必须在返回前结束本轮任务。后续需要最新上下文时，可调用以下接口：

```python
context = await service.get_agent_core_context(conversation_id, turn_id, owner=owner)
```

该方法重新读取同一份驻留数据并渲染。因此工作区工具完成版本提交后，下次读取立即使用最新工作区；旧 context 只是旧请求的快照，不会被后台隐式修改。主循环在工作区提交与原生交互保存之后、下一次模型请求之前重新读取，不复用旧 messages。

FIFO 计数版本为 fifo-task-mixed-v2：有 rendered_content 的已结束任务按实际历史块文本计数，当前任务按带原生消息的 JSON 进行接口记账，仍包括系统提示。后者不是 vLLM 聊天模板展开后的精确计数；角色、工具定义和特殊 token 由主循环另外通过聊天分词接口进行整请求校验。摘要过滤前移除派生 rendered_content，再按原始来源去除系统提示，避免通过历史块夹带提示。

装配层只生成动态部分；主循环注入按实际工作区/摘要渲染文本计数的回调，并负责稳定前缀、工具定义和聊天模板的完整请求校验。

---

## 原生轨迹写入与任务收束

```python
await service.record_agent_core_exchange(
    conversation_id, turn_id, owner=owner,
    assistant_message=actual_assistant_message,
    result=accepted_fifo_execution_result,
)
context = await service.get_agent_core_context(conversation_id, turn_id, owner=owner)
```

该内部接口只接受 succeeded 的执行结果。assistant_message 必须包含恰好一个真实函数调用，arguments 保持原始 JSON 字符串；工具反馈根据实际执行结果构造并匹配调用 ID。校验拒绝重复 ID、半完成配对、多个调用、非法 JSON、普通文本和私有推理字段。配对后的系统提示保存来源，不从文本猜测身份。

每个完整交互批次原子追加到当前任务 payload.agent_messages。相同 ID 和内容重复保存无副作用，不同内容复用 ID 则拒绝。failed/unknown 结果、待执行调用及临时纠错不写入已接受列表，由主循环持有其临时消息和私有审计。该方法不重新执行工具。

任务 completed、cancelled、superseded 或 failed 时，终态投影在提交前调用单任务渲染函数验收完整历史表示，然后冻结同一任务记录。正常最终正文只出现在最终输出区，原生 finish_task 配对不再重复展示；终止、替代或失败无正文时仅展示结束方式。最终消息及原生成功回执须在终态封闭前记录，封闭后拒绝写入。

下一轮装配按终态将该记录放入历史任务区，不再把它的原生消息追加到模型消息列表；新的当前任务建立自己的原生轨迹。数据库仍只有同一条任务记录，保存原始已接受消息与用户展示投影；渲染文本不重复落库，也不额外追加一条“历史任务”记录。后续装配允许重新渲染，避免维护第二份缓存状态。

---

## 验证

`tests/test_agent_core_context.py` 使用临时 SQLite 和模拟门禁/执行入口，覆盖最新摘要边界、refresh 不回填、当前请求、终止状态、未准入历史过滤、附件元数据、工作区更新后的读取、用户隔离、半成品/错误过滤、结构化摘要存储及非法数据拒绝。相关工作流、执行器生命周期和三类渲染测试同时回归；不调用真实模型，也不修改业务数据库。

原生轨迹与终态转换另由 `tests/test_agent_native_context.py` 验证：正常完成、用户终止、方向调整、配对和参数保真、回执防重、终态写入拒绝、当前原生消息与历史块不重复、混合 FIFO 计数及系统提示过滤。


---

## 任务结束后的提示清理

完成、取消、调整方向或失败均通过统一终态投影清理 system-guidence：先验证原生调用/反馈配对及提示来源，再移除独立操作提示，保留成功交互、用户输入和实际最终输出。清理与终态校验共同提交，终态验收失败不会提前修改活动任务。公开轨迹中明确标记为 system_guidence 的条目同样移除，不通过正文标签猜测来源。

活动任务仍保留提示并计入 FIFO 容量；已结束任务的驻留 payload、后台备份和重新加载结果均不携带这些提示。历史渲染额外过滤旧记录中的操作提示，不修改旧数据库。工具结果引用同名标签的普通文本保持原样。执行错误继续只进入临时纠错及私有审计；清理有效任务轨迹不删除私有审计。

`tests/test_agent_native_context.py` 覆盖四种结束方式、备份/重新加载、活动期保留、非法配对不能靠清理通过，以及终态验收失败时的原子性。
