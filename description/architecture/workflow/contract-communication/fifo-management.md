# FIFO 任务轨迹管理

> **当前状态：** 已实现接口计数、安全前缀选择、容量分支、自动摘要、工具执行与唯一尾节点。正式主循环已接入驻留摘要提交；所有主助手工具（包括工作区增删改）均经过本子图。

---

## 职责与接口

代码位于 `agent_core/subgraph/fifo_management/`。`build_fifo_management_subgraph` 注入 token_budget、counter、executor、compressor、summary_counter 及可选 summary_commit；工具注册由 ToolExecutor 负责，子图不依赖具体工具名称。

输入 `FIFOManagementRequest` 包含当前 fifo、累计 summary 与单个 operation。FIFO 必须恰有一个 active 尾任务，operation.task_id 与之相同；调用方提供真实待执行 assistant 工具消息。每个历史任务具有稳定 task_id，已结束任务可携带 rendered_content 用于计数，活动任务保持原生消息。

输出 `FIFOManagementResult` 分离管理状态 status、实际执行状态 execution_status、result_type、tool_result、全部 system_guidence、验收后的 fifo/summary、更新后的预算及 can_continue。不能根据管理失败推断原工具未执行，也不能因工具成功就忽略容量错误。

---

## 计数和压缩范围

默认计数通过 vLLM `/tokenize`。`fifo-task-mixed-v2` 对已结束任务按展示块计数，对活动任务按包含原生交互的任务 JSON 记账；系统提示参与计数。完整聊天模板与工具定义由[主循环](agent-runtime.md)另做整请求守卫。

压缩范围按 token 用量取前约 70%，不是按任务个数。只选择完整连续前缀；到达目标之后适当扩大，直至最后一个任务为 completed。interrupted/failed 可包含在前缀内部，不能成为最后一项；活动任务永不驱逐。没有安全边界时明确失败，不猜测范围。

系统提示中的范围使用 start_task_id、end_task_id 与完整任务边界。摘要器收到选定任务的业务交互副本；输入前去除派生 rendered_content、system-guidence 与不合格错误交互，避免通过渲染块重新夹带提示。私有审计从不作为摘要源。

---

## 子图路径

```text
count_fifo_tokens → choose_capacity_branch
  满容量 → compress_before_execution → execute_after_compression
  未满   → execute_without_compression
→ recheck_after_execution
  实际满容量 → compress_after_execution
  可继续     → build_final_result
→ build_final_result → END
```

build_final_result 是所有路径的唯一出口；压缩或输入失败也归入此节点，不跳过结果构造。

模型只观察 80% 整理提醒与 100% 自动压缩。内部达到 95% 视作可用容量耗尽；正常工具结果返回后处于 95% 至不足 100% 时，提示“100%”，允许下一次工具调用先压缩再执行。实际达到预算 100% 时执行后压缩，以免再请求模型越界。提示本身计入容量，分支会复核提示加入后的用量。

达到 80% 时，提示模型仅提取选定前缀中仍有效的重要信息到工作区，并说明未及时保存的信息可能在摘要后丢失。不存在优先执行的信息提取特例；工作区工具同样先经过 FIFO 自动压缩，再执行原操作。

---

## 自动摘要与提交

默认 compressor 已调用 [FIFO 自动摘要子图](fifo-summary.md)，按主题规划、并发生成、Reduce 验收得到结构化累计摘要。兼容函数名 summarize_fifo_placeholder 不代表当前仍返回占位结果。

外围验证完整任务范围、摘要结构可序列化、摘要新增用量与剩余轨迹容量。新摘要在轨迹区内部占用配额，FIFO 新预算为旧预算加旧摘要 token 数减新摘要 token 数；压缩后连同提示必须低于内部可用容量阈值。

仅验收通过后调用 `summary_commit(expected_summary, summary, scope)`。生产回调核对旧摘要基线和当前任务状态，将摘要直接插入驻留历史的实际压缩末项之后；后续任务顺延，下一次上下文装配自然采用新边界。执行前压缩先提交摘要，再执行原工具；若后续工具失败，已提交摘要仍有效。

独立使用本子图可不传 summary_commit，仅返回候选；正式主循环必须提供该回调。下一次持久化将摘要与已落盘任务的排序修改放在同一事务，详见[历史驻留](../../system/communication-history.md#自动摘要与驻留排序)。

---

## 失败与验证

- 输入或执行前压缩失败：不执行原操作，不发布未验收摘要。
- 工具明确失败：失败调用不写入正常 FIFO，由主循环进行有界纠错，并在下一次动作成功后清除连续失败链。
- 执行状态 unknown：停止，不猜测副作用，不自动重放。
- 工具成功、执行后压缩失败：保留真实工具结果及已提交工作区，禁止继续普通生成；最终输出已成功时由主循环封闭任务。
- 全部系统提示按来源 fifo/workspace/tool 顺序保留，不能相互覆盖。

单元测试覆盖计数、完整任务边界、容量提示、失败隔离、工具执行次数与摘要前后分支；主循环和 SQLite 联动分别见 `tests/test_agent_core_runtime.py`、`tests/test_agent_summary_persistence.py`。离线测试不代替真实模型摘要质量验收。
