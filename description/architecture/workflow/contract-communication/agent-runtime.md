# 主助手生成循环

> **当前状态：** 已实现串行生成、FIFO 外层与工作区内层联动、用户输出、驻留状态刷新及摘要位置持久化；已完成真实 vLLM 与实际扫描图片的首批实验；更新、整理和摘要同步有通过样本；workspace_add 嵌套对象字符串故障已通过共享兼容层修复，并完成真实短轨迹复测。

---

## 入口与职责

`agent_core/runtime.py` 的 `run_agent_core(service, conversation_id, turn_id, owner, *, context, ...)` 由正式启动装配注入 `CommunicationWorkflowService`。完整业务门禁通过后才运行。

主循环通过统一 ToolRegistry 提供三个工作区工具及 emit_progress、finish_task；additional_tools 接收完整 RegisteredTool，模型 Schema、参数解析与执行绑定从同一表生成，详见[统一工具注册](tool-execution.md#统一注册契约)。正式宿主另注册 view_session_file、view_contract_file 并注入页面解析器；另已注册记忆检索与翻页、外部专家求助与追问、合同一跳关联查看、元数据读取与注意事项分页；合同库自然语言检索尚未注册；不能把提示词中的能力说明当成工具已实现。采样、输出上限和 thinking 开关采用实际 MLLM generation 配置；默认最多 96 次生成、连续 3 次错误，均可通过入口参数调整。

---

## 每轮上下文与计数

稳定 system 组合角色、工具交互、工作区和 FIFO 规则；工具调用格式采用启动期加载的模板。tools 由函数 Schema 注入，聊天模板固定 before_task、tool_task_index=1，纠错 user 消息不移动工具位置。

每轮重新读取同一份驻留工作区、最新摘要与其后任务，复用上下文装配函数。当前任务保持原生 assistant/tool 配对；已结束任务按历史块展示。只有连续纠错期间额外追加临时消息。

分词全部采用 vLLM `/tokenize`，不下载本地 tokenizer，不回退字符估算：

- 固定输入：通过聊天分词接口合计完整 system、tools、空动态消息外壳和生成前缀，直接传入 `calculate_context_budget_from_fixed_input`，不虚构分项计数。
- 配额：扣除固定输入、实际最大输出和显式管理预留（默认 1024 token），动态预算按工作区 30%、轨迹 70% 分配；轨迹配额扣除最新摘要的实际渲染 token 数后作为 FIFO 配额。
- 工作区与摘要：管理图注入各自实际展示文本的计数器。独立子图未注入时仍使用其原计数口径。
- FIFO：已结束任务计数展示文本，当前任务按原生消息 JSON 记账，用于范围/阈值管理。这是管理记账，不等于聊天模板精确用量。
- 完整请求：每次生成前再次按实际 messages、tools 和相同模板参数分词，输入加最大输出不得超过配置上下文；失败则停止生成。

聊天分词字段对应 [vLLM TokenizeChatRequest](https://docs.vllm.ai/en/v0.15.1/api/vllm/entrypoints/serve/tokenize/protocol/)；服务端必须支持当前模板与 tools 参数。此实现没有自动重试分词或模型请求。

---

## 工具执行与状态提交

所有主助手工具先进入 FIFO 子图，工作区工具再进入工作区管理图。每轮可按最新摘要重建 FIFO 图，但本任务复用同一个 ToolExecutor 和回执表。子 Agent 的内部工具不递归进入主循环。

工作区成功验收后，在共享锁内检查活动任务和 revision，立刻替换驻留工作区，再返回成功和用量。摘要成功验收后调用 summary_commit，插入唯一驻留历史的压缩末项之后；工具执行前压缩时先插入再执行原调用。下次生成直接重走上下文装配，不保存另一份模型历史。

新摘要、后续已持久化记录的序号变化和待保存任务通过后台备份共同提交，详见[摘要与驻留排序](../../system/communication-history.md#自动摘要与驻留排序)。不将部分历史摘要追加到会话末尾。

emit_progress 发布阶段性输出后继续。finish_task 实际发布最终正文后，先记录成功的原生调用/反馈，再提交 completed 终态。若最终输出已成功但后续容量处理失败，也封闭任务，不重发正文。用户取消/替代通过现有生命周期取消生成，迟到结果不能写回已结束任务。

---

## 错误与审计

无工具、多工具、输出截断、普通文本、非法 JSON 或重复调用 ID 进入有限协议恢复。工具明确失败时允许有限纠错；管理结果的 can_continue=false 不授权普通业务继续，纠错请求仍受完整上下文检查。unknown、压缩失败、容量不足则停止，不猜测副作用、不自动重放。

复用 ToolProtocolRecovery 管理连续失败边界，下一次动作完全通过后删除整段失败消息。成功交互才写入 payload.agent_messages。错误正文不回显，反馈使用统一 system-guidence 和注入的工具模板。

私有审计保存在服务的每任务列表，包括模型响应、调用参数、用量、耗时和管理结果；普通输出文本限制长度，开启 thinking 后，已接受响应的原生推理由独立 FIFO 窗口按原位置回注。子 Agent 审计单独挂在对应调用下。审计不写入 SSE、工作区或摘要源；会话驱逐时释放，不是跨进程持久化审计。

---

## 验证范围

`tests/test_agent_core_runtime.py` 覆盖工作区下一轮刷新、中途/最终输出、失败轨迹清理、协议恢复、自动摘要插入、压缩失败阻止执行、执行后容量失败保留成功结果、取消和整请求容量守卫。`tests/test_chat_token_count.py` 检查 HTTP 参数，`tests/test_agent_summary_persistence.py` 检查 SQLite 事务与重新加载边界。

这些测试验证程序编排与存储一致性，不代表真实模型的工具成功率、摘要质量或长会话性能已经验收。


---

## 任务结束后的提示清理

活动任务保留 system-guidence 并计入容量；完成、取消、调整方向和失败时，统一终态投影先校验再移除操作提示。成功交互保留，私有审计不清除；终态验收失败不改变活动轨迹。具体存储、渲染和验证边界见[任务结束后的提示清理](context-assembly.md#任务结束后的提示清理)。


---

## 真实模型首批实验

实验入口为 [Agent Core 真实运行验证](../../../../experiment/agent-core-live/README.md)。通过局部注册表注入mock检索/打开工具，实际加载data/contract页面PNG，正式主循环、工作区/FIFO管理、摘要生成、内存提交和SQLite备份真实运行。图片回传由实验适配器完成，不代表通用生产文件工具已实现。

长轨迹预置20个已结束任务、60组成功工具交互，约2.66万FIFO token，重点执行最后1～2个用户任务。基线发现workspace_add将对象value输出/解析为字符串，连续校验失败；替换已有条目、删除冗余、80%主动整理与满容量自动压缩均出现成功。对照限制只更新已有条目后，短轨迹和自动摘要后的长轨迹各两个用户任务完整完成，历史位置、剩余任务和重载均一致。详细结果和限制以各运行analysis.md为准，不将对照通过解释为新增工具已经修复。


---

## 正式门禁连接与本机启用

启动装配已连接 `业务门禁 → 准入附件及记录摘要/页数 → 上下文装配 → run_agent_core → finish_task → 任务终态与后台备份`。全程复用同一 conversation_id、turn_id 与 SSE 队列。拒绝或门禁失败不调用主助手；主助手失败也不改写为门禁拒绝。

正式启动始终绑定 `run_agent_core`，演示服务及配置开关已移除。启动日志输出“Communication 正式问答已启用”。当前正式注册工具包括工作区增删改、两种用户输出与两种文件查看工具；文件检索及外部检索尚未注册。文件查看的来源、授权、落盘限制与生命周期见[文件工具装配](page-content-management.md#正式循环装配)。

`tests/test_gate_agent_core_integration.py` 以真实门禁图、真实主循环和管理图验证同轮执行，仅替换模型与分词接口：放行后附件描述进入上下文，字符串对象新增成功并刷新下一轮工作区，唯一 final 收束并备份；损坏 PDF 被拒绝时模型循环不被调用。这些回归验证编排连接，不宣称门禁模型的实际判断质量。


普通工具结果默认保持原反馈与轨迹格式；注册项现已声明成功返回的内容类别（ordinary/foldable）。主循环注入异步 page_resolver 后支持可折叠内容的可配置轮数的展示与隐藏；未注入时仍拒绝此类注册项，详见[工具返回契约](tool-execution.md#工具返回的内容分类)。


原生思考的 K 轮窗口、跨任务定位及重启恢复见[原生思考 FIFO 窗口](reasoning-window.md)。默认保留 3 轮、最多 16384 token；仅保存已接受动作对应的完整思考，任务结束后窗口仍可保留该任务的条目。


## 用户输出流式展示

主循环请求使用流式工具响应，仅对 `emit_progress` 和 `finish_task` 增量展示正文，未增加工具注册属性。完整工具响应仍由 FIFO 管理及工具校验后提交；失败预览标记 interrupted，并排除出模型的成功轨迹。传输、前端消费与恢复边界见[交互工具增量展示](interaction-tools.md#增量展示)。


## 工具执行状态展示

所有工具注册项默认使用 thinking。实际执行前可按注册配置切换 task.progress，结束后恢复 thinking；应用在会话锁内去重，最终答复完成或任务终止后不再切换。该机制不展示工具参数及结果，不影响两个用户输出工具的正文流。注册和扩展方式见[工具执行状态](tool-execution.md#工具执行状态)。


合同图像检索与结果查看已通过正式 `CommunicationFileTools` 注入同一注册表、FIFO 执行及页面解析器，详见[合同图像相似检索](contract-image-search.md)。


`search_contracts_by_question` 已注入同一工具注册表，可引用图像或问题检索的已有结果集限定范围；分页仍由 `view_contract_search_results` 提供，详见[按问题检索合同](contract-question-search.md)。
