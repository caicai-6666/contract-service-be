# 待审请求后台发布

将已经完整保存到独立待审区的 PDF 与快照字段，通过中间件发布接口提交审核。仅负责发送和发送结果落盘，不消费审核结果、不正式入库。

---

## 组成与生命周期

- `app/service/ingestion_review_publisher.py`：`IngestionReviewPublisher`，单后台任务扫描、有界串行上传、结果落盘与停止清理。
- `app/infrastructure/middleware.py`：`MiddlewareClient.publish_ingestion()`，一次 multipart HTTP 请求及结果分类，无隐式重试。
- `app/service/middleware_session.py`：`get_access_token()` 返回仍有效的内部令牌，`invalidate_access_token()` 仅失效匹配令牌，避免迟到 401 清掉新会话。
- `app/infrastructure/ingestion_review_store.py`：领取发送、保存结果、重启恢复。
- `app/bootstrap.py`：应用启动时派发后台任务，关闭时先停止上传再关闭会话；连接中间件不阻塞应用启动。

沿用项目单 worker 约束。上传使用独立 HTTP 客户端，心跳任务继续运行；上传和读取令牌不延长会话。尚未取得有效令牌时不领取待审记录、不增加尝试次数。

---

## 上传契约与映射

`POST /middleware-service/api/ingestion-requests/publish`，仅使用 `Authorization: Bearer <access_token>` 鉴权，不提交 platform_code。

| multipart 字段 | 快照来源 | 校验 |
| --- | --- | --- |
| `name` | `file_name` | 非空白，1–200 字符。 |
| `abstract` | `summary` | 非空白，1–10000 字符。 |
| `source_id` | `document_id` | 必填、非空白，1–128 字符；本平台使用处理版 PDF 的 64 位 SHA-256。 |
| `note` | 待审记录独立列 `note` | 始终提交，允许空字符串，最多 10000 字符。 |
| `reviewer` | `submitted_by` | 非空白，1–200 字符。 |
| `file_extension` | 固定 `pdf` | 不带点。 |
| `file` | 待审 PDF 原始字节 | 上传前重新校验本地哈希。 |

上传文件名使用 `contract.pdf`，媒体类型为 `application/pdf`，并不依赖文件名识别后缀。不会将 Core、Clause 或向量擅自加到接口表单里，它们仍保留在本地快照。摘要、名称、备注和提交人超限时不截断，标记需要处理。

`source_id` 标识合同数据，直接沿用待审快照及后续正式入库的 `document_id`，不使用待审申请 `submission_id` 或中间件 `message_id`。已有快照无需迁移，发送时从快照读取；它不替代消息回执和审核反馈的关联标识，中间件当前按 `(platform_code, source_id)` 对尚未被 registry 确认的申请去重，支持使用原 source_id 重试。

---

## 扫描与一致性

服务启动后将上次残留的 `publishing` 记录转为 `uncertain` 并设置重试时间；旧 `uncertain` 记录若没有等待时间，也补齐配置的重试间隔。随后扫描 `review_status=pending_send`、`delivery_status=pending/uncertain` 且重试时间已到的记录，即使已有候选 message_id 也可以重试。领取与 `publish_attempts+1` 在同一 SQLite 事务中完成，网络请求不持有数据库事务。

单个进程的扫描互斥，每轮最多处理配置的批量数。HTTP 201、`status=published` 且具有合法非空 message_id 时保存成功。HTTP 409 符合约定的重复申请对象时，关联此前申请的 message_id 并转为待审核，不要求新建消息；此时 offset 可以为 null 或非负整数，发布响应中的 offset 不用于审核反馈 ack。两种情况均原子保存 message_id、`delivery_status=published` 与 `review_status=pending_review`，409另保存 duplicate_request 脱敏代码。消息已成功发布不代表审核通过。

若远端响应已取得但 SQLite 暂时不可写，结果暂存后台服务内存，后续优先重试本地落盘，不再次上传。如果进程同时退出而本地仍未记录结果，重启后将 `publishing` 恢复为延迟重试，依托中间件去重核对，而不猜测远端是否收到。

---

## 错误处理

| 情况 | 本地处理 |
| --- | --- |
| 建连失败、连接超时、连接池等待超时 | 未开始上传，延迟重试。 |
| HTTP 401 | 令牌失效，等待会话服务重登后重试。 |
| HTTP 403 / 422 | `blocked`，权限或参数需处理，不循环上传。 |
| HTTP 500 且 detail 为“文件保存失败” | 按明确失败延迟重试。 |
| HTTP 503 且 detail 为“消息服务暂不可用，请稍后重试” | 按明确失败延迟重试。 |
| HTTP 503 返回 detail.message_id | `uncertain`，保存 message_id，延迟后使用相同 source_id 重试。 |
| HTTP 503 无法确认是否重复 | `uncertain`，保留已有 message_id 并延迟重试；不视为成功。 |
| HTTP 409 合法重复申请对象 | 复用原 message_id，进入 pending_review，停止发送。 |
| HTTP 409 缺失 ID、错误提示或非法 offset | 保留不确定状态并延迟重试，不伪造成功。 |
| 读取/写入超时、上传总超时、连接中断、未知 HTTP 响应、非法成功体 | 结果可能已发布，`uncertain`，保存响应中可用的 message_id，按相同间隔重试。 |
| 本地文件丢失/损坏或字段不合法 | `blocked`，保留原快照。 |
| 取消发送或上次发送进程中断 | 保存已有明确回执，否则标记 `uncertain` 并设置重试时间。 |

重试期间收到无 ID 的错误响应不会抹掉已有候选 message_id；明确接收的审核反馈会将发送状态确认为 published 并停止重试。审核反馈可能与上传并发，迟到的发送结果不得将 approved/rejected 回退为待发送，消息身份冲突仍报错。

中间件去重只覆盖 registry 尚未确认的申请；pull 不解除限制，registry ack 原消息后可以再次提交同一 source_id，产生新 message_id。去重不核对本地申请 UUID，也不构成无限期幂等或端到端恰好一次保证。重试保持同一快照、source_id、字段与文件；不确定占位尚未找到流消息时，中间件继续返回503，本平台继续定时等待。中间件尚无发布结果查询或自动补偿接口，持续故障不会被伪装为成功。删除送审也已采用其独立申请流的去重契约，重试规则见[删除审核发布服务](deletion-review-publisher.md)。

---

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `INGESTION_REVIEW_SCAN_INTERVAL_SECONDS` | `30` | 每轮完成后的扫描间隔，秒。 |
| `INGESTION_REVIEW_PUBLISH_RETRY_SECONDS` | `60` | 明确失败或结果不确定后最短重试间隔，实际受扫描周期影响。 |
| `INGESTION_REVIEW_PUBLISH_TIMEOUT_SECONDS` | `120` | 单次上传与发布总超时，秒。 |
| `INGESTION_REVIEW_PUBLISH_BATCH_SIZE` | `10` | 单轮最大串行处理数。 |

前三项为有限正数，批量数为正整数。地址及认证复用[中间件平台会话](../infrastructure/middleware-session.md)。日志只记录申请 ID、状态、错误代码或异常类型，不记录令牌及原始响应正文。

---

## 验证与范围

`tests/test_ingestion_review_publisher.py` 通过 HTTP 模拟响应和临时 SQLite 验证 multipart 字段、成功只发一次、401 重登、明确失败与不确定结果定时重试、409重复响应（含null offset）、503查重不可用、断连、并发扫描、旧挂起与中断恢复、取消、远端成功后本地落盘失败以及审核反馈和发送回执并发。没有向真实审核队列投放测试合同。

[待审存储](../../architecture/data/ingestion-review.md)为发送服务提供完整快照。入库接口现已提交待审快照，必填 note；请求返回后由本后台任务扫描发送，不在 HTTP 请求内等待中间件。审核反馈拉取与批准后入库已由独立的[审核反馈消费者](ingestion-review-consumer.md)接入。
