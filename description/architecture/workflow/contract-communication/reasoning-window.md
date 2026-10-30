# 原生思考随消息保留

> **状态：** Agent Core 已取消独立思考 FIFO、保留轮数配置及独立存储表。完整原生思考随已接受的 assistant 消息保存、备份和恢复。

---

## 收录与上下文边界

主助手开启 thinking 时，完整响应中的 `reasoning` 和 `reasoning_content` 原样附在对应 assistant 工具调用消息中。只有动作通过协议、参数、状态与业务校验并执行成功后，才随调用及反馈一起提交到任务的 `payload.agent_messages`。失败调用、普通文本、截断和纠错轨迹继续遵循[上下文管理规范](../../../standard/agent-context-management.md)，不会成为正式历史。

不按思考条数或独立 token 预算驱逐，不截断单条思考。任务结束后，历史原生消息仍保留完整思考；关闭 thinking 时仅在请求副本中去除原生字段，不删除持久化内容。原生思考不向用户 SSE、快照或公开历史披露，也不写入工作区。

正常任务 FIFO 与总体请求预算继续生效。思考计入任务及完整请求 token；任务被摘要覆盖后，不再把其消息或思考重新注入。摘要输入继续过滤原生思考，不把它转成长期事实。

---

## 请求与模板

Agent Core 生成与 `/tokenize` 使用一致的模板参数：`preserve_thinking=true`、`clear_thinking=false`，防止 Qwen 或 GLM 模板清除较早 assistant 消息中的思考。其他流程未指定该参数时仍采用原有模板默认行为。模板只能保留请求中实际存在的思考，不能恢复已压缩任务。

---

## 存储与旧库升级

思考使用已有 `conversation_records.payload.agent_messages`，没有独立表、独立提交或位置回注服务，随任务终态备份保存。尚未备份的活动任务仍不保证进程重启恢复。

初始化旧库时，在同一事务内按会话、原 `task_id` 和 `call_id` 将 `conversation_reasoning_windows` 的可定位条目迁入对应 assistant 消息，然后删除旧表。缺失原任务或调用时跳过，不伪造轨迹；字段冲突或迁移校验失败回滚事务，不提前删表。重复初始化不会重新创建旧表。

`VLLM_MLLM_REASONING_WINDOW_ROUNDS` 已移除，旧私有 `.env` 中的同名配置不再生效。此前已被旧 FIFO 驱逐的思考无法通过本次升级恢复。

---

## 实现与验证

- `native_messages.py`：接受非空原生思考字段，保持单调用与反馈配对校验。
- `runtime.py`：将完整思考附到待验收消息，成功后统一提交。
- `context.py`：直接读取原生消息；按 thinking 开关控制请求副本。
- `communication_store.py`：旧表迁移删除，任务备份沿用现有事务。

本地 `tests/test_native_thinking.py` 的7项回归已通过，验证多轮完整保留、历史与当前消息装配、公开投影隔离、摘要过滤、模板渲染、生成与计数参数一致、旧库迁移与重复初始化及冲突回滚。真实模型服务对照和回答质量评估需另行执行。
