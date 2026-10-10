# 合同入库审核存储

保存前端确认后、正式入库前所需的 PDF 与结构化快照。待审区独立于正式 SQLite、ES、Neo4j 和正式 PDF 目录；存储模块不调用模型；后台发送服务通过中间件 HTTP 接口发布待审申请。

本模块统一采用 `ingestion-review` / `ingestion_review` / `IngestionReview` 命名，与[删除审核区](deletion-review.md)区分。

---

## 存储与初始化

```text
data/ingestion-review/
├── reviews.db
└── files/
    └── <submission_id>.pdf
```

### 旧版本目录与配置迁移

旧 `data/pending-review` 目录需在后端停止后整体更名为 `data/ingestion-review`，保留数据库、PDF 及 SQLite 日志文件。环境变量前缀 `PENDING_REVIEW_` 全部改为 `INGESTION_REVIEW_`，包括目录、扫描、发送、轮询和清理配置；自定义目录也须同步更新。不要让运行中的服务继续使用已移动的旧地址，也不要将两个已有数据库目录相互覆盖。

初始化将旧 `pending_reviews` 表原地更名为 `ingestion_reviews` 并重建具名索引，保留快照、备注、申请 ID、消息 ID、审核结果及 processed_review_receipts；若新旧申请表同时存在则拒绝自动合并。数据库文件名仍为 reviews.db。快照 schema_version、审核状态 `pending_review` 和 UUID 生成命名空间保持原值，避免改名后重新投递或生成不同申请 ID。

入库审核查询前缀已改为 `/contract/api/ingestion-reviews/...`，旧 `/contract/api/pending-reviews/...` 已移除；资源 PDF 仍使用 `/contract/api/resource/pending-review-pdf/...`。Python 路由模块和文档文件使用 ingestion_review 名称。旧配置变量不再读取，部署时需要更新配置后再启动。

`INGESTION_REVIEW_DIRECTORY` 默认 `data/ingestion-review`，相对项目根目录解析。`get_settings().ingestion_review_path` 提供绝对路径。后端 lifespan 创建目录、初始化 SQLite（WAL、FULL 同步），并将 `ingestion_review_store`、`ingestion_review_service` 保存至 `application.state`。存储初始化不自动转存提取任务；后台发送服务只处理待审表中完整保存的记录。

数据库、PDF、WAL/SHM 和临时文件不进入 Git。Docker 持久化 `/workspace/data`，默认待审目录随 `backend-data` 保存；自定义到该目录外时需另行挂载。已有卷通过初始化代码补建目录。

---

## 快照和记录契约

`app/schema/ingestion_review.py` 定义两个对象：

- `IngestionReviewSnapshot`：提交时的不可变业务快照。
- `IngestionReviewRecord`：申请 ID、快照、入库员备注、消息关联、状态、时间及 PDF 相对路径。

| 快照字段 | 用途 |
| --- | --- |
| `schema_version` | 当前固定为 1，后续格式升级依据。 |
| `run_id` | 提取任务 ID；同一任务只接受一份提交。 |
| `document_id`、`page_count` | PDF SHA-256 与页数，存入时校验实际文件。 |
| `file_name`、`summary` | 用户确认的合同名称和摘要。 |
| `submitted_by` | 提交入库的用户，后续接口必须从认证身份取得。 |
| `classification`、`category_reasoning` | 类别、场景及分类理由。 |
| `core`、`clauses` | 复用当前提取模型，保存最终确认值。 |
| `retrieval_questions` | 原始检索问题，按原顺序保留。 |
| `embedding_model`、`vector_dimensions` | 已生成融合向量的模型与维度。 |
| `question_fusion_vector`、`page_fusion_vector` | 必要的两类融合向量，不重新计算、不建检索索引。 |

完整快照以规范 JSON 保存，包括向量，确保读取时恢复现有入库所需的字段而不重复建模。此阶段尚未拆分向量 BLOB；JSON 是快照权威内容，独立列用于定位和扫描。

`ingestion_reviews` 表保存：

| 列 | 约束与含义 |
| --- | --- |
| `submission_id` | 本平台生成并由调用方保留的 UUID 主键，用于发送前定位和幂等重试。 |
| `run_id` | 唯一提取任务 ID。 |
| `message_id` | 中间件成功接收后返回的消息 ID；初始 NULL，非空值唯一，后续回执以此关联。 |
| `review_message_id` / `review_offset` | 反馈消息 ID（唯一）与当前交付位置；不是原发布消息 ID。 |
| `review_payload_json` | 首次接收的完整反馈，用于检查重投内容一致性。 |
| `review_note` / `reviewed_by` / `reviewed_at` | 审核平台的备注、审核员与带时区时间，独立于入库员信息。 |
| `passport` | 批准时为跨平台关联标识；拒绝为空字符串；尚无反馈为 NULL。允许多份合同申请使用同一 passport；初始化移除旧 ingestion_reviews_passport 唯一索引。 |
| `review_processed_at` / `review_acked_at` | 本地业务处理完成时间与本地确认收到 ack 成功的时间，后者不代替远端进度。 |
| `last_review_error` | 最近正式入库失败的异常类型，不保存响应正文。 |
| `note` | 入库员备注，独立 TEXT 列，默认空字符串，最多 10000 字符；仅用于审核沟通，不写入合同快照或正式数据库。 |
| `document_id`、`submitted_by`、`schema_version` | 快照关键身份投影。 |
| `snapshot_json`、`snapshot_sha256` | 完整快照与校验摘要。 |
| `pdf_relative_path` | `<submission_id>.pdf`，相对待审 `files/` 目录，不接受外部指定路径。 |
| `review_status` | `pending_send / pending_review / approved / rejected`，初始待发送。 |
| `ingestion_status` | `pending / ingesting / succeeded / failed`，初始未入库。 |
| `delivery_status` | `pending / publishing / published / uncertain / blocked`，独立表示消息发送状态。 |
| `publish_attempts` | 已领取发送次数。 |
| `next_publish_at` | 明确失败或结果不确定后下次可重试的 UTC Unix 秒数。 |
| `last_publish_error` | 脱敏错误代码，不保存令牌或远端错误正文。 |
| `created_at`、`updated_at` | 带 UTC 时区的 ISO 时间。 |

审核状态与正式入库状态独立。当前已实现保存、后台扫描发布，以及成功后从 `pending_send` 变为 `pending_review`；批准、拒绝与正式入库由[审核反馈消费者](../../capability/application/ingestion-review-consumer.md)推进。状态加创建时间建立联合索引，方便后续扫描。旧的早期待审库初始化时补建 `message_id` 列，旧库初始化时新增 `note` 列；将旧快照中的备注移出并重算快照哈希，未填写的备注保留为空字符串。初始化同时幂等补齐外部审核字段，旧记录保持 NULL；将旧 `files/<submission_id>.pdf` 路径简化为 `<submission_id>.pdf`，实际 PDF 仍保存在 `files/`，无需移动。

---

## 服务接口

`app/service/ingestion_review.py` 的 `IngestionReviewService` 对外提供异步方法，SQLite 与文件操作通过线程执行：

| 方法 | 行为 |
| --- | --- |
| `initialize()` | 幂等创建目录与表。 |
| `save(submission_id=..., snapshot=..., processed_pdf_bytes=..., note="")` | 保存并返回完整待审记录；调用方首次生成 UUID，失败重试必须复用。 |
| `get(submission_id)` | 按本地申请 ID 读取记录；不存在抛出 `LookupError`。 |
| `read_pdf(submission_id)` | 返回 PDF bytes，并重新校验哈希。 |
| `bind_message_id(submission_id, message_id)` | 发送确认后原子关联消息并标记待审核；同值重试不更新状态或时间。 |
| `get_by_message_id(message_id)` | 按中间件消息 ID 查找待审记录。 |

成功发布或结果不确定时，若中间件返回了消息 ID，均保存至 `message_id`；后者保持审核状态未确认，以 `delivery_status=uncertain` 区分，但会按 next_publish_at 定时重试，不能视为成功送审。409合法重复申请回执关联原消息并进入待审核。

消息 ID 是中间件的 opaque 字符串，不假设 UUID 格式，不截断或重写。已确认绑定的申请不能通过 bind_message_id 更换消息 ID；后台重试期间保留候选 ID，明确回执决定最终关联。同一消息不能绑定两份申请，冲突抛出 `IngestionReviewConflictError`。

服务校验基础结构、向量维度/有限值/非零、条款页码、PDF 格式与未加密状态、哈希和页数。它不替代正式入库的动态 Core/category 业务约束校验；后续接入提交接口时仍需在送审前执行该校验。摘要向量目前仍属于正式入库阶段的生成职责，不在这份快照中伪造。

---

## 一致性和恢复边界

同一申请的快照不允许修改。同 ID、同快照且同备注重复存入返回原记录，不覆盖备注、状态或时间；不同内容或同一 run_id 使用另一申请 ID 均拒绝。重审及修改后重新提交策略尚未定义，不提供原位修改接口。

存入流程以 `BEGIN IMMEDIATE` 串行保护并发提交，先将 PDF 写入同目录临时文件，flush/fsync 后原子重命名，再提交数据库记录。文件成功而数据库失败时保留孤立 PDF，重试时校验哈希并复用；定时任务只扫描数据库已提交的记录。当前不自动删除孤立文件，避免误删尚待重试的快照。

进程中断或线程等待取消时，调用方不能推断是否已经提交，应以同一 submission_id 查询或重试。读取发现文件缺失或损坏会抛出 `IngestionReviewFileError`；同内容重试可补回缺失文件，但不会覆盖损坏文件。

发送成功与本地绑定 message_id 不共享事务。当前中间件按平台与 document_id（source_id）对 registry 尚未确认的申请去重，后台允许重试不确定结果，并根据409响应关联原请求。该保护在 registry ack 后解除，不等同于永久幂等。状态恢复、重试与并发反馈规则见[待审发布服务](../../capability/application/ingestion-review-publisher.md)。

---

## 当前边界与验证

`POST /contract/api/contract/extraction-runs/{run_id}/ingestion` 已接入待审提交，必填 note；成功持久化后释放提取运行。后台扫描与消息发送已实现，详见[待审请求发布](../../capability/application/ingestion-review-publisher.md)。审核回执拉取、状态处理及批准后自动入库已接入后台消费者；消费者不依赖提取运行，使用已持久化快照完成正式入库。

`tests/test_ingestion_review.py` 使用临时库和真实生成的 PDF，验证重启读取、备注、并发幂等、冲突拒绝、消息绑定及唯一性、旧库迁移、错误文件、文件写入失败和 SQLite 提交失败后的恢复。

---

## 审核处理方法

`receive_review(delivery)` 按发布消息 ID 匹配申请、原子保存不可变反馈，重复反馈校验内容一致性。`update_review_processing(submission_id, state=...)` 推进 ingesting、failed、completed、acknowledged；拒绝记录保持入库 pending，以 review_status=rejected 和 review_processed_at 表示已闭环。后台消费者复用这些方法；完成后的待审文件和记录由独立定时清理服务按保留期删除。


---

## 已完成申请清理与处理凭据

[定时清理服务](../../capability/application/ingestion-review-cleanup.md)按本地 review_processed_at 计龄，清理 approved+succeeded 或 rejected 的申请。删除整行 ingestion_reviews（包含 snapshot_json、向量、双方备注）及对应临时 PDF；正式存储不受影响。

清理事务同时创建 `processed_review_receipts`，用于清理后的反馈重投：

| 字段 | 含义 |
| --- | --- |
| `submission_id` | 已清理申请 UUID，主键。 |
| `request_id` | 原发布请求 ID，唯一。 |
| `review_message_id` | 审核反馈 ID，唯一。 |
| `review_payload_sha256` | 规范化反馈内容摘要，用于拒绝内容冲突；不保留正文和备注。 |
| `review_processed_at` | 本地业务处理完成时间。 |
| `review_acked_at` | 本地确认收到 ack 的时间，可空。 |
| `cleaned_at` | 清理事务的带时区时间。 |

凭据不含快照、PDF、用户名称或 passport，暂不设自动过期。`receive_review` 对已清理且匹配的反馈返回 `ProcessedReviewReceipt`，消费端只 ack；冲突反馈拒绝确认。`update_review_processing(..., state='acknowledged')` 可更新凭据中的确认时间。原申请的普通 get 在清理后返回不存在，save 不允许复活同一 submission_id。


---

## 只读列表与预览

已提供[待入库申请 API](../../api/ingestion-review.md)。`/list` 仅返回全部尚未清理的申请 ID（包括批准、拒绝）；`/detail/{submission_id}` 分区包装基本信息、两类备注、各阶段状态及 PDF 地址，不返回完整快照或向量。清理计时以本地 review_processed_at 为准，正式入库时间另从合同元数据读取，不能用提交时间代替。临时 PDF 经认证与哈希校验后读取。
