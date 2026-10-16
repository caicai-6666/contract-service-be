# 删除审核反馈消费与资源清理

后台从中间件拉取删除审核结果，完成正式资源删除或拒绝后的标志恢复，再确认消息。依赖[平台会话](../infrastructure/middleware-session.md)、[删除审核库](../../architecture/data/deletion-review.md)及[正式删除执行器](deletion-review-executor.md)。

---

## 组件与生命周期

`app/service/deletion_review_consumer.py` 的 `DeletionReviewConsumer` 提供 `start()`、`scan_once()`、`close()`。HTTP 协议由 `app/infrastructure/middleware.py` 适配，应用 lifespan 装配并保存为 `application.state.deletion_review_consumer`。启动只创建后台任务，中间件故障不阻塞启动；关闭先停止消费者并等待当前删除操作结束，再关闭执行器、平台会话和存储依赖。

沿用单 worker 部署约定，每轮扫描互斥，反馈串行处理。无有效令牌时不请求，401 使当前令牌失效，由平台会话服务重新登录。删除期间心跳独立运行，ack 前重新读取有效令牌。日志仅记录申请/反馈 ID、异常类型和 HTTP 状态，不输出凭据或反馈正文。

---

## 中间件接口与字段映射

| 接口 | 请求 | 响应处理 |
| --- | --- | --- |
| `POST /middleware-service/api/deletion-results/pull` | Bearer，无 body、平台或 offset 参数。 | 200 校验 offset 与 message；204 结束本轮，之后继续轮询。 |
| `POST /middleware-service/api/deletion-results/ack` | Bearer，JSON 仅 `message_id`，使用本次反馈 ID。 | 200 校验 status=acknowledged、反馈 ID 和本次 pull 的 offset。 |

pull 的 `message.request_id` 匹配审核表中送审发布的 `message_id`；`message.message_id` 是独立的反馈 ID。`target_platform_code` 必须等于本项目配置。`approved` 必须为真实布尔值：true 批准，false 拒绝，不根据备注或通行证推断。`reviewer`、`note`、带时区的 `created_at` 分别保存为 `reviewed_by`、`review_note`、`reviewed_at`，不覆盖申请人的备注。

---

## 处理顺序与确认条件

1. 保存审核结果和反馈 ID，重复同值反馈复用原记录；未知请求、冲突反馈或平台不匹配直接报错，不 ack。
2. 批准时调用原 `DeletionReviewExecutor.scan_once()`，复用领取状态、合同锁、身份核验和失败退避；其后台定时扫描也继续运行，两者通过同一执行器锁串行。
3. 执行器核对申请冻结的入库时间和完整通行证集合，依次移除 Neo4j 节点及关联边、ES 文档、正式 PDF，最后移除 SQLite 合同元数据。SQLite 级联清除注意事项、类别关联及对应全文/向量数据；共享类别、用户和会话不在删除范围。
4. 批准必须已写入 `deletion_status=succeeded` 和 `review_processed_at` 才 ack。拒绝必须完成适用原合同的 `can_delete` 恢复并写入 `review_processed_at` 才 ack；不执行正式资源删除。
5. 处理完成的申请仍保留在审核区，按[独立清理策略](deletion-review-cleanup.md)清除。此消费者不新增本地 ack 时间字段，远端持久化消费进度由中间件负责。

消费者不把“保存了批准结论”当作删除成功。执行器可能先处理较早申请，当前申请尚未完成时保留本条反馈，下轮再拉取确认。

---

## 失败、重放与恢复

| 情况 | 行为 |
| --- | --- |
| ES、Neo4j、PDF 或 SQLite 删除失败 | 执行器保存 failed 和重试时间，不 ack；后续扫描继续清理。 |
| 合同已重新入库且身份与快照不同 | 不删除新合同，不 ack，保留失败供核对。 |
| 正式元数据已不存在 | 元数据在最后删除，按已完成继续保存本地结果。 |
| 本地反馈写入或拒绝标志恢复失败 | 不 ack，下轮以同值反馈继续恢复。 |
| 实际删除结束、审核库结果写入失败 | 执行器优先补写结果，避免重复外部删除；未确认完成前不 ack。 |
| ack 断连、503 或超时 | 保留本地完成标记；重复 pull 跳过业务操作，只重试 ack。 |
| ack 返回409 | 不视作成功，下轮重新 pull，以当前交付反馈为准。 |
| 审核行已按保留期清理 | 验证最小幂等回执的请求 ID、反馈 ID 和内容哈希，匹配则直接 ack，不重复删除。 |
| 无法匹配或内容冲突 | 不跳过消息，保留队首供核对，避免误确认。 |

正式库、审核库与消息流没有跨系统事务，通过完成标记、幂等资源清理和清理后最小回执支持重试，不承诺请求恰好调用一次。ack 已成功但响应丢失时，下一次 pull 可能已返回后续消息，本地已完成结果保持有效。

---

## 配置与验证

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `DELETION_REVIEW_RESULT_POLL_INTERVAL_SECONDS` | `30` | 每轮完成或失败后的等待秒数，有限正数。 |
| `DELETION_REVIEW_RESULT_BATCH_SIZE` | `10` | 每轮最多串行处理反馈数，正整数。 |
| `MIDDLEWARE_REQUEST_TIMEOUT_SECONDS` | `10` | 每次 pull/ack 的总超时与 HTTP 超时。 |

实际删除的批次、失败退避使用原执行器配置，见[正式删除执行器](deletion-review-executor.md#配置与已有数据)。部署模板与后端示例使用相同键和默认值，Compose 通过 `backend.env_file` 统一注入；更新模板不会覆盖已有私有配置。审核历史保留时长不控制再次申请资格，成功删除后的重新入库可立即创建新申请。

`tests/test_deletion_review_consumer.py` 使用临时 SQLite 和真实临时 PDF，模拟 HTTP、ES 与 Neo4j，验证协议、批准清理后确认、拒绝恢复后确认、删除失败退避、ack 失败重放、清理后的回执恢复、身份冲突及后台重试。共享 HTTP 适配同时回归入库反馈消费者；不消费、确认或删除真实业务消息和合同。
