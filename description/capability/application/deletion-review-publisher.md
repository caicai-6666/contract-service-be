# 删除审核申请后台发布

将删除审核区的申请与原正式合同 PDF 提交到中间件，保存发送状态和消息回执。发布成功只代表申请进入审核队列，不触发正式合同删除。它与[批准后的正式删除执行器](deletion-review-executor.md)、[审核数据清理服务](deletion-review-cleanup.md)分别运行。

---

## 组成与生命周期

- `app/service/deletion_review_publisher.py`：`DeletionReviewPublisher`，有界串行扫描、上传、回执落盘和取消处理。
- `app/infrastructure/middleware.py`：`MiddlewareClient.publish_deletion()` 与 `MiddlewareDeletionPublishFields`，单次 multipart 请求及响应分类。入库与删除发布复用文件上传结果分类，不自动重试 HTTP 请求。
- `app/infrastructure/deletion_review_store.py`：短事务领取、发送结果保存、进程中断恢复及旧记录迁移。
- `app/bootstrap.py`：正式合同服务与中间件会话就绪后启动发布任务，退出时先停止发布，再关闭文件与会话依赖。

沿用项目单 worker 约束。中间件故障在后台记录并重试，不阻碍应用启动。尚未取得有效令牌时不领取记录，也不增加尝试次数；心跳继续由[平台会话服务](../infrastructure/middleware-session.md)负责。

---

## 提交契约

`POST /middleware-service/api/deletion-requests/publish`，使用 `Authorization: Bearer <access_token>`。所有以下字段都会提交，不传 platform_code 或旧 reviewer 字段。

| multipart 字段 | 本地来源 | 校验 |
| --- | --- | --- |
| source_id | document_id | 64 位合同内容哈希，不是申请 UUID 或消息 ID；接口允许1–128字符。 |
| passport | 申请快照的 passport | 非空且非纯空白；历史 null 不能替代为其他标识。 |
| name | file_name | 非空白，1–200字符，沿用不带 PDF 后缀的业务名称。 |
| abstract | summary | 非空白，1–10000字符；历史 null 不自动编造摘要。 |
| uploader | uploader | 原合同上传人，非空白，1–200字符。 |
| applicant | requested_by | 本次删除申请人，非空白，1–200字符。 |
| note | note | 来自删除接口必填 JSON 字段的申请人备注，按原文冻结并发送；允许空字符串，最多10000字符。 |
| file_extension | 固定 pdf | 显式声明后缀，不带点。 |
| file | 原正式合同 PDF 字节 | 文件名固定 contract.pdf，媒体类型 application/pdf。 |

字段超限不截断。准备文件时使用正式入库服务的文档锁，核对快照的入库时间、passport、file_uri，要求正式记录仍为 ready 且 can_delete=false；读取后核验字节 SHA-256 与 document_id 一致。锁内文件读取完成后释放锁，再进行网络上传。删除审核区仍只保存正式文件引用，不复制本地 PDF。

---

## 发送状态与一致性

启动恢复将上次遗留的 publishing 记录转为 uncertain，并设置配置的等待时间；旧 uncertain 记录没有 next_publish_at 时补齐重试时间。旧库补齐发送字段时，已绑定 message_id 的记录初始化为 published，避免升级后再次送审。

每轮仅领取 review_status=pending_send、delivery_status=pending/uncertain 且等待时间已到的申请，候选 message_id 非空不阻止重试，领取与尝试次数增加在同一 SQLite 事务完成。网络请求不持有数据库事务。

HTTP 201、status=published、合法非空 message_id 视为发布成功；HTTP 409 合法重复申请对象则关联此前的 message_id。重复响应的 message 必须匹配删除申请契约，offset 可以为 null 或非负整数；这个 offset 不用于审核反馈 ack。同事务保存 message_id、delivery_status=published、review_status=pending_review。明确失败与不确定结果都设置 next_publish_at；明确未发布的失败回到 pending，但已有候选 ID 时继续保持 uncertain；它不解锁合同的 can_delete。

不确定响应中可能携带 message_id，此时保存 ID 但 delivery_status=uncertain、review_status=pending_send，不能仅凭 ID 非空认定发布成功；后台按相同 source_id 定时重试，409合法重复响应才会确认原申请。详情的 delivery.status 直接反映这些状态。

远端响应已取得但本地事务失败时，回执暂存在发布服务内存；后续仅重试落盘，不再次 HTTP 上传。单条保存失败不阻止其他申请处理。进程退出前取消正在上传的申请，保存已有明确回执，否则保存 uncertain 与重试时间；SQLite 线程事务和锁内读取在取消时等待完成。若进程强制退出且回执丢失，重启后按 uncertain 恢复延迟重试。

---

## 错误边界

| 情况 | 处理 |
| --- | --- |
| 建连失败、连接超时、连接池等待超时 | pending，延迟重试。 |
| 401 | pending，失效本次使用的令牌，等待平台重新登录。 |
| 403 / 422 | blocked，停止自动发送。 |
| 500 且 detail 明确为“文件保存失败” | pending，延迟重试。 |
| 503 且 detail 明确为“消息服务暂不可用，请稍后重试” | pending，延迟重试。 |
| 503 返回 detail.message_id | uncertain，保留消息 ID，使用相同 source_id 延迟重试。 |
| 503 无法确认是否重复 | uncertain，保留原候选 ID，延迟重试。 |
| 409 合法重复申请对象 | 关联原 message_id，published / pending_review，停止发送。 |
| 409 缺少身份、非法 offset 或非删除冲突对象 | uncertain，延迟重试，不视为成功。 |
| 读取/写入超时、断连、未知响应或不完整成功体 | uncertain，设置等待时间后重试，不能推断远端未收到。 |
| 文件缺失、损坏、原合同身份改变、未锁定或字段不合法 | blocked，保留申请及正式合同。 |
| 本地数据库读取暂时故障、尚未发起 HTTP | pending，延迟重试。 |

中间件按 `(platform_code, source_id)` 在删除申请流中去重，精确比较、不裁剪、不忽略大小写。已有消息尚未被 registry ack 时返回409；pull 不解除限制，registry ack 到原 offset 后允许同键重提。删除流与入库流分别维护进度，相同平台和 source_id 可以分别发起两类申请。保护不是永久幂等，也不承诺跨进程并发或端到端恰好一次。

去重读取失败或不确定占位始终未找到消息时，中间件返回503，本平台继续等待重试，不绕过去重。重试始终使用原快照、source_id、文件及人员字段；后续无ID响应不会抹掉原候选 message_id。已有审核反馈时停止发送，迟到的发布回执不能回退批准/拒绝状态；身份不一致仍报错。

尚无投递结果查询或解除 blocked 的管理接口。审核结果拉取与 ack 协议仍待提供，本模块不猜测审核结果或提前删除合同。

---

## 配置与验证

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| DELETION_REVIEW_PUBLISH_SCAN_INTERVAL_SECONDS | 30 | 发送扫描间隔，秒。 |
| DELETION_REVIEW_PUBLISH_RETRY_SECONDS | 60 | 明确失败或结果不确定后的最短重试等待，秒。 |
| DELETION_REVIEW_PUBLISH_TIMEOUT_SECONDS | 120 | 单次上传与发布的总超时，秒。 |
| DELETION_REVIEW_PUBLISH_BATCH_SIZE | 10 | 每轮最大串行发送条数。 |

前三项为有限正数，批量数为正整数。发送配置与批准后删除执行配置分开；地址及认证复用 MIDDLEWARE 配置。日志仅保留申请 ID、状态和脱敏错误代码，不记录密钥、令牌或原始响应正文。

`tests/test_deletion_review_publisher.py` 使用临时双 SQLite、PDF 及 HTTP 模拟响应，验证表单精确映射、成功只发一次、无令牌不领取、401与延迟重试、不确定回执、单条故障隔离、文件与身份检查、旧库迁移、旧挂起恢复、定时重试、409合法与非法回执、连续503、超时及401保留候选ID、取消、重启恢复和审核反馈先于发送回执的状态保护。相关回归覆盖现有入库发布、删除申请、查询、正式删除执行和清理。测试未向真实队列投放删除申请。
