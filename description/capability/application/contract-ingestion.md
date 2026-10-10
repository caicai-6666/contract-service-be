# 合同待审提交与审核后正式入库

> **用途：** 说明用户确认后保存独立待审快照，再由中间件审核反馈驱动正式入库的职责划分。

HTTP 契约见[提交合同待审申请](../../api/contract.md#正式入库合同)，存储结构见[待审快照](../../architecture/data/ingestion-review.md)、[正式 SQLite 元数据](../../architecture/data/contract-sqlite-metadata.md)与[ES 文档](../../architecture/data/contract-elasticsearch-document.md)。

---

## 职责与输入

`ContractExtractionService.ingest_run` 按 run_id 和当前认证用户读取内存聚合，接收必填 file_name、summary、note、完整 Core 和 Clause。阶段全部成功且前置结果完整后，复用 `ContractIngestionService.validate_ingestion_review` 的纯业务校验，补齐处理版 PDF、分类及分类理由、全部检索问题、两个融合向量、向量模型和提交人，交给 `IngestionReviewService` 保存。

note 允许空字符串、最多 10000 字符，只存于待审表并发送中间件。送审名称最多 200 字符，与中间件 name 限制一致。稳定 submission_id 由 run_id 派生；相同用户、相同确认值重试返回同一申请，释放运行或重启后，在待审保留期内仍可恢复；清理后返回 404。不同内容返回 409，其他用户返回 404。

待审 SQLite 和 PDF 保存成功后，发布 `run.submitted`、关闭已有 SSE 并释放内存运行。失败保留运行，允许同内容重试。该 HTTP 请求不调用模型、中间件或正式存储；201 只表示本地申请已保存，后台发送状态可能仍为 pending_send。

[后台发布服务](ingestion-review-publisher.md)发送申请；[反馈消费者](ingestion-review-consumer.md)按 passport 判定：空串拒绝，非空批准。批准才调用 `ContractIngestionService.ingest`，以快照 submitted_by 作为正式 uploader，向量化最终摘要并协调正式 SQLite、PDF、ES 和 Neo4j。合同名称不参与向量化；摘要向量与对应文本在 SQLite 同事务写入。正式合同只保存上传人；外部审核员与审核备注仅留在待审记录中，不写正式合同。

合同分类同时投影到 SQLite 两种结构：`contracts.category` 保留以 ` / ` 连接的 code 摘要（未映射时保留类型说明），`contract_category_assignments` 通过类别外键保留可精确筛选的多标签关系，并逐关联保存模型的 `reasoning_summary`。新入库与 ES 对账均按分类 code 生成摘要；前端通过类别目录映射中文名称。推理摘要从运行聚合内的完整分类结果取得，不扩大面向前端的紧凑分类 View。

提交人不由请求体提供。待审提交执行与快照、SSE、重试相同的运行所有权校验，跨用户访问按任务不存在处理。正式入库审核员来自中间件反馈。

---

## 入库条件与校验

八个用户业务阶段必须全部为 `succeeded`，并且内存聚合中必须同时存在分类、建议名称、Core、Clause、Retrieval 和 PDF 查重结果。分支结果可以是 `partial`：用户提交的完整最终值会替换自动 Core 和 Clause，且最终文件名可以与自动建议不同；两个合同级向量仍必须已经成功形成。

Core 按启动期不可变字段目录执行动态校验。

送审前即要求 `core.signing_date` 非空，且为真实、完整的年月日；缺失、`null`、空字符串或非法日期均返回 `422`，不会开始 SQLite、PDF 或 ES 写入。通过校验后统一为 `YYYY-MM-DD`。提取阶段仍允许未知日期为 `null`，不能为满足入库约束而猜测；审核人需补充有依据的签订日期后再次提交。此规则只约束新入库，不回填或删除历史空日期记录。

其余校验规则：

- 顶层必须精确包含全部 Core `code`，拒绝未知或缺失字段；
- `single` 单属性字段使用标量，`single` 多属性字段使用对象，`multiple` 字段使用对象数组；
- 对象拒绝未知属性，并要求所有 `required: true` 属性存在；
- 字符串、整数、数值和布尔值必须符合定义类型，字符串不能为空白，数值必须有限；
- 顶层 `null` 和空多值数组在最终投影时省略，不写入 Elasticsearch。

Clause 至少包含一条，并校验 `clause_id` 唯一、`order` 按数组顺序从 1 连续增长、父条款先于子条款出现、页码不超过处理版 PDF 总页数，以及编号、路径和正文非空。`title`、`parent_clause_id` 为 `null` 时在最终文档中省略。

检索问题原文必须是非空列表，所有元素必须是非空文本；在任何持久化写入前校验，随后按原顺序保存至 ES `retrieval_questions`。问题原文先保存于待审快照，批准后进入 ES，正式元数据表不增加问题字段。包括部分 Embedding 失败在内的保存边界及后续换模型方式，见[检索问题原文契约](../../architecture/data/contract-elasticsearch-document.md#检索问题原文)。

`file_name` 会去除首尾空白，并拒绝路径分隔符、控制字符、平台保留符号、首尾句点和过长名称（HTTP 送审最多 200，内部正式入库最多 255 字符）。它不决定物理文件名；处理版 PDF 始终使用 `document_id.pdf`。

---

## 持久化与失败边界

```mermaid
flowchart TD
    request["确认值与必填 note"] --> validation["身份、阶段、动态字段及条款校验"]
    validation --> pending["保存待审 SQLite 与 PDF"]
    pending --> response["201 submitted / run.submitted，释放提取运行"]
    pending --> publish["后台发布中间件"]
    publish --> review["拉取外部审核反馈"]
    review -->|passport 为空| reject["记录拒绝后 ack"]
    review -->|passport 非空| embedding["最终摘要向量化"]
    embedding --> sqlite["正式 SQLite ingesting，保存 passport"]
    sqlite --> file["正式 PDF"]
    file --> es["Elasticsearch"]
    es --> graph["Neo4j"]
    graph --> ready["SQLite ready"]
    ready --> completed["待审处理完成后 ack"]
```

SQLite 默认位于 `data/abstract/contracts.db`。服务首先将最终 Core `signing_date` 规范为 `YYYY-MM-DD`，再以同一个短事务写入名称、类别展示摘要、规范签约日期、文件地址、上传人、入库时间和全部已知类别关联，并把状态设为 `ingesting`；Elasticsearch Core 写入同一日期值。SQLite 事务提交后才开始文件和 ES I/O，不会在网络调用期间持有写锁。普通文件管理只能读取 `ready` 记录；类别筛选使用关联表和 `contract_category_metadata.code`，不解析展示摘要。

处理版 PDF 先保存到 `data/contract/<document_id>.pdf`。文件存储重新计算 SHA-256，拒绝字节与身份不一致的内容；写入使用同目录临时文件和原子替换，已有同身份文件会先核对内容后直接复用。

ES 写入使用 `ELASTICSEARCH_INDEX_NAME`，默认 `contracts-v1`，并以 `document_id` 同时作为 `_id` 和 `_source.document_id`。反馈重试按相同 document_id 和 passport 复用已 ready 合同；不同身份或 deleting 状态禁止覆盖。部分入库重试使用稳定 document_id 修复，不使用实验索引配置。

PDF、ES 或 Neo4j 写入失败时保留 SQLite `ingesting` 记录作为持久化恢复入口，失败原因保留在异常链与日志，待审快照不会删除；消费者不 ack，后续反馈重投继续尝试。提取运行在送审完成时已释放。ES 超时会立即实时读取同一 `_id`，元数据匹配时继续；无法确认时不丢弃 SQLite 对账依据。ES 成功后通过 `ContractGraphStore.ensure_contract()` 幂等创建 `(:Contract {document_id})`，不覆盖既有节点属性或关系。图节点成功后才发布 SQLite `ready`，消费者随后提交本地处理完成状态并 ack；不再发布提取运行事件。

相同 `document_id` 的入库和删除在当前单进程内共用文档锁。应用启动时初始化 Neo4j 唯一约束，然后处理非就绪记录：`deleting` 继续删除；`ingesting` 核验 PDF 哈希及 ES 元数据，匹配后确保图节点存在再发布 `ready`，不匹配则先持久化 `deleting`，再清理四处存储。随后为所有 `ready` 历史合同幂等补建图节点。恢复期间外部存储不可达或清理失败会阻止启动，不将未完成记录发布为可用合同。

---

## 依赖与验证

### 正式合同删除

`ContractIngestionService.delete_document()` 复用文档锁。首次接受 `ready`，重试接受 `deleting`；`ingesting` 仍返回冲突。先以短事务登记 `deleting`，从列表、摘要、注意事项和新的会话合同引用中隐藏，然后依次清理 Neo4j 节点及全部关联边 → ES → PDF → SQLite。SQLite 按 `document_id + ingestion_id` 条件删除，并级联清理类别关联与注意事项。

失败时保留 `deleting`，由[后台执行器](deletion-review-executor.md)延迟重试，或下次启动继续清理；不自动回滚已完成删除。图节点、ES 文档、PDF 已不存在均视为该步骤完成。删除中的合同不能被新入库覆盖或重新发布为 `ready`；未来关系新增入口必须校验双方为 `ready`，并协调相同合同锁。PDF 固定路径与符号链接防护保持不变。HTTP 删除入口已改为保存申请并置 can_delete=false，只有审批通过才进入上述清理，见[提交删除审核](../../api/contract.md#删除正式合同)。

### 装配与验证

应用启动时由 `app.bootstrap` 使用共享 `AsyncElasticsearch`、`Neo4jClient` 和 `ContractGraphStore`、正式索引名、固定 Core 目录、向量维度、本地合同文件存储和 SQLite 元数据存储装配入库服务。SQLite 路径由 `CONTRACT_METADATA_DATABASE_FILE` 配置，默认 `data/abstract/contracts.db`。

不连接外部服务的基础静态验证命令为：

```bash
python -m compileall -q app
```

联调时还应确认正式索引 mapping 已完成启动同步，并分别验证正常写入、覆盖同 `document_id`、非法 Core/Clause、ES 不可用后重试、非 `ready` 记录不进入文件列表、启动对账恢复，以及待审保存成功后提取查询返回 `404`、重复提交返回同一申请。

---

## 外部审核后的自动入库

[审核反馈消费者](ingestion-review-consumer.md)复用本服务，传入待审快照、外部审核员及非空 passport。passport 写入 SQLite 合同元数据，不加入 ES；拒绝不调用本服务。对于已 ready 且 document_id、passport 均一致的合同，返回原入库结果以覆盖反馈重投窗口。仍处于 ingesting 的相同身份允许继续修复；不同身份或 deleting 状态拒绝覆盖。HTTP 入库入口已切换为保存待审申请。
