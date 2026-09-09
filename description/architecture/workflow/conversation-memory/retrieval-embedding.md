# 会话历史检索向量化契约

> **设计决定：** 会话历史向量化与后续查询端统一采用双侧场景化方案 `contract-history-pair-v1`，对应实验选项 `scenario-both`。节点2已接入总结侧编码并由节点3返回待入库数据；[归档Service](../../system/communication-archive.md)已实现向量落库，查询端尚未实现。

---

## 用途与边界

本契约只面向合同业务交互历史的任务检索，不替换合同正文检索、PDF查重或合同问题向量化指令。

检索文本 `retrieval_text` 只承担定位任务的职责：查询问题编码 → 与任务总结向量匹配 → 按任务ID回连原始轨迹 → 将原始轨迹按上下文规则提供给智能体。智能体不以这段总结代替原始轨迹作答。

这里的任务总结不是用于上下文截断的 `summary` 历史记录。原始轨迹、检索文本和向量的存储边界见[Communication SQLite存储](../../data/communication-sqlite.md)。

---

## 固定的双侧指令

两侧指令作为一组维护，接入时必须使用以下英文原文，不临时翻译、增删领域规则或回退到通用指令。本方案是项目场景化指令，不是官方原句。

### 查询侧

```text
Retrieve past interactions with a contract business assistant that are relevant to the user's current question, including prior requirements, decisions, findings, and planned actions.
```

目的：召回与当前问题相关的历史交互，包括此前的要求、决策、发现及任务安排。用户问题放入独立的输入正文，不拼进系统指令。

### 总结侧

```text
Represent this summary of a past contract-business interaction for retrieval, preserving its topic, entities, user intent, key facts, constraints, and planned actions.
```

目的：为单个任务的总结建立检索表示，关注主题、对象、用户意图、事实、约束及后续安排。输入正文只使用已成功生成且非空的 `retrieval_text`。

---

## 输入格式

适用模型为当前配置的 `qwen3-vl-embedding-8b`。每条输入使用以下Qwen聊天序列；其中占位文字应分别替换为对应指令原文与问题/总结正文：

```text
<|im_start|>system
对应侧的固定指令<|im_end|>
<|im_start|>user
问题或任务总结正文<|im_end|>
<|im_start|>assistant
```

- 严格保留角色边界和换行，末尾为 `assistant` 后的换行，不额外追加回答、结束标记或工具模板。
- 本次验证通过 `/v1/embeddings` 的 `input` 字符串接口发送已渲染序列；不要再次套聊天模板造成双重包装。
- 不混用Qwen3纯文本Embedding的 `Instruct/Query` 格式。
- 不把任务ID、批内序号、预期答案、审计、think、提取依据或整理理由混入编码正文。
- 原轨迹和时间元数据独立保留，不为编码偷偷补写原总结中缺失的信息。若将来要把时间、状态等加入编码正文，应作为新的编码方案进行验证，而非沿用本版本名。
- 空总结、`no_memory`或失败结果不生成占位向量；空查询应在调用模型前拒绝。

---

## 向量空间与身份映射

本方案验证使用4096维向量和显式L2归一化，以余弦相似度排序。响应需要校验数量、索引对应、维度、有限数值及非零范数；不得在编码失败后以空向量或部分结果伪装成功。

每份总结向量与原始任务ID关联，不把本批任务序号作为持久身份。召回后应按ID读取该任务的原始轨迹；向量相似不能代替用户权限校验。用户范围、时间范围等过滤是查询层职责，不由Embedding指令负责，也未在本次实验实现。

Top K、最低相似度阈值、重排、查询改写与具体工具/API契约尚未确定。本次实验的Top 3只是评估口径，不是已确认的正式返回数量；余弦分数不是相关概率。

---

## 版本与后续变更

- 当前配对版本固定为 `contract-history-pair-v1`；两侧指令及总结侧渲染集中维护于 `app/agent/conversation_memory/prompt/embedding.py`，实验也复用这些常量。后续查询端应复用同一契约，避免各自硬编码后发生偏差。
- 同一检索空间统一模型、维度、归一化和对应的双侧编码方案，不混合不同模型或不兼容编码版本。
- 文档侧指令、模型或编码格式改变时，需要重新生成相应任务总结的向量；仅修改查询侧不必然要求重算文档向量，但仍须进行配对检索验证并更新方案标识。
- 本文版本标识用于约束编码方案，不要求新增每条记录的 `embedding_model` 或版本字段，不改变既有SQLite表结构。
- 官方通用指令及仅查询侧场景化方案保留为实验对照，不作为后续正式接入的默认方案。实验CLI默认仍为 `official`，复现本选定方案需显式传入 `--variant scenario-both`。

---

## 选择依据与复现

[三组对照](../../../../experiment/conversation-memory-retrieval/output/20260909T045426.163563Z/analysis.md)使用相同7题和16份原始总结，三组均7/7首选正确；双侧场景化的正确项与最近干扰项平均分差为0.2166，官方基线为0.1728，且7题中6题分差更大。因此本方案被选为当前后续接入约定，不表示已证明生产场景下全局最优。

实验入口和原始产物契约见[会话记忆向量召回实验](../../../../experiment/conversation-memory-retrieval/README.md)。复现命令：

```bash
python experiment/conversation-memory-retrieval/run.py --run-model --variant scenario-both
```

节点2已验证总结侧输入与本契约逐字一致及编码失败边界，节点3输出见[待入库数据](pending-records.md)。后续仍需验证查询端输入、ID回连，以及包含同主题近邻、时间差异和无答案问题的扩展样本。本次没有新增查询端、自动后台向量队列或业务数据迁移。
