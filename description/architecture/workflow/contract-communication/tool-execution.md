# 主助手工具执行与双层容量管理

> **当前状态：** 注册表执行器、工作区增删改适配及 FIFO 外层装配已实现，可注入依赖独立运行；[正式主助手模型循环](agent-runtime.md)、活动任务校验和完整上下文刷新已接入。

本模块连接 [FIFO 管理](fifo-management.md)与[工作区管理](workspace-management.md)。FIFO 只依赖统一执行契约，不识别具体工具名称；新增工具通过完整注册项扩展，同一注册表生成模型工具定义及执行映射。

---

## 调用链与职责

```text
主助手的一次合法工具调用
→ FIFO 计数、必要时先自动摘要
→ ToolExecutor 按名称分派
→ WorkspaceToolHandler 读取当前工作区
→ 工作区管理图：预估、必要时压缩、应用原操作
→ commit(payload, expected_revision) 提交
→ FIFO 追加工具反馈和系统提示、重新计数、构造最终结果
```

`build_agent_tool_registry` 自动注册三个工作区工具；提供 `publish_output` 时注册[两个用户交互工具](interaction-tools.md)。主循环的其他工具通过 `additional_tools` 提供完整注册项，也经过相同的 FIFO 外层；所有重复名称（包括内建工具）均在装配时拒绝。工作区压缩与 FIFO 摘要子 Agent 的内部调用继续使用自己的工具循环，不递归进入本入口。

`tool/registry.py` 统一管理名称、说明、参数模型、解析器和处理器；`tool/executor.py` 管理分派与执行回执；`tool/workspace_executor.py` 管理工作区读写适配；`tool_management.py` 负责装配两层图。原 `execute_workspace_tool` 保留为底层独立适配，不是主助手的容量管理入口。

---

## 对外接口

```python
from app.agent.contract_communication.agent_core import build_tool_management_subgraph

graph = build_tool_management_subgraph(
    fifo_token_budget=7000,
    workspace_token_budget=3000,
    read_workspace=read_bound_workspace,
    commit_workspace=commit_bound_workspace,
    additional_tools=[search_registration],
)
output = await graph.ainvoke({"request": fifo_request})
result = output["result"]
```

`fifo_request`、输入轨迹的任务状态与输出类型沿用 FIFO 契约。调用方须先记录本次 assistant 工具调用，图只追加实际反馈。上例预算仅为示例，生产应使用[上下文预算](context-budget.md)的计算结果。

| 依赖 | 契约 |
| --- | --- |
| `read_workspace()` | 异步返回当前 `WorkspaceSnapshot`；在 FIFO 执行前压缩完成后读取。 |
| `commit_workspace(payload, expected_revision)` | 异步原子校验版本、提交并返回内容一致且版本加一的快照。 |
| 注册工具处理器 | 异步接收 `FIFOOperation` 和已解析的参数模型，返回 `FIFOExecutionResult`；成功必须代表动作已完成。 |
| 计数器与压缩器 | 默认复用两个子图的接口计数及自动压缩，也可注入测试实现。 |

读取与提交回调必须由程序绑定用户和会话；提交还须校验活动任务、取消和替代状态，避免迟到结果。可以封装 `ConversationHistoryService.get_workspace/update_workspace`；只将确定发生在写入前的版本冲突转换为 `WorkspaceCommitRejected`，其他异常保持不确定状态。

成功工具反馈包含 `status=success`、`message`（成功提示与当前工作区使用量）和 `revision`。完整工作区从存储快照刷新到后续模型上下文，不重复塞进 FIFO；新增条目的程序 ID 也从最新快照取得。工作区系统提示以 `source=workspace` 交给 FIFO，提交前不发布成功或压缩完成通知。

---

## 失败与重复调用

| 情况 | 行为 |
| --- | --- |
| FIFO 执行前压缩失败 | 不读取或修改工作区，返回 `not_executed`。 |
| 工具未注册、参数或工作区管理校验失败 | 返回 `failed`，不提交候选或成功提示。 |
| 明确提交拒绝 | 返回 `failed`，本次修改未生效。 |
| 提交异常、非法提交回执或处理器异常 | 返回 `unknown`，不声称未执行，不自动重试。 |
| 提交成功后 FIFO 计数或压缩失败 | 保留 `succeeded` 和工具结果；外部依据 `can_continue` 暂停后续模型请求。 |

每个会话独立创建并复用 `ToolExecutor`。同一实例串行执行，并按 `call_id` 保存回执；相同调用返回回执，不再次执行处理器；复用 ID 却改变任务、名称或原始参数则拒绝。取消继续向外传播，已进入处理器的调用留下 `unknown` 回执。异常细节记录在程序日志，不放进模型反馈。

回执是进程内保护，不提供跨实例或重启后的持久化幂等，也不按语义识别不同 ID 的重复操作。随会话执行器一起释放，不能在仍可能重放时清空。若重新装配图以更新 FIFO 预算，应通过 `executor=` 传入同一 `ToolExecutor`；此时由调用方自行注册 `WorkspaceToolHandler` 和其他工具，不能同时传 `handlers`、`additional_tools` 或 `publish_output`。

调用回执复用只避免副作用重放，不负责 FIFO 整体请求幂等。调用方应恢复已返回的 FIFO/摘要、使用更新后的预算，不得在已有工具反馈的轨迹上重复追加同一回执。错误纠正轨迹清理、私有审计与正式模型循环仍由外层运行时负责。

---

## 验证

`tests/test_agent_tool_management.py` 使用真实管理图与确定性依赖，覆盖增删改、两层压缩顺序、提示来源、提交前失败、提交状态不明、提交后计数失败、扩展工具、并发重复调用和取消后的回执保护。不调用真实模型，不代表真实模型的能力或质量验收。


---

## 统一注册契约

`RegisteredTool(name, description, arguments_model, handler, parser=None, return_types=('ordinary',), progress=ToolProgress())` 是完整注册项。`ToolRegistry` 在装配时检查函数名称、重复名称、说明、参数模型、处理器及嵌套字段 description；动态生成的 Pydantic 模型同样检查。注册集合装配后固定，`definitions()` 返回独立的函数 Schema，`handlers()` 返回同一集合的解析执行入口，不允许模型修改注册信息。

默认解析使用[共享 JSON 兼容层](../../../capability/infrastructure/model-json.md)，然后执行参数模型校验。按 path 消歧的工作区工具显式绑定原解析器；工作区管理图仍独立验证完整候选和引用关系。解析失败时返回 failed，处理器不执行；处理器异常仍由 Executor 标为 unknown，不将可能生效的操作误记为参数错误。原始 arguments 和私有审计保持原样。

新增工具示例：

```python
from pydantic import BaseModel, ConfigDict, Field
from app.agent.contract_communication.agent_core.tool import RegisteredTool, ToolProgress
from app.agent.contract_communication.agent_core.subgraph.fifo_management.schema import FIFOExecutionResult

class SearchArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    query: str = Field(min_length=1, description='用户要检索的合同关键词。')

async def search_handler(operation, arguments):
    # search_bound_to_owner 由程序绑定用户、权限及数据源，不接受模型提供执行身份。
    records = await search_bound_to_owner(arguments.query)
    return FIFOExecutionResult(status='succeeded', tool_result={'records': records})

search_registration = RegisteredTool(
    name='search_records', description='检索当前用户有权读取的合同。',
    arguments_model=SearchArguments, handler=search_handler,
    progress=ToolProgress(type="local-search", message="正在查找合同"),
)
# run_agent_core(..., additional_tools=[search_registration])
```

主循环在计算固定输入及预算前取得注册表的完整工具定义，因此新增工具 Schema 自动纳入 token 配额；执行映射由同一表生成。同一轮仍复用一个 Executor，不随 FIFO 重建丢失回执。注册只绑定能力，不能绕过 FIFO、授权回调和任务生命周期检查。

独立管理图保留旧 `handlers` 参数作为非模型调用方的兼容入口，该入口不生成工具定义；新增模型工具应使用 `additional_tools`。子 Agent 继续使用各自的工具集，不自动获得主助手扩展工具。`build_workspace_tools`、`build_interaction_tools` 保留独立构造用途，与注册项共享各模块的参数模型和说明。

`tests/test_tool_registry.py` 覆盖定义与执行名称一致、Schema 副本、重复名称、嵌套字段描述、嵌套 JSON 兼容、解析失败无执行、执行异常保持 unknown、回执复用，以及新增工具经过真实主循环和 FIFO 后记录轨迹。真实模型实验脚本已通过 additional_tools 注册模拟文件能力，无需替换主循环内部构造函数。


---

## 工具返回的内容分类

内容只分两类，由工具实现返回、注册表校验；不由模型填写，也不改变模型可见函数参数 Schema：

| `content.type` | 名称 | 处理意图 |
| --- | --- | --- |
| `ordinary` | 普通结果 | 正常保留，不折叠。现有工作区增删改及中途/最终输出均显式声明这一类。 |
| `foldable` | 可折叠结果 | 图片、表格和长文本页面等临时大内容，交给统一展示与折叠层。 |

`FIFOExecutionResult.status` 仍表达 succeeded/failed/unknown，`FIFOManagementResult.result_type` 仍表达管理结果类别，不能与内容分类混用。业务反馈保留在 `tool_result` 字典中；新增的 `content` 是独立的强类型、以 type 为判别字段的联合对象。旧适配不提供 content 时默认 ordinary，原有模型反馈正文保持不变。

普通成功返回示例：

```json
{
  "status": "succeeded",
  "tool_result": {"message": "操作成功"},
  "content": {"type": "ordinary"}
}
```

可折叠结果使用 `FoldableToolContent`，包含非空 pages 列表。每个 `ToolPageReference` 包含 resource_id、独立 display_id、locator、media_type（text/table/image）及简短 description。同次返回的 display_id 不可重复。这里的页面代表本次查看的内容片段，不要求资源本身必须具有固定物理页码。类型仅保存轻量引用，不携带整页数据或 Base64；资源实体由工具解析器管理，展示状态由主循环窗口管理。

每个注册项通过不可变 return_types 元组声明允许的成功返回类型，可声明一种或两种。默认 ordinary；缺失声明、重复类别、未知类别在装配时拒绝。执行后实际内容类别必须在声明范围内。未声明的返回或非法结果属于执行后的契约异常，Executor 保留 unknown 回执，禁止自动重放；失败及不确定结果只能携带 ordinary 反馈，返回失败提示不要求工具额外声明普通成功结果。

**当前支持边界：** 主循环配置 page_resolver 后支持可配置轮数的展示与折叠；未配置时仍在装配期阻断 foldable。独立 FIFO 图默认无展示能力，收到页面结果时保留执行事实并返回 unsupported_content；主循环显式启用 supports_foldable 后保存轻量回执。驻留轨迹使用与 FIFO 相同的序列化函数，只持久化来源定位及隐藏占位，不保存完整页面。

展示生命周期与仍待实现的资源管理边界，详见[页面内容折叠与资源建模](page-content-management.md)。`tests/test_tool_content_contract.py` 验证普通兼容、内建声明、可折叠结构、非法类型、返回越界、回执不重放及未接入展示时保留执行事实。


---

## 工具执行状态

每个 `RegisteredTool` 都有不可变 `progress: ToolProgress`，默认 `type=thinking`、`message=正在思考`，无需在每个注册点重复填写。工作区增删改及两个用户输出工具均使用默认值。文件查找等扩展工具可以覆盖为 `local-search`，联网检索可以覆盖为 `online-search`，外部专家咨询使用 `external-expert`；当前允许类型复用 `TaskProgressData`，新增类型须同步前端契约。

状态文案必须采用“正在＋动作”，由 `ToolProgress` 在注册时校验；旧展示历史仍可读取，不回写既有事件。状态切换仅说明当前展示步骤变化，不是前一步成功的凭证。

这项配置只控制用户可见执行状态，不进入模型的工具定义或返回内容。只有 `emit_progress`、`finish_task` 输出消息正文；其他工具不会因为配置状态而向前端公开参数、结果或推理。

`ToolRegistry.handlers(publish_progress=...)` 在参数校验通过后、调用处理器之前通知状态；处理器返回、抛错或取消时恢复默认 thinking。状态发布位于实际工具执行入口，因此 FIFO 自动压缩等执行前管理仍维持 thinking；参数错误、未知工具及 Executor 回执重放不会触发工具状态。业务拒绝的工具在实际检查期间仍可能显示对应状态。

正式主循环绑定 `CommunicationEventService.publish_tool_progress`，在会话锁内比较当前状态，只有 type 或 message 改变才发布 `task.progress`。终态任务或最终正文已 completed 时不再恢复 thinking，不覆盖最终输出。状态跟随既有 SSE、快照和展示历史投影，不新增事件类型。独立管理图可以通过同名 `publish_progress` 参数注入宿主回调；无回调时不产生展示副作用。旧式裸 handlers 不具有注册元数据，正式扩展工具应使用 `RegisteredTool`。

`tests/test_tool_registry.py` 覆盖默认和非法配置、模型 Schema 隔离、校验后切换、异常及取消恢复、回执防重，以及真实运行时的 SSE 去重和最终输出边界。


---

## 文件查看注册

正式宿主通过 build_file_view_registrations 注册 view_session_file、view_contract_file，分别绑定会话附件查看器与合同查看器；两个 Pydantic 参数模型及类 docstring 是 Schema 与描述的来源。成功结果为 foldable，默认状态为正在思考；图片经组合 page_resolver 进入后续上下文，不通过用户正文 SSE 发布。来源、池配置、生命周期与附件尚未落盘的限制见[正式循环装配](page-content-management.md#正式循环装配)。


---

## 日期计算工具

工具描述以“当你需要计算精确的日期值时，使用这个工具”开头，通过“昨天”“三个月后”等场景说明用途，并强调月末、闰年和跨年的准确计算。开始时间须有依据；负数向前计算，省略的增减量按零处理，目标月份没有对应日期时取月末，未指定时区时采用北京时间。不计算工作日、节假日，也不判断业务期限的起止规则。

`calculate_date`是无状态内建工具，实现在 `agent_core/tool/date_calculator.py`。统一注册表发布其定义与执行绑定，主循环通过FIFO外层和Executor调用；结果为ordinary，不折叠，进度采用默认think，不产生用户输出SSE。

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| start_time | 必填字符串 | YYYY-MM-DD或包含时分秒的ISO时间，可带小数秒及Z或±HH:MM时区；仅日期按零点，未指定时区按UTC+08:00。 |
| years / months | 可空整数 | 年/月增减量，负数向前；省略或null为0。 |
| days / hours / minutes / seconds | 可空整数 | 日/时/分/秒增减量，负数向前；省略或null为0。 |

程序先合并年月增减量并定位目标月，原日超出目标月份时截断到月末，再叠加日时分秒。比如2026-01-31增加一个月为2026-02-28；2026-03-31减一个月再加一天为2026-03-01。2024-02-29加一年同时减12个月仍是原日期，不先做中间截断。未提供任何偏移时返回开始时间本身。

```json
{"start_time":"2026-01-31","months":1,"days":null}
```

普通成功结果：

```json
{
  "start_time":"2026-01-31T00:00:00+08:00",
  "result_time":"2026-02-28T00:00:00+08:00",
  "weekday":"星期六",
  "month_end_clamped":true
}
```

month_end_clamped只表示年月步骤是否截断日期，最终结果还会继续应用日时分秒。原始开始时间本身若为2月31日等非法日期则拒绝，不自动修正；非法参数通过注册表反馈，计算越界返回failed普通反馈，支持范围为公元1至9999年。所有偏移必须是整数或null，拒绝布尔值和小数。

工具保持输入的固定时区偏移，不解析地区时区或夏令时规则；不计算工作日、法定节假日、含首尾日的业务期限。交互提示词要求日期增减优先使用该工具，开始时间须来自任务时间、用户明确输入或已知事实。模型仍负责确定基准与业务口径，程序负责正确的日历运算。无需新增环境变量或数据库表。测试覆盖闰年、月末、跨年、混合正负偏移、空参数、时区、错误反馈及注册Schema。


---

## 历史记忆结果查看

view_memory_query由正式Communication主循环按会话注册，接收query_id和可选page，省略表示下一页；成功为foldable文本页，失败为普通提示，默认thinking状态。复用会话10项LRU结果池，详见[记忆检索与查看](memory-retrieval.md#主助手查看工具)。search_memory也已注册，接收自然语言query，调用多轮检索子Agent并返回query_id；执行状态为local-search，结果为ordinary，之后使用view_memory_query读取。


---

## 外部专家工具

`ask_external_expert` 与 `continue_external_expert` 已由宿主注入统一注册表，经 FIFO 和 Executor 执行，返回普通文字结果；执行状态为 `external-expert`。参数、会话隔离、延迟初始化、错误反馈和释放机制详见[外部专家求助](external-expert.md#主循环装配与生命周期)。

---

## 合同关联工具模块

`tool/contract_relations.py` 已提供一跳关联查看器、参数模型、注册工厂和渲染器，SSE 使用默认 thinking；已由正式会话宿主装配到主循环。分页、驻留和实时读取边界见[合同一跳关联查看工具](contract-relations-tool.md)。


---

## 工具描述的调用意图

主助手现有工具统一以“当你需要……时，使用这个工具”说明调用时机，再说明用途、操作方式与必要边界。覆盖工作区增删改、中途输出、最终输出、日期计算、附件与合同查看、记忆检索与翻页、专家求助与追问、合同一跳关联查看。

用途说明分别聚焦保存/更新/清理信息、向用户报告或结束任务、准确计算日期、核对原文、找回历史、读取召回结果、获得专业分析和补充关联背景。字段级参数描述仍独立保留，完整标识、分页、专家上下文与任务收束约束不因统一措辞而移除。分页工具注册时继续按配置注入每页条数；此次仅调整模型可见描述，不改变执行逻辑或 SSE 状态。

---

## 合同元数据工具

`get_contract_metadata` 已接入正式主循环，按完整合同 ID 读取 SQLite 中可用合同的名称、摘要、审核人、签署日期和入库时间。结果为 ordinary，默认 thinking，无分页及驻留缓存。详见[合同元数据查看工具](contract-metadata-tool.md)。

---

## 合同注意事项工具

`view_contract_notes` 已接入主循环，按最近创建优先查看用户补充记录，支持分页与会话LRU。正文为 foldable，SSE为默认 thinking。配置、驻留及删除边界见[合同注意事项查看工具](contract-notes-tool.md)。


## 合同图像相似检索

已注册 `search_contracts_by_image`（`local-search`）与 `view_contract_search_results`（`thinking`）。搜索只返回结果集引用和数量，查看工具首次展示第一页，随后支持翻页；页面遵循 foldable 契约。文件驻留、全页融合、配置及结果释放见[合同图像相似检索](contract-image-search.md)。


## 按问题检索合同

已注册 `search_contracts_by_question`，仅接收 `query` 和可选 `result_id`，使用 `local-search` 状态。与图片检索共用结果池和 `view_contract_search_results`，详见[按问题检索合同](contract-question-search.md)。

- `search_contracts_by_notes` 已接入主循环，状态 `local-search`，返回结果集引用；查看复用 `view_contract_search_results` 的 `thinking` 与 foldable，详见[用户备注检索](contract-note-search.md)。

- [合同摘要混合检索](contract-summary-search.md)：向量＋BM25、RRF 排名、范围限制与共享分页，已接入主循环。

- [合同名称检索](contract-name-search.md)：以“xxx合同”式名称进行 BM25 检索，复用范围与分页，已注册进入主循环。

- [合同关系检索](contract-relation-search.md)：可选起点筛选、关系描述 BM25＋向量、边结果集及两端合同分页。

- [合同候选查询子图](contract-retrieval.md)：四种文本查询已移入内部工具；主模型使用 `search_contracts` 获取最终结果集引用，再用 `view_contract_candidates` 分页查看，内部只回显池引用与数量。图片和关系工具保持独立。


动态状态可在 `RegisteredTool` 中配置 `progress_factory(arguments)`：参数解析通过后生成 `ToolProgress`，未配置则沿用静态 `progress`。返回值仍校验类型及“正在”前缀，通过既有发布回调发送，执行完毕或失败后恢复默认思考状态。当前网页打开工具用它展示关注关键词，不新增 SSE 类型或事件。

合同库概览由已注册的 `get_contract_library_statistics` 提供，参数与统计口径见[合同库统计工具](contract-statistics.md)。
