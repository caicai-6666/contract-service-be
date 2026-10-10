# 合同删除审核存储

保存合同删除申请的轻量身份快照、消息关联及外部审核结果，供后续删除送审流程使用。与[入库待审区](ingestion-review.md)分开存储，正式合同仍保留在原 SQLite、ES、Neo4j 和 PDF 目录中。

---

## 存储位置与配置

默认目录结构：

```text
data/deletion-review/
  .gitkeep
  deletions.db
```

数据库表为 `deletion_reviews`。`DELETION_REVIEW_DIRECTORY` 默认 `data/deletion-review`，相对路径按项目根目录解析，也支持绝对路径。启动期由 `app/bootstrap.py` 初始化数据库，启用 WAL 和 FULL 同步；只跟踪目录占位，数据库与日志不进入版本控制。默认位于容器持久化的 data 目录内；改到目录外时需要另行挂载。

删除区只保存正式 PDF 的 `file_uri` 引用，不创建 files 子目录、不复制 PDF，不搬迁或提前删除正式文件。正式合同删除后，这个地址不再保证能读取文件。

模块统一采用 `deletion-review` / `deletion_review` / `DeletionReview` 命名。旧 `data/pending-deletion` 目录需停服后整体更名为 `data/deletion-review`，并将 `PENDING_DELETION_DIRECTORY` 改为 `DELETION_REVIEW_DIRECTORY`；数据库文件名仍为 deletions.db。初始化会将旧 `pending_deletions` 表原地更名为 `deletion_reviews`，保留申请和审核结果；新旧表同时存在时拒绝自动合并。旧配置变量不再读取。

---

## 表结构

| 字段 | SQLite 类型 | 约束与含义 |
| --- | --- | --- |
| `submission_id` | `TEXT` | 删除申请 UUID 主键；调用方生成并保留，用于本地幂等重试。 |
| `document_id` | `TEXT` | 正式合同的 64 位 SHA-256；也是后续送审所用的本平台数据身份。 |
| `passport` | `TEXT NULL` | 原正式合同的跨平台通行证；历史缺失值保持 NULL，不猜测。 |
| `file_name` | `TEXT` | 申请时的合同名称；对应中间件资料名称 name。 |
| `summary` | `TEXT NULL` | 申请时的合同摘要；对应 abstract；历史缺失值保留 NULL。 |
| `uploader` | `TEXT` | 原合同上传人，即合同入库员；来自正式元数据。 |
| `requested_by` | `TEXT` | 本平台发起删除申请的用户，由删除接口从认证身份取得。 |
| `file_uri` | `TEXT` | 正式 PDF 的根相对地址，严格为 `/<document_id>.pdf`。 |
| `contract_ingested_at` | `TEXT` | 原合同入库时间，保留时区。 |
| `note` | `TEXT` | 删除申请人的审核沟通备注，最多 10000 字符。HTTP 申请必须提交该字段，允许空字符串；内部兼容保存与历史记录默认空字符串。 |
| `message_id` | `TEXT NULL` | 中间件确认接收删除请求后的消息 ID，非空时唯一。 |
| `delivery_status` | `TEXT` | pending / publishing / published / uncertain / blocked；发送结果不确定时即使有 message_id 也不视为发布成功。 |
| `publish_attempts` | `INTEGER` | 原子领取发送的次数，默认0。 |
| `next_publish_at` | `REAL NULL` | 明确未发布失败后的最早重试 UTC Unix 秒数。 |
| `last_publish_error` | `TEXT NULL` | 脱敏发送错误代码，不存原始响应或凭据。 |
| `review_status` | `TEXT` | `pending_send / pending_review / approved / rejected`。 |
| `review_message_id` | `TEXT NULL` | 外部反馈消息 ID，非空时唯一，用于幂等接收结果。 |
| `reviewed_by` | `TEXT NULL` | 外部审核员，独立于原上传人和删除申请人。 |
| `review_note` | `TEXT NULL` | 外部审核备注，最多 10000 字符。 |
| `reviewed_at` | `TEXT NULL` | 外部审核时间，必须带时区。 |
| `deletion_status` | `TEXT` | 实际删除执行状态：`pending / deleting / succeeded / failed`，默认 pending。 |
| `deletion_attempts` | `INTEGER` | 领取执行次数，默认 0，非负。 |
| `next_deletion_at` | `REAL NULL` | 下次可重试的 UTC Unix 秒数；NULL 表示无等待限制。 |
| `last_deletion_error` | `TEXT NULL` | 最近失败的脱敏异常类型或恢复代码。 |
| `deletion_completed_at` | `TEXT NULL` | 实际删除完成时间，UTC ISO 8601。 |
| `review_processed_at` | `TEXT NULL` | 本地审核结果处理完成时间；批准后正式删除完成，或拒绝后标志协调完成才保存，用于保留期计时。 |
| `created_at` | `TEXT` | 本地申请创建时间，UTC ISO 8601。 |
| `updated_at` | `TEXT` | 最近发送状态、消息绑定或审核结果变更时间，UTC ISO 8601。 |

尚未产生的审核字段保持 NULL。批准和拒绝都保存记录；`approved` 仅说明批准删除，不说明正式合同已经删除成功。

[后台执行器](../../capability/application/deletion-review-executor.md)领取已批准且有反馈消息 ID 的申请；只有 `deletion_status=succeeded` 表示正式删除完成。初始化为旧表补齐执行字段，默认 pending，不改变已有审核结论。

同一 document_id 的未拒绝申请建立部分唯一索引，阻止两条并发有效申请。拒绝后允许新 UUID 保存新申请，原拒绝记录在保留期内可查询；同一 passport 可以关联多份合同，不能作为删除对象唯一键。[定时清理](../../capability/application/deletion-review-cleanup.md)在处理完成且保留期届满后移除审核行，释放 document_id 的申请唯一性；批准记录在保留期内仍占用该唯一性。

`processed_deletion_review_receipts` 保存清理后的最小幂等凭据：submission_id 主键、request_id 唯一、review_message_id 唯一、review_payload_sha256、review_processed_at、cleaned_at，均为非空 TEXT。时间为 UTC ISO 8601；不保留合同 ID、PDF 引用、passport、人员或备注，不参与对外查询，暂不自动过期。反馈规范化为 UTC 后计算内容哈希，重放必须匹配请求 ID、反馈 ID 和哈希。

---

## 模型、存入服务及读写方法

- `app/schema/deletion_review.py`：不可变 `DeletionReviewSnapshot`、完整 `DeletionReviewRecord`、内部 `DeletionReviewFeedback` 及审核状态枚举。
- `app/infrastructure/deletion_review_store.py`：`SQLiteDeletionReviewStore`，负责初始化、幂等保存、查询、消息绑定和审核结果保存。
- `app/service/deletion_review.py`：`DeletionReviewService`，阻塞数据库操作在线程中执行，从正式元数据构造精简快照。
- `app/service/deletion_review_publisher.py`：定时发送申请与正式 PDF，保存回执并定时重试不确定发送。
- `app/service/deletion_review_executor.py`：已批准申请的定时扫描、正式删除及失败重试。
- `app/service/deletion_review_cleanup.py`：清理完成且保留期届满的审核行，保留最小幂等回执，不操作正式文件。
- `app/service/deletion_review_query.py` 与 `app/router/deletion_review.py`：按当前用户是删除提交人或合同上传人查询申请 ID 和详情，HTTP 契约见[删除审核 API](../../api/deletion-review.md)。

异步服务方法：

| 方法 | 行为 |
| --- | --- |
| `initialize()` | 初始化独立目录、数据库与索引；不发起删除或网络请求。 |
| `save(submission_id, document_id, requested_by, note)` | 从正式元数据读取身份快照，仅首次接收 ready 且 can_delete=true 的合同。 |
| `submit(document_id, requested_by, note)` | HTTP 删除入口：校验必填字符串 note（最多 10000 字符，允许空字符串），生成申请 UUID、保存精简快照及原始备注并将 can_delete 置 false。 |
| `reconcile_flags()` | 按每份合同最新申请补齐待审锁定与拒绝恢复，仅操作身份匹配的 ready 合同。 |
| `cleanup_completed(submission_id, cutoff)` | 在共享合同锁内清理满足完成状态和截止时间的申请，事务内保留幂等回执。 |
| `get(submission_id)` / `list_ids()` | 按 ID 读取完整记录或读取按创建时间倒序排列的全部申请 ID。 |
| `bind_message_id(submission_id, message_id)` | 绑定中间件回执，由 pending_send 变为 pending_review。 |
| `get_by_message_id(message_id)` | 根据原请求消息 ID 读取本地记录。 |
| `save_review_result(message_id, feedback)` | 根据原请求消息 ID 保存审核结果；内部 approved 布尔值明确区分批准和拒绝。 |

相同申请 ID、快照和 note 可重复保存，不改变创建时间、消息和已有审核结果；同一申请不能替换合同、操作人或备注。定时发送重试也沿用这些冻结字段。消息绑定及审核结果使用短事务，重复同值回执直接返回原记录；清理后同值反馈返回 `ProcessedDeletionReviewReceipt`，冲突抛出 `DeletionReviewConflictError`。已清理申请不能按原 ID 重新创建，旧消息 ID 不能绑定新申请。反馈中的 approved 必须为真实布尔值，不能按字符串、备注或 passport 猜测。匹配已保存候选 message_id 的真实审核反馈可确认原不确定申请已被处理：delivery_status 变为 published，停止发送重试；迟到的发送结果不能回退审核终态。

服务重复保存时先读取已有申请，不因正式合同后续变化而重新构造快照。数据库缺失记录抛出 LookupError；首次保存的正式合同不存在或状态不可用时使用现有合同元数据错误类型。消息 ID、姓名和摘要不采用字符串插值拼接 SQL，所有业务值使用参数化语句。

发送存储另提供 `claim_next_publication()`、`finish_publication()` 和 `recover_interrupted_publications()`，分别负责原子领取、结果落盘和启动恢复延迟重试。旧库迁移补齐发送字段，已绑定回执的记录标记 published；不确定结果按原 source_id 重试，409合法重复回执关联原消息并进入待审核。细则见[删除送审发布服务](../../capability/application/deletion-review-publisher.md)。

存储层额外提供 `claim_next_deletion()` 原子领取并增加尝试次数、`finish_deletion()` 保存成功或延迟重试结果，以及 `recover_interrupted_deletions()` 恢复上次进程遗留的执行状态。成功删除时同事务保存 review_processed_at；拒绝后由 `mark_rejection_processed()` 在标志协调成功后首次保存。`list_cleanup_candidates()` 与 `cleanup_completed()` 复核处理状态及保留期，清理索引覆盖完成时间、审核状态和删除状态。完整规则见[删除审核定时清理](../../capability/application/deletion-review-cleanup.md)。

只读接口使用 `list_ids_for_user(user_name)` 与 `get_for_user(submission_id, user_name)`，在 SQL 中以 `requested_by=? OR uploader=?` 限定相同人员范围。两个角色分别建立查询索引；同一用户兼任两个角色不产生重复记录。过滤基于冻结的人员快照，不要求正式合同仍存在。

---

## 与删除信号量的衔接边界

正式表 `contracts.can_delete` 是面向全平台的删除申请标志，详情见[删除申请标志](contract-sqlite-metadata.md#删除申请标志)。`submit()` 与正式入库/删除共享文档锁，先持久化申请，再置 false；低层 `save()` 仅保存快照，不单独切换标志。部分唯一索引在跨库写入间隔仍阻止重复有效申请。

删除申请 HTTP 接口、标志切换及拒绝恢复已接入；接口改为202返回申请ID，见[提交删除审核](../../api/contract.md#删除正式合同)。审批通过后的实际删除由后台执行器完成，完成后的审核数据由定时清理服务按保留期清除。[中间件发布](../../capability/application/deletion-review-publisher.md)已接入；删除审核结果拉取与 ack 尚未接入。

正式库与审核库是两个独立数据库，不承诺跨库原子提交。申请是持久化恢复依据：启动及每轮后台扫描调用 `reconcile_flags()`，待审/批准保持 false，拒绝恢复 true。每份合同最新申请决定当前标志，尚未完成的旧拒绝也单独补齐处理完成时间；按冻结入库时间和 passport 核对原身份，再以当前 ingestion_id 更新。旧反馈不能修改后来新申请或重新入库合同的标志。快照已保存、标志写入失败时不撤销申请，同提交人且备注未变的待发送请求可以修复后返回原 ID；更换备注或其他重复申请拒绝。请求取消时等待线程内写入完成再释放文档锁。

中间件删除请求已按 multipart 契约发布，审核反馈协议尚未提供；`DeletionReviewFeedback` 是内部存储模型，并非对外 HTTP 契约。待正式协议明确后再做字段转换。

---

## 验证

`tests/test_deletion_review.py` 使用临时 SQLite 验证重启读取、幂等重放、合同并发申请唯一性、消息唯一性、批准/拒绝保存、冲突反馈防覆盖、拒绝后重新申请、历史空摘要/通行证、文件地址约束和服务从正式元数据取值。测试不会写入正式运行数据库、复制 PDF 或向审核队列发送消息。

`tests/test_deletion_review_submission.py` 使用临时正式库与审核库验证202提交、合同可见性保持、并发/重复拦截、快照失败、标志失败恢复、启动协调、拒绝后新申请、旧反馈重放、批准后后台删除和请求中断期间文档锁保护。
