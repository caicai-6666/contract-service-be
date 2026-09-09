# 记忆筛选工具

## 用途与契约

实现位于 `app/agent/conversation_memory/tool.py`，版本为 `conversation-memory-tools-v7`。工具只负责当前批次的筛选动作，不调用模型、不生成向量、不读附件或写入数据库。v7 允许将明确用户意图展开为完整陈述，但不得新增事实或强化确认状态；保留单任务焦点，不新增参数或恢复跨任务引用。语义来源仍需模型遵循和人工复核。相关任务定义见[筛选提示词](planning-prompt.md)。

| 工具 | 必填参数 | 行为 |
| --- | --- | --- |
| `think` | `reasoning_summary`，1–2000 字符 | 允许当前任务相关推理，如梳理目标和任务关系、比较信息价值、识别冲突、规划提取重点或下一步动作；不提交选择。 |
| `select_task` | `task_number`，正整数；`evidence`、`reasoning_summary`，各 1–1500 字符；`extraction_requirements`，1–2000 字符 | 选择一项任务，提供该任务自身的可定位依据、检索价值和提取要求；真实 task_id 由程序映射。 |
| `finish_selection` | `reasoning_summary`，1–1500 字符 | 声明已检查全部任务且已提交全部选择，说明结束依据；允许全部跳过。 |

所有文本拒绝纯空白。模型可见参数的含义、来源和边界均写入实际 JSON Schema 的字段 description。工具采用 `strict:false`、`tool_choice:auto`，客户端用严格 Pydantic 校验类型和额外参数，不将数字字符串或布尔值转换为任务序号。重复 JSON 属性、未知工具、非法 JSON 和非对象参数均拒绝；当前参数没有嵌套对象，不额外解码文本内的 JSON。

select_task 不接收跨任务引用参数，正式计划也不保存来源任务ID；仅保留本任务自身 task_number 到 task_id 的身份映射。遇到本任务内无法解释的指代，保留原称谓，不从其他任务补全。

---

## 动作状态与结果

每批创建独立 `MemoryPlanningTools(request)`，通过 `execute(tool_calls)` 接收一轮 OpenAI 字典格式的工具调用，成功返回 `MemoryToolFeedback`，失败抛出 `ValueError` 或其子类 `ValidationError`。

- 每轮必须恰好一个 function 调用，首个成功动作必须是 think。
- 不允许连续成功调用 think；失败不推进顺序，也不能借失败绕过此限制。
- select_task 序号必须在本批范围内且严格递增，允许跳过，不允许回头或重复。
- 参数、身份和顺序校验成功后才更新状态，失败不污染已接受选择。
- `selected_tasks` 只读地返回已接受选择，不代表筛选已经完成。
- `result` 在 finish_selection 成功前为 None，成功后包含有序 selected_tasks、按原批次顺序排列的 skipped_task_ids 和结束说明。此后任何调用均拒绝。
- think 不进入正式选择或最终结果；节点1保留think及工具交互，不因select_task成功而裁剪。纠错成功后只移除程序插入的system_guidence，失败调用及工具反馈仍保留，但不改变权威选择；私有审计完整保留。

程序只能验证结构、范围和顺序，不能仅凭自然语言依据证明任务确有检索价值，也不能证明模型实际上检查了全部任务；finish_selection 的全部检查声明仍须通过实际模型实验评估。

---

## 调用方式与接入边界

```python
import json
from app.agent.conversation_memory.tool import MemoryPlanningTools, MEMORY_PLANNING_TOOLS

executor = MemoryPlanningTools(request)  # request 为已校验的 MemoryGenerationInput
feedback = executor.execute([{
    "id": "call-1",
    "type": "function",
    "function": {
        "name": "think",
        "arguments": json.dumps({"reasoning_summary": "先核对用户目标及后续修正。"}),
    },
}])
```

节点1已将 `MEMORY_PLANNING_TOOLS` 作为 tools，显式采用 `before_task` 和 `tool_task_index=1`；成功返回 planned 及完整计划，失败返回 failed 且无部分计划。完整图在筛选成功后分发节点2并发整理与向量化，再由节点3组装待入库数据；实际落盘由已接入的[归档Service](../../system/communication-archive.md)执行。

执行器不管理 messages、system_guidence、失败重试或审计；这些职责已由 node.py 的调用循环承担，详见[执行与上下文](readme.md#节点1执行与上下文)。工具自身仍为可独立测试的动作校验器。

错误分类使用显式异常：协议/顺序违规抛出MemoryFlowViolation，调用循环将其转换为system_guidence；其他工具解析、参数及取值错误通过配对tool消息反馈，不额外发送user指引。异常均兼容ValueError，失败不推进执行器状态。

---

## 验证

离线测试覆盖三工具的字段描述、任务相关推理、合法序列、全部跳过、首轮约束、失败重试、禁止连续 think、范围及顺序、非法协议、严格参数、终态冻结、任务身份映射、输入重校验和批次隔离。未执行真实模型或向量服务实验。
