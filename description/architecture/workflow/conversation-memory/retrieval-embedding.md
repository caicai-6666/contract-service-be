# 任务检索文本模板与向量化契约

## 当前契约

编码使用qwen3-vl-embedding-8b，4096维，显式L2归一化。生产入口为 `embed_task_text`；用户输入中的用户文字、每份文件的格式化内容分别并发编码后等权平均并归一化；最终输出单次编码，中途每条输出独立编码后平均并归一化。失败不产生可入库半成品。详细图结构见[单任务记忆加工](readme.md)。


---

## 存储字段与编码单元

本文是检索正文模板与向量化规则的主文档。数据库约束及事务见[SQLite存储契约](../../data/communication-sqlite.md#conversation_task_retrievals三入口检索投影)，任务筛选及调度见[单任务记忆加工](readme.md)。实现入口为 `node.model_task`、`node.collect_task_retrieval` 和 `embedding.embed_task_text`。

| 检索文本列 | 保存内容 | 独立编码单元 | 编码类型 | 对应向量列 |
| --- | --- | --- | --- | --- |
| user_input_text | 用户问题区块与各文件区块按顺序拼接 | 用户文字一项，每份非空文件各一项 | user_question / file | user_input_embedding：所有单元等权融合 |
| intermediate_output_text | 已完成中途输出按编号模板拼接 | 每条完整中途消息各一项 | excerpt | intermediate_output_embedding：所有消息等权融合 |
| final_output_text | 已完成最终输出正文 | 整份最终输出文本一项 | final_output | final_output_embedding：单次编码 |

三组字段属于 `conversation_task_retrievals`，以 `record_id` 回连 `conversation_records` 原任务。原始payload仍保存文件标识、页数和任务轨迹；检索文本是精简投影，不替换原始数据。数据库不保存问题、每份文件或每条中途消息的独立向量，也不保存三入口合并的任务向量。

---

## 用户输入存储模板

### 原始字段与文本标签

| 原始字段路径 | 当前文本标签 | 规则 |
| --- | --- | --- |
| payload.input.text | 用户问题： | 整段用户文字，不拆句、不改写 |
| payload.input.files[i].file_name | 文件名： | 单份文件的文件名 |
| payload.input.files[i].display_name | 展示名称： | 单份文件的展示名称 |
| payload.input.files[i].summary | 文件摘要： | 单份文件的摘要原文 |
| payload.input.contracts[i].file_name | 文件名： | 正式合同引用快照中的文件名 |
| payload.input.contracts[i].summary | 文件摘要： | 正式合同引用快照中的摘要原文 |

原始结构的key为英文；当前存储及编码文本标签为中文，使用全角冒号。文件字段顺序固定为file_name、display_name、summary。区块顺序为用户文字、上传附件、引用合同；两类文件各自保持输入列表顺序。合同与附件共用文件模板和 `file` 编码指令，每份分别并发编码后参与等权融合；合同没有 display_name，不补造该字段。合同 document_id 不进入检索文本或向量，仍完整保留在原任务中。附件保持输入列表顺序，缺失字段直接省略，不生成空标签、null或占位说明；一份文件三字段均无内容则整个文件区块省略。不根据文件名、摘要内容对附件去重。

原文及字段先去除首尾空白，保留内部换行。用户文字中明确以file_id=或file_id:标记的规范UUID/64位十六进制值会从检索投影去除，不删除其他一般数字；附件的file_id、page_count从不作为模板字段加入。非法非字符串正文报错，不强转字符串。

### 精确拼接方式

`user_input_text = "\n\n".join(非空区块)`；文件区块内部为 `"\n".join(非空字段行)`。问题区块在前，文件区块依次在后，不增加附件编号或分隔线。

下面是“一段用户文字＋两份附件”实际存储形态的示例，其中第二份文件只有摘要：

```text
用户问题：比较两份文件的交付安排。

文件名：服务协议.pdf
展示名称：服务协议
文件摘要：约定分阶段交付及验收。

文件摘要：约定一次性交付及验收。
```

没有用户文字时从第一个文件区块开始；没有文件时只保留用户问题。两者均空时user_input_text与user_input_embedding同时为NULL。

### 实际编码边界

上面的存储文本不会整段送去编码，而是并发发送三份正文：

| 单元 | 正文 | 指令类型 |
| --- | --- | --- |
| 1 | 用户问题：比较两份文件的交付安排。 | user_question |
| 2 | 文件名、展示名称、文件摘要组成的完整三行文件区块 | file |
| 3 | 文件摘要：约定一次性交付及验收。 | file |

用户问题标签和文件字段标签参与编码；不单独为同一份文件的三个字段分别编码。类型由任务建模节点明确传递，不根据正文是否包含“文件摘要”等文字猜测。

---

## 中途与最终输出存储模板

只读取公开 `payload.trace` 中type=message、status=completed的消息，以message_kind区分intermediate/final；工具反馈、错误、system-guidence、内部思考、未完成消息不进入这些正文。相同message_id的同类消息碎片按原顺序直接拼接，保留片段间原始空格；不同消息即使正文相同也不去重。

中途输出采用 `numbered-intermediate-v1`：每条正文去除首尾空白，编号从1开始，区块使用 `\n\n---\n\n` 连接。

```text
### 中途输出 1

已核对第一份文件的交付安排。

---

### 中途输出 2

接下来核对第二份文件的验收约定。
```

只有两段正文分别参与编码，`### 中途输出 N`、空行分隔及横线不参与编码。仅一条时保留一个编号区块；无有效消息时该文本与向量均为NULL。

最终输出不增加标题，保存实际完成的final消息正文。如果存在多条完整final消息，以 `\n\n` 连接后整体编码一次：

```text
两份文件分别采用分阶段交付和一次性交付。
```

没有有效最终答复时保持NULL，不为取消、终止或调整方向生成替代结论。编号和拼接不会生成摘要或改写消息的时序关系。

---

## Embedding请求模板与融合

单个编码单元通过 `render_task_embedding_input(text, kind=...)` 构造如下精确聊天序列；这里的占位符由程序替换，末尾包含换行：

```text
<|im_start|>system
{该类型的指令}<|im_end|>
<|im_start|>user
{单个编码单元正文}<|im_end|>
<|im_start|>assistant
```

指令用于生成向量表示，不要求模型先生成改写文本。下面各节给出实际指令全文和版本。

对用户输入和中途输出的每个非空单元并发调用Embedding，受局部Semaphore和进程级全局请求额度约束。设原始向量为e_i：

```text
u_i = e_i / ||e_i||₂
m = (u_1 + ... + u_n) / n
v = m / ||m||₂
```

用户文字和每份文件各占一份相同权重，不按长度、字段数量加权；文件较多时其合计权重自然变大。中途消息也按条等权。只有一个单元时保留其单位向量。融合不保留单元的时序信息，不实现“后文覆盖前文”。最终输出直接使用单次编码得到的单位向量。

没有单元时跳过分支，不能编码空字符串或构造零向量。任何单元请求失败、返回非法向量或融合范数无效时，整个任务不发布部分检索结果，也不能改成NULL冒充无内容。向量存为4096维float32 BLOB；转换前后精度及配对校验由存储契约规定。

提示词版本及content_kind记录于调用审计，不写入检索文本；当前检索表没有逐行提示词版本字段。修改编码策略不会自动重算已加工任务；统一历史策略需要另行重建投影，不能仅重新编码拼接文本来复现分块向量。

---

## 文件检索查询格式边界

已讨论的查询约定使用英文key，支持任意单字段，例如：

```text
summary: 分阶段交付及验收
```

也可使用已知字段组合，不补齐未知字段：

```text
file_name: 服务协议.pdf
summary: 分阶段交付及验收
```

可选key为file_name、display_name、summary。用户问题查询则由检索模型改写为用户口吻。这是后续查询构造约定，正式检索工具尚未实现；当前文档侧文件文本仍为中文标签，不能把上述英文查询模板误当成已上线的存储模板。

最近的对照实验使用自然语言查询，未验证上述英文单字段/多字段格式。是否在查询编码前统一标签、如何选择查询指令及融合多路召回，仍需查询端实现与实验确认。本次文档完善不改变已有编码代码或数据库内容。

---

## 配对指令

文档指令版本：`three-view-excerpt-v2-readable-input`。

```text
Represent this excerpt of a past contract-business interaction for retrieval, preserving its topic, entities, user intent, key facts, constraints, and planned actions.
```

查询指令保留用于向量召回实验；正式检索工具尚未接入：

```text
Retrieve past interactions with a contract business assistant that are relevant to the user's current question, including prior requirements, decisions, findings, and planned actions.
```

`prompt/embedding.py` 将指令和正文渲染为system/user聊天序列并追加assistant前缀。原文为空时拒绝编码；不注入record_id、任务序号、模型推理或审计。

---

## 校验与历史边界

EmbeddingClient校验响应数量与索引；编码层进一步校验模型名称、维度、有限值和非零范数并归一化，保留私有用量/耗时审计。模型服务配置不匹配时在发送请求前失败。

旧综合摘要编码入口、批量任务筛选和总结工具已删除。历史实验输出保留，旧向量召回实验需要的历史指令在实验运行器中冻结，不再依赖生产兼容函数。

测试见 `tests/test_memory_embedding.py` 与 `tests/test_conversation_memory.py`，覆盖输入、归一化、异常响应、连接失败、取消清理及新图融合。

---

## 用户输入指令实验

独立[三指令实验](../../../../experiment/user-input-instruction/README.md)比较当前通用英文、意图优先英文和对应中文，仅改变用户输入的文档侧指令。44任务、58查询中，用户意图主集首位命中分别22/24、24/24、24/24；诊断集分别25/31、22/31、21/31。中英文主集持平，输出事实类查询有退化，尚不能确定普遍最优方案。完整证据见[实验分析](../../../../experiment/user-input-instruction/output/20260916T062011.354683Z/analysis.md)。该实验当时生产仍使用通用指令；当前用户输入已切换为下述两份专用指令，未据旧实验宣称其质量更优。

扩大测试改用48条用户输入正例及12条输出线索误导题，候选68任务；正例首位命中分别46/48、48/48、48/48。误导分数与正例仍有重叠，不能以Embedding指令代替字段路由或拒绝阈值。按误导来源任务排序衡量，原始指令在Top1/Top3抑制上较好，英文在平均名次和Top5上较好，中文较弱；详见[扩大测试分析](../../../../experiment/user-input-instruction/output/20260916T062938.290853Z/analysis.md)。

这些指令实验采用整段用户输入编码；当前生产已改为分块并发融合，该策略的最新真实对照见下文。历史已加工向量不自动回填，见[编码策略更新](readme.md#用户输入编码策略更新)。


---

## 用户问题与文件专用指令

版本为 `user-input-question-file-v1`。程序在任务建模时记录区块类型，用户文字使用 `user_question`，每份附件使用 `file`；不根据正文标签推测类型。`embed_task_text(text, audit, kind=...)` 将对应指令送入实际Embedding请求，并在审计中记录content_kind与prompt_version。默认excerpt继续用于中途输出；最终输出显式使用final_output。

用户问题匹配以用户口吻改写的请求，保留操作、对象、范围和约束：

```text
Represent this historical user question for retrieval by a request phrased in the user's voice, focusing on the question, requested action, relevant subjects, and explicit scope and constraints. Use only information provided in the question without adding unstated information.
```

文件匹配单份格式化附件，允许只含文件名、展示名称、摘要中的一项，不补齐缺失字段：

```text
Represent this file for retrieval using the supplied file name, display name, and summary. Match queries containing any one or more of these fields, preserving the file identity, topic, and explicitly described contents. Missing fields are allowed; do not invent or complete them.
```

例如，“只核对付款条件”保留核对请求及范围；“文件摘要：涉及验收条件”即使缺少文件名，也单独使用文件指令编码。输入仍沿用中文标签，结构化原始数据key仍为file_name、display_name、summary。该接口输出4096维单位向量，不生成改写文本；查询改写工具尚未实现。空区块跳过，未知编码类型拒绝执行。

分块并发、等权融合及存储结构保持一致；旧投影不自动重算。已测试真实图到Embedding客户端的指令路由、仅摘要附件、标签相似的用户问题及最终输出专用指令；已完成下述真实模型召回复测。


---

## 分块专用指令复测

相同68任务、48正例和12误导题下，新方案正例Top1为48/48，误导来源平均名次6.42，Top3进入7/12；同轮旧英文指令分别48/48、3.33、10/12。误导Top1仍5/12，未完全抑制。分块通用消融为47/48、4.08、10/12。详见[完整分析](../../../../experiment/user-input-instruction/output/20260916T065422.939064Z/analysis.md)。本集无多附件任务，查询未改为格式化字段，结论不覆盖这些后续场景。


---

## 中途输出指令实验

[中途输出指令对照](../../../../experiment/intermediate-instruction/README.md)比较通用指令与强调发现、进度、待确认和下一步动作的新英文指令。41候选、38正例、12误导题下，正例Top1由33/38降为30/38，Top3同为37/38；误导来源平均名次4.75→5.50，Top1进入数5/12→4/12。优先正确字段召回，保留原通用指令。详见[分析与逐题证据](../../../../experiment/intermediate-instruction/output/20260916T071350.472742Z/analysis.md)。


---

## 最终结论指令实验

[结论指令对照](../../../../experiment/final-instruction/README.md)包含41候选、31正例和12误导题。结论专用候选Top1为23/31，原版20/31，Top3均31/31；6份约1000字符长答复的18条局部查询Top1由7/18增至10/18。误导来源排序无变化。详见[分析及长文位置指标](../../../../experiment/final-instruction/output/20260916T072633.494975Z/analysis.md)。现已按用户确认将该候选接入最终输出编码；中途输出仍保留原通用指令。


---

## 最终输出专用指令

`FINAL_OUTPUT_INSTRUCTION` 对应 `final-response-v1`，生产节点通过 `kind='final_output'` 显式选择。整份最终答复只编码一次，正文存储模板、向量维度和空值规则保持不变。指令全文与已验证候选一致：

```text
Represent this response for retrieval, focusing on the question addressed, stated conclusions, key facts, recommendations, and unresolved matters, along with relevant subjects and applicable conditions. Preserve negation and uncertainty. Use only information explicitly provided in the text.
```

例如，“尚未确认付款，建议补充回单”应表示未决付款状态及补充材料建议，不应补成已经付款的事实。该规则指导向量表示，不要求额外生成文本。中途输出继续使用excerpt原指令。调用审计记录final_output类型和版本；已有持久化向量不自动重算。
