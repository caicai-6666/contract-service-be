# 复核后合同正式入库

> **用途：** 本文说明审核用户如何基于内存 `run_id` 提交最终文件名、Core 和 Clause，以及服务端如何协调 SQLite 文件目录、处理版 PDF 和正式 Elasticsearch 索引。

SQLite 字段以[合同 SQLite 元数据结构](../../architecture/data/contract-sqlite-metadata.md)为准，最终 Elasticsearch 字段以[合同 Elasticsearch 文档结构](../../architecture/data/contract-elasticsearch-document.md)为准；HTTP 请求和响应见[合同 API](../../api/contract.md#正式入库合同)。

---

## 职责与输入

正式持久化前新增名称与摘要合并向量化：以用户最终确认的 `file_name` 与 `summary` 编码，复用当前 Embedding 配置。编码失败时不开始持久化；成功后向量与对应文本在同一 SQLite 事务写入。详见[合同名称与摘要向量](../../architecture/data/contract-sqlite-metadata.md#合同名称与摘要向量)。注意事项不参与编码，旧合同不自动回填。

正式入库由 `app.service.contract_ingestion.ContractIngestionService` 承担投影与四处持久化编排，`app.infrastructure.contract_metadata_store.SQLiteContractMetadataStore` 使用短事务维护文件目录和状态，`ContractExtractionService` 负责按 `run_id` 定位运行、校验所有权和控制生命周期。

调用方提交以下最终审核值：

- `file_name`：最终展示名称，不作为物理存储路径；
- `summary`：非空的合同事实摘要，最多 3000 字符，写入 SQLite；
- `core`：包含启动期 Core 目录全部稳定 `code` 的完整对象，没有最终值时使用 `null`；
- `clauses`：按原合同阅读顺序排列的完整最终条款。

服务端从运行聚合补齐：

- 处理版 PDF 的 `document_id`、字节、页数和 `file_uri`；
- 已确认的合同分类；
- Retrieval 分支的全部正式生成问题原文（`retrieval_questions`）及问题融合向量；
- PDF 查重阶段的页面融合向量；
- 当前登录审核人的名称和带时区入库时间。

合同分类同时投影到 SQLite 两种结构：`contracts.category` 保留以 ` / ` 连接的 code 摘要（未映射时保留类型说明），`contract_category_assignments` 通过类别外键保留可精确筛选的多标签关系，并逐关联保存模型的 `reasoning_summary`。新入库与 ES 对账均按分类 code 生成摘要；前端通过类别目录映射中文名称。推理摘要从运行聚合内的完整分类结果取得，不扩大面向前端的紧凑分类 View。

审核人不由请求体提供。入库调用继续执行与快照、SSE、重试相同的运行所有权校验，跨用户访问按任务不存在处理。

---

## 入库条件与校验

八个用户业务阶段必须全部为 `succeeded`，并且内存聚合中必须同时存在分类、建议名称、Core、Clause、Retrieval 和 PDF 查重结果。分支结果可以是 `partial`：用户提交的完整最终值会替换自动 Core 和 Clause，且最终文件名可以与自动建议不同；两个合同级向量仍必须已经成功形成。

Core 按启动期不可变字段目录执行动态校验。

正式入库额外要求 `core.signing_date` 非空，且为真实、完整的年月日；缺失、`null`、空字符串或非法日期均返回 `422`，不会开始 SQLite、PDF 或 ES 写入。通过校验后统一为 `YYYY-MM-DD`。提取阶段仍允许未知日期为 `null`，不能为满足入库约束而猜测；审核人需补充有依据的签订日期后再次提交。此规则只约束新入库，不回填或删除历史空日期记录。

其余校验规则：

- 顶层必须精确包含全部 Core `code`，拒绝未知或缺失字段；
- `single` 单属性字段使用标量，`single` 多属性字段使用对象，`multiple` 字段使用对象数组；
- 对象拒绝未知属性，并要求所有 `required: true` 属性存在；
- 字符串、整数、数值和布尔值必须符合定义类型，字符串不能为空白，数值必须有限；
- 顶层 `null` 和空多值数组在最终投影时省略，不写入 Elasticsearch。

Clause 至少包含一条，并校验 `clause_id` 唯一、`order` 按数组顺序从 1 连续增长、父条款先于子条款出现、页码不超过处理版 PDF 总页数，以及编号、路径和正文非空。`title`、`parent_clause_id` 为 `null` 时在最终文档中省略。

检索问题原文必须是非空列表，所有元素必须是非空文本；在任何持久化写入前校验，随后按原顺序保存至 ES `retrieval_questions`。前端请求契约不变，SQLite 不增加问题字段。包括部分 Embedding 失败在内的保存边界及后续换模型方式，见[检索问题原文契约](../../architecture/data/contract-elasticsearch-document.md#检索问题原文)。

`file_name` 会去除首尾空白，并拒绝路径分隔符、控制字符、平台保留符号、首尾句点和超过 255 个字符的名称。它不决定物理文件名；处理版 PDF 始终使用 `document_id.pdf`。

---

## 持久化与失败边界

```mermaid
flowchart TD
    request["最终 file_name、Core、Clause"] --> run["按 run_id 与审核人读取聚合"]
    run --> readiness["校验阶段与内部结果完整性"]
    readiness --> validation["校验最终审核值并组装 ES 文档"]
    validation --> sqlite_ingesting["SQLite 短事务<br/>登记 ingesting"]
    sqlite_ingesting --> file["幂等保存 document_id.pdf"]
    file --> es["写入 ELASTICSEARCH_INDEX_NAME"]
    es --> graph["Neo4j MERGE 合同节点"]
    graph --> sqlite_ready["SQLite 短事务<br/>发布 ready"]
    sqlite_ready --> event["发布 run.ingested"]
    event --> remove["删除内存 run_id 并关闭 SSE"]
```

SQLite 默认位于 `data/abstract/contracts.db`。服务首先将最终 Core `signing_date` 规范为 `YYYY-MM-DD`，再以同一个短事务写入名称、类别展示摘要、规范签约日期、文件地址、审核人、入库时间和全部已知类别关联，并把状态设为 `ingesting`；Elasticsearch Core 写入同一日期值。SQLite 事务提交后才开始文件和 ES I/O，不会在网络调用期间持有写锁。普通文件管理只能读取 `ready` 记录；类别筛选使用关联表和 `contract_category_metadata.code`，不解析展示摘要。

处理版 PDF 先保存到 `data/contract/<document_id>.pdf`。文件存储重新计算 SHA-256，拒绝字节与身份不一致的内容；写入使用同目录临时文件和原子替换，已有同身份文件会先核对内容后直接复用。

ES 写入使用 `ELASTICSEARCH_INDEX_NAME`，默认 `contracts-v1`，并以 `document_id` 同时作为 `_id` 和 `_source.document_id`。同一 `document_id` 再次写入会覆盖该合同文档，支持审核用户在查重后选择更新同身份合同；不会使用实验索引配置。

PDF、ES 或 Neo4j 写入失败时保留 SQLite `ingesting` 记录作为持久化恢复入口，失败原因保留在异常链与日志，运行聚合不会删除，用户可使用同一 `run_id` 重试。ES 超时会立即实时读取同一 `_id`，元数据匹配时继续；无法确认时不丢弃 SQLite 对账依据。ES 成功后通过 `ContractGraphStore.ensure_contract()` 幂等创建 `(:Contract {document_id})`，不覆盖既有节点属性或关系。图节点成功后才发布 SQLite `ready` 和 `run.ingested`、释放运行。

相同 `document_id` 的入库和删除在当前单进程内共用文档锁。应用启动时初始化 Neo4j 唯一约束，然后处理非就绪记录：`deleting` 继续删除；`ingesting` 核验 PDF 哈希及 ES 元数据，匹配后确保图节点存在再发布 `ready`，不匹配则先持久化 `deleting`，再清理四处存储。随后为所有 `ready` 历史合同幂等补建图节点。恢复期间外部存储不可达或清理失败会阻止启动，不将未完成记录发布为可用合同。

---

## 依赖与验证

### 正式合同删除

`ContractIngestionService.delete_document()` 复用文档锁。首次接受 `ready`，重试接受 `deleting`；`ingesting` 仍返回冲突。先以短事务登记 `deleting`，从列表、摘要、注意事项和新的会话合同引用中隐藏，然后依次清理 Neo4j 节点及全部关联边 → ES → PDF → SQLite。SQLite 按 `document_id + ingestion_id` 条件删除，并级联清理类别关联与注意事项。

失败时保留 `deleting`，通过同 ID 删除请求或下次启动继续；不自动回滚已完成删除，当前未提供运行期后台重试队列。图节点、ES 文档、PDF 已不存在均视为该步骤完成。删除中的合同不能被新入库覆盖或重新发布为 `ready`；未来关系新增入口必须校验双方为 `ready`，并协调相同合同锁。PDF 固定路径与符号链接防护保持不变。HTTP 契约见[删除正式合同](../../api/contract.md#删除正式合同)。

### 装配与验证

应用启动时由 `app.bootstrap` 使用共享 `AsyncElasticsearch`、`Neo4jClient` 和 `ContractGraphStore`、正式索引名、固定 Core 目录、向量维度、本地合同文件存储和 SQLite 元数据存储装配入库服务。SQLite 路径由 `CONTRACT_METADATA_DATABASE_FILE` 配置，默认 `data/abstract/contracts.db`。

不连接外部服务的基础静态验证命令为：

```bash
python -m compileall -q app
```

联调时还应确认正式索引 mapping 已完成启动同步，并分别验证正常写入、覆盖同 `document_id`、非法 Core/Clause、ES 不可用后重试、非 `ready` 记录不进入文件列表、启动对账恢复，以及入库成功后 `run_id` 返回 `404`。
