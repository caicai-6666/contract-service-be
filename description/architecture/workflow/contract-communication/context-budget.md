# Agent Core 上下文预算

> **当前状态：** 已实现预算契约、纯计算方法及现有 MLLM 配置适配。主循环已通过聊天分词接口合计完整固定输入，并将预算绑定到两个管理图。

本模块为[工作区管理子图](workspace-management.md)和 [FIFO 管理](fifo-management.md)提供统一预算来源；上下文组成见 [Agent Core](agent-core.md)。代码位于 [context_budget](../../../../app/agent/contract_communication/agent_core/context_budget/__init__.py)，策略版本为 `agent-core-context-budget-v1`。

---

## 输入契约

`ContextBudgetRequest` 的所有字段均必填，使用严格整数校验；未知开销不能省略或以 0 冒充已知。允许为零的字段只有在实际为空或明确不预留时才传 0。

| 字段 | 含义 |
| --- | --- |
| `context_window_tokens` | 模型最大上下文，必须为正，包含输入与输出。 |
| `system_prompt_tokens` | 实际完整稳定系统提示词的计数。 |
| `tool_definition_tokens` | 当前注入的全部工具定义开销。 |
| `message_template_tokens` | 角色、分隔符、聊天模板及其他尚未覆盖的输入包装开销。 |
| `output_reserve_tokens` | 本次请求最大生成 token 数，必须为正。 |
| `management_reserve_tokens` | 明确为整理调用、反馈及计数误差预留的余量。 |
| `summary_tokens` | 当前唯一累计摘要的计数，无摘要时为 0。 |

每份内容或包装只能归属一个计数项。动态 system-guidence、正常工具调用与结果进入 FIFO，不再作为固定系统提示词重复扣除。工具调用格式说明若已包含在稳定提示词中，不重复算入工具定义。管理预留是尚未消费的余量，不是已存在提示的第二份计数。

固定开销应来自实际配置下的分词接口与模板装配；主循环已经提供这部分计数；计算器本身不推断、不请求接口。多模态内容的视觉 token 及其他未覆盖输入不能忽略，调用方需纳入实际请求容量校验或明确扣除，不能认为该纯文本记忆分配器已经覆盖它们。

---

## 计算与输出

```text
固定输入 = system_prompt_tokens + tool_definition_tokens + message_template_tokens
预留 = output_reserve_tokens + management_reserve_tokens
动态预算 = context_window_tokens − 固定输入 − 预留
工作区预算 = floor(动态预算 × 3 / 10)
轨迹区预算 = 动态预算 − 工作区预算
FIFO 预算 = 轨迹区预算 − summary_tokens
```

工作区与轨迹区不相互借用；取整余数归轨迹区，保证总额守恒。累计摘要增大只减少 FIFO 配额，不改变工作区配额。动态预算、工作区预算或 FIFO 预算不足时抛出 `ContextBudgetError`，不返回负配额或把不足夹成 1。

`ContextBudgetResult` 返回策略版本、上下文上限、固定输入、预留、动态预算、工作区预算、轨迹区预算、摘要用量和 FIFO 预算，供程序审计及图装配使用。预算是配额，不代表实际已使用量；各管理图仍自行计数。

FIFO 内部实际用量达到 95% 映射为满容量的规则仍由 FIFO 管理负责；本计算器不提前乘 95%，避免重复扣减。主助手提示词继续只说明原有 80%/100% 规则，不增加模型需要理解的预算计算细节。

例如最大上下文 10000，系统提示词 1000，工具定义 500，模板 100，输出预留 1000，管理预留 400，则动态预算为 7000：工作区 2100、轨迹区 4900。当前摘要占 900 时，FIFO 预算为 4000。

---

## 对接现有方法

`calculate_context_budget(request)` 为纯计算入口；`calculate_context_budget_from_settings(settings, ...)` 从已有 `MLLMSettings.context_window_tokens` 和 `generation.max_completion_tokens` 获取最大上下文与输出预留，其余开销必须显式提供。

对应环境配置为 `VLLM_MLLM_CONTEXT_WINDOW_TOKENS` 和 `VLLM_MLLM_MAX_COMPLETION_TOKENS`。不新增相互独立的工作区/FIFO 总容量配置，也不挪用合同视觉流程的 reserved_prompt_tokens 或 reserved_runtime_tokens。若本轮覆写最大输出，使用基础契约传入实际值。

```python
from app.agent.contract_communication.agent_core.context_budget import (
    calculate_context_budget_from_settings,
)
from app.agent.contract_communication.agent_core.subgraph.workspace_management import (
    build_workspace_management_subgraph,
)
from app.agent.contract_communication.agent_core.subgraph.fifo_management import (
    build_fifo_management_subgraph,
)

# 各 measured_* 由上下文装配/计数层提供，不能使用未知值的占位估算。
budget = calculate_context_budget_from_settings(
    mllm_settings,
    system_prompt_tokens=measured_system_tokens,
    tool_definition_tokens=measured_tool_tokens,
    message_template_tokens=measured_template_tokens,
    management_reserve_tokens=management_reserve,
    summary_tokens=measured_summary_tokens,
)
workspace_graph = build_workspace_management_subgraph(
    token_budget=budget.workspace_budget_tokens,
)
fifo_graph = build_fifo_management_subgraph(token_budget=budget.fifo_budget_tokens)
```

这是装配方式示例，不代表完整上下文或工具执行已经接通。当前图预算在构建时绑定；摘要、提示词、工具列表或输出配置变化后须重新计算预算并重新装配相应图，不能沿用过期的 FIFO 配额。

---

## 验证与边界

`tests/test_agent_core_context_budget.py` 覆盖预算守恒、整数取整、摘要扣减、零/负剩余配额、严格类型、遗漏输入和配置适配。计算过程无副作用，不访问分词或模型服务。

已有工作区计数、FIFO 逐任务计数可以提供各自实际用量，尚不能提供所有固定开销。逐块分词相加与最终聊天模板的完整 token 数可能不同；正式运行前仍需用最终请求校验输入与输出预留是否符合最大上下文。管理预留不能替代这个校验。


---

## 完整固定输入计数入口

`calculate_context_budget_from_fixed_input` 接受聊天分词接口得到的 fixed_input_tokens 总量以及上下文、实际输出预留、管理预留和摘要用量。它复用相同 3:7 公式，避免将合计值伪装成系统、工具、模板三个独立测量项。主循环使用该入口，原分项契约继续供已有调用方使用。完整请求的实际消息计数与超限处理见[主助手生成循环](agent-runtime.md#每轮上下文与计数)。
