# 审核反馈拉取与批准入库

后台从中间件获取本平台审核反馈，使用待审快照完成正式入库，并在本地处理完成后确认消息。依赖[平台会话](../infrastructure/middleware-session.md)、[待审快照](../../architecture/data/ingestion-review.md)与[正式入库服务](contract-ingestion.md)。

---

## 组件与生命周期

`app/service/ingestion_review_consumer.py` 的 `IngestionReviewConsumer` 提供 `start()`、`scan_once()`、`close()`；HTTP 协议适配在 `app/infrastructure/middleware.py`，状态落盘在 `SQLiteIngestionReviewStore`。应用 lifespan 创建并启动后台任务，保存为 `application.state.ingestion_review_consumer`；关闭时先停止消费者，再释放入库依赖。

沿用单进程部署边界，每轮扫描互斥、消息串行处理。中间件不可用不阻塞应用启动，异常记录类型与 HTTP 状态后等待下轮；不记录令牌和反馈正文。没有有效令牌时不请求；401 使对应令牌失效，等待平台会话服务重登。入库过程中独立心跳继续运行，ack 前重新读取有效令牌。

---

## 请求与审核判定

| 接口 | 请求 | 响应处理 |
| --- | --- | --- |
| `POST /middleware-service/api/ingestion-results/pull` | 仅 Bearer，无 body、平台参数或 offset。 | 200 校验 offset 与完整 message；204 结束本轮，之后继续轮询。 |
| `POST /middleware-service/api/ingestion-results/ack` | Bearer，JSON 仅 `message_id`，使用反馈 ID。 | 200 校验 acknowledged、反馈 ID 和当前 offset；其他状态保留本地完成记录。 |

`message.request_id` 对应待审表的发布 `message_id`；`message.message_id` 是独立反馈 ID。目标平台必须等于本项目配置。

- `passport == ""`：拒绝，只保存审核结果，不调用正式入库。
- 非空且无首尾空白的 `passport`：批准，作为跨平台关联标识写入正式合同元数据。
- 缺失、null、纯空白或其他类型：无效反馈，不根据 note 猜测，不入库、不 ack。历史反馈没有 passport 时需先与中间件核对处理。

审核员保存为 `reviewed_by`，外部备注保存为 `review_note`，不覆盖入库员的 `note`；审核时间必须带时区。正式合同仅写 `uploader`，值来自快照 `submitted_by`；外部审核员不进入正式合同。

---

## 持久化顺序与幂等

1. pull 后按 request_id 查找申请；事务保存完整反馈及其 ID、offset、审核人、备注、时间、passport。批准标记 approved，拒绝标记 rejected。反馈一旦绑定不可被另一反馈覆盖。
2. 批准时置 ingestion_status=ingesting，读取并校验待审 PDF，复用 `ContractIngestionService.ingest`：摘要向量化、正式 SQLite/PDF/ES/Neo4j 写入及 ready 发布。拒绝不进入该步骤。
3. 成功后记录 `review_processed_at`；批准置 succeeded，拒绝保持 pending（未执行入库）。该完成标记提交成功后才允许 ack。
4. ack 成功后记录 `review_acked_at`。待审 PDF 和快照由[定时清理服务](ingestion-review-cleanup.md)在本地处理完成并达到保留期后删除。

反馈 ID 唯一；多份合同允许共用 passport，同 ID 内容变化或同申请不同反馈仍拒绝处理。重复 pull 已本地完成的反馈时跳过入库，只确认当前消息。已清理申请通过最小处理凭据核验并直接 ack；确认时间可写入该凭据，不再读取快照。相同反馈重投到另一 offset 时使用新 pull 的 offset 校验 ack。

正式合同 SQLite 为 passport 建立普通索引，同一通行证可关联多份合同，历史合同保留 NULL。启动初始化自动移除正式库和待审库的旧通行证唯一约束。批准入库遇到相同 document_id、相同 passport 且已 ready 时直接返回原结果，不重复编码、不改写入库时间；身份不同或正在删除则失败。部分持久化失败继续沿用既有 ingesting 恢复机制。

---

## 失败与恢复边界

| 故障 | 行为 |
| --- | --- |
| 未找到本地 request_id、反馈冲突或非法结构 | 不 ack，保留远端待确认消息，记录异常，后续仍拉取该消息。 |
| 文件丢失、向量生成或任一正式库写入失败 | 本地记录 failed 及异常类型，不 ack；下次重投重试。 |
| 进程在正式入库后、本地完成标记前退出 | 下次依靠相同 passport 的正式入库幂等检查继续完成。 |
| 已本地完成，ack 503、断连或超时 | 不重做入库；下一轮重新 pull，若仍为该反馈则再次 ack。 |
| 会话切换后 ack 409 | 不把 409 当作成功；下一轮重新 pull 取得当前待确认消息。 |
| ack 已提交但响应丢失，或本地 ack 时间写入失败 | 远端可能已推进，后续 pull 不再返回旧消息；本地处理结果仍完整，不强行补 ack。 |
| 进程取消 | 不把取消推断为写入失败；保留持久化状态供重启恢复。 |

`review_acked_at` 仅表示本地已确认收到成功响应，不是远端消费进度的权威查询。两个系统没有分布式事务，不能保证 HTTP 恰好调用一次；通过本地完成标记与正式入库幂等保证重复反馈不会反复生成合同。

未匹配反馈不自动跳过，避免把无法恢复的数据当作已处理。人工对账、重审修改和历史反馈迁移接口不在本次范围。

---

## 配置与验证

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `INGESTION_REVIEW_RESULT_POLL_INTERVAL_SECONDS` | `30` | 每轮完成或失败后的等待秒数，有限正数。 |
| `INGESTION_REVIEW_RESULT_BATCH_SIZE` | `10` | 每轮最多处理反馈数，正整数。 |
| `MIDDLEWARE_REQUEST_TIMEOUT_SECONDS` | `10` | 单次 pull/ack 总超时及 HTTP 超时。 |

本地测试 `tests/test_ingestion_review_consumer.py` 使用临时真实 SQLite/PDF、模拟 HTTP 和外部依赖，覆盖协议、批准/拒绝、失败重试、重启、身份冲突、正式入库幂等与迁移。未向真实审核队列投放合同或确认真实消息。

extraction-runs 入库接口已改为提交待审申请；消费者处理已保存并绑定发布消息 ID 的记录。提取运行在待审保存成功后释放，消费者完全依赖持久化快照。
