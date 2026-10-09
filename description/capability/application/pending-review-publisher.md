# 待审请求后台发布

将已经完整保存到独立待审区的 PDF 与快照字段，通过中间件发布接口提交审核。仅负责发送和发送结果落盘，不消费审核结果、不正式入库。

---

## 组成与生命周期

- `app/service/pending_review_publisher.py`：`PendingReviewPublisher`，单后台任务扫描、有界串行上传、结果落盘与停止清理。
- `app/infrastructure/middleware.py`：`MiddlewareClient.publish_ingestion()`，一次 multipart HTTP 请求及结果分类，无隐式重试。
- `app/service/middleware_session.py`：`get_access_token()` 返回仍有效的内部令牌，`invalidate_access_token()` 仅失效匹配令牌，避免迟到 401 清掉新会话。
- `app/infrastructure/pending_review_store.py`：领取发送、保存结果、重启恢复。
- `app/bootstrap.py`：应用启动时派发后台任务，关闭时先停止上传再关闭会话；连接中间件不阻塞应用启动。

沿用项目单 worker 约束。上传使用独立 HTTP 客户端，心跳任务继续运行；上传和读取令牌不延长会话。尚未取得有效令牌时不领取待审记录、不增加尝试次数。

---

## 上传契约与映射

`POST /middleware-service/api/ingestion-requests/publish`，仅使用 `Authorization: Bearer <access_token>` 鉴权，不提交 platform_code。

| multipart 字段 | 快照来源 | 校验 |
| --- | --- | --- |
| `name` | `file_name` | 非空白，1–200 字符。 |
| `abstract` | `summary` | 非空白，1–10000 字符。 |
| `note` | 待审记录独立列 `note` | 始终提交，允许空字符串，最多 10000 字符。 |
| `reviewer` | `submitted_by` | 非空白，1–200 字符。 |
| `file_extension` | 固定 `pdf` | 不带点。 |
| `file` | 待审 PDF 原始字节 | 上传前重新校验本地哈希。 |

上传文件名使用 `contract.pdf`，媒体类型为 `application/pdf`，并不依赖文件名识别后缀。不会将 Core、Clause 或向量擅自加到接口表单里，它们仍保留在本地快照。摘要、名称、备注和提交人超限时不截断，标记需要处理。

---

## 扫描与一致性

服务启动后先将上次残留的 `publishing` 记录转为 `uncertain`，随后扫描 `review_status=pending_send`、`delivery_status=pending`、message_id 为空且重试时间已到的记录。领取与 `publish_attempts+1` 在同一 SQLite 事务中完成，网络请求不持有数据库事务。

单个进程的扫描互斥，每轮最多处理配置的批量数。仅 HTTP 201、`status=published` 且具有非空 message_id 才视为成功；原子保存 message_id、`delivery_status=published` 与 `review_status=pending_review`。消息已成功发布不代表审核通过。

若远端响应已取得但 SQLite 暂时不可写，结果暂存后台服务内存，后续优先重试本地落盘，不再次上传。如果进程同时退出而本地仍未记录结果，重启后只能将 `publishing` 挂起，不能自动判断远端没有收到。

---

## 错误处理

| 情况 | 本地处理 |
| --- | --- |
| 建连失败、连接超时、连接池等待超时 | 未开始上传，延迟重试。 |
| HTTP 401 | 令牌失效，等待会话服务重登后重试。 |
| HTTP 403 / 422 | `blocked`，权限或参数需处理，不循环上传。 |
| HTTP 500 且 detail 为“文件保存失败” | 按明确失败延迟重试。 |
| HTTP 503 且 detail 为“消息服务暂不可用，请稍后重试” | 按明确失败延迟重试。 |
| HTTP 503 返回 detail.message_id | `uncertain`，保存 message_id，不自动重发。 |
| 读取/写入超时、上传总超时、连接中断、未知 HTTP 响应、非法成功体 | 结果可能已发布，`uncertain`，保存响应中可用的 message_id。 |
| 本地文件丢失/损坏或字段不合法 | `blocked`，保留原快照。 |
| 取消发送或上次发送进程中断 | 保存已有明确回执，否则保守标记 `uncertain`。 |

中间件目前没有幂等请求和结果查询接口。因此无法保证外部 exactly-once；暂停不确定请求是避免自动重复审核的必要边界。暂未提供人工解除挂起或对账 API，后续需结合中间件能力实现，不直接修改数据库重试未知请求。

---

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PENDING_REVIEW_SCAN_INTERVAL_SECONDS` | `30` | 每轮完成后的扫描间隔，秒。 |
| `PENDING_REVIEW_PUBLISH_RETRY_SECONDS` | `60` | 明确失败后最短重试间隔，实际受扫描周期影响。 |
| `PENDING_REVIEW_PUBLISH_TIMEOUT_SECONDS` | `120` | 单次上传与发布总超时，秒。 |
| `PENDING_REVIEW_PUBLISH_BATCH_SIZE` | `10` | 单轮最大串行处理数。 |

前三项为有限正数，批量数为正整数。地址及认证复用[中间件平台会话](../infrastructure/middleware-session.md)。日志只记录申请 ID、状态、错误代码或异常类型，不记录令牌及原始响应正文。

---

## 验证与范围

`tests/test_pending_review_publisher.py` 通过 HTTP 模拟响应和临时 SQLite 验证 multipart 字段、成功只发一次、401 重登、明确失败重试、503 不确定、断连、并发扫描、进程恢复、取消以及远端成功后本地落盘失败。没有向真实审核队列投放测试合同。

[待审存储](../../architecture/data/pending-review.md)为发送服务提供完整快照。入库接口现已提交待审快照，必填 note；请求返回后由本后台任务扫描发送，不在 HTTP 请求内等待中间件。审核反馈拉取与批准后入库已由独立的[审核反馈消费者](pending-review-consumer.md)接入。
