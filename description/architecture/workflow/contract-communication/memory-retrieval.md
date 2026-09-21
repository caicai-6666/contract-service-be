# 记忆检索

位于 `app/agent/contract_communication/agent_core/subgraph/memory_retrieval/`。查询当前会话已经持久化且具有三入口检索投影的任务，与[记忆加工工作流](../conversation-memory/readme.md)分工。

当前已实现查询会话类、六工具参数与调用适配，以及真实字段召回、RRF和结构化降序结果。多轮模型节点已接入，原五节点骨架已替换为单节点retrieve_memory，由其执行多轮条件设置与最终查询，不再为查询方法另建子图。

---

## 查询建模与工具

每次调用创建独立 `MemoryRetrievalSession(request, database=None, encoder=None)`。request复用 `MemoryRetrievalRequest`，包含由已鉴权调用方注入的conversation_id、自然语言query、UTC毫秒reference_time与固定kind=task。调用方负责会话归属授权；数据库查询再次固定会话及task过滤，不允许模型改写。

时间和终态默认不限，三个检索文本默认未启用。条件存放在冻结的 `RetrievalConditions` 中，更新在完整校验后原子替换；失败不改变旧条件。执行期间以及成功结束后禁止修改。

| 工具 | 参数模型 | 行为 |
| --- | --- | --- |
| set_time_filter | SetTimeFilterArguments | 单个北京时间date或UTC毫秒范围；范围为包含下界、不含上界，全部null清除。 |
| set_status_filter | SetStatusFilterArguments | 完整替换终态集合；null清除，空列表和重复项非法。 |
| set_user_input_query | SetUserInputQueryArguments | 用户口吻问题，或file_name/display_name/summary中的已知文件线索。 |
| set_intermediate_output_query | SetIntermediateOutputQueryArguments | 公开阶段性发现、进展、待确认事项和下一步动作。 |
| set_final_output_query | SetFinalOutputQueryArguments | 最终答复的结论、事实、建议、条件和未决事项。 |
| execute_query | ExecuteQueryArguments | 无参数；至少启用一个字段后执行，成功包括真实空结果。 |

三个query必填但允许null清除对应字段；空白字符串非法。工具定义由 `build_memory_retrieval_tools()` 生成，描述来自参数类docstring，参数Schema每个属性均有description。只用于检索子Agent，不注册到主助手全局工具列表。

统一执行入口现为异步，旧同步调用方需要改为await：

```python
session = MemoryRetrievalSession(request)
feedback = await execute_memory_retrieval_tool(
    session, 'set_user_input_query', {'query': 'summary:设备验收安排'},
)
result = await execute_memory_retrieval_tool(session, 'execute_query', {})
```

参数通过统一model_json兼容层及Pydantic校验，允许合法的字符串JSON数组还原，不接受重复JSON键或额外字段。设置成功返回完整conditions；失败仅反馈字段问题，不回显原始参数。execute_query成功保存正式result，重复执行返回同一结果；失败不结束并保留条件。调用数校验、有限恢复、私有审计及纠错轨迹清理由agent.py模型循环负责。

---

## 实际查询流程

`query.py` 按顺序组织以下阶段，不使用额外子图：

1. 只读打开业务SQLite，通过一次JOIN读取原任务和检索投影快照；固定当前会话和kind=task，追加创建时间和终态过滤。
2. 每个选中字段创建BM25、向量两个BaseRetriever，字段文本或向量为空时不进入该路候选。
3. QueryFusionRetriever异步并发调用最多六路检索器，执行RRF融合。
4. 回连快照中的原任务，输出按RRF降序排列的RankedTask；并列按sequence、record_id排序。

目前每路和融合后的Top K均为20。内部召回返回结构化任务及分数，最终执行工具将结果建模为query_id轻量回执；已支持LRU、实时分页展示，已接入主助手查看工具的foldable，不调用模型重写任务内容。

### 中文BM25

复用[Lindera Jieba](../../../capability/infrastructure/sqlite-fts.md)。每路在独立内存库中建立候选全文索引，查询也通过相同分词器；文件格式键在BM25查询侧移除，只保留其值。去除纯标点词项，将词项安全引用并用OR连接，不直接执行模型提供的MATCH语法。没有有效词项时该路返回空列表。

SQLite BM25越小越好，适配层取负值后交给LlamaIndex按降序处理。不修改业务表，不持久化全文索引；代价是每次查询复制候选并重建索引。当前实现适用于先打通会话内检索，历史规模扩大时需改成持久化FTS，不能把此实现当成已优化的大库检索。

### 向量与融合

每个非空选中字段的查询文本分别调用现有Embedding服务，采用已有MEMORY_QUERY_INSTRUCTION查询指令，不用文档编码指令替代查询指令。模型固定与当前存储一致，校验4096维、有限值及非零范数后归一化，sqlite-vec计算余弦相似度。encoder注入仅供测试替换，默认使用真实客户端。

LlamaIndex当前版本0.14.24的RRF采用零起始rank与常数60，即每路贡献 `1/(60+rank)`。空字段或未召回任务不贡献分数，不做分母补偿。每路同一record_id构造相同TextNode正文与身份，避免同一任务不同字段因hash不同无法合并，也避免不同任务同文误合并。

num_queries=1关闭额外查询扩写；每个检索器绑定本字段查询文本，不把主需求直接用作所有字段查询。显式提供无生成用途的MockLLM仅用于阻止框架隐式解析默认OpenAI配置，不调用它、不生成模拟检索数据。

任一路执行失败不发布部分融合结果，返回query_execution_failed并记录服务端异常；不在工具内无限重试。取消传播且释放会话执行标记。未来外围对服务故障应有限重试或终止，不能通过改写文本规避。

---

## 提示词与未接入边界

`prompt.py` 版本memory-retrieval-v1，`build_memory_retrieval_prompt(tool_template)` 注入真实工具协议模板；动态需求、参考时间、条件与轨迹由未来运行时提供。提示词区分任务时间与业务日期、任务终态与事项状态，规定三字段转写、事实边界及单轮单工具。

成功execute_query即结束，不设finish、不审核召回结果；真实空结果也结束。主模型search_memory入口、多轮检索Agent循环和查看结果工具均已接入，build_memory_retrieval_subgraph现在必须绑定result_pool。

---

## 验证

本地4组测试通过：六工具Schema及必填项、更新原子性与隔离、真实SQLite/Lindera/sqlite-vec召回和过滤、跨字段RRF去重/降序/分数、空召回及成功后禁止修改。查询向量由测试固定注入以验证精确排名，没有调用实际Embedding或MLLM，因此尚不能据此判断真实语义召回质量。


---

## 任务拉取与独立渲染

`tasks.py` 提供 `pull_ranked_tasks(rows, rankings)`，已接入run_query，在同一次已过滤候选快照中按record_id回连原任务。缺失或重复标识显式报错，避免跳过任务改变排名；不重新读数据库混入查询后的变化。

`MemoryQueryPool`现替代原先保存完整任务快照的MemoryTaskResults。每个会话默认最多10份查询，LRU驱逐；每份只保存任务标识、RRF分数和当前页码，不保存任务轨迹或渲染内容。

`render_memory_task_page(page)` 位于rendering.py，版本memory-task-render-v2，生成独立“历史记忆检索结果”包络，明确历史内容不是当前请求。显示查询标识（若传入）、当前页/总页数、已召回数量和本页排名范围。每条使用“召回记录 N”，展示完整任务标识、北京时间、任务终态及实际存在的历史用户输入、附件、执行轨迹和最终输出。仅显示排名，不显示RRF分数，不复用Agent Core当前任务的包络。

原生消息优先复用native_trace_entries的清理逻辑；没有原生消息时从公开trace按message_id合并分片，排除失败工具调用及反馈、系统提示和未完成消息。不会直接序列化整个payload，私有审计和思考不进入展示。附件保留完整file_id、文件名、display_name、摘要、页数和已有准入状态；展示元数据不授予附件读取权限。没有最终输出的终态只展示真实终态，不补写业务结论。正文使用长度自适应代码围栏，避免正文中的反引号破坏分区。


默认每页3条任务，页首显示“当前第N页 / 共M页 · 已召回K条”，页尾显示“第N / M页结束”。页面分隔使用无框角的对称直线；总页数按本次保留结果计算，空结果为0页，不生成查询ID。


---

## 驻留查询、实时查看与生命周期

执行查询成功且非空时生成UUID query_id，结果池仅保存record_id与RRF分数的有序列表。execute_query回执为success、query_id、total和total_pages，tasks为空；查询会话自身也不再保留任务正文。没有命中时返回成功、total=0、total_pages=0，不生成查询标识、不占用结果池。

正式装配时通过 `await history.memory_query_pool(conversation_id, secret_key=owner)` 获取已鉴权驻留会话的共享池，并注入 `MemoryRetrievalSession(..., result_pool=pool)`。默认容量10、每页3条，添加第11项驱逐最久未访问项。有效查询标识被访问即更新LRU；非法页不推进游标。历史服务在会话驱逐、删除和关闭时调用close，清空并撤销旧实例。池不落盘，重启后旧标识失效。

独立调用未注入池时创建当前实例专属池，适合独立测试；多次任务共享10项容量必须使用上述会话装配入口。主模型生成循环已注册search_memory和view_memory_query。

```python
pool = await history.memory_query_pool(conversation_id, secret_key=owner)
session = MemoryRetrievalSession(request, result_pool=pool)
# 设置查询条件后执行：
result = await session.execute_query()
page = await view_memory_query(pool, {'query_id': result.query_id})
```

查看参数由ViewMemoryQueryArguments定义，query_id必填，page可省略/null。首次默认第1页，成功后默认下一页；指定页成功后也以该页作为后续游标基准。末页不回绕，返回end_of_results；非法页返回invalid_page并指出合法范围；未知或驱逐标识返回query_expired并提示重新检索。

每次只读SQLite当前页的任务，固定conversation_id与kind=task，按保存排名恢复顺序，然后实时渲染并返回content、页码、总页数、召回数量与has_next。此处“实时”指读取当前持久化记录，不读取尚未备份的内存轨迹。任务被删除或移出会话时返回task_unavailable，不静默跳过；读取或渲染失败不推进页码。任务正文和页面文本只在本次调用中临时存在，不存入池。

验证覆盖10项LRU、默认翻页、末页提示、指定非法页、实时更新后重新读取、删除任务、跨会话过滤、整体释放和空结果；原召回与参数测试同步通过。未进行真实模型测试。


---

## 主助手查看工具

`agent_core/tool/memory_viewer.py` 定义ViewMemoryQueryArguments、MemoryQueryViewer及build_memory_view_registration。主循环实际工具名为view_memory_query，参数query_id必填，page可选。参数类已移至tool目录，子图保留view_memory_query函数作为底层参数适配。

CommunicationWorkflowService在完整门禁通过后取得已鉴权会话的查询池，将注册项与文件查看工具合并，通过additional_tools注入主模型和Executor。该工具默认thinking状态，经过统一FIFO工具执行链路；search_memory负责生成新的query_id，view_memory_query负责后续读取。

成功回执仅含query_id、页码、总页数、条数和has_next，正文通过foldable文本页面引用展示。每次查看生成独立display_id，签名绑定资源、页码和展示身份，不保存轨迹或页面文本。页面解析时实时读库并渲染，advance=False确保上下文重复注入不改变游标。查询在展示期间被驱逐则显示真实失效提示，伪造或跨查看器引用被拒绝。

解析器与文件解析器按资源前缀分派；会话驱逐时清除查看器，池仍由历史服务负责释放。模型循环按现有页面保留轮数自动折叠。本功能不新增页面长期缓存、SSE消息事件或自动重新查询。

本次48项测试通过，覆盖注册执行、foldable引用签名及失效提示、页面重注入不推进游标、LRU和主循环/工作流回归；测试宿主已适配正式注入的additional_tools/page_resolver。尚未调用真实模型验证工具选择。


---

## 发起检索与多轮执行

主工具 `search_memory` 位于agent_core/tool/memory_search.py，SearchMemoryArguments仅暴露必填非空query。会话、结果池和参考时间由CommunicationWorkflowService按当前已鉴权会话和任务创建时间绑定；模型不能传入其他会话或覆盖时间基准。工具注册为ordinary返回，执行期间发布local-search“正在检索历史记忆”，结束后恢复thinking。

入口经过统一FIFO/Executor，内部调用 `build_memory_retrieval_subgraph(result_pool=pool)`。图仅有retrieve_memory节点，该节点运行agent.py中的多轮工具循环。模型看到固定规则、注入工具格式、明确的北京时间、自然语言需求与初始条件；后续成功回执包含最新条件。使用真实MLLM客户端、配置中的采样参数与输出上限，开启思考，但长思考只保留在私有审计中，不反复注入。

默认最多16轮、最多3次连续错误。每轮只接受一个完整工具调用；参数经统一兼容层和工具业务校验后才生效。连续错误共用ToolProtocolRecovery清理边界；合法动作被接受后清除整段失败轨迹，原响应与反馈仍留在私有审计。工具成功返回后进入下一轮；execute_query成功包括空结果时立即返回，不调用finish、不再要求模型复核结果。

模型服务故障或实际查询服务失败明确返回错误，不自动改写条件掩盖故障，不发布部分结果；取消继续传播。主工具非空成功回执包含query_id、total、total_pages并提示调用view_memory_query；空结果只返回total=0、total_pages=0和未召回提示，省略query_id且无需翻页。主助手可在下一轮查看第一页，不自动提前消费翻页游标。

每轮任务的检索审计由服务私有_memory_search_audits保存，随会话驱逐或服务关闭释放，不混入用户任务正文。内部六工具不重复注册到主助手。

51项本地测试通过，包括子Agent连续纠错清理、成功空查询收敛、连续错误上限、主工具注册与查询标识保存，以及主循环、工作流、召回和翻页回归。模型响应使用测试替身，实际SQLite召回检查使用固定测试向量，尚未执行真实模型端到端召回质量测试。

---

## 驻留容量配置

`COMMUNICATION_MEMORY_QUERY_CACHE_MAX_QUERIES` 控制每个会话查询池容量（默认10），`COMMUNICATION_MEMORY_QUERY_PAGE_SIZE` 控制每页任务数（默认3）。均通过 Settings 与环境变量加载，要求正整数；修改后重启服务生效。分页工具定义按实际页大小生成，不改变已有页码语义。


零结果不生成 ID：记忆检索会话直接形成成功终态，不调用结果池；结果池也拒绝空集合，避免无效驻留或驱逐已有非空结果。
