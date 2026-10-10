# 已完成待审申请定时清理

`IngestionReviewCleanupService` 定时删除已完成申请的待审快照、`ingestion_reviews` 行以及 `data/ingestion-review/files/<submission_id>.pdf`，不访问或删除正式合同 SQLite、PDF、ES、Neo4j。服务依赖[待审存储](../../architecture/data/ingestion-review.md)，与[审核反馈消费](ingestion-review-consumer.md)共同维护重投幂等。

---

## 清理条件与计时

同时满足以下条件才允许清理：

- 已保存反馈消息 ID，并有本地 `review_processed_at` 完成时间。
- 审核状态为 approved 且入库状态为 succeeded，或者审核状态为 rejected。
- 从本地处理完成时间算起，已达到配置的保留时长。

不以申请创建时间、外部审核时间或最近更新时间计龄。approved 但仍在入库、入库失败、未送审、待审核和没有本地完成标记的记录不会清理。默认完成后保留 7 天；每小时扫描一轮，每轮最多 100 份。应用启动后立即执行首轮扫描，后续按间隔运行，实际删除时间可能比保留期更晚。

不要求 `review_acked_at` 非空：远端可能已经 ack 成功但响应丢失，本地无法补齐该时间。清理后由最小处理凭据支持仍未确认反馈的重投，而不是无限期保留大体积快照。

---

## 清理与幂等凭据

每份申请在独立 SQLite 写事务中重新核验资格，按顺序执行：

1. 在 `processed_review_receipts` 保存最小处理凭据。
2. 删除以规范 UUID 生成的待审 PDF 路径，并同步文件目录。
3. 删除 `ingestion_reviews` 整行并提交事务；快照 JSON、融合向量、提交备注、审核备注及其他申请字段随行删除。

凭据只含 submission_id、原请求 ID、反馈 ID、反馈内容 SHA-256、本地处理完成时间、本地 ack 时间与清理时间。不保存合同正文、PDF、快照、双方备注、用户名称或 passport。由于消息重投没有约定最大期限，该最小表暂不自动过期；它不用于恢复已删除快照。

再次收到同一反馈时，存储层按请求 ID、反馈 ID 和内容哈希核验，返回已完成凭据。消费者跳过入库，直接确认当前 pull 的消息；若内容或身份不一致则仍拒绝确认。清理与 ack 交错时，ack 时间写入凭据表，不依赖已经删除的待审行。同一 submission_id 的保存重试不能复活已清理申请。

---

## 失败、并发与范围

| 情况 | 处理 |
| --- | --- |
| 文件删除失败 | 回滚该条事务，保留待审行，下轮重试；继续本轮其他候选。 |
| 文件已缺失 | 视为无需再删除，继续完成数据库清理。 |
| 文件已删除、数据库提交失败 | 数据库回滚保留快照；下轮按缺失文件继续清理。该申请已处理完成，消费者不重新读取 PDF 或入库。 |
| 并发扫描 | 进程内扫描锁加逐条数据库事务，重复删除返回未清理，不重复生成凭据。 |
| 进程退出 | 停止调度；等待正在执行的删除事务结束，再关闭审核消费与其依赖。 |
| 记录中路径异常 | 不使用记录里的任意路径，固定删除待审 files 下的 UUID.pdf；符号链接仅删除链接本身。 |

服务不会遍历删除未关联文件、临时写入中的文件或正式合同文件。单份失败只记录申请 ID 与异常类型，不打印快照或备注。单轮最多处理配置数量；大量长期删除失败的旧申请需要人工修复文件权限等原因。

清理后原提交接口无法再恢复完整申请回执，通常返回 404；在保留期内，相同内容重复提交仍返回原申请。前端不能把重提取同一合同当成查询历史申请的方式。

---

## 装配与配置

代码入口：`app/service/ingestion_review_cleanup.py`。`start()` 非阻塞启动、`scan_once()` 返回本次删除数量、`close()` 停止并等待删除任务。数据库方法为 `list_cleanup_candidates(cutoff, limit)` 与 `cleanup_completed(submission_id, cutoff)`。

lifespan 将服务保存在 `application.state.ingestion_review_cleanup_service`，待审消费者启动后启动清理，退出时先停止清理。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INGESTION_REVIEW_RETENTION_SECONDS` | `604800` | 本地处理完成后的保留秒数，即 7 天，有限正数。 |
| `INGESTION_REVIEW_CLEANUP_INTERVAL_SECONDS` | `3600` | 每轮结束后等待秒数，有限正数。 |
| `INGESTION_REVIEW_CLEANUP_BATCH_SIZE` | `100` | 每轮最多处理申请数，正整数。 |

`.env` 和 `.env.example` 已提供配置。初始化幂等补建凭据表和清理索引，不对未完成申请补造完成时间。

---

## 验证

`tests/test_ingestion_review_cleanup.py` 使用临时真实 SQLite/PDF，覆盖批准/拒绝删除、保留期边界、失败及未完成保留、无 ack 时间清理、清理后重投、反馈冲突、清理/ack 交错、文件失败、删除后事务失败恢复、路径隔离、禁止复活、批量并发、定时失败重试和停止等待。仅操作临时测试数据，不执行当前开发库的清理。
