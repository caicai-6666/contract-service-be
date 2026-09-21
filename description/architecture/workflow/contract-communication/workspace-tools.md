# 工作区管理工具

> **当前状态：** 已实现三个模型可见工具的参数 Schema、路径编辑、完整校验和可注入提交适配；尚未接入主助手模型循环、私有审计和纠错调度；压缩子 Agent 已复用这些工具并实现自身审计与纠错，不代表正式问答已经调用这些工具。

FIFO 外层与工作区内层已通过注册表执行器装配，入口、提交与回执边界见[主助手工具执行](tool-execution.md)；正式模型循环仍待接入。

已实现的容量管理流程见[工作区管理子图](workspace-management.md)，当前执行适配尚未串接该子图。主助手见 [Agent Core 设计](agent-core.md)，工作区内容契约见 [SQLite 工作区](../../data/communication-sqlite.md#工作区-kv-内容)。工具按能力组织在 `agent_core/tool/` 包中；工作区工具定义与编辑实现位于 [tool/workspace.py](../../../../app/agent/contract_communication/agent_core/tool/workspace.py)。


`tool/__init__.py` 统一导出工作区工具的参数模型、定义构造、参数解析、候选编辑和执行适配；原 `app.agent.contract_communication.agent_core.tool` 导入路径继续可用。后续工具按能力新增同级模块，不集中堆放于包初始化文件。

---

## 统一位置与操作边界

位置从工作区 payload 根开始，以 `/` 分隔固定字段和稳定条目键，例如 `/known_information/info_001/status`。这是受限的绝对路径，不支持 JSON Pointer 转义、通配符、相对路径、数组下标或表达式执行；现有工作区键不包含斜线，无需转义。

| 工具 | 位置 | 行为 |
| --- | --- | --- |
| `workspace_replace` | 已存在的字段或完整条目。 | 用 value 完全替换目标，其他内容保留。 |
| `workspace_add` | 补充、已知信息、剩余方向或已探索方向容器。 | 新增一个条目，生成的稳定键反映在最新工作区；新增探索结果执行原子转移。 |
| `workspace_delete` | 已存在的完整条目。 | 删除该条目；不级联处理引用，不删除必填字段或容器。 |

根、四区域容器及 task_constraints 整体不能被替换或删除。任务文本通过 `/task_constraints/task` 替换，清空用 null；信息 source 可替换为 null，但 confirmed 信息必须仍满足来源约束。information_ids 用完整列表替换，不提供数组索引操作。

所有工具都先在深拷贝中生成候选结果，再校验完整 WorkspacePayload；是否提交由执行适配或外部调用方负责。参数、位置、类型或引用错误不产生任何状态写入。

---

## workspace_replace

参数由 `WorkspaceReplaceArguments` 定义：

| 参数 | 含义 |
| --- | --- |
| `path` | 已有字段或完整条目路径。 |
| `value` | 对应位置的新值；整体对象必须包含完整必要字段，不使用隐式合并。 |

示例为工具参数，不是普通文本模拟调用：

```json
{
  "path": "/known_information/info_001/status",
  "value": "confirmed"
}
```

该操作只修改信息状态；若 source 仍为 null，完整校验会拒绝。需要同时补充来源和修改状态时，替换整个 `/known_information/info_001` 对象，使两个字段一次生效。条目键保持不变，未知字段或不存在的路径不会被隐式创建。

---

## workspace_add

参数由 `WorkspaceAddArguments` 定义：

| 参数 | 含义 |
| --- | --- |
| `path` | 四个允许的新增容器之一。 |
| `value` | 与目标容器对应的完整新增内容。 |
| `direction_id` | 仅新增已探索结果时必填；其他新增省略或为 null。 |

| path | value |
| --- | --- |
| `/task_constraints/supplements` | 非空补充文本。 |
| `/known_information` | `{content, source, status}`。 |
| `/remaining_directions` | `{plan}`。 |
| `/explored_directions` | `{outcome, conclusion, information_ids}`；information_ids 可省略为空列表。 |

普通新增不传自拟 ID，由程序生成稳定键；模型通过最新工作区读取实际路径。例如：

```json
{
  "path": "/remaining_directions",
  "value": {"plan": "查找付款凭证并核实首款支付情况"}
}
```

### 探索结果的原子新增

向已探索区新增时，direction_id 必须是剩余区域中已有的方向键。程序读取原 plan，在同一次提交中移除剩余项、以相同 ID 创建探索结果，不允许模型改写本次实际尝试的原规划：

```json
{
  "path": "/explored_directions",
  "direction_id": "direction_001",
  "value": {
    "outcome": "inconclusive",
    "conclusion": "当前材料中未找到付款凭证，无法据此判断是否已经付款。",
    "information_ids": ["info_001"]
  }
}
```

information_ids 必须已经存在且无重复；失败时原剩余规划完整保留。不得先删除剩余方向再新增结果。需要再试时新增不同方向 ID，并在 plan 中说明调整，旧探索记录保留其原始结果。

---

## workspace_delete

参数由 `WorkspaceDeleteArguments` 定义，只接收 path：

```json
{"path": "/remaining_directions/direction_002"}
```

允许删除补充、已知信息、已探索方向或剩余方向的完整条目。已知信息若仍被探索结论引用，删除失败；调用者需根据业务判断先修改引用，而非由程序静默级联删除。可以清理过时或不再有用的探索条目，不能用删除掩盖仍有价值的失败经验；删除当前工作区条目不会删除历史轨迹或私有审计。

不存在的条目明确报错，不以“成功删除”掩盖定位错误。对象必填字段通过替换处理，不支持直接删除。

---

## 工具定义、提交与反馈

`build_workspace_tools()` 按 replace、add、delete 的固定顺序生成 OpenAI 兼容函数工具，使用 strict=false，并为所有参数与嵌套对象属性提供字段级 description。每次返回独立定义，调用方将其交给现有 vLLM 模板注入。

`prepare_workspace_edit(snapshot, name, raw_arguments)` 只生成已校验候选，不写入状态；参数 JSON 拒绝重复键、额外字段和非标准数值，也不从普通文本猜测工具调用。

`execute_workspace_tool(name=..., raw_arguments=..., snapshot=..., commit=...)` 在候选通过后调用注入的 `commit(payload, expected_revision)`。程序持有用户、会话和版本，模型参数不包含这些身份。提交函数必须绑定受权会话，并在真实执行器接入时校验活动轮次、取消或替代状态，阻止迟到提交。

服务装配示例：

```python
from app.agent.contract_communication.agent_core import execute_workspace_tool

async def commit(payload, expected_revision):
    # history、conversation_id、secret_key 由当前已认证执行器绑定。
    return await history.update_workspace(
        conversation_id, secret_key=secret_key,
        payload=payload, expected_revision=expected_revision,
    )

result = await execute_workspace_tool(
    name=tool_call_name,
    raw_arguments=tool_call_arguments,
    snapshot=current_workspace,
    commit=commit,
)
```

该独立适配尚未接入容量子图，其内部成功返回 `status=accepted`、实际 `path`、可选的 `removed_path`、新 `revision` 和完整新 `workspace`。普通新增返回的 path 含程序生成的 ID；探索收束同时给出新路径与移除路径。返回内容用于下一次上下文构造，不代表已完成磁盘备份或释放上下文。正式容量子图的成功工具反馈已定义为“成功提示 + 当前工作区使用量”，详见[子图输入与输出](workspace-management.md#输入与输出)；该内部对象不应直接作为正式模型工具消息。

失败抛出校验、版本或提交异常，不返回成功结果。执行器后续必须先审计、检查恰好一个工具调用，再执行工具；将子图 error_feedback 作为普通工具错误反馈；无法形成合法工具调用的协议错误使用最小 system-guidence。有限纠错成功后清除整段失败轨迹，遵循[上下文规范](../../../standard/agent-context-management.md)。本模块不另建模型循环或复制恢复协议，也不自动把普通文本写入工作区。

---

## 验证

`tests/test_workspace_tools.py` 覆盖实际工具 Schema 的字段描述、重复键与非法参数、路径边界、对象与字段替换、程序 ID、原子转移、信息引用约束、失败不修改原快照，以及注入真实历史服务后的版本冲突和备份恢复。

这些是本地确定性验证，未调用真实模型。内部历史服务的原有读写、备份和四区域校验仍由工作区与存储测试覆盖。


---

## 模型参数兼容

工作区工具与压缩子 Agent 共用[模型结构化输出兼容层](../../../capability/infrastructure/model-json.md)。模型将对象或数组编码成字符串时，解析器依据 path 对 value 进行类型消歧，再执行原有参数、路径及完整工作区校验；正文、任务补充等字符串位置保持原样。原始调用参数仍保留在轨迹和私有审计中。
