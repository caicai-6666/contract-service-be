# 主助手上下文渲染

> **当前状态：** 已实现历史摘要区、工作区与单任务渲染。现有驻留消息到任务分区的适配及三个动态区域拼接已接入[门禁后装配](context-assembly.md)；完整模型消息与接口计数已由[主循环](agent-runtime.md)接入。

模块位于 `agent_core/context_rendering/`。它面向主助手阅读，与 [FIFO 自动摘要](fifo-summary.md)子 Agent 的资料输入渲染分别维护；不调用模型、不修改工作区或轨迹、不重新推断摘要。

---

## 历史摘要区

```python
from app.agent.contract_communication.agent_core.context_rendering import (
    SUMMARY_RENDER_VERSION,
    render_summary_section,
)

text = render_summary_section(summary)
```

`SUMMARY_RENDER_VERSION` 为 `agent-core-summary-render-v1`。输入接受 `FIFOTopicSummary`、累计摘要字典或 `None`，返回字符串。读取契约复用 `previous_topics`：支持当前 v2 两区域、旧 v1 五区域及现有无版本兼容结构；拒绝未知版本、非法条目和重复主题 ID。该函数只校验展示所需主题，不替代完整累计摘要的存储校验。

展示由以下层级组成：

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
历史摘要
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

以下内容来自较早的任务记录；发生冲突时，以后续任务中的明确更新为准。
摘要中的未决事项不自动构成当前任务要求。

## 主题 1 · 资料核查

### 已知情况

- 已核实范围，尚不能确认全部内容。
  来源：
    - 用户提供的说明
  来源任务：
    - task1

### 未决事项

- 仍需核对附件。

━━━━━━━━━━━━ 历史摘要结束 ━━━━━━━━━━━━
```

| 内部字段 | 展示名称 |
| --- | --- |
| `current_memory` | 已知情况 |
| `open_items` | 未决事项 |
| `sources` | 来源，按原顺序逐项展示。 |
| `task_ids` | 来源任务，按原顺序逐项展示。 |

主题间使用细分隔线；主题及条目顺序沿用输入，不按名称或 ID 排序。主题序号仅用于阅读，内部 topic ID 不展示。`latest_coverage` 仅表示最近一次压缩范围，不作为全部摘要的来源范围展示。

空区域和空来源直接省略；没有条目的主题也省略。`None`、空主题列表或所有主题都没有条目时返回空字符串，不显示空摘要外壳。非法结构不能当成“无摘要”静默省略。

正文和来源的换行使用续行缩进保留；Markdown 控制符进行转义，避免资料中的标题或围栏破坏展示层级。主题标题的多行文本仅在标题行合并为空格。转义影响展示编码，不改变摘要数据，也不提供来源认证或完整提示注入防护保证。

旧五区域摘要保留“用户要求、已知信息、探索与结果、已交付内容、未决事项”的原分类，并注明其历史性质，不在渲染时将历史动作改写成当前事实。

---

## 当前工作区

```python
from app.agent.contract_communication.agent_core.context_rendering import (
    WORKSPACE_SECTION_RENDER_VERSION,
    render_workspace_section,
)

text = render_workspace_section(snapshot.payload)
```

实现位于 `context_rendering/workspace.py`，版本为 `agent-core-workspace-render-v2`。接受当前 `WorkspacePayload` 或四区域字典；快照应显式取 `.payload`。输入按工作区 Schema 校验，缺失数据、额外字段、悬空引用和非法结构直接报错，不自动创建空工作区或迁移旧结构。

固定展示顺序为“任务约束、已知信息、已探索方向、剩余规划”。外层采用“当前工作区”标题和粗边界，区域采用二级标题、真实区域路径和独立 YAML 代码块，区域间使用细分隔线。例如：

````text
## 任务约束

路径：/task_constraints

```yaml
task: 比较两份材料的付款安排
```
````

空区域不渲染；任务约束中 task 为 null 时省略该字段，supplements 为空时也省略，仅剩补充要求时仍展示任务约束区域。整个工作区为空时只返回“当前工作区为空。”，不展示外壳、标题或分隔线。内部数据不变。已有条目的字段保持完整，包括 source=null 和 information_ids=[]；条目 ID、枚举和信息引用保持实际值与插入顺序，不翻译、不重新编号。路径是定位说明，不是可编辑字段。

YAML 使用局部 SafeDumper：支持中文，多行文本优先使用块样式，歧义字符串和特殊字符由序列化器转义。代码围栏自动长于正文中的连续反引号，防止正文截断代码块；不会修改全局 PyYAML 配置。序列化后的已有条目可以重新解析为相同数据；省略的空区域及空任务约束字段不在展示文本中恢复。

本函数不展示 revision、updated_at 或容量元信息，也不推断是否持久化。容量信息尚未接入此展示，不能从正文长度估算或伪造。

**计数边界：** 现有 `workspace_management/token_count.py` 仍使用 `workspace-json-v1`，压缩子 Agent 沿用该格式。本次只提供主助手展示组件，不替换现有计数器。后续完整上下文装配必须让实际展示和预算计数采用一致口径，不能把旧 JSON 的用量称为此 YAML 展示的精确 token 数。

`tests/test_agent_workspace_rendering.py` 验证区域顺序及路径、空区域省略、全空提示、已有条目空字段保留、条目顺序、引用、多行文本和特殊字符往返还原、输入不变、非法结构拒绝及局部序列化行为。

---

## 单个任务

实现位于 `context_rendering/task.py`，版本为 `agent-core-task-render-v2`。入口 `render_task_section(task)` 接受 `TaskRenderInput` 或相应字典；完整任务展示用于已结束任务。`render_task_input(task)` 只展示当前任务身份和用户输入，执行轨迹由独立原生消息携带。它使用明确的三分区输入，不从 FIFO 消息正文、工具名称或用户可见文本猜测任务终态。

```python
from app.agent.contract_communication.agent_core.context_rendering import render_task_section

text = render_task_section({
    "task_number": 12,
    "task_id": "task_012",
    "user_input": {"content": "比较两份材料的付款安排。", "files": []},
    "execution_trace": [],
})
```

| 字段 | 契约 |
| --- | --- |
| `task_number` | 程序提供的正整数展示序号，不在渲染时重新编号。 |
| `task_id` | 稳定任务身份。 |
| `user_input.content` | 用户原始问题；仅附件或仅引用合同时可为空。 |
| `user_input.files` | 有序附件列表，每项必须包含 file_id、file_name、display_name、summary、page_count；页数为程序确认的正整数，同一任务内 file_id 不重复。 |
| `user_input.contracts` | 有序正式合同快照，每项为 document_id、file_name、summary；旧记录缺省为空列表。独立渲染“引用合同 N”，不要求页数或展示名称。 |
| `execution_trace` | 按实际发生顺序排列的展示条目，不包含私有推理或审计。 |
| `final_output` | 当前任务省略或为 null；已封闭任务包含 kind。completed 必须有非空 content，其他结束方式可省略正文或传 null。 |

轨迹类型为 `tool_call`（工具调用）、`tool_result`（工具反馈）、`intermediate`（中途输出）、`system_guidence`（系统提示）和 `assistant`（助手说明）。每项 content 是非空原始文本；工具调用必须提供 name 和 call_id，工具反馈必须提供 call_id，允许附带 name。原始参数 JSON 不被渲染器重新解析，不把普通文本推断为执行成功。

已成功的最终输出正文由调用方单独放入 final_output，不重复放入 execution_trace。调用方仍需完整维护原始工具协议与审计；此展示对象不替代原始消息列表。渲染器不会按名称删除 finish_task 调用，也不会清除临时错误，以免隐藏仍需要纠正的当前轨迹。

终态 kind 对应 completed（最终反馈）、interrupted（用户终止）、superseded（用户调整方向）、failed（执行失败）；completed 的 content 必须是实际提交的最终正文。其他结束方式仅凭 kind 即可展示；只有确实存在程序确认的结束说明时才可提供 content。缺少正文时不展示正文块、空值或占位提示，也不生成业务完成情况。渲染函数只展示已提供的事实，不修改任何任务状态，不调用模型生成结束说明。

任务使用粗边界与身份标题，内部以二级标题区分用户输入、执行轨迹和最终输出，三级标题标记问题、各附件和轨迹步骤。附件身份信息以可读 YAML 展示，摘要另行展示。正文使用长度自适应的文本围栏，保留换行和资料原文，避免资料中的 Markdown 标题或围栏破坏区域层级；来源身份仍由程序确认，不能靠文本标签认证。

**当前任务没有最终输出区域，也没有任务结束分隔线。** 空执行轨迹仅保留“执行轨迹”标题，不添加进行中、暂无结果或空值占位。任务封闭后才展示最终输出和“任务 N 结束”边界。正文文本块自身的闭合围栏不是任务结束标志。

门禁后装配已将打开检查确认的 page_count 写入当前附件记录；已有记录缺少该字段时会拒绝，不能猜测；本函数不打开文件或计算页数。现有 FIFOTask 仍采用 messages 结构，其本身仍不自动适配三分区；驻留任务 payload 的适配由门禁后装配负责，FIFO 已采用 fifo-task-mixed-v2：历史按渲染块计数，当前任务按原生消息 JSON 记账；主循环另行执行完整聊天模板计数。完整接入时须协调最终正文去重、失败轨迹清理、附件元数据、展示 token 计数与真实消息协议。

`tests/test_agent_task_rendering.py` 覆盖当前任务无结束包络、四种终态、非正常结束无正文、正常完成必须有正文、附件五字段、纯文本与纯附件输入、顺序、来源不推断、文本围栏、输入不变及非法结构拒绝。

---

## 集成与验证

调用方在任务轨迹之前插入非空摘要区。历史摘要不能提升为系统指令；未决事项不自动成为当前任务要求。正式组装后应对实际渲染文本计数，不能将另一种摘要序列化格式的 token 数视作此文本的精确用量；当前函数不改动既有容量计数实现。

`tests/test_agent_summary_rendering.py` 覆盖稳定渲染、模型对象与字典一致性、主题顺序、来源保真、输入不变、空内容、非法结构、旧版本及嵌入 Markdown 的边界；无需真实模型或网络请求。


---

## 临时页面的前置提醒

组装侧组件 `context_rendering/page.py` 已提供 `render_tool_page_messages(result, visible_pages=...)`，版本为 `agent-core-page-display-v2`。它读取程序确认的工具结果及本次可见页面内容，统一生成先提示、后资料的两条 user 消息；工具不提供或拼接提醒文本。

第一条复用独立的 system-guidence/action_guidance 格式，明确“剩余可见轮次：N（包含本轮）；次数用完后将隐藏这些内容”，并要求及时提取有效事实、来源及限制。这里的本次响应不等于整个用户任务结束。第二条按工具返回顺序排列页面定位及实际 text/image_url 内容块，表格以 text 内容块展示。

`visible_pages` 由展示管理层提供，以 display_id 映射已解析的实际内容；只为本次真正展示的页面生成提醒。ordinary、没有可见页面时返回空列表，不对普通反馈、隐藏占位或无内容的资源引用发出虚假提醒。未知展示标识、空内容或与声明不符的内容块直接拒绝，不修改原工具结果。工具成功状态与原生调用配对仍由原有层负责。

**接入边界：** 主循环已通过 PageDisplayWindow 接入页面解析、可配置轮数的展示和自动折叠；注入异步 page_resolver 后启用 foldable。提醒与完整内容在请求计数前注入，模型完整返回后减少一次，次数耗尽后仅保留来源占位。没有解析器时仍拒绝此类工具。资源建模和分页接口尚未实现，详见[页面管理](page-content-management.md#已实现接口与容量边界)。

`tests/test_page_display_rendering.py` 验证文本与图片前的统一提醒、只展示部分页面、无页面不提醒、严格输入、顺序、稳定渲染及原结果不变；窗口与主循环联调另由 test_page_display_window.py 和 test_agent_core_runtime.py 覆盖。


---

## 合同引用渲染

`render_contract_references` 复用自适应文本围栏与 YAML 元数据格式，只展示完整合同 ID、库中文件名及摘要。缺失摘要展示“该合同尚未保存摘要。”；此提示仅属于展示，不写入摘要字段或检索文本。当前与历史任务使用同一快照，记忆结果翻页也展示引用合同。用户输入允许只有合同引用，不构造虚假的用户问题。
