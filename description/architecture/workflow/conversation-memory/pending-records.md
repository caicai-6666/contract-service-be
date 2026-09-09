# 会话记忆待入库数据

> **实现边界：** 节点2完成单任务总结及总结侧向量化，节点3组装 `pending_records` 返回调用方。图不写文件或SQLite，不修改历史驻留队列或持久化标记；[归档Service](../../system/communication-archive.md)已接通，查询端仍未实现。

---

## 节点2：总结后编码

模型继续通过think和extract_memory完成单任务整理，工具定义与模型纠错协议不变。extract_memory通过全部校验后，程序对非空retrieval_text调用EmbeddingClient.create_embeddings，每次仅编码当前任务正文。

编码规则使用[检索向量化契约](retrieval-embedding.md)的总结侧指令，统一常量位于prompt/embedding.py。程序校验配置和响应模型为qwen3-vl-embedding-8b、向量数量为1、维度为4096、数值有限且范数非零，再显式L2归一化。无记忆结果不调用Embedding，不制造占位向量。

| 单任务status | 检索文本与向量 | 含义 |
| --- | --- | --- |
| summarized | 两者均有值 | 文本整理及向量校验均完成；沿用原枚举名称，不表示已经落盘。 |
| no_memory | 两者均为null | 模型有依据地判断没有可靠内容；原始轨迹仍应保留。 |
| failed | 两者均为null | 总结或向量化失败，不发布不完整的检索数据。 |

embedding为4096维归一化浮点数元组，图JSON序列化时为数组。模型依据和整理理由保留在results；MLLM逐轮audit与embedding_audit分别保存，均不拼入检索正文或待入库payload。embedding_audit记录方案版本、模型、token、向量数量、维度、校验前范数、耗时和成功/失败类型，不重复保存完整向量或异常敏感正文。

向量失败不向MLLM发送system_guidence、不重新生成总结、不在本节点自动重试。成功的extract_memory调用仍保留于私有审计，但该任务最终标记failed，交由后续Service决定重试；取消异常继续向上传播并释放连接。

---

## 节点3：覆盖校验与组装

节点3不调用模型或数据库，执行以下校验与转换：

1. 筛选计划的已选与跳过集合必须互斥且完整覆盖原输入；已选身份与原批内序号一致且严格递增。
2. 每个已选任务必须恰好返回一个结果，不得缺失、重复或混入计划外任务。
3. 重新校验结果字段和向量，不能信任绕过构造器修改过的模型实例。
4. 按原输入顺序复制任务；成功和no_memory任务进入待入库列表，未选任务以空检索字段进入；加工失败任务不进入待入库列表，其失败结果保留在results。
5. 原payload深拷贝，不使用模型渲染投影覆盖它；上传文件引用、完整有序轨迹、原任务状态和计时信息不被摘要加工改变。

任务自身status=failed与本次加工失败是两回事：原本执行失败的任务，只要本次加工成功或被跳过，仍可进入pending_records。

---

## 返回契约

MemoryGenerationOutput新增pending_records：

| 字段 | 含义 |
| --- | --- |
| pending_records=null | 尚未汇总，例如只运行节点1或筛选失败。 |
| pending_records=[] | 已汇总但没有可提交的任务，例如所有输入均入选且加工全部失败。 |
| pending_records=[...] | 按原输入顺序排列的可提交内容，不含私有审计。 |

每项为MemoryPendingRecord，字段如下：

| 字段 | 来源与约束 |
| --- | --- |
| task_id | 调用方提供的稳定原任务标识，用于回连，不能用本批任务序号替代。 |
| status | 原任务六种终态之一，不改写为本次加工状态。 |
| payload | 原任务内容的深拷贝，保留input、文件引用和有序trace等已有字段。 |
| created_at | 原任务创建时间，UTC Unix毫秒；沿用输入，缺失为null，不补加工时间。 |
| activated_at | 原任务激活时间，UTC Unix毫秒；缺失为null。 |
| processing_duration_ms | 原任务处理总时长，非总结或向量化耗时；缺失为null。 |
| retrieval_text | 已选任务生成的检索正文；跳过或no_memory为null。 |
| embedding | 与正文配对的4096维归一化浮点向量；无正文时为null。 |

retrieval_text与embedding必须同时存在或同时为空。待入库集合必须完整覆盖成功及跳过任务；它们的检索字段与results逐项一致。results仍只含已选任务，保持筛选计划顺序；pending_records含跳过任务，保持全部输入的相对顺序。

批次execution_status沿用summarized/partial_failed/failed：全部已选任务成功或no_memory（含零选择）为summarized；部分已选失败为partial_failed；全部已选失败为failed。即使全部已选失败，若还有跳过任务，pending_records仍可含这些无向量轨迹；调用方应分别检查错误与可提交数据。

---

## Service接入责任

pending_records是待入库内容，不是可以直接展开为SQL参数的完整数据库行。Agent输入不持有用户密钥、conversation_id、record_id、会话sequence或SQLite事务，不能在图内伪造这些值。

- Service明确使用原record_id作为task_id关联注册时已有的记录身份与顺序，并校验会话归属，不能将task_id直接当作turn_id。
- created_at在数据库中不可为空；若输入缺失，应从原记录取得真实值后才能写入，不使用当前时间补造历史。
- 写库时将embedding序列编码为SQLite支持的float32 BLOB，并将payload序列化为JSON；这属于存储适配，不在图中执行。
- SQLiteCommunicationStore.backup_task仅备份原轨迹；实际归档使用archive_tasks_with_workspace，将检索对、加工标记和当前工作区原子保存。相同结果幂等确认，不覆盖已加工的不同结果。
- 失败项的重试、文件保存/验证与fresh/persisted标记都由Service负责；只有实际提交成功后才能改变持久化标记，原内存轨迹不因备份被删除。

图仍返回可用的部分结果；当前Service采用整批成功后提交，任何加工失败均保留原批次在下一扫描重试，不拆散原批上下文。成功批次不因后续批次失败回滚，具体调度与驱逐门禁见[归档服务](../../system/communication-archive.md)。

---

## 并发与验证

Send分支内串行执行总结和向量化。图默认max_concurrency取MLLM与Embedding配置并发上限中的较小值，保守限制整个分支；调用config可覆盖，调用方须自行保证覆盖值合适。这不是跨批次或进程的全局限流。

测试覆盖固定指令输入、向量归一化、错误模型/维度/数量、零向量及NaN/Inf、网络故障、取消、无记忆不编码、混合成功失败、未选任务保留、逆序结果恢复、原始payload与计时复制、实例篡改拒绝以及内存SQLite的成对字段约束。不改变模型可见工具参数，也不增加查询API。
